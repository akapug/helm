"""Is a seat IDLE, and for how long? One reading, from the seat's own hooks.

THE GAP (task/3118). Two AX bars ask about idle seats: no seat sits more than
ten minutes idle on an owed row, and an idle seat's owed rows are re-surfaced
to it. Nothing in helm could say whether a seat was idle. `.seen` cannot: the
inbox beacon polls delivery, and delivery beats presence on every poll
(seats_delivery.deliver_any), so a seat asleep at its prompt with a beacon
armed reads `fresh` forever. ownership.py says the same thing about the same
field. The pane tail (seat_lifecycle.seat_liveness) can say IDLE at one
instant, but not since when, and it costs a pane read per seat.

THE SOURCE IS THE SEAT'S OWN HOOK RECORD, and it has three edges, all in the
one file the recorder already keeps per session (record.py counters.json):

    call-at         every PostToolUse / PostToolUseFailure (record._record),
                    a subagent's calls included: delegated work in flight is
                    work in flight
    turn-opened-at  every validated UserPromptSubmit, including a session's
                    first turn before any tool call (record.turn_open)
    turn-ended-at   every Stop the stop-guard door answered with rc 0
                    (record.turn_closed), a re-stop after a refusal included:
                    the owed-row rung refuses every idle stop once, so an idle
                    seat's real turn end IS a re-stop

The session is the one the ROSTER carries for the seat now. The hook-latency
stream was the obvious source and is the wrong one: at fleet rate its three
generations hold minutes (hooklatency.py measured 0.15 h), so a seat idle past
ten minutes has no row left in it. `turnstamp` records helm's Stop allow too,
but its only writer sits in hookrun.run_event, which the installed Stop hook
(`chat stop-guard --hook-json`) never passes through, so it has never been
written on this box; and it skips the re-stop an idle owing seat always ends on.

THE STATES. UNKNOWN is never a guessed IDLE:

    IDLE     the last edge is a turn end: the seat's turn ended at `since` and
             nothing has run in its session since. `idle_s` is how long.
    BUSY     the last edge is a call or a turn start after the last turn end;
             with no recorded turn end, a call inside RECENT_S or a newly
             opened call-free turn inside ten minutes. `since` is that edge.
    UNKNOWN  anything this cannot read: the roster, the seat's row, its
             session, the session's hook record, an edge that is not a time.
             Also a session with no recorded turn end whose call is older
             than RECENT_S, or whose call-free turn start is over ten minutes
             old: in flight, or ended unrecorded.
    RESTING  the owner paused the seat (helm/seat_rest.py, task/3280). Read
             FIRST, so a resting seat is never IDLE and never IDLE-OWING; a
             rest record helm cannot read is UNKNOWN, never IDLE.

WHAT IT CANNOT SEE, named. A turn that ends without a Stop helm answered (the
harness died, an API error the harness does not route through Stop, a guard
disabled by HELM_NO_STOP_GUARD) stays BUSY from its last call, and the reading
says how old that call is. A long single tool call has no edge until it
finishes, so it reads BUSY from the call before it. A foreign Stop hook that
vetoes after helm allowed leaves an IDLE reading until the continuation's
first call. Each of those errs toward BUSY or lasts one turn; none invents an
IDLE-OWING, which needs ten quiet minutes after a turn end.

THE MEASUREMENT (item 3). `helm seat idle-dispatch --owing [--hours H]`
answers the two bars over a window from a ledger the idle-dispatch timer
writes: one row per sending pass, each owing seat's reading and whether the
pass re-rang it (record_pass). A pass is the sample, so minutes are a lower
bound by up to one pass interval, and the report prints its coverage.
"""
import json
import math
import os
import re
import sys
import time

from . import home, pk

BUSY, IDLE, UNKNOWN, RESTING = "BUSY", "IDLE", "UNKNOWN", "RESTING"
#: The bar: an owed row on a seat IDLE this long while owing it is IDLE-OWING.
IDLE_OWING_S = 10 * 60
#: A session that has never recorded a turn end reads BUSY only while its
#: last call is this young; older is UNKNOWN. autocompact.WORKING_S's number.
RECENT_S = 5 * 60
#: A BUSY reading whose last edge is older than this says so in its reason:
#: the longest whole-suite gate measured is ~34 minutes.
STALE_BUSY_S = 60 * 60
#: How far in the future an edge may sit before it is not a time at all.
SKEW_S = 120
SOURCE = ("the seat's own hook record: PostToolUse call, UserPromptSubmit "
          "turn start, stop-guard turn end (record.py counters)")
EDGES = ("call-at", "turn-opened-at", "turn-ended-at")


def _age(seconds):
    s = max(0, int(seconds))
    if s < 60:
        return "%ds" % s
    if s < 3600:
        return "%dm" % (s // 60)
    return "%dh%02dm" % (s // 3600, s % 3600 // 60)


def _clock(epoch):
    return time.strftime("%H:%MZ", time.gmtime(epoch))


def _rest(seat, now):
    """The owner's rest for `seat` (seat_rest.pause), or None.

    THE REST MODULE MAY NOT BE ON THIS TREE (lane 3280), and the two compose
    whichever lands first: absent, nothing is resting; present, its reader
    decides, and this module never reads its file itself."""
    try:
        from . import seat_rest
    except ImportError:
        return None
    return seat_rest.pause(seat, now)


_SID = re.compile(r"\A[A-Za-z0-9_-]{1,128}\Z")


def _sid(session):
    """A session id as a reason may print it: eight characters of a token,
    never a roster field that is not one (the roster is written unvalidated)."""
    return session[:8] if isinstance(session, str) and _SID.match(session) \
        else "(unprintable)"


def _unknown(seat, why, session=None):
    return {"seat": seat, "state": UNKNOWN, "idle_s": None, "since": None,
            "session": session, "why": why, "source": SOURCE}


def _edge(value, now):
    """A recorded edge as epoch seconds, or None when it is not one."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or value <= 0 or value > now + SKEW_S:
        return None
    return float(value)


def roster_rows():
    """(rows, why) for readings that share one roster read. `why` is set when
    the roster did not read, which makes every seat UNKNOWN, never absent."""
    from .seats_roster import roster_checked
    try:
        rows, failed = roster_checked()
    except Exception as exc:                     # noqa: BLE001
        return None, "the roster could not be read (%s)" % exc.__class__.__name__
    return (None, "the roster could not be read") if failed else (rows, None)


def counters_path(session):
    from . import record
    return os.path.join(record.session_dir(session), "counters.json")


def reading(seat, now=None, rows=None, roster_why=None):
    """{seat, state, idle_s, since, session, why, source} for ONE seat.

    `rows` is a roster read the caller already holds (see `readings`); None
    reads it here. Never raises: every failure is an UNKNOWN with its reason."""
    now = time.time() if now is None else now
    seat = str(seat or "").strip()
    try:
        rest = _rest(seat, now)
    except Exception as exc:                     # noqa: BLE001
        return _unknown(seat, "the owner-rest record could not be asked (%s)"
                        % exc.__class__.__name__)
    if rest:
        if rest.get("state") == RESTING:
            return {"seat": seat, "state": RESTING, "idle_s": None,
                    "since": None, "session": None, "source": SOURCE,
                    "why": rest.get("reason") or RESTING}
        return _unknown(seat, rest.get("reason") or "the owner-rest record "
                        "could not be read")
    if rows is None and roster_why is None:
        rows, roster_why = roster_rows()
    if roster_why:
        return _unknown(seat, roster_why)
    from .seats_common import canonical_seat
    key, err = canonical_seat(seat, rows)
    if err:
        return _unknown(seat, "the roster names this seat ambiguously")
    if key is None:
        return _unknown(seat, "no roster row names this seat")
    session = (rows.get(key) or {}).get("session")
    if not isinstance(session, str) or not _SID.match(session):
        return _unknown(seat, "the roster row carries no current session "
                        "helm can read")
    try:
        c = pk.read_json(counters_path(session), None, strict=True)
    except Exception as exc:                     # noqa: BLE001
        return _unknown(seat, "the hook record for session %s is unreadable "
                        "(%s)" % (_sid(session), exc.__class__.__name__),
                        session)
    if c is None:
        return _unknown(seat, "no hook record for session %s: it has made no "
                        "tool call helm recorded" % _sid(session), session)
    if not isinstance(c, dict):
        return _unknown(seat, "the hook record for session %s is not an "
                        "object" % _sid(session), session)
    call = _edge(c.get("call-at"), now)
    if call is None and "call-at" not in c:
        # A RECORD WRITTEN BEFORE `call-at` EXISTED still dates its last call,
        # to the second, in `ts`.
        call = _edge(pk.parse_ts_epoch(c.get("ts")), now)
    opened = _edge(c.get("turn-opened-at"), now)
    ended = _edge(c.get("turn-ended-at"), now)
    bad = [k for k in EDGES if k in c and _edge(c[k], now) is None]
    if bad:
        return _unknown(seat, "the hook record for session %s carries %s that "
                        "is not a time" % (_sid(session), ", ".join(bad)),
                        session)
    active = max([t for t in (call, opened) if t is not None], default=None)
    if active is None and ended is None:
        return _unknown(seat, "the hook record for session %s dates no call, "
                        "no turn start and no turn end" % _sid(session), session)
    out = {"seat": seat, "session": session, "source": SOURCE,
           "last_call": call, "turn_opened": opened, "turn_ended": ended}
    if ended is not None and (active is None or ended >= active):
        return dict(out, state=IDLE, idle_s=max(0.0, now - ended),
                    since=ended, why="its turn ended %s ago and nothing has "
                    "run in its session since" % _age(now - ended))
    quiet = now - active
    # The prompt hook can open a fresh session before its first call. Give
    # that one turn at most ten minutes of provisional BUSY; a stale call in a
    # later turn does not extend it. After a call, the five-minute recent-call
    # rule applies as before. The beacon proof independently enforces its bar.
    provisional = opened is not None and (call is None or call < opened)
    if provisional and quiet > IDLE_OWING_S:
        return dict(out, state=UNKNOWN, idle_s=None, since=None,
                    why="turn start for session %s was %s ago without a call "
                    "in this turn: helm cannot prove it is still in flight"
                    % (_sid(session), _age(quiet)))
    if ended is None and not provisional and quiet > RECENT_S:
        return dict(out, state=UNKNOWN, idle_s=None, since=None,
                    why="no turn end is recorded for session %s and its last "
                    "call was %s ago: in flight or ended unrecorded, helm "
                    "cannot say" % (_sid(session), _age(quiet)))
    why = "its last call or turn start was %s ago, after its last turn end" \
        % _age(quiet) if ended is not None else \
        "%s %s ago, and no turn end recorded yet" % (
            "a turn started" if provisional else "a call", _age(quiet))
    if quiet > STALE_BUSY_S:
        why += ("; nothing for %s is a single very long call or a turn that "
                "ended without a Stop helm answered" % _age(quiet))
    return dict(out, state=BUSY, idle_s=None, since=active, why=why)


def readings(seats, now=None):
    """{seat: reading} over one roster read."""
    now = time.time() if now is None else now
    rows, why = roster_rows()
    return {s: reading(s, now=now, rows=rows, roster_why=why)
            for s in sorted({str(x or "").strip() for x in seats} - {""})}


def owing_s(r, row_age_s):
    """Seconds the seat has been IDLE while owing a row this old, or None
    when it is not IDLE. The stretch starts at the later of its turn end and
    the row's arrival."""
    if not r or r.get("state") != IDLE or r.get("idle_s") is None:
        return None
    return max(0.0, min(float(r["idle_s"]), float(row_age_s or 0)))


def is_owing(r, row_age_s):
    """IDLE-OWING: idle at least IDLE_OWING_S while owing this row."""
    s = owing_s(r, row_age_s)
    return s is not None and s >= IDLE_OWING_S


def phrase(r):
    """One line: what the reading says and why."""
    if not r:
        return "idle UNKNOWN (not read)"
    state = r.get("state")
    if state == IDLE:
        return "IDLE %s (turn ended %s, nothing run since)" % (
            _age(r["idle_s"]), _clock(r["since"]))
    if state == BUSY:
        return "BUSY (%s)" % r.get("why")
    if state == RESTING:
        return r.get("why") or RESTING
    return "idle UNKNOWN (%s)" % (r.get("why") or "not read")


def list_suffix(r, row_age_s, late):
    """The marker `helm dispatch list` puts on an owed row, or "".

    IDLE-OWING on any owed row whose recipient has sat idle ten minutes on
    it; on a late row that is not, the recipient's reading, so an overdue row
    says whether its seat is busy, briefly idle, or unreadable."""
    s = owing_s(r, row_age_s)
    if s is not None and s >= IDLE_OWING_S:
        return "  IDLE-OWING %s (recipient idle since %s)" % (
            _age(s), _clock(r["since"]))
    if not late:
        return ""
    return "  recipient " + phrase(r)


# ---------------------------------------------------------------------------
# the pass ledger and the measurement over a window
# ---------------------------------------------------------------------------

LEDGER_MAX = 1024 * 1024
LEDGER_V = 1
#: The idle-dispatch timer's cadence (helm-idle-dispatch.timer). A gap wider
#: than two of these is reported as a hole in the measurement.
PASS_S = 5 * 60


def ledger_path():
    return os.path.join(home.global_dir(), ".state", "idle-owing.jsonl")


def record_pass(now, seats):
    """Append ONE pass: {seat: {state, since, owing: [[id8, since], ...],
    ring}}. Rotated to `.1` past LEDGER_MAX. -> True when written. Never
    raises: a lost sample is a hole the report counts, never a failed pass."""
    try:
        path = ledger_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        line = json.dumps({"v": LEDGER_V, "ts": now, "seats": seats},
                          sort_keys=True, separators=(",", ":")) + "\n"
        try:
            if os.path.getsize(path) + len(line) > LEDGER_MAX:
                os.replace(path, path + ".1")
        except FileNotFoundError:
            pass
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW,
                     0o600)
        try:
            return os.write(fd, line.encode("utf-8")) == len(line.encode("utf-8"))
        finally:
            os.close(fd)
    except Exception as exc:                     # noqa: BLE001
        print("helm seat idle-dispatch: the idle-owing sample was not written "
              "(%s: %s)" % (exc.__class__.__name__, exc), file=sys.stderr)
        return False


def _passes(start, end):
    """(passes in [start, end] oldest first, unreadable-line count, why) —
    `why` set when neither generation could be opened for a reason other than
    absence."""
    out, bad, why = [], 0, None
    base = ledger_path()
    for path in (base + ".1", base):
        try:
            with pk.open_regular(path, encoding="utf-8") as f:
                for line in f:
                    try:
                        p = json.loads(line)
                    except ValueError:
                        bad += 1
                        continue
                    ts = p.get("ts") if isinstance(p, dict) else None
                    if not isinstance(ts, (int, float)) or \
                            not isinstance(p.get("seats"), dict):
                        bad += 1
                        continue
                    if start <= ts <= end:
                        out.append(p)
        except FileNotFoundError:
            continue
        except OSError as exc:
            why = "%s: %s" % (exc.__class__.__name__, exc)
    out.sort(key=lambda p: p["ts"])
    return out, bad, why


def measure(hours=2.0, now=None):
    """The rubric over a window: per seat, the minutes it sat IDLE while
    owing a row, and whether each such stretch over the bar was re-rung.

    A STRETCH is one idle episode (one turn end) on which the seat owed at
    least one row: it begins at the later of the turn end, the earliest owed
    row's arrival and the window start, and ends at the last pass that saw
    it. A stretch is re-surfaced when a pass inside it sent the seat its ring."""
    now = time.time() if now is None else now
    start = now - hours * 3600
    passes, bad, why = _passes(start, now)
    stamps = [start] + [p["ts"] for p in passes] + [now]
    gap = max(b - a for a, b in zip(stamps, stamps[1:]))
    stretches, owed_seen, states = {}, {}, {}
    for p in passes:
        for seat, e in sorted(p["seats"].items()):
            if not isinstance(e, dict):
                continue
            owing = [o for o in e.get("owing") or ()
                     if isinstance(o, list) and len(o) == 2
                     and isinstance(o[1], (int, float))]
            if not owing:
                continue
            owed_seen[seat] = owed_seen.get(seat, 0) + 1
            tally = states.setdefault(seat, {})
            word = e.get("state") if e.get("state") in (
                BUSY, IDLE, RESTING) else UNKNOWN
            tally[word] = tally.get(word, 0) + 1
            since = e.get("since")
            if e.get("state") != IDLE or not isinstance(since, (int, float)):
                continue
            begin = max(since, min(o[1] for o in owing), start)
            if p["ts"] < begin:
                continue
            s = stretches.setdefault((seat, since), {
                "seat": seat, "since": since, "begin": begin, "end": p["ts"],
                "rows": [], "rings": []})
            s["begin"], s["end"] = min(s["begin"], begin), max(s["end"], p["ts"])
            s["rows"] = sorted(set(s["rows"]) | {str(o[0]) for o in owing})
            if e.get("ring"):
                s["rings"].append([p["ts"], str(e["ring"])])
    seats = {}
    for (seat, _since), s in sorted(stretches.items()):
        s["minutes"] = round((s["end"] - s["begin"]) / 60.0, 1)
        s["over"] = s["end"] - s["begin"] > IDLE_OWING_S
        s["resurfaced"] = any(r == "sent" for _t, r in s["rings"])
        seats.setdefault(seat, []).append(s)
    report = []
    for seat in sorted(set(owed_seen) | set(seats)):
        mine = seats.get(seat, [])
        over = [s for s in mine if s["over"]]
        report.append({"seat": seat, "passes_owing": owed_seen.get(seat, 0),
                       "states": states.get(seat, {}),
                       "idle_owing_min": round(sum(s["minutes"] for s in mine), 1),
                       "stretches": mine, "over": len(over),
                       "over_not_resurfaced": sum(1 for s in over
                                                  if not s["resurfaced"])})
    return {"window_h": hours, "start": start, "end": now,
            "passes": len(passes), "largest_gap_s": gap, "unreadable": bad,
            "ledger_error": why, "bar_s": IDLE_OWING_S, "seats": report,
            "seats_over": sum(1 for r in report if r["over"]),
            # A SEAT UNKNOWN IN A PASS IS UNMEASURED THERE, never "not idle":
            # its harness may run no hook helm records (a codex CLI seat).
            "seats_unknown": sum(1 for r in report
                                 if r["states"].get(UNKNOWN)),
            "over_not_resurfaced": sum(r["over_not_resurfaced"] for r in report)}


def measure_lines(m):
    """The report. Seat names come from dispatch rows, which carry roster
    spellings, so each passes the display launder (`_seat_label`)."""
    from .seats_common import _seat_label
    head = ("idle-owing over %.1fh (%s..%s): %d pass%s, largest gap %s%s. Bar: "
            "no seat IDLE over %dm while owing a row, and each such stretch "
            "re-surfaced. Minutes run to the last pass that saw the stretch, a "
            "lower bound by up to one pass."
            % (m["window_h"], _clock(m["start"]), _clock(m["end"]), m["passes"],
               "" if m["passes"] == 1 else "es", _age(m["largest_gap_s"]),
               " (a HOLE: stretches inside it are unmeasured)"
               if m["largest_gap_s"] > 2 * PASS_S else "", IDLE_OWING_S // 60))
    lines = [head]
    if m.get("ledger_error"):
        lines.append("  ledger UNREADABLE: %s" % m["ledger_error"])
    if m.get("unreadable"):
        lines.append("  %d ledger line(s) did not parse and are not counted"
                     % m["unreadable"])
    if not m["passes"]:
        lines.append("  UNKNOWN: no idle-dispatch pass is recorded in this "
                     "window, so nothing was measured (the timer's sending "
                     "passes write %s)" % ledger_path())
        return lines
    for r in m["seats"]:
        tally = ", ".join("%s %d" % (k, r["states"][k])
                          for k in (BUSY, IDLE, RESTING, UNKNOWN)
                          if r["states"].get(k))
        if not r["stretches"]:
            lines.append("  %-18s    0m IDLE while owing (owed rows in %d "
                         "pass%s: %s)"
                         % (_seat_label(r["seat"]), r["passes_owing"],
                            "" if r["passes_owing"] == 1 else "es", tally))
            continue
        lines.append("  %-18s %4dm IDLE while owing, %d stretch%s over %dm "
                     "(passes owing: %s)"
                     % (_seat_label(r["seat"]), round(r["idle_owing_min"]),
                        r["over"], "" if r["over"] == 1 else "es",
                        IDLE_OWING_S // 60, tally))
        for s in r["stretches"]:
            sent = [t for t, v in s["rings"] if v == "sent"]
            lines.append("      %s-%s %dm rows %s: %s" % (
                _clock(s["begin"]), _clock(s["end"]), round(s["minutes"]),
                ",".join(_seat_label(x) for x in s["rows"]),
                "re-surfaced (rung %s)" % ", ".join(_clock(t) for t in sent)
                if sent else ("NOT re-surfaced (%s)" % (
                    s["rings"][-1][1] if s["rings"] else "no ring attempted")
                    if s["over"] else "under the bar")))
    lines.append("verdict: %d seat%s over the bar; %d over-bar stretch%s not "
                 "re-surfaced; %d seat%s UNKNOWN in some pass (unmeasured "
                 "there, never counted as not idle)"
                 % (m["seats_over"], "" if m["seats_over"] == 1 else "s",
                    m["over_not_resurfaced"],
                    "" if m["over_not_resurfaced"] == 1 else "es",
                    m["seats_unknown"], "" if m["seats_unknown"] == 1 else "s"))
    return lines


OWING_USAGE = ("usage: helm seat idle-dispatch --owing [--hours H] [--json]\n"
               "  per seat, the minutes it sat IDLE while owing a dispatch row "
               "over the last H hours (default 2), and whether each stretch "
               "over %dm was re-surfaced; read from the idle-dispatch timer's "
               "pass ledger" % (IDLE_OWING_S // 60))


def cmd_owing(args):
    args = list(args or ())
    hours = 2.0
    if "--hours" in args:
        i = args.index("--hours")
        try:
            hours = float(args[i + 1])
        except (IndexError, ValueError):
            hours = -1.0
        if not (0 < hours <= 24 * 30):
            print(OWING_USAGE, file=sys.stderr)
            return 2
    m = measure(hours)
    if "--json" in args:
        print(json.dumps(m, sort_keys=True))
    else:
        print("\n".join(measure_lines(m)))
    return 0 if m["passes"] and not m.get("ledger_error") else 1


def with_reading(row, seat, now=None):
    """`row` (a `seat where` record) with this seat's reading under "idle",
    or None for no row. The one door both `seat where` forms go through."""
    return None if row is None else dict(row, idle=reading(seat, now=now))
