#!/usr/bin/env python3
"""helm.pile — the Lego reflex: one read-only screen of the whole inventory a
seat can build with. Owner-asked (task/3868): the owner noticed idle seats,
creds with room, the kimi reset, unused cred and landed-but-open rows on his
own, and wanted that reflex baked into helm for every seat at the moment it
matters.

READ-ONLY, BY DESIGN. This verb only READS the fleet and prints what it finds;
it writes no ledger, closes no row, routes nobody. It composes, it never
re-derives: every section reads ONE existing authority that already owns that
question and prints its own sentence — `seat_usability.join` for usability,
`creds._rows` for headroom, the git log for what landed, `tasks.open_rows` for
what is still open, `dispatches.snapshot` for FIX verdicts not yet taken,
`ownerasks.decisions_snapshot` for today's rulings, and FLOW (helm/pileflow.py)
for the fleet's throughput against its own 7-day medians. A section whose source
cannot be read (or is cut by its deadline — the usability join can be slow)
degrades to `unknown: <why>` with the time it was cut, never a traceback, and
its count prints `?`, never a number.

ONE NEW MODULE (the brief: do not grow an existing one). Not a NOARG verb —
it takes `--json`; `--json` is the machine surface, the text table is the
human one.
"""
import json
import os
import shlex
import subprocess
import sys
import threading
import time


#: The wall-clock budget each source gets before it is cut. `helm seat status`
#: folds the whole dispatch ledger and can take long; a section cut by its
#: deadline still prints, stamped with the time it was read.
SOURCE_DEADLINE_S = 20

_CUT = object()


def _now():
    return time.strftime("%H:%M:%S")


def _piece(name, detail="", act=""):
    return {"name": name, "detail": detail, "act": act}


def _patch_label(row):
    """The review-fix piece's name: its lane when it has one, else `row <id[:12]>`.
    A dispatch id is not a task number, so no `task/` prefix."""
    return row.get("lane") or ("row %s" % str(row.get("id") or "")[:12])


def _run_with_deadline(fn, secs=SOURCE_DEADLINE_S):
    """Run `fn()` in a thread, returning (result, cut_at). If it does not
    finish in `secs`, `result` is the sentinel `_CUT` and `cut_at` is the
    stamp the section prints; the caller never hangs the verb on `seat
    status`-speed reads."""
    box = []

    def _target():
        try:
            box.append(("ok", fn()))
        except Exception as exc:                      # noqa: BLE001
            box.append(("err", exc))
    thread = threading.Thread(target=_target, daemon=True)
    thread.start()
    thread.join(secs)
    if box:
        kind, val = box[0]
        if kind == "err":
            raise val
        return val, _now()
    return _CUT, _now()


def _unknown(why):
    """`unknown: <why>`, prefixed once: a source that already said
    `unknown: ...` is not stamped a second time."""
    why = str(why)
    return why if why.startswith("unknown:") else "unknown: %s" % why


def _section(fn, secs=SOURCE_DEADLINE_S):
    """Run one section's source under the deadline, returning (pieces,
    reason). `pieces` is a list of _piece; when the source is cut by its
    deadline or unreadable there are NO pieces and the reason is
    `unknown: <why>`, never a raise; the reason stamps the time it was cut.
    An unknown section carries no placeholder piece, because a placeholder
    is counted: the header would read `(1)` for a source nobody read.
    `secs` overrides the per-source budget so a test can force the cut
    quickly."""
    try:
        result, cut_at = _run_with_deadline(fn, secs=secs)
    except Exception as exc:                    # noqa: BLE001 — a source read, not a crash
        return [], "unknown: %s at %s" % (exc.__class__.__name__, _now())
    if result is _CUT:
        return [], "unknown: cut at %s (deadline %ds)" % (cut_at, secs)
    pieces, reason = result[0], result[1]
    if not pieces:
        # a degraded read is `unknown: <why>`; a clean empty is an empty
        # reason (kept a string so the output layer prints `none`)
        return [], _unknown(reason) if reason else ""
    return pieces, reason or ""


# ---------------------------------------------------------------------------
# the sections, each one existing source
# ---------------------------------------------------------------------------
def _seats():
    """Section 1: seats that are usable and holding nothing — a free piece.
    Reads `seat_usability.join` (one fold for the whole fleet). A seat whose
    holding count the join could not read (`holding` None) is not called
    idle: it is listed with `holding UNKNOWN`, never folded into zero."""
    from . import seat_usability
    join = seat_usability.join()
    pieces = []
    for seat, row in (join or {}).items():
        if not isinstance(row, dict) or not row.get("can_take_work"):
            continue
        holding = row.get("holding")
        if holding is None:
            pieces.append(_piece(seat, "holding UNKNOWN"))
        elif holding == 0:
            pieces.append(_piece(seat, row.get("reason") or ""))
    return (pieces, None)


def _creds():
    """Section 2: credentials with room that strands at reset — an account that
    has headroom but resets soon is the only one that loses it. Listed only
    when it is usable, has at least 50% headroom, and resets within the next
    six hours; that is room that will be lost."""
    pieces = []
    from . import creds
    now_s = time.time()
    for r in creds._rows() or []:
        if not r.get("usable"):
            continue
        headroom = r.get("headroom") or 0
        if headroom < 0.5:
            continue
        ms = r.get("resets_at_ms")
        if not ms:
            continue
        s = ms / 1000.0 - now_s
        if s > 6 * 3600:
            continue
        pieces.append(_piece(
            "%s %s" % (r.get("provider"), r.get("account")),
            "headroom %s%%, resets %s" % (
                round(headroom * 100), _human_seconds(s))))
    return (pieces, None)


def _resets():
    """Section 3: credentials whose window resets within the next hour."""
    from . import creds
    out = []
    now_s = time.time()
    for r in creds._rows() or []:
        ms = r.get("resets_at_ms")
        if not ms:
            continue
        s = ms / 1000.0 - now_s
        if 0 <= s <= 3600:
            out.append(_piece(
                "%s %s" % (r.get("provider"), r.get("account")),
                "resets %s" % _human_seconds(s)))
    return (out, None)


def _landed_today_open():
    """Section 4: rows whose lane landed on trunk today but the row is still
    open — the close-a-piece the owner catches.

    The task a merge served is taskhygiene's answer, read as `helm stale`
    reads it: `parse_train` on the subject, then `land_link` against the
    repository's lane records (`taskkey.lane_records`). Only a train car
    links a task, so a back-merge or a hand merge into a lane links none. A
    car that carried only a PART of its task says so: its row stays open
    for the rest. A trunk log that cannot be read, or lane records that
    cannot be read when a car's task was inferred from its lane's name, make
    the section unknown, never a clean empty."""
    from . import tasks
    from . import taskhygiene
    from . import taskkey
    lines = _git_log_since_midnight("--merges --format=%s")
    if lines is None:
        return ([], "trunk log unreadable")
    open_ids = {row.get("id") for row in (tasks.open_rows() or [])
                if isinstance(row, dict) and row.get("id")}
    lanes, lanes_bad = taskkey.lane_records(_repo_root())
    pieces, seen, blind = [], set(), []
    for line in lines:
        parsed = taskhygiene.parse_train(line)
        task, why = taskhygiene.land_link(parsed, lanes, lanes_bad)
        if why and lanes_bad:
            blind.append(why)
        if not task or task in seen or task not in open_ids:
            continue
        pieces.append(_piece(task, "a part landed today, the rest still open"
                             if parsed["partial"] else
                             "landed today, still open"))
        seen.add(task)
    return (pieces, _unknown("; ".join(blind)) if blind else None)


def _reviewer_patches():
    """Section 6: reviewer FIX patches not yet taken. A folded row in the
    ledger is a FIX when its verdict is a fix polarity (`dispatches` verdict
    rows carry `status == "verdict"` and `polarity == "fix"`), it is still
    owed, and it is the newest row of its chain — a later verdict in the same
    chain_root supersedes it. The row is older than a week before it drops off.
    A FIX verdict with no `patch_tip` is a finding, not a patch: nothing was
    committed for the lane to take, so it is not listed here."""
    from . import dispatches
    current, unavailable = dispatches.snapshot()
    if unavailable:
        return ([], "dispatch ledger unreadable")
    # Iterate the WHOLE snapshot, not `dispatches.owed(current)`: owed() returns
    # only OPEN rows, and an open row never carries a verdict, so the fix
    # filter could never match and the section always read 0. A folded FIX row
    # is no longer open, but it is still owed until its lane takes it.
    rows = [r for r in current.values() if isinstance(r, dict)]
    now = time.time()
    # A FIX that a newer row in the same chain supersedes is not owed — the
    # later verdict in the same chain_root is the current state. Keep the
    # newest row (by ts, then id) of each chain_root.
    newest_by_chain = {}
    for r in rows:
        if not isinstance(r, dict):
            continue
        chain = r.get("chain_root") or r.get("id")
        prev = newest_by_chain.get(chain)
        key = (_parsed_ts(r.get("ts")), r.get("id") or "")
        if prev is None or key > prev[0]:
            newest_by_chain[chain] = (key, r)
    pieces = []
    for _key, r in newest_by_chain.values():
        if r.get("status") != "verdict" or r.get("polarity") != "fix":
            continue
        if not r.get("patch_tip"):
            continue
        ts = r.get("ts")
        if ts and _parsed_ts(ts) < now - 7 * 86400:
            continue
        pieces.append(_piece(
            _patch_label(r),
            "FIX patch %s from %s, owed by %s, row %s%s" % (
                str(r.get("patch_tip"))[:12], r.get("recipient"), r.get("sender"),
                str(r.get("id"))[:12] if r.get("id") else "",
                "  " + r.get("task") if r.get("task") else "")))
    return (pieces, None)


def _today_rulings():
    """Section 7: today's owner rulings (reads `ownerasks.decisions_snapshot`).
    A RULING is the card's `verdict`, which only an owner door casts, and its
    time is the verdict's own `ts`. The card's top-level `ts` is when it was
    FILED: a card filed today and still open is a question, not a ruling, and
    a card filed last week and ruled today is today's ruling. Only verdicts at
    or after local midnight are listed."""
    from . import ownerasks
    cards, unavailable = ownerasks.decisions_snapshot()
    if unavailable:
        return ([], "owner-decisions ledger unreadable")
    pieces = []
    midnight = _midnight_s()
    for rid, card in (cards or {}).items():
        if not isinstance(card, dict):
            continue
        verdict = card.get("verdict")
        if not isinstance(verdict, dict):
            continue
        ts = verdict.get("ts")
        if ts and _parsed_ts(ts) >= midnight:
            pieces.append(_piece(
                "owner ruling %s" % rid, "%s at %s" % (
                    verdict.get("label") or verdict.get("choice") or "?", ts)))
    return (pieces, None)


def _flow():
    """Section 7: FLOW, the fleet's throughput against its own 7-day medians
    (task/4184, helm/pileflow.py). Its lines carry a `state` (FLAG, ok or
    UNKNOWN), and its header counts the flagged and unknown lines, not the
    lines: nine measured lines are not nine pieces."""
    from . import pileflow
    return (pileflow.lines(), None)


FLOW_TITLE = "FLOW (throughput against its own 7-day median)"

_SECTIONS = [
    ("SEATS (usable and idle)", _seats),
    ("CREDENTIALS (room that strands)", _creds),
    ("RESETS WITHIN THE HOUR", _resets),
    ("LANDED TODAY, ROW STILL OPEN", _landed_today_open),
    ("REVIEWER PATCHES NOT TAKEN", _reviewer_patches),
    ("TODAY'S OWNER RULINGS", _today_rulings),
    (FLOW_TITLE, _flow),
]


def _count(sec):
    """The header's count: `?` for an unread section; for a section whose
    lines carry a state (FLOW), the flagged and unknown lines; else the
    pieces."""
    if sec["reason"].startswith("unknown:"):
        return "?"
    pieces = sec["pieces"]
    if not any("state" in p for p in pieces):
        return len(pieces)
    flagged = sum(1 for p in pieces if p.get("state") == "FLAG")
    unknown = sum(1 for p in pieces if p.get("state") == "UNKNOWN")
    return "%d flagged%s" % (flagged, ", %d unknown" % unknown
                             if unknown else "")


def _render(p):
    if "state" in p:
        line = "  %-7s %s" % (p["state"], p["name"])
    else:
        line = "  " + p["name"]
    if p["detail"]:
        line += " — " + p["detail"]
    if "state" in p and p.get("act") and p["state"] != "ok":
        # who acts is printed where someone must act; --json keeps it always
        line += " (%s acts)" % p["act"]
    return line


def cmd_pile(args):
    """pile [--json] — one read-only screen of the whole inventory a seat can
    build with (the Lego reflex, owner-asked task/3868). READ-ONLY: it lists
    every unused piece and names the source and act verb for each; it writes
    nothing, closes nothing, routes nobody."""
    as_json = "--json" in (args or [])
    sections = []
    for title, fn in _SECTIONS:
        pieces, reason = _section(fn)
        sections.append({"title": title, "pieces": pieces, "reason": reason})
    if as_json:
        print(json.dumps({"sections": sections, "cut_at": _now()}, indent=2))
        return 0
    for sec in sections:
        print("== %s (%s) ==" % (sec["title"], _count(sec)))
        for p in sec["pieces"]:
            print(_render(p))
        if sec["reason"]:
            print("  " + sec["reason"])
        elif not sec["pieces"]:
            # a measured empty: the header's (0) is the only count; name it
            print("  none")
    return 0


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _git_log_since_midnight(fmt):
    """The one git read the whole suite counts: route it through the vcs seam
    (helm/vcs.py), never a bare subprocess, so the spawn stays in the seam.
    None when the log cannot be read: an unread trunk is not an empty day."""
    root = _repo_root()
    start = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(_midnight_s()))
    try:
        from . import vcs
        rc, out, _err = vcs.backend(root).text(
            root, "log", vcs.backend(root).trunk_ref(root),
            "--since=%s" % start, *shlex.split(fmt))
    except Exception:                             # noqa: BLE001
        return None
    return out.strip().splitlines() if rc == 0 else None


def _human_seconds(s):
    if s < 60:
        return "%ds" % s
    if s < 3600:
        return "%dm" % (s // 60)
    return "%.1fh" % (s / 3600)


def _repo_root():
    """The repo this module ships in — the parent of `helm/` — so the git log
    runs against the checkout that owns it, never a hardcoded path that only
    happens to be right on one machine."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _midnight_s():
    """The epoch (true seconds) of local midnight — the shared 'today'
    boundary both the git-log and the owner-ruling sections measure against,
    so neither drifts onto a different start-of-day than the other.

    `time.mktime` reads the struct as LOCAL time. `calendar.timegm` would read
    the local wall clock as if it were UTC, which moves 'today' by the zone's
    offset (west of UTC, it starts in the evening of the day before)."""
    lt = time.localtime()
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))


def _parsed_ts(ts):
    """The epoch of a stamp, accepting either the store's ISO `%Y-%m-%dT%H:%M:%
    SZ` (UTC, `calendar.timegm` reads the `Z`) or a numeric epoch in seconds.
    Numeric and ISO are both true seconds, so both compare against the same
    `_midnight_s()`. An unparseable stamp returns 0, which fails every today
    and 7-day window — the card is dropped, never guessed as recent."""
    if isinstance(ts, (int, float)):
        return float(ts)
    import calendar
    import datetime
    try:
        local = datetime.datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ")
        return calendar.timegm(local.timetuple())
    except (ValueError, TypeError):
        return 0
