#!/usr/bin/env python3
"""Durable rename facts keyed by one immutable Seat incarnation.

This ledger is deliberately NOT an absence authority.  Helm cannot currently
re-verify Telegram origin and payload binding after an update is acknowledged,
and dispatch rows do not yet carry the stable Seat incarnation needed to spend
a permanent terminal safely.  Therefore disown/decommission always refuse and
this module is not a reachability provider.

The one fact it can preserve is a measured rename: label A and label B named the
same already-measured incarnation.  Every record carries that incarnation and
the evidence reference used to establish the lineage.  Readers resolve only
when the caller supplies the same incarnation, so later reuse of label A cannot
inherit an old edge.
"""

import hashlib
import os
import re
import sys

from . import eventledger, home, pk

LEDGER = "seat-lifecycle.jsonl"
SCHEMA_V = 1
EVENT = "seat-rename"
RENAMED = "renamed"
DISOWNED = "disowned"
DECOMMISSIONED = "decommissioned"
TRANSITIONS = (RENAMED, DISOWNED, DECOMMISSIONED)
ACTIVE = "ACTIVE"
MOVED = "RENAMED"
UNKNOWN = "UNKNOWN"
ABSENCE_SUCCESSOR_LANE = "seat-lifecycle-authority"
ABSENCE_REFUSAL = (
    "absence append is unavailable: no landed primitive can re-verify owner "
    "origin plus payload binding against a stable dispatch Seat incarnation; "
    "continue successor lane %s" % ABSENCE_SUCCESSOR_LANE)
_MAX_CHAIN = 64
_HEX32 = re.compile(r"[0-9a-f]{32}\Z")
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_EVIDENCE = re.compile(r"[A-Za-z0-9][A-Za-z0-9:._/@+-]{0,255}\Z")
_FIELDS = frozenset(("v", "id", "event", "seat", "successor", "incarnation",
                    "evidence_ref", "transition", "ts"))


def ledger_path():
    return os.path.join(home.global_dir(), LEDGER)


def _token(value):
    from .seats_common import _canonical_recipient
    return _canonical_recipient(value)


def _incarnation(value):
    value = str(value or "").strip()
    return value if _HEX32.fullmatch(value) else None


def _evidence(value):
    value = str(value or "").strip()
    return value if _EVIDENCE.fullmatch(value) else None


def _row_id(seat, successor, incarnation, evidence_ref):
    payload = "\x1e".join((EVENT, seat, successor, incarnation, evidence_ref))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _valid(row):
    if not isinstance(row, dict) or set(row) != _FIELDS:
        return False
    seat, seat_err = _token(row.get("seat"))
    successor, successor_err = _token(row.get("successor"))
    values = {
        "v": row.get("v") == SCHEMA_V,
        "id": type(row.get("id")) is str and bool(_HEX64.fullmatch(row["id"])),
        "event": row.get("event") == EVENT,
        "transition": row.get("transition") == RENAMED,
        "seat": seat_err is None,
        "successor": successor_err is None,
        "different": seat_err is None and successor_err is None and seat != successor,
        "incarnation": _incarnation(row.get("incarnation")) is not None,
        "evidence": _evidence(row.get("evidence_ref")) is not None,
        "ts": type(row.get("ts")) is str and bool(row.get("ts")),
    }
    if not all(values.values()):
        return False
    return row["id"] == _row_id(str(seat), str(successor),
                                row["incarnation"], row["evidence_ref"])


def snapshot(path=None):
    """Return one strict, shared-ledger reading as ``(rows, unavailable)``."""
    rows, unavailable = eventledger.checked_events(path or ledger_path(), strict=True)
    if unavailable:
        return (), "seat rename ledger unavailable: %s" % unavailable
    if any(not _valid(row) for row in rows):
        return (), "seat rename ledger contains a record this reader cannot interpret"
    return tuple(rows), None


def _index(rows):
    edges, poisoned = {}, set()
    for row in rows:
        key = (row["seat"], row["incarnation"])
        fact = (row["successor"], row["evidence_ref"])
        prior = edges.get(key)
        if prior is None:
            edges[key] = fact
        elif prior != fact:
            poisoned.add(key)
    return edges, poisoned


def resolve(seat, incarnation, rows=None, path=None):
    """Return ``(final_label, state, detail)`` for one exact incarnation."""
    seat, err = _token(seat)
    incarnation = _incarnation(incarnation)
    if err or incarnation is None:
        return None, UNKNOWN, "seat and incarnation must be exact canonical values"
    if rows is None:
        rows, unavailable = snapshot(path)
        if unavailable:
            return None, UNKNOWN, unavailable
    edges, poisoned = _index(rows)
    name, seen = str(seat), []
    while len(seen) <= _MAX_CHAIN:
        key = (name, incarnation)
        if key in poisoned:
            return None, UNKNOWN, "@%s has conflicting rename facts" % name
        if key in seen:
            return None, UNKNOWN, "rename chain cycles for incarnation %s" % incarnation
        seen.append(key)
        fact = edges.get(key)
        if fact is None:
            if len(seen) == 1:
                return name, ACTIVE, "no rename fact for @%s incarnation %s" % (
                    name, incarnation)
            return name, MOVED, "@%s incarnation %s became @%s via %s" % (
                seat, incarnation, name,
                " -> ".join(source for source, _inc in seen))
        name = fact[0]
    return None, UNKNOWN, "rename chain exceeds %d hops" % _MAX_CHAIN


def _shape(seat, transition, successor, incarnation, evidence_ref):
    if transition in (DISOWNED, DECOMMISSIONED):
        return None, ABSENCE_REFUSAL
    if transition != RENAMED:
        return None, "transition must be one of %s" % ", ".join(TRANSITIONS)
    seat, seat_err = _token(seat)
    successor, successor_err = _token(successor)
    incarnation = _incarnation(incarnation)
    evidence_ref = _evidence(evidence_ref)
    refusals = (
        seat_err,
        successor_err,
        None if incarnation else "incarnation must be 32 lowercase hex characters",
        None if evidence_ref else "evidence_ref must be one bounded lineage reference",
        None if seat_err or successor_err or seat != successor
        else "a seat cannot be renamed to itself",
    )
    refusal = next((item for item in refusals if item), None)
    if refusal:
        return None, refusal
    return (str(seat), str(successor), incarnation, evidence_ref), None


def record(seat, transition, successor=None, incarnation=None,
           evidence_ref=None, path=None):
    """Append one rename fact under the ledger lock; terminals always refuse."""
    shaped, refusal = _shape(seat, transition, successor, incarnation, evidence_ref)
    if refusal:
        return None, refusal
    seat, successor, incarnation, evidence_ref = shaped
    path = path or ledger_path()
    with eventledger.locked(path) as held:
        if not held:
            return None, "seat rename ledger lock could not be acquired"
        rows, unavailable = snapshot(path)
        if unavailable:
            return None, unavailable
        edges, poisoned = _index(rows)
        key = (seat, incarnation)
        if key in poisoned:
            return None, "@%s incarnation %s has conflicting rename facts" % key
        fact = (successor, evidence_ref)
        prior = edges.get(key)
        if prior is not None:
            if prior == fact:
                return None, "rename fact already recorded; idempotent, nothing appended"
            return None, "one incarnation gets one onward rename; newest does not win"
        probe = dict(edges)
        probe[key] = fact
        name, seen = successor, {seat}
        for _hop in range(_MAX_CHAIN + 1):
            if name in seen:
                return None, "rename would create a cycle"
            seen.add(name)
            onward = probe.get((name, incarnation))
            if onward is None:
                break
            name = onward[0]
        else:
            return None, "rename chain would exceed %d hops" % _MAX_CHAIN
        row = {"v": SCHEMA_V, "event": EVENT, "seat": seat,
               "successor": successor, "incarnation": incarnation,
               "evidence_ref": evidence_ref, "transition": RENAMED,
               "ts": pk.now_ts()}
        row["id"] = _row_id(seat, successor, incarnation, evidence_ref)
        if not eventledger.append_unlocked(path, row):
            return None, "seat rename ledger could not be appended"
        return row, None


_USAGE = """usage: helm seat lifecycle <verb>
  show [<seat> --incarnation ID]
  record <seat> renamed --successor S --incarnation ID --evidence REF
  record <seat> disowned|decommissioned  (always refuses; see successor lane)"""


def _value(args, flag):
    return args[args.index(flag) + 1] if flag in args else None


def cmd_lifecycle(args):
    from .cli import guard_tail
    args = list(args)
    verb = args[0] if args else ""
    if verb == "show":
        if len(args) == 1:
            seat, tail = None, ()
        else:
            seat, tail = args[1], args[2:]
        rc = guard_tail("helm seat lifecycle show", tail,
                        valued=("--incarnation",), usage=_USAGE)
        if rc is not None:
            return rc
        rows, unavailable = snapshot()
        if unavailable:
            print("helm seat lifecycle: " + unavailable, file=sys.stderr)
            return 1
        if seat is None:
            for row in rows:
                print("%-24s -> %-24s incarnation=%s evidence=%s" % (
                    row["seat"], row["successor"], row["incarnation"],
                    row["evidence_ref"]))
            if not rows:
                print("no measured rename facts")
            return 0
        incarnation = _value(tail, "--incarnation")
        _final, state, detail = resolve(seat, incarnation, rows=rows)
        print("%-8s %s" % (state, detail))
        return 1 if state == UNKNOWN else 0
    if verb == "record" and len(args) >= 3:
        seat, transition = args[1:3]
        tail = args[3:]
        rc = guard_tail("helm seat lifecycle record", tail,
                        valued=("--successor", "--incarnation", "--evidence"),
                        usage=_USAGE)
        if rc is not None:
            return rc
        row, refusal = record(
            seat, transition, successor=_value(tail, "--successor"),
            incarnation=_value(tail, "--incarnation"),
            evidence_ref=_value(tail, "--evidence"))
        if refusal:
            print("helm seat lifecycle: " + refusal, file=sys.stderr)
            return 1
        print("recorded %s -> %s incarnation=%s" % (
            row["seat"], row["successor"], row["incarnation"]))
        return 0
    print(_USAGE, file=sys.stderr)
    return 2
