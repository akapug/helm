#!/usr/bin/env python3
"""helm.pileflow — the FLOW section of `helm pile`: the fleet's throughput,
measured, so a bottleneck is flagged by a number and not found by hand
(owner P0, task/4184).

WHY. A slow-down that nobody measures is found by hand, hours late: land
gates can run serial at four to six times their usual time, or a tick
component can raise on every pass for days, and no surface says so. FLOW
puts each such number beside its own recent history.

ONE LINE PER QUESTION, EACH READ FROM THE SOURCE THAT ALREADY OWNS IT:

  land gate      the newest land-gate receipt in the gate ledger
                 (`gate.receipts_path`; a land gate is one a train of the
                 week names, `gate:<id>` in its history): its wall time
                 against the 7-day median of the land gates before it,
                 sliced or serial (a sliced
                 receipt carries `slice_authority.kind == "gateslice"`), and
                 the canary (`gate.sliced_land_disabled`,
                 `gatecanary.standing`), which decides whether land gates may
                 run sliced at all.
  lands per hour the project's land log (`autoland.land_log_path`): lands in
                 the last six hours against the 7-day median of the six-hour
                 windows before them.
  owner P0s      the task ledger, replayed (`eventledger.checked_events`):
                 the open owner P0 count against its own hourly 7-day median,
                 with the median age and the oldest row.
  leads          per seat that owns an open P0/P1: the rows nothing routes
                 (no open dispatch row carries the row, no live lease names
                 its number, and the row is not claimed) against what that
                 seat has in flight: open dispatch rows it sent and lane
                 leases it holds. Harness subagents are NOT counted: the
                 idle-capacity signal (task/3821, lane
                 idle-capacity-signal-3821) is not on trunk, and the line
                 says so.
  land train     auto-land's store (`autoland.active`, `read_control`): a
                 STOPPED train or a pause needs a person; a train in flight
                 is held against the 7-day median of the DONE trains.
  fab jobs       `fab status --live`, the one external call: a job its node
                 reads STALE (no log output for 30+ min).
  helm units     `systemctl --user --failed 'helm*'`.
  dark seats     the burn snapshot's dark families (`darkmove.dark_families`,
                 the mover's FAMILY leg) and the work their seats hold that
                 has STARTED (a claimed task): the mover moves only unstarted
                 work, so started work waits for a person. The mover's own
                 state file is written on every pass that reads the fleet, so
                 its age is the mover's liveness.
  mirrors        each reference mirror and our fork of the proxy, from local
                 git data only (no fetch): how far HEAD is behind the
                 upstream ref the last fetch left, by the date of the oldest
                 upstream commit HEAD lacks, and how old that fetch is.

THE RULE FOR A FLAG. A number is flagged only when it is 2x worse than its
own 7-day median (a count's median is floored at one, so a single row is
never "2x"). A line whose question is a state that is never normal (a
stopped train, a STALE job, a failed unit, started work on a dark seat, a
mirror a week behind) flags on that state. A line with no earlier sample in
the week says "no 7-day median" and does not flag.

CHEAP AND FAIL-CLOSED. Every reader runs in its own thread under one
deadline; a reader that raises, or is still running at the deadline, prints
one UNKNOWN line naming why, and every other line is still measured. Nothing
here writes, posts, routes or fetches.

WHO ACTS is read, never spelled: `seatevents.steward` names the integrator
(`build-lanes`), the credential steward and the local operator from the
roster and local-names, the same answer a #seats row would carry.
"""
import json
import os
import re
import shutil
import subprocess
import threading
import time

FLAG, OK, UNKNOWN = "FLAG", "ok", "UNKNOWN"
FACTOR = 2.0
HOUR = 3600.0
DAY = 86400.0
WEEK = 7 * DAY
#: The wall-clock budget the whole FLOW section gets; each reader runs in
#: parallel inside it.
FLOW_DEADLINE_S = 15
FAB_TIMEOUT_S = 12
#: The lands-per-hour window.
LANDS_WINDOW_S = 6 * HOUR
#: A mirror whose oldest missing upstream commit is older than this is
#: behind; a fetch older than this leaves the reading stale.
MIRROR_LAG_S = 7 * DAY
#: The dark-seat mover rides the idle-dispatch tick (every five minutes) and
#: writes its state on each pass that reads the fleet; six missed passes is a
#: stopped mover.
MOVER_STALE_S = 30 * 60
#: Lines shown per kind before "and N more".
SHOWN = 6
#: Mirrors read when HELM_PILE_MIRRORS names none: every checkout under
#: ~/dev/references and our fork of the proxy.
MIRRORS_ENV = "HELM_PILE_MIRRORS"
DEFAULT_MIRRORS = (os.path.join("~", "dev", "references", "*"),
                   os.path.join("~", "dev", "akapug", "CLIProxyAPI"))
OPEN_STATUSES = ("open", "in_progress")
IDLE_CAPACITY_NOTE = ("lanes and dispatch rows only: harness subagents are "
                      "not counted until idle-capacity-signal-3821 lands")


def _line(name, state, detail, act=""):
    return {"name": name, "state": state, "detail": detail, "act": act}


def median(values):
    vals = sorted(v for v in values if isinstance(v, (int, float)))
    if not vals:
        return None
    mid = len(vals) // 2
    return vals[mid] if len(vals) % 2 else (vals[mid - 1] + vals[mid]) / 2.0


def _span(s):
    s = max(0.0, float(s))
    if s < 90:
        return "%ds" % s
    if s < 90 * 60:
        return "%.0f min" % (s / 60)
    if s < 2 * DAY:
        return "%.1fh" % (s / HOUR)
    return "%.1fd" % (s / DAY)


def _epoch(ts):
    """Epoch seconds of a stamp: a number, or the ledgers' `%Y-%m-%dT%H:%M:%SZ`.
    None when it does not parse, so a row with no readable time is left out
    of every window rather than guessed into one."""
    if isinstance(ts, bool):
        return None
    if isinstance(ts, (int, float)):
        return float(ts)
    import calendar
    try:
        return float(calendar.timegm(time.strptime(str(ts),
                                                   "%Y-%m-%dT%H:%M:%SZ")))
    except (ValueError, TypeError):
        return None


def against(value, base, higher_is_worse=True, floor=None, fmt=None):
    """(flagged, text) — `value` beside its 7-day median `base`. Flagged only
    at FACTOR times worse; `floor` lifts a small median (a count's floor is
    one) so one row against a median of zero is no deviation."""
    fmt = fmt or (lambda v: "%g" % v)
    if base is None:
        return False, "no 7-day median to compare"
    ref = max(base, floor) if floor is not None else base
    if higher_is_worse:
        if ref <= 0:
            return False, "7-day median %s" % fmt(base)
        ratio = value / float(ref)
        return ratio >= FACTOR, "%.1fx its 7-day median of %s" % (
            ratio, fmt(base))
    if ref <= 0:
        return False, "7-day median %s" % fmt(base)
    if value <= 0:
        return True, "none against a 7-day median of %s" % fmt(base)
    ratio = ref / float(value)
    return ratio >= FACTOR, "%.0f%% of its 7-day median of %s" % (
        100.0 * value / ref, fmt(base))


def _who(component):
    """The seat that acts for a component, read from the roster and
    local-names (`seatevents.steward`), never spelled here."""
    try:
        from . import seatevents
        seat, why = seatevents.steward(component)
    except Exception as exc:                  # noqa: BLE001 — a line still prints
        return "the %s steward (unread: %s)" % (component,
                                                exc.__class__.__name__)
    return seat if seat else "the %s steward (%s)" % (component, why)


# ---------------------------------------------------------------- 1 land gate
def gate_line(rows, now, canary, who=_who, ids=None):
    """The newest LAND gate against the 7-day median of the land gates
    before it. `ids` are the receipt ids the week's trains name (a train's
    history says `gate:<id>` for every gate it ran); a receipt labelled with
    a train's name counts too. None reads every whole-suite receipt, which is
    what a caller whose trains did not read gets, and the line says so."""
    suite = []
    for r in rows:
        if not isinstance(r, dict) or r.get("event") != "gate" \
                or not r.get("suite"):
            continue
        if ids is not None and r.get("id") not in ids \
                and not str(r.get("label") or "").startswith("train"):
            continue
        t, wall = _epoch(r.get("ts")), r.get("wall")
        if t is None or not isinstance(wall, (int, float)) \
                or isinstance(wall, bool):
            continue
        suite.append((t, float(wall), r))
    act = who("build-lanes")
    if not suite:
        return [_line("land gate", UNKNOWN,
                      "no whole-suite gate receipt in the ledger%s" % (
                          " that a train of the week names"
                          if ids is not None else ""), act)]
    suite.sort(key=lambda x: x[0])
    t, wall, newest = suite[-1]

    def sliced(r):
        return (r.get("slice_authority") or {}).get("kind") == "gateslice"
    base = median(w for ts, w, _r in suite[:-1] if ts >= now - WEEK)
    flagged, text = against(wall, base, fmt=_span)
    recent = [r for ts, _w, r in suite if ts >= now - LANDS_WINDOW_S]
    serial = sum(1 for r in recent if not sliced(r))
    detail = "newest %s gate %s took %s (%s, %s, %s ago): %s" % (
        "land" if ids is not None else "whole-suite (trains unread)",
        newest.get("id") or "?", _span(wall),
        "sliced" if sliced(newest) else "serial",
        newest.get("status") or "?", _span(now - t), text)
    if recent:
        detail += "; last 6h: %d of %d gates serial" % (serial, len(recent))
    if canary:
        detail += "; canary: %s" % canary
    return [_line("land gate", FLAG if flagged else OK, detail, act)]


def _receipts_path():
    from . import gate
    return gate.receipts_path()


def _gate_rows(path):
    """The whole-suite gate rows of the receipts ledger. A line that is not
    a gate receipt is skipped by a byte test before it is parsed, so the
    40 MB of failure tails is never decoded."""
    out = []
    with open(path, "rb") as fh:
        for raw in fh:
            if b'"suite"' not in raw or b'"gate"' not in raw:
                continue
            try:
                row = json.loads(raw)
            except ValueError:
                continue
            if isinstance(row, dict):
                out.append(row)
    return out


def canary_text():
    """One sentence on whether land gates may run sliced, from the marker
    and the canary record the land door reads."""
    from . import gate, gatecanary
    if gate.sliced_land_disabled():
        return ("the canary DISABLE marker stands, so land gates run serial "
                "until a person clears it (`helm gate canary clear --reason "
                "...`)")
    st = gatecanary.standing()
    if st.get("met"):
        return "the canary stands for slices"
    return ("the canary record does not stand for slices (%d of %d agreeing "
            "trees since the last DIVERGED), so land gates run serial "
            "(`helm gate canary status`)" % (st.get("agree") or 0,
                                             st.get("needed") or 0))


_GATE_NOTE = re.compile(r"\bgate:([0-9a-f]{16})\b")


def land_gate_ids(trains):
    """The gate receipt ids the given train records name: every gate a train
    ran is a `gate:<id>` in its history, red or green."""
    ids = set()
    for st in trains:
        if not isinstance(st, dict):
            continue
        for note in st.get("history") or ():
            if isinstance(note, dict):
                ids.update(_GATE_NOTE.findall(str(note.get("note") or "")))
        if isinstance(st.get("receipt"), str):
            ids.add(st["receipt"])
    return ids


def _gate(ctx):
    rows = _gate_rows(_receipts_path())
    try:
        canary = canary_text()
    except Exception as exc:                   # noqa: BLE001
        canary = "UNKNOWN (%s)" % exc.__class__.__name__
    try:
        st, _control, done = ctx.trains()
        ids = land_gate_ids(done + ([st] if st else []))
    except Exception:                          # noqa: BLE001 — said on the line
        ids = None
    return gate_line(rows, ctx.now, canary, ids=ids)


# ----------------------------------------------------------- 2 lands per hour
def lands_line(stamps, now, who=_who):
    stamps = sorted(s for s in stamps if isinstance(s, (int, float)))
    hours = LANDS_WINDOW_S / HOUR

    def rate(end):
        return sum(1 for s in stamps if end - LANDS_WINDOW_S <= s < end) \
            / hours
    current = rate(now + 1e-6)
    samples = [rate(now - k * HOUR) for k in range(int(hours), 7 * 24 + 1)]
    base = median(samples) if stamps else None
    flagged, text = against(current, base, higher_is_worse=False,
                            fmt=lambda v: "%.1f/h" % v)
    last = ("the last land was %s ago" % _span(now - stamps[-1])) \
        if stamps else "no land is recorded"
    return [_line("lands per hour", FLAG if flagged else OK,
                  "%.1f/h over the last 6h: %s; %s" % (current, text, last),
                  who("build-lanes"))]


def _lands(ctx):
    from . import autoland
    path = autoland.land_log_path(ctx.root())
    stamps = []
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            try:
                row = json.loads(raw)
            except ValueError:
                continue
            t = _epoch(row.get("ts")) if isinstance(row, dict) else None
            if t is not None:
                stamps.append(t)
    return lands_line(stamps, ctx.now)


# ------------------------------------------------------------- task history
def _event_ts(e):
    return _epoch(e.get("last_updated", e.get("ts"))) or 0.0


def _owner_p0(row):
    return row.get("status") in OPEN_STATUSES \
        and row.get("origin") == "owner" and row.get("priority") == "P0"


def _hot(row):
    """An open P0/P1 row: the only rows a sample measures."""
    return row.get("status") in OPEN_STATUSES \
        and row.get("priority") in ("P0", "P1")


def replay(events, now):
    """(current rows by id, hourly samples over the 7 days before `now`).
    Each sample is (t, [the open P0/P1 rows at t]): the ledger's events are
    whole rows, so the row at t is its newest event at or before t. The
    current rows are the ledger-order fold, the same answer `tasks.rows`
    gives."""
    current = {}
    for e in events:
        if isinstance(e, dict) and e.get("id") is not None:
            current[str(e["id"])] = e
    order = sorted((e for e in events if isinstance(e, dict)
                    and e.get("id") is not None), key=_event_ts)
    times = [now - k * HOUR for k in range(7 * 24, 0, -1)]
    hot, samples, j = {}, [], 0
    for t in times:
        while j < len(order) and _event_ts(order[j]) <= t:
            e = order[j]
            j += 1
            if _hot(e):
                hot[str(e["id"])] = e
            else:
                hot.pop(str(e["id"]), None)
        samples.append((t, list(hot.values())))
    return current, samples


def _num_key(tid):
    m = re.search(r"(\d+)$", str(tid))
    return (0, int(m.group(1)), "") if m else (1, 0, str(tid))


def _row_epoch(row):
    return _epoch(row.get("ts"))


# -------------------------------------------------------------- 3 owner P0s
def owner_p0_line(events, now, who=_who, replayed=None):
    current, samples = replayed or replay(events, now)
    rows = [r for r in current.values() if _owner_p0(r)]
    # oldest first; a tie goes to the lower row number, the one filed first
    ages = sorted(((now - _row_epoch(r), r["id"]) for r in rows
                  if _row_epoch(r) is not None),
                  key=lambda a: (-a[0], _num_key(a[1])))
    counts, med_ages = [], []
    for t, hot in samples:
        p0 = [r for r in hot if _owner_p0(r)]
        counts.append(len(p0))
        a = median(t - _row_epoch(r) for r in p0
                   if _row_epoch(r) is not None)
        if a is not None:
            med_ages.append(a)
    flagged, text = against(len(rows), median(counts) if samples else None,
                            floor=1, fmt=lambda v: "%g" % v)
    detail = "%d open: %s" % (len(rows), text)
    if ages:
        detail += "; median age %s (7-day median %s); oldest %s at %s" % (
            _span(median(a for a, _id in ages)),
            _span(median(med_ages)) if med_ages else "none",
            ages[0][1], _span(ages[0][0]))
    owners = {}
    for r in rows:
        key = r.get("owner") or None
        owners[key] = owners.get(key, 0) + 1
    named = sorted(((n, o) for o, n in owners.items() if o), reverse=True)
    act = ", ".join("%s (%d)" % (o, n) for n, o in named[:SHOWN])
    if owners.get(None):
        act = (act + "; " if act else "") + "%d unowned: %s routes them" % (
            owners[None], who("build-lanes"))
    return [_line("owner P0s", FLAG if flagged else OK, detail,
                  act or who("build-lanes"))]


# ------------------------------------------------------------------ 4 leads
def _number(tid):
    m = re.search(r"(\d+)\s*$", str(tid or ""))
    return re.compile(r"(?<![0-9])%s(?![0-9])" % m.group(1)) if m else None


def lead_lines(rows, dispatch_rows, claims, baselines, who=_who):
    """Per seat that owns an open P0/P1: the rows nothing routes against
    what it has in flight. `rows` are the task rows, `dispatch_rows` the
    open dispatch rows, `claims` the live lease ledger, `baselines` {seat or
    None (unowned): the 7-day median of its unclaimed open P0/P1 count}.

    A seat is FLAGGED only when its unrouted count is 2x that median. In
    flight is printed beside it, never flagged on: it counts lanes and
    dispatch rows only, so a lead driving harness subagents reads zero
    there, and a flag on zero would be noise. The baseline counts every
    unclaimed open P0/P1 a seat owned (routed or not), so it can only sit
    above the unrouted history, never flag early."""
    leases = [(str(k), str(v.get("holder") or ""))
              for k, v in (claims or {}).items()
              if isinstance(v, dict) and str(k).startswith("worktree:")]
    sent, carried, lanes = {}, set(), []
    for d in dispatch_rows or ():
        if not isinstance(d, dict):
            continue
        sent[d.get("sender")] = sent.get(d.get("sender"), 0) + 1
        if d.get("task"):
            carried.add(str(d["task"]))
        lanes.append(str(d.get("lane") or ""))
    unrouted = {}
    for r in rows:
        if not isinstance(r, dict) or r.get("status") != "open" \
                or r.get("priority") not in ("P0", "P1"):
            continue
        tid = str(r.get("id"))
        num = _number(tid)
        if tid in carried or (num and (
                any(num.search(k) for k, _h in leases)
                or any(num.search(lane) for lane in lanes))):
            continue
        unrouted.setdefault(r.get("owner") or None, []).append(tid)

    def ids(tids):
        return ", ".join(tids[:3]) + (" ..." if len(tids) > 3 else "")
    out = []
    for seat, tids in sorted(unrouted.items(),
                             key=lambda kv: (-len(kv[1]), str(kv[0]))):
        flagged, text = against(len(tids), baselines.get(seat), floor=1)
        if not flagged:
            continue
        if seat is None:
            out.append(_line("leads", FLAG, "%d unowned P0/P1 rows nothing "
                             "routes (%s): %s" % (len(tids), ids(tids), text),
                             who("build-lanes")))
            continue
        held = sum(1 for _k, h in leases if h.casefold() == seat.casefold())
        out.append(_line("lead %s" % seat, FLAG, (
            "%d unrouted P0/P1 (%s): %s; in flight %d (%d rows sent, %d lanes "
            "held)" % (len(tids), ids(tids), text, sent.get(seat, 0) + held,
                       sent.get(seat, 0), held)), seat))
    more = len(out) - SHOWN
    out = out[:SHOWN]
    if more > 0:
        out.append(_line("leads", FLAG, "and %d more like these" % more,
                         who("build-lanes")))
    owned = sum(len(t) for k, t in unrouted.items() if k)
    seats = len([k for k in unrouted if k])
    summary = ("%d unrouted P0/P1 owned across %d seat%s, %d unowned; %d "
               "dispatch rows and %d lanes in flight; %s" % (
                   owned, seats, "" if seats == 1 else "s",
                   len(unrouted.get(None) or []), sum(sent.values()),
                   len(leases), IDLE_CAPACITY_NOTE)) if unrouted else \
        "no lead holds an unrouted P0/P1; " + IDLE_CAPACITY_NOTE
    return out + [_line("leads", OK, summary)]


def lead_baselines(samples):
    """{seat, or None for unowned: the 7-day median of its unclaimed open
    P0/P1 count}, one count per hourly sample (zero where it held none)."""
    per = {}
    for i, (_t, hot) in enumerate(samples):
        seen = {}
        for r in hot:
            if r.get("status") == "open":
                key = r.get("owner") or None
                seen[key] = seen.get(key, 0) + 1
        for seat, n in seen.items():
            per.setdefault(seat, [0] * len(samples))[i] = n
    return {s: median(v) for s, v in per.items()}


def _owner_p0s(ctx):
    current, samples = ctx.tasks()
    return owner_p0_line(None, ctx.now, replayed=(current, samples))


def _leads(ctx):
    from . import dispatches
    current, samples = ctx.tasks()
    snap, unavailable = dispatches.snapshot()
    if unavailable:
        raise OSError("dispatch ledger unreadable: %s" % unavailable)
    claims = dispatches.live_claims()
    return lead_lines(list(current.values()), dispatches.open_rows(snap),
                      claims, lead_baselines(samples))


# ------------------------------------------------------------- 5 land train
def train_line(st, control, done, now, who=_who):
    act = who("build-lanes")
    paused = (control or {}).get("paused")
    lines = []
    if paused:
        lines.append(_line("land train", FLAG, (
            "auto-land is PAUSED by %s %s ago (%s): no train forms until "
            "`helm train auto --resume`" % (
                paused.get("by") or "?",
                _span(now - (_epoch(paused.get("ts")) or now)),
                paused.get("reason") or "no reason given")), act))
    durations = [_epoch(d.get("archived_ts")) - _epoch(d.get("created_ts"))
                 for d in done or () if isinstance(d, dict)
                 and d.get("state") == "DONE"
                 and _epoch(d.get("archived_ts")) is not None
                 and _epoch(d.get("created_ts")) is not None]
    base = median(durations)
    if st and st.get("state") == "STOPPED":
        stop = st.get("stopped") or {}
        lines.append(_line("land train", FLAG, (
            "%s is STOPPED at %s%s and needs a person: %s; `helm train auto "
            "--resume` retries it, `--abandon --reason R` ends it" % (
                st.get("name") or st.get("train") or "?",
                stop.get("state") or "?",
                "/" + stop["step"] if stop.get("step") else "",
                str(stop.get("why") or "no reason recorded")[:240])), act))
    elif st and st.get("state"):
        age = now - (_epoch(st.get("created_ts")) or now)
        flagged, text = against(age, base, fmt=_span)
        lines.append(_line("land train", FLAG if flagged else OK,
                           "%s is %s, %s old: %s" % (
                               st.get("name") or "?", st.get("state"),
                               _span(age), text), act))
    elif not paused:
        lines.append(_line("land train", OK, (
            "no train in flight; a train took %s (7-day median)" % _span(base)
            if base is not None else "no train in flight"), act))
    return lines


def _done_trains(state_dir, now):
    """The archived trains of the last week, read from `done/`; a file is
    named `<train>-<archived epoch>-<hex>.json`, so older ones are not
    opened."""
    where = os.path.join(state_dir, "done")
    out = []
    try:
        names = os.listdir(where)
    except FileNotFoundError:
        return out
    for name in names:
        m = re.search(r"-(\d{9,})-[0-9a-f]+\.json$", name)
        if not m or float(m.group(1)) < now - WEEK:
            continue
        try:
            with open(os.path.join(where, name), encoding="utf-8") as fh:
                row = json.load(fh)
        except (OSError, ValueError):
            continue
        if isinstance(row, dict):
            out.append(row)
    return out


def _train(ctx):
    st, control, done = ctx.trains()
    return train_line(st, control, done, ctx.now)


# ---------------------------------------------------------------- 6 fab jobs
_FAB_HOST = re.compile(r"^== (\S+) ==\s*$")
_FAB_GONE = re.compile(r"^fab: (\S+) unreachable")
_FAB_ID = re.compile(r"\s(\S+) \(pgid ")


def fab_lines(rc, out, err, who=_who):
    """The STALE jobs `fab status --live` names, one FLAG line each, acted on
    by the seat that launched it: the name after the last `/` of the line's
    `launched by` field."""
    gone = [m.group(1) for m in map(_FAB_GONE.match,
                                    (err or "").splitlines()) if m]
    if "--live" in gone:
        return [_line("fab jobs", UNKNOWN, (
            "this fab has no `status --live` (it read --live as a host); "
            "a fab that reports STALE is not installed here"),
            who("local-serving"))]
    hosts, host, stale, flying = [], None, [], 0
    for raw in (out or "").splitlines():
        m = _FAB_HOST.match(raw)
        if m:
            host = m.group(1)
            hosts.append(host)
            continue
        if "(pgid " not in raw:
            continue
        flying += 1
        if "STALE:" not in raw:
            continue
        jid = (_FAB_ID.search(raw) or [None, "?"])[1]
        age = re.search(r"(\d+) min old", raw)
        by = re.search(r"launched by (\S+)", raw)
        seat = by.group(1).rsplit("/", 1)[-1] if by else None
        stale.append(_line("fab jobs", FLAG, (
            "%s on %s is STALE: no log output for 30+ min%s; `fab tail %s %s "
            "-n 20`, and kill it if nobody owns it" % (
                jid, host or "?", ", %s min old" % age.group(1) if age else "",
                host or "<host>", jid)),
            seat if seat and seat != "unknown" else who("local-serving")))
    if rc is None or (not hosts and gone):
        return [_line("fab jobs", UNKNOWN, "fab status --live did not read%s"
                      % (" (%s)" % ", ".join(gone) if gone else ""),
                      who("local-serving"))]
    tail = "; unreachable, not read: %s" % ", ".join(gone) if gone else ""
    if not stale:
        return [_line("fab jobs", OK, "%d in flight on %d hosts, none STALE%s"
                      % (flying, len(hosts), tail), "")]
    stale[0]["detail"] += tail
    return stale


def _fab(ctx):
    fab = shutil.which("fab")
    if not fab:
        return [_line("fab jobs", UNKNOWN, "fab is not on PATH", "")]
    try:
        p = subprocess.run([fab, "status", "--live"], capture_output=True,
                           text=True, timeout=FAB_TIMEOUT_S,
                           stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return [_line("fab jobs", UNKNOWN, "fab status --live took over %ds"
                      % FAB_TIMEOUT_S, "")]
    return fab_lines(p.returncode, p.stdout, p.stderr)


# -------------------------------------------------------------- 7 helm units
def units_line(rc, out, who=_who):
    if rc is None:
        return [_line("helm units", UNKNOWN,
                      "systemctl --user could not be asked", "")]
    failed = [raw.split()[0] for raw in (out or "").splitlines()
              if raw.strip()]
    if not failed:
        return [_line("helm units", OK, "no helm user unit failed", "")]
    return [_line("helm units", FLAG, "%d failed: %s; `systemctl --user "
                  "status <unit>` says why" % (len(failed),
                                               ", ".join(failed[:SHOWN])),
                  who("local-serving"))]


def _units(ctx):
    from . import timerhealth
    rc, out = timerhealth._systemctl(["--failed", "--no-legend", "--plain",
                                      "helm*"])
    return units_line(rc, out)


# -------------------------------------------------------------- 8 dark seats
def dark_line(dark, family_of, task_rows, dispatch_rows, mover_at,
              switch_on, now, who=_who):
    """`dark` {family: cause}; `family_of(seat)` -> family or None;
    `mover_at` the epoch the mover last wrote its state (None: never)."""
    memo = {}

    def fam(seat):
        if seat not in memo:
            memo[seat] = family_of(seat)
        return memo[seat]
    started, waiting = [], 0
    for r in task_rows or ():
        if isinstance(r, dict) and r.get("status") == "in_progress" \
                and r.get("owner") and fam(r["owner"]) in dark:
            started.append("%s (%s)" % (r.get("id"), r["owner"]))
    for d in dispatch_rows or ():
        if isinstance(d, dict) and d.get("recipient") \
                and fam(d["recipient"]) in dark:
            waiting += 1
    parts, flagged = [], False
    if started:
        flagged = True
        parts.append("%d started rows held by a dark seat: %s%s" % (
            len(started), ", ".join(started[:SHOWN]),
            " ..." if len(started) > SHOWN else ""))
    if waiting:
        parts.append("%d open dispatch rows wait on a dark seat" % waiting)
    act = who("credentials")
    if mover_at is None or now - mover_at > MOVER_STALE_S:
        flagged = True
        parts.append("the dark-seat mover has not finished a pass %s" % (
            "in %s" % _span(now - mover_at) if mover_at else "ever"))
        act += "; the mover: %s" % who("helm-friction")
    if not switch_on:
        parts.append("the mover is OFF (report only)")
    if dark:
        parts.append("dark: %s" % ", ".join(sorted(dark)))
    if not parts or (not flagged and not waiting):
        parts.insert(0, "no dark seat holds started work")
    return [_line("dark seats", FLAG if flagged else OK, "; ".join(parts),
                  act if flagged else "")]


def _dark(ctx):
    from . import burnflags, darkmove, dispatches
    flags, _age = burnflags.cached_flags()
    if not flags:
        raise OSError("no fresh burn snapshot names a dark family")
    current, _samples = ctx.tasks()
    snap, unavailable = dispatches.snapshot()
    if unavailable:
        raise OSError("dispatch ledger unreadable: %s" % unavailable)
    try:
        mover_at = os.stat(darkmove._state_path()).st_mtime
    except FileNotFoundError:
        mover_at = None
    on, _sentence = darkmove.switch_state()
    return dark_line(darkmove.dark_families(flags), darkmove._family_of,
                     list(current.values()), dispatches.open_rows(snap),
                     mover_at, on, ctx.now)


# ---------------------------------------------------------------- 9 mirrors
def _git(path, *args):
    from . import vcs
    rc, out, _err = vcs.backend(path).text(path, *args)
    return out.strip() if rc == 0 else None


def mirror_line(path, now):
    """How far one checkout's HEAD is behind the upstream ref its last fetch
    left, from local git data alone. The upstream ref is the `upstream`
    remote's HEAD (a fork), else HEAD's tracking branch (a mirror)."""
    name = os.path.basename(path.rstrip(os.sep))
    if _git(path, "rev-parse", "--is-inside-work-tree") != "true":
        return _line("mirror %s" % name, UNKNOWN, "not a git checkout: %s"
                     % path, "")
    up = None
    for ref in ("refs/remotes/upstream/HEAD", "refs/remotes/upstream/main",
                "refs/remotes/upstream/master", "HEAD@{upstream}",
                "refs/remotes/origin/HEAD"):
        if _git(path, "rev-parse", "-q", "--verify", ref + "^{commit}"):
            up = ref
            break
    if not up:
        return _line("mirror %s" % name, UNKNOWN,
                     "no upstream ref to compare HEAD with", "")
    stamps = _git(path, "log", "--format=%ct", "HEAD.." + up)
    if stamps is None:
        return _line("mirror %s" % name, UNKNOWN, "git log HEAD..%s did not "
                     "read" % up, "")
    missing = [int(s) for s in stamps.split() if s.isdigit()]
    gitdir = _git(path, "rev-parse", "--git-common-dir") or ".git"
    try:
        fetched = os.stat(os.path.join(path, gitdir, "FETCH_HEAD")
                          if not os.path.isabs(gitdir)
                          else os.path.join(gitdir, "FETCH_HEAD")).st_mtime
    except OSError:
        fetched = None
    label = up.replace("refs/remotes/", "")
    parts, flagged = [], False
    if missing:
        lag = now - min(missing)
        flagged = lag > MIRROR_LAG_S
        parts.append("HEAD lacks %d commit%s of %s, the oldest %s old" % (
            len(missing), "" if len(missing) == 1 else "s", label, _span(lag)))
    else:
        parts.append("HEAD has every commit of %s" % label)
    if fetched is None or now - fetched > MIRROR_LAG_S:
        flagged = True
        parts.append("last fetched %s" % ("%s ago, so upstream since then "
                                         "is unread" % _span(now - fetched)
                                         if fetched else "never"))
    return _line("mirror %s" % name, FLAG if flagged else OK,
                 "; ".join(parts), "")


def mirror_paths():
    import glob
    raw = os.environ.get(MIRRORS_ENV)
    specs = raw.split(os.pathsep) if raw else DEFAULT_MIRRORS
    out = []
    for spec in specs:
        for path in sorted(glob.glob(os.path.expanduser(spec.strip()))):
            if os.path.isdir(path) and path not in out:
                out.append(path)
    return out


def _mirrors(ctx, who=_who):
    paths = mirror_paths()
    if not paths:
        return [_line("mirrors", UNKNOWN, "no mirror checkout found (%s "
                      "names them)" % MIRRORS_ENV, "")]
    rows = [mirror_line(p, ctx.now) for p in paths]
    rows = [r for r in rows if not (r["state"] == UNKNOWN
                                    and r["detail"].startswith("not a git"))]
    act = who("local-serving")
    for r in rows:
        if r["state"] == FLAG:
            r["act"] = act
    if not rows:  # zero git checkouts measured is not a clean reading
        return [_line("mirrors", UNKNOWN, "none of the %d named paths is a "
                      "git checkout" % len(paths), act)]
    flagged = [r for r in rows if r["state"] != OK]
    if flagged:
        return flagged
    return [_line("mirrors", OK, "%d checkouts within 7 days of upstream "
                  "(local git data, no fetch)" % len(rows), "")]


# ------------------------------------------------------------------ runner
class _Ctx(object):
    """What two readers share, read once: the task-ledger replay and the
    repository root."""

    def __init__(self, now):
        self.now = now
        self._lock = threading.Lock()
        self._train_lock = threading.Lock()
        self._tasks = None
        self._trains = None

    def tasks(self):
        with self._lock:
            if self._tasks is None:
                from . import eventledger, tasks
                events, unavailable = eventledger.checked_events(
                    tasks.ledger_path())
                if unavailable:
                    raise OSError("task ledger unreadable: %s" % unavailable)
                self._tasks = replay(events, self.now)
            return self._tasks

    def trains(self):
        """(active train or None, control, the week's archived trains)."""
        with self._train_lock:
            if self._trains is None:
                from . import autoland
                root = self.root()
                st, why = autoland.active(root)
                if why:
                    raise OSError(why)
                control, why = autoland.read_control(root)
                if why:
                    raise OSError(why)
                self._trains = (st, control, _done_trains(
                    autoland.state_dir(root), self.now))
            return self._trains

    def root(self):
        from . import dispatches
        gitdir, why = dispatches.home_repo_id()
        if why or not gitdir:
            raise OSError(why or "this helm's repository did not resolve")
        return os.path.dirname(gitdir.rstrip(os.sep))


READERS = (
    ("land gate", _gate),
    ("lands per hour", _lands),
    ("owner P0s", _owner_p0s),
    ("leads", _leads),
    ("land train", _train),
    ("fab jobs", _fab),
    ("helm units", _units),
    ("dark seats", _dark),
    ("mirrors", _mirrors),
)


def lines(readers=None, deadline=FLOW_DEADLINE_S, now=None):
    """Every FLOW line, in reader order. The readers run in parallel under
    one deadline; one that raises or is still running prints a single
    UNKNOWN line, never a traceback, and the others are still measured."""
    ctx = _Ctx(time.time() if now is None else now)
    runs = []
    for name, fn in (READERS if readers is None else readers):
        box = []

        def target(fn=fn, box=box):
            try:
                box.append(("ok", fn(ctx)))
            except Exception as exc:          # noqa: BLE001 — a line, not a crash
                box.append(("err", exc))
        th = threading.Thread(target=target, daemon=True)
        th.start()
        runs.append((name, th, box))
    end = time.monotonic() + deadline
    out = []
    for name, th, box in runs:
        th.join(max(0.0, end - time.monotonic()))
        if not box:
            out.append(_line(name, UNKNOWN, "cut at %s (deadline %gs)" % (
                time.strftime("%H:%M:%S"), deadline)))
        elif box[0][0] == "err":
            exc = box[0][1]
            out.append(_line(name, UNKNOWN, "%s: %s" % (
                exc.__class__.__name__, str(exc)[:200])))
        else:
            out.extend(box[0][1] or [_line(name, UNKNOWN,
                                           "the reader returned no line")])
    # Every FLAG and UNKNOWN line names who acts: a reader that set no actor
    # falls back to the local-serving steward, who owns the probes themselves.
    steward = None
    for line in out:
        if line["state"] != OK and not line["act"]:
            steward = steward or _who("local-serving")
            line["act"] = steward
    return out
