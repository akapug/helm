#!/usr/bin/env python3
"""helm ownerasks — the OWNER-ASK LEDGER: the durable fix for dropped owner
asks (root cause: asks lived in memory-only panes + siloed scratch, and
report-back went to AGENTS, not the owner).

One append-only jsonl at `~/.helm/_global/owner-asks.jsonl` — the canonical
fleet owner-ask list (premise: builtin-tasklist-mirrored-to-dregg is the
fleet coordination primitive; agents SELF-ADD, a2a-self-add culture). Every
mutation appends a full SNAPSHOT row (event-sourced: last line per id wins),
so writes are O(1) — one unbuffered O_APPEND write, never a read — and the
history is never lost. FAIL-OPEN: ledger trouble never raises; an add that
did not land says so loudly (a silent success-lie would re-create the very
bug this ledger fixes).

Row schema (every snapshot carries the full shape):
  {id, ts, ask, source, status: open|done|reported,
   done_ref, report_ref, last_updated}

THE BAR (owner-surface-is-the-bar): `done` does NOT close a row. Work merely
finished is invisible work; only `report` — the chat-post id that told the
OWNER — moves a row to `reported`. Anything not `reported` is still open
fleet debt, and the stop-whisper's top rung (seats._ask_candidate) keeps
naming the oldest such row until the owner has actually been told.
"""
import hashlib
import json
import os
import sys

from . import home, pk

STATUSES = ("open", "done", "reported")


def ledger_path():
    return os.path.join(home.global_dir(), "owner-asks.jsonl")


def _append(row):
    """The O(1) append: makedirs + ONE unbuffered O_APPEND os.write (torn-line
    proof under concurrent appenders — kernel appends are atomic for one small
    write; the emit-law kin). Fail-open: False on any trouble, never a raise.
    No rotation — this ledger is DURABLE record, not telemetry exhaust."""
    try:
        path = ledger_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            os.write(fd, (json.dumps(row, ensure_ascii=False) + "\n").encode("utf-8"))
        finally:
            os.close(fd)
        return True
    except Exception:
        return False


def rows():
    """id -> latest snapshot (last line per id wins). Fail-open to {}:
    garbled lines skip, a missing/unreadable ledger reads as empty."""
    out = {}
    try:
        with open(ledger_path(), encoding="utf-8") as f:
            for ln in f:
                try:
                    d = json.loads(ln)
                except ValueError:
                    continue
                if isinstance(d, dict) and d.get("id"):
                    out[str(d["id"])] = d
    except OSError:
        pass
    return out


def add(ask, source=None):
    """Open a new ask row — the owner's words verbatim. -> row, or None when
    the ledger would not take the write (caller must surface that)."""
    ask = (ask or "").strip()
    if not ask:
        return None
    ts = pk.now_ts()
    rid = hashlib.blake2b(("%s|%s|%d" % (ts, ask, os.getpid())).encode("utf-8"),
                          digest_size=4).hexdigest()
    row = {"id": rid, "ts": ts, "ask": ask,
           "source": source or home.session_id() or "cli",
           "status": "open", "done_ref": None, "report_ref": None,
           "last_updated": ts}
    if not _append(row):
        return None
    pk.event("asks-add", rid, ask)
    return row


def _update(rid, status, **patch):
    """Append the row's next snapshot. -> (row, None) | (None, why)."""
    r = rows().get(str(rid or ""))
    if not r:
        return None, "no such ask: %s (helm asks list)" % rid
    if r.get("status") == "reported":
        return None, "ask %s already reported (closed) — add a new ask" % rid
    row = dict(r)
    row.update(patch)
    row["status"] = status
    row["last_updated"] = pk.now_ts()
    if not _append(row):
        return None, "ledger unwritable (%s) — update NOT recorded" % ledger_path()
    pk.event("asks-" + status, str(rid), str(patch.get("done_ref")
                                             or patch.get("report_ref") or ""))
    return row, None


def mark_done(rid, evidence):
    """Work landed (commit/evidence) — the row stays OPEN DEBT until report."""
    if not (evidence or "").strip():
        return None, "done needs evidence (a commit sha / artifact ref)"
    return _update(rid, "done", done_ref=str(evidence).strip())


def mark_report(rid, post_id):
    """THE ONLY CLOSER: the chat-post id that told the OWNER."""
    if not (post_id or "").strip():
        return None, "report needs the chat-post id that told the owner"
    return _update(rid, "reported", report_ref=str(post_id).strip())


def unreported():
    """Every row the owner has NOT been told about (open OR done), oldest
    first — the stop-whisper rung reads [0]."""
    rs = [r for r in rows().values() if r.get("status") != "reported"]
    rs.sort(key=lambda r: (str(r.get("ts") or ""), str(r.get("id") or "")))
    return rs


def oldest_unreported():
    rs = unreported()
    return rs[0] if rs else None


USAGE = ("usage: helm asks add <text> [--source S] | done <id> <evidence> | "
         "report <id> <chat-post-id> | list [--open] [--json]")


def cmd_asks(args):
    """asks add|done|report|list — the owner-ask ledger verbs."""
    args = list(args or [])
    if not args:
        print(USAGE, file=sys.stderr)
        return 2
    verb, rest = args[0], args[1:]
    if verb == "add":
        source = None
        if "--source" in rest:
            i = rest.index("--source")
            source = rest[i + 1] if len(rest) > i + 1 else None
            del rest[i:i + 2 if source else i + 1]
        text = " ".join(rest).strip()
        if not text:
            print(USAGE, file=sys.stderr)
            return 2
        row = add(text, source=source)
        if not row:
            print("helm asks: ledger unwritable (%s) — ask NOT recorded"
                  % ledger_path(), file=sys.stderr)
            return 1
        print("ask %s open: %s" % (row["id"], row["ask"]))
        return 0
    if verb in ("done", "report"):
        if len(rest) < 2:
            print(USAGE, file=sys.stderr)
            return 2
        rid, ref = rest[0], " ".join(rest[1:]).strip()
        row, why = (mark_done if verb == "done" else mark_report)(rid, ref)
        if not row:
            print("helm asks: " + why, file=sys.stderr)
            return 1
        if verb == "done":
            print("ask %s done (%s) — still OPEN until the owner hears it: "
                  "helm asks report %s <chat-post-id>" % (rid, ref, rid))
        else:
            print("ask %s reported — closed (post %s)" % (rid, ref))
        return 0
    if verb == "list":
        rs = sorted(rows().values(),
                    key=lambda r: (str(r.get("ts") or ""), str(r.get("id") or "")))
        if "--open" in rest:
            rs = [r for r in rs if r.get("status") != "reported"]
        if "--json" in rest:
            print(json.dumps(rs, indent=2, ensure_ascii=False))
            return 0
        if not rs:
            print("no asks" + (" open" if "--open" in rest else ""))
            return 0
        for r in rs:
            refs = " ".join(x for x in (
                ("done:%s" % r["done_ref"]) if r.get("done_ref") else "",
                ("post:%s" % r["report_ref"]) if r.get("report_ref") else "") if x)
            print("%s  %-8s  %s  %s%s" % (r.get("id"), r.get("status"),
                                          r.get("ts"), r.get("ask"),
                                          ("  [%s]" % refs) if refs else ""))
        return 0
    print(USAGE, file=sys.stderr)
    return 2
