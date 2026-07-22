#!/usr/bin/env python3
"""helm dispatches — the DISPATCH LEDGER: a durable obligation for every piece
of work handed to another seat, with a deadline.

ROOT CAUSE (owner, 2026-07-21): "we definitely need some timer fallback for
anything that is sent to them, to make sure it is remembered to check on their
progress" and, after a seat sat stuck unnoticed, "the failure was still yours
in not checking in". Dispatch tracking lived in per-session Monitor watchdogs,
so it EVAPORATED at compaction or session end — the same class as
landed-but-never-armed. A dispatch has to outlive the session that made it.

THE BAR, exactly parallel to the owner-ask ledger's: POSTING IS NOT DONE. A
dispatch closes only on a VERDICT from the recipient (prem ensure-contributions-
acked: "a contribution is done when the integrator ACKNOWLEDGES + LANDS it").
`ack` records that the seat picked it up; it does NOT close the row. Anything
without a verdict is live fleet debt and keeps surfacing.

DEADLINES ARE ADVISORY-BY-DESIGN, and that is deliberate: `overdue` names a row
for a HUMAN-OR-AGENT CHECK-IN, never an automatic reassignment. Tonight proved
why — a lane untouched 47 minutes looked exactly like a dead seat and was a
long turn (prem pending-zero-means-delivered-not-lost); an auto-reassign on
that signal would have duplicated live work. Overdue means LOOK, not ACT.

Storage reuses the owner-ask ledger's proven mechanics (event-sourced snapshot
rows in one append-only jsonl, last line per id wins, O(1) unbuffered O_APPEND
write, FAIL-OPEN everywhere) via ownerasks' now path-parameterised primitives —
the same shape, not a second copy of it.

Row schema:
  {id, ts, recipient, lane, ref, note, deadline_ts,
   status: open|acked|verdict, ack_ref, verdict_ref, last_updated}
"""
import calendar
import hashlib
import json
import os
import sys
import time

from . import home, ownerasks, pk

LEDGER = "dispatches.jsonl"
STATUSES = ("open", "acked", "verdict")
DEFAULT_DEADLINE_S = 2700          # 45min — a long review turn is normal


def ledger_path():
    return ownerasks.ledger_path(LEDGER)


def rows():
    return ownerasks.rows(ledger_path())


def add(recipient, lane, ref=None, note=None, deadline_s=DEFAULT_DEADLINE_S):
    """Open a dispatch. -> row, or None when the ledger refused the write
    (the caller MUST surface that; a silent success-lie re-creates the very
    bug this ledger exists to fix)."""
    recipient = (recipient or "").strip()
    lane = (lane or "").strip()
    if not recipient or not lane:
        return None
    ts = pk.now_ts()
    rid = hashlib.blake2b(
        ("%s|%s|%s|%d" % (ts, recipient, lane, os.getpid())).encode("utf-8"),
        digest_size=4).hexdigest()
    row = {"id": rid, "ts": ts, "recipient": recipient, "lane": lane,
           "ref": (ref or "").strip() or None,
           "note": (note or "").strip() or None,
           "deadline_s": int(deadline_s),
           "source": home.session_id() or "cli",
           "status": "open", "ack_ref": None, "verdict_ref": None,
           "last_updated": ts}
    if not ownerasks._append(row, ledger_path()):
        return None
    pk.event("dispatch-add", rid, "%s -> %s" % (recipient, lane))
    return row


def _update(rid, status, **patch):
    r = rows().get(str(rid or ""))
    if not r:
        return None, "no such dispatch: %s (helm dispatch list)" % rid
    if r.get("status") == "verdict":
        return None, ("dispatch %s already has a verdict (closed) — open a new "
                      "one" % rid)
    row = dict(r)
    row.update(patch)
    row["status"] = status
    row["last_updated"] = pk.now_ts()
    if not ownerasks._append(row, ledger_path()):
        return None, "ledger unwritable (%s) — update NOT recorded" % ledger_path()
    pk.event("dispatch-" + status, str(rid),
             str(patch.get("verdict_ref") or patch.get("ack_ref") or ""))
    return row, None


def mark_ack(rid, ref):
    """The seat picked it up. Does NOT close the row — an ack is a promise,
    and promises are what this ledger exists to stop trusting."""
    if not (ref or "").strip():
        return None, "ack needs a ref (the chat-post id that acknowledged it)"
    return _update(rid, "acked", ack_ref=str(ref).strip())


def mark_verdict(rid, ref):
    """THE ONLY CLOSER: the verdict/commit the recipient actually produced."""
    if not (ref or "").strip():
        return None, "verdict needs a ref (commit sha or chat-post id)"
    return _update(rid, "verdict", verdict_ref=str(ref).strip())


def _age_s(row):
    """Seconds since the dispatch was opened. pk.now_ts() is a UTC
    '%Y-%m-%dT%H:%M:%SZ' string, so parse it back through calendar.timegm —
    time.mktime would read it as LOCAL and silently skew every deadline by the
    UTC offset (7h here, which would hide every overdue row all evening).
    Fail-open to 0: an unparseable ts reads as brand new, never as overdue,
    so a bad row can never manufacture a false alarm."""
    try:
        t = time.strptime(str(row.get("ts") or ""), "%Y-%m-%dT%H:%M:%SZ")
        return max(0, int(time.time() - calendar.timegm(t)))
    except (ValueError, TypeError):
        return 0


def open_rows():
    """Every dispatch still awaiting a verdict, oldest first."""
    rs = [r for r in rows().values() if r.get("status") != "verdict"]
    rs.sort(key=lambda r: (str(r.get("ts") or ""), str(r.get("id") or "")))
    return rs


def overdue():
    """Open dispatches past their deadline — LOOK, do not act. See the module
    docstring: an auto-reassign on this signal duplicates live work."""
    out = []
    for r in open_rows():
        d = int(r.get("deadline_s") or DEFAULT_DEADLINE_S)
        if _age_s(r) >= d:
            out.append(r)
    return out


def oldest_overdue():
    rs = overdue()
    return rs[0] if rs else None


USAGE = ("usage: helm dispatch add <recipient> <lane> [--ref R] [--note N] "
         "[--deadline SECONDS] | ack <id> <ref> | verdict <id> <ref> | "
         "list [--open] [--overdue] [--json]")


def _flag(args, name):
    if name in args:
        i = args.index(name)
        if i + 1 < len(args):
            v = args[i + 1]
            del args[i:i + 2]
            return v
        del args[i:i + 1]
    return None


def _fmt(r):
    age = _age_s(r) // 60
    d = int(r.get("deadline_s") or DEFAULT_DEADLINE_S) // 60
    late = " OVERDUE" if r in overdue() else ""
    return "  %s  %-16s %-26s %-8s %3dm/%dm%s%s" % (
        r["id"], r.get("recipient", "?"), r.get("lane", "?"),
        r.get("status", "?"), age, d, late,
        ("  " + r["ref"]) if r.get("ref") else "")


def cmd_dispatch(args):
    """dispatch add|ack|verdict|list — every hand-off is an obligation with a
    deadline, durable past the session that made it."""
    args = list(args or [])
    if not args or args[0] in ("-h", "--help"):
        print(USAGE, file=sys.stderr)
        return 2
    verb, rest = args[0], args[1:]
    if verb == "add":
        ref = _flag(rest, "--ref")
        note = _flag(rest, "--note")
        dl = _flag(rest, "--deadline")
        pos = [a for a in rest if not a.startswith("--")]
        if len(pos) < 2:
            print(USAGE, file=sys.stderr)
            return 2
        try:
            deadline = int(dl) if dl else DEFAULT_DEADLINE_S
        except ValueError:
            print("helm dispatch: --deadline takes SECONDS", file=sys.stderr)
            return 2
        row = add(pos[0], pos[1], ref=ref, note=note, deadline_s=deadline)
        if row is None:
            print("helm dispatch: ledger would not take the write — dispatch "
                  "NOT recorded (fix %s before relying on it)" % ledger_path(),
                  file=sys.stderr)
            return 1
        print("helm dispatch: %s -> @%s  %s  (check back in %dm)" % (
            row["id"], row["recipient"], row["lane"], deadline // 60))
        return 0
    if verb in ("ack", "verdict"):
        pos = [a for a in rest if not a.startswith("--")]
        if len(pos) < 2:
            print(USAGE, file=sys.stderr)
            return 2
        fn = mark_ack if verb == "ack" else mark_verdict
        row, why = fn(pos[0], pos[1])
        if why:
            print("helm dispatch: " + why, file=sys.stderr)
            return 1
        print("helm dispatch: %s -> %s (%s)" % (
            row["id"], row["status"],
            row.get("verdict_ref") or row.get("ack_ref")))
        return 0
    if verb == "list":
        if "--overdue" in rest:
            rs = overdue()
        elif "--open" in rest:
            rs = open_rows()
        else:
            rs = sorted(rows().values(),
                        key=lambda r: str(r.get("ts") or ""))
        if "--json" in rest:
            print(json.dumps(rs, ensure_ascii=False, indent=1))
            return 0
        if not rs:
            print("helm dispatch: nothing outstanding — every hand-off has a "
                  "verdict")
            return 0
        print("helm dispatch — %d row%s (id | recipient | lane | status | "
              "age/deadline)" % (len(rs), "s"[:len(rs) != 1]))
        for r in rs:
            print(_fmt(r))
        od = overdue()
        if od:
            print("⚠ %d OVERDUE — CHECK IN, do not reassign on this signal "
                  "alone: a long turn looks identical to a dead seat. Verify "
                  "at the recipient side first." % len(od))
        return 0
    print(USAGE, file=sys.stderr)
    return 2
