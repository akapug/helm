"""The broken-seat door: a seat helm KNOWS is broken takes no new work (task/3546).

THE OWNER'S RULING, 2026-09-28: "if something is broken we should canonically
not keep using it until it's fixed". A broken seat takes no work; its fix is
the work. The dispatch door (`dispatches._validate_recipient_usable`, which
every send, add, rebind and reassign reaches) asks this module first, and a
refusal names the fact, since when, and a seat to use instead.

THREE FACTS ARE READ HERE, and each is owned elsewhere or recorded here once:

  HOLD        an OPERATOR's per-seat hold: reason, since, who set it, and
              what lifts it. Recorded by `helm seat hold` in
              <HELM_HOME>/_global/seat-holds.jsonl, append-only: setting,
              clearing and every `--force` past it are events, and a cleared
              hold stays in the log with its clearing beside it.
  DROP-STORM  the silent-drop watchdog's latch (`silent_drop.storm`): a run of
              drops close together on one seat that it did not recover from.
  TURN-WALL   the seat's own last turn ended on a provider billing or
              credential refusal (`turnwall.seat_wall`, task/3587). It lifts
              on the seat's next successful turn, or ages out.

The fourth broken fact, a family WALLED on money, is the budget rung's
(`dispatches._validate_recipient_budget`), which already refuses at this door;
it is read here only so `--force` past it is recorded like the other three.

AN OPERATOR HOLD LIFTS ONLY ON ITS OWN TERMS: `--clear`, or the `--until` it
was set with, either a task whose row closes as landed (a close reason
starting "landed", which the task sweep's confirm-close and quiet-window
close write; auto-land closes no task since task/3643, so the task's owner
closes it after re-reading the whole ask) or a deadline the operator wrote. It NEVER lifts on the seat's activity or on a
healthy reading: an operator holds a seat for a defect helm does not measure,
and a seat still working its old rows reads healthy on every rung helm has
while that defect is still there. A MEASURED refusal lifts on its own signal:
a storm ends on HEALTHY_TURNS clean turns after its last drop, ages out once
no drop has been seen for `silent_drop.storm_age_s()`, and `--clear`
acknowledges it (the clear event's time is read as the storm's floor).

UNKNOWN WARNS, NEVER REFUSES. A hold log or latch helm cannot read admits the
row and SAYS so on it; only a readable fact refuses. So does a storm whose
drops the seat's transcript cannot show answered or not: no transcript, or a
tail that begins after them (a relaunched seat answered them in its old
session). A readable hold whose lift condition cannot be read (the task
ledger will not parse) stands, and its refusal says the lift is unverified.
A hold event or latch of the wrong shape is never a traceback: the door
admits past it and the listing says the latch is UNKNOWN.

`--force --reason R` at the dispatch CLI admits the repair work itself, and
the admission is recorded here (`force_leg`, `record_forced`).
"""
import json
import os
import re
import sys
import time

from . import eventledger, home, pk

LEDGER = "seat-holds.jsonl"
REASON_CAP = 300
FORCED_SHOWN = 20         # the listing's forced admissions past no current hold
_USAGE = ("usage: helm seat hold [--json] | hold <seat> --reason R "
          "[--until task/N|TIME] | hold <seat> --clear --reason R\n"
          "  TIME is an ISO UTC time (2026-10-01T12:00Z) or Nm, Nh, Nd from "
          "now")
_SPAN = re.compile(r"^(\d{1,6})([mhd])$")
_SPAN_S = {"m": 60, "h": 3600, "d": 86400}


def ledger_path():
    return os.path.join(home.global_dir(), LEDGER)


def _writer():
    """The DECLARED seat name of this process, or UNKNOWN."""
    try:
        return home.chat_name() or "UNKNOWN"
    except Exception:                       # noqa: BLE001 — a hostile name is no name
        return "UNKNOWN"


def _text(value, cap=REASON_CAP):
    return pk.launder(" ".join(str(value or "").split()))[:cap]


def _label(name):
    """A seat name for display: a hostile roster key loses its control bytes."""
    return _text(name, 64)


def _canonical(token):
    from . import seats
    canon, err = seats._canonical_recipient(token)
    return (str(canon), None) if not err else (None, err)


def _append(event):
    """(event, None) once it is durable in the hold log, else (None, why).
    Every event carries its own id, which the ledger's row grammar needs."""
    event = dict({"id": os.urandom(8).hex()}, **event)
    path = ledger_path()
    with eventledger.locked(path) as held:
        ok, why = eventledger.append_unlocked_checked(path, event) if held \
            else (False, "its lock could not be taken")
    return (event, None) if ok else (
        None, "the hold log %s was not written (%s)" % (path, why))


def _until(token, now=None):
    """(task id, deadline, error) for one `--until`: a task that EXISTS and is
    open, or a time in the future. A task the ledger does not hold would
    never land, so the hold would never lift, and saying "lifts when it
    lands" forever is a promise nothing keeps."""
    from . import tasks
    tid = tasks.normalize_id(token)
    if tid:
        try:
            rows, unread = tasks.snapshot(strict=True)
        except Exception as exc:            # noqa: BLE001 — a reason, never a verdict
            rows, unread = {}, "%s: %s" % (exc.__class__.__name__, exc)
        if unread:
            return None, None, (
                "--until %s cannot be checked: the task ledger cannot be read "
                "(%s). Hold with a time --until, or none and clear it by hand"
                % (tid, unread))
        row = rows.get(tid)
        if not isinstance(row, dict):
            return None, None, ("--until %s names no task in the ledger, so "
                                "nothing would ever lift the hold" % tid)
        if row.get("status") == "closed":
            return None, None, ("--until %s is already closed (%s), so its "
                                "landing cannot lift a new hold"
                                % (tid, _text(row.get("closed_reason"), 120)))
        return tid, None, None
    now = time.time() if now is None else now
    span = _SPAN.match(str(token).strip())
    at = now + int(span.group(1)) * _SPAN_S[span.group(2)] if span \
        else pk.parse_ts_epoch(str(token))
    if at is None:
        return None, None, ("--until %r is neither a task id (task/N) nor a "
                            "time (an ISO UTC time, or Nm, Nh, Nd from now)"
                            % token)
    if at <= now:
        return None, None, "--until %s is already past" % pk.epoch_ts(at)
    return None, pk.epoch_ts(at), None


def hold(seat, reason, until=None, by=None):
    """Record an operator HOLD on `seat` -> (event, error)."""
    seat, err = _canonical(seat)
    if err:
        return None, err
    reason = _text(reason)
    if not reason:
        return None, "a hold needs --reason: a seat held for no stated reason " \
                     "cannot be argued with or lifted by its fix"
    tid = deadline = None
    if until is not None:
        tid, deadline, err = _until(until)
        if err:
            return None, err
    return _append({"ev": "hold", "seat": seat, "reason": reason,
                    "until": tid, "deadline": deadline, "by": by or _writer(),
                    "at": pk.now_ts()})


def clear(seat, reason, by=None):
    """Clear the HOLD on `seat`, and acknowledge a drop storm on it ->
    (event, error). The hold stays in the log; the clear's time is the floor
    below which `silent_drop.storm` reads no drop of this seat."""
    seat, err = _canonical(seat)
    if err:
        return None, err
    reason = _text(reason)
    if not reason:
        return None, "clearing a hold or a drop storm needs --reason"
    current, cleared, unread = state()
    if unread:
        return None, "the hold log cannot be read (%s)" % unread
    facts = ["HOLD"] if seat in current else []
    rd, storm_unread = _silent_drop().storm(seat, after=cleared.get(seat))
    if rd and not rd["healthy"]:
        facts.append("DROP-STORM")
    if not facts:
        return None, ("%s is neither held nor in a drop storm — nothing to "
                      "clear%s" % (seat, " (the drop-storm reading is UNKNOWN: "
                                   "%s)" % storm_unread if storm_unread else ""))
    return _append({"ev": "clear", "seat": seat, "reason": reason,
                    "cleared": facts, "by": by or _writer(),
                    "at": pk.now_ts()})


def record_forced(seat, row_id, facts, reason, by=None):
    """Record that `--force` admitted row `row_id` past `facts` -> (event, error)."""
    return _append({"ev": "forced", "seat": str(seat), "row": row_id,
                    "facts": [f["fact"] for f in facts],
                    "reason": _text(reason), "by": by or _writer(),
                    "at": pk.now_ts()})


def _events():
    return eventledger.checked_events(ledger_path(), strict=True)


def _fold(events):
    """({seat: its current hold event}, {seat: epoch of its latest clear}).
    A seat's last hold stands until a later clear."""
    current, cleared = {}, {}
    for ev in events:
        if not isinstance(ev, dict) or not isinstance(ev.get("seat"), str) \
                or not ev["seat"]:
            continue
        if ev.get("ev") == "hold":
            current[ev["seat"]] = ev
        elif ev.get("ev") == "clear":
            current.pop(ev["seat"], None)
            at = pk.parse_ts_epoch(ev.get("at"))
            if at is not None:
                cleared[ev["seat"]] = at
    return current, cleared


def state():
    """(holds, clears, unreadable-why) from the log, read once."""
    events, unread = _events()
    if unread:
        return {}, {}, unread
    current, cleared = _fold(events)
    return current, cleared, None


def holds(events=None):
    """({seat: its current hold event}, unreadable-why)."""
    unread = None
    if events is None:
        events, unread = _events()
    if unread:
        return {}, unread
    return _fold(events)[0], None


def lifted(h, now=None):
    """(why it lifted, or None; a note on a lift that could not be read).

    Only the hold's own terms lift it: its deadline, or its task landing."""
    deadline = h.get("deadline")
    if deadline:
        at = pk.parse_ts_epoch(deadline)
        if at is not None and (time.time() if now is None else now) >= at:
            return "its --until %s passed" % deadline, None
        return None, None
    until = h.get("until")
    if until:
        from . import tasks
        try:
            rows, unread = tasks.snapshot(strict=True)
        except Exception as exc:            # noqa: BLE001 — a reason, never a verdict
            rows, unread = {}, "%s: %s" % (exc.__class__.__name__, exc)
        if unread:
            return None, ("whether %s landed is UNKNOWN (the task ledger "
                          "cannot be read: %s)" % (until, unread))
        task = rows.get(until) if isinstance(until, str) else None
        if not isinstance(task, dict):
            return None, ("%s is not in the task ledger, so nothing lifts "
                          "the hold: clear it by hand" % until)
        reason = str(task.get("closed_reason") or "")
        if task.get("status") == "closed" \
                and reason.strip().lower().startswith("landed"):
            return "%s landed (%s)" % (until, _text(reason, 120)), None
        if task.get("status") == "closed":
            return None, ("%s closed WITHOUT landing (%s), so it lifts "
                          "nothing: clear the hold by hand"
                          % (until, _text(reason, 120) or "no reason"))
    return None, None


def _clear_hint(seat):
    return "`helm seat hold %s --clear --reason R`" % _label(seat)


def _hold_text(h, note):
    hint = _clear_hint(h["seat"])
    lift = ("lifts when %s lands, or by %s" % (_text(h["until"], 64), hint)
            if h.get("until") else "lifts at %s, or by %s" % (h["deadline"], hint)
            if h.get("deadline") else
            "lifts only by %s, never on the seat's own activity" % hint)
    return ("HOLD since %s, set by %s: %s — %s%s"
            % (h.get("at"), _text(h.get("by"), 64), _text(h.get("reason")),
               lift, "; " + note if note else ""))


def _storm_text(rd):
    sd = _silent_drop()
    return ("DROP-STORM since %s: %d silent drops it did not recover from, "
            "the last at %s, and %d of %d clean turns since — it returns on "
            "%d clean turns, ages out %s after its last drop, or by %s"
            % (rd["since"], rd["drops"], rd["last"], rd["clean_turns"],
               sd.HEALTHY_TURNS, sd.HEALTHY_TURNS, _span(sd.storm_age_s()),
               _clear_hint(rd["seat"])))


def _span(seconds):
    return "%dh" % (seconds // 3600) if seconds % 3600 == 0 \
        else "%ds" % seconds


def _silent_drop():
    from . import silent_drop
    return silent_drop


def reading(seat, walled=False, current=None, cleared=None, latch=None):
    """(facts, notes) — the broken facts that refuse `seat` new work, and the
    notes an admission carries (an UNKNOWN reading, a lifted hold).

    A fact is {"fact", "since", "text"}. `walled` adds the budget rung's
    money wall, for the force leg only (the door's budget rung refuses it).
    `current`, `cleared` and `latch` are the caller's reads when it reads
    many seats."""
    seat = str(seat)
    facts, notes = [], []
    if current is None:
        current, cleared, unread = state()
        if unread:
            notes.append("seat hold UNKNOWN — %s could not be read (%s); "
                         "admitted unverified" % (ledger_path(), unread))
    h = current.get(seat)
    if h:
        why, note = lifted(h)
        if why:
            notes.append("the hold on %s has lifted: %s" % (_label(seat), why))
        else:
            facts.append({"fact": "HOLD", "since": h.get("at"),
                          "text": _hold_text(h, note)})
    rd, unread = _silent_drop().storm(seat, latch=latch,
                                      after=(cleared or {}).get(seat))
    if unread:
        notes.append("drop-storm reading UNKNOWN — %s; admitted unverified"
                     % unread)
    elif rd and not rd["healthy"]:
        facts.append({"fact": "DROP-STORM", "since": rd["since"],
                      "text": _storm_text(rd)})
    wall = _turnwall().seat_wall(seat)
    if wall:
        facts.append({"fact": "TURN-WALL", "since": pk.epoch_ts(wall["at"]),
                      "text": _turnwall().text(wall)})
    if walled:
        from . import dispatches
        ok, refusal, _note = dispatches._validate_recipient_budget(seat, False)
        if not ok:
            facts.append({"fact": "WALLED", "since": None,
                          "text": refusal.split(" Wait for the reset")[0]})
    return facts, notes


def usability_rows():
    """{seat: usability row} for the whole roster, from the join the dispatch
    door refuses on. The one roster read in this module; raises when the join
    does."""
    from . import seat_usability, seats
    return seat_usability.join(seats=sorted(seats.roster()),
                               need_holding=False)


def live_seats(seat, family=None, fleet=None, fresh=False):
    """[(seat, family)] a seat helm may hand `seat`'s work instead, best
    first: measured USABLE by the usability join, not held, not storming, no
    wall on its own last turn, and a family walled on neither MONEY nor
    REACH — the recipient's own family first. Raises on an unreadable join;
    `instead` and the dark-seat mover each say that in their own words.
    `fresh` FAILS CLOSED on the burn read: a seat whose family has no fresh
    flag is not a place to MOVE work (the mover asks it; the door's prose
    `instead` does not)."""
    from . import burnflags, seat_usability
    rows = fleet if fleet is not None else usability_rows()
    family = family or ((rows or {}).get(str(seat)) or {}).get("family")
    current, cleared, _unread = state()
    latch = _latch()
    picks = []
    for name, row in sorted((rows or {}).items()):
        name = str(name)
        if name.casefold() == str(seat).casefold() \
                or (row or {}).get("verdict") != seat_usability.USABLE:
            continue
        # A HOLD THAT LIFTED IS NO HOLD: its seat is a candidate again.
        if name in current and not lifted(current[name])[0]:
            continue
        fam = row.get("family")
        flag = burnflags.family_flag(fam) if fam else None
        if fresh and not flag:
            continue
        axes = (flag or {}).get("axes") or {}
        if burnflags.RED in (axes.get("money"), axes.get("reach")):
            continue
        rd, unread = _silent_drop().storm(name, latch=latch,
                                          after=cleared.get(name))
        if unread or (rd and not rd["healthy"]):
            continue
        if _turnwall().seat_wall(name):
            continue
        picks.append((0 if family and fam == family else 1, name, fam))
    return [(name, fam) for _same, name, fam in sorted(picks)]


def instead(seat, family=None, fleet=None):
    """One sentence naming a seat to use instead of `seat`: the first of
    `live_seats`. Never raises."""
    try:
        picks = live_seats(seat, family=family, fleet=fleet)
        if picks:
            # THE ROSTER KEY IS LAUNDERED AT THE ONE PLACE IT IS RENDERED.
            name, fam = picks[0]
            return ("Use @%s instead (%s, measured USABLE now)."
                    % (_label(name), _text(fam, 64) or "family unknown"))
        return ("No other seat measures USABLE right now; `helm route "
                "build|review` weighs the fleet.")
    except Exception as exc:                # noqa: BLE001 — prose only
        return ("No alternative could be measured (%s); `helm route "
                "build|review` weighs the fleet." % exc.__class__.__name__)


def _turnwall():
    from . import turnwall
    return turnwall


def _latch():
    """The silent-drop latch read once, or None to let each reading say why
    (it cannot be read, or is not a JSON object)."""
    try:
        latch = pk.read_json(_silent_drop()._state_path(), {}, strict=True) \
            or {}
    except Exception:                       # noqa: BLE001 — per-seat read says why
        return None
    return latch if isinstance(latch, dict) else None


def broken_seats(names):
    """{seat: [fact words]} for each named seat helm knows is broken (a hold
    in force, a drop storm, or a wall on its own last turn). The router's N4 drops them, so it never
    recommends a seat the dispatch door refuses. An unreadable fact is no
    fact here, as at the door; never raises."""
    try:
        current, cleared, _unread = state()
        latch = _latch()
        out = {}
        for name in names:
            facts, _notes = reading(name, current=current, cleared=cleared,
                                    latch=latch)
            if facts:
                out[name] = [f["fact"] for f in facts]
        return out
    except Exception:                       # noqa: BLE001 — a router never stops here
        return {}


def refusal_text(seat, facts, family=None):
    return ("recipient %r is BROKEN and takes no new work: %s. Its fix is the "
            "work: `--force --reason R` admits the repair work itself, and "
            "the admission is recorded (`helm seat hold` lists it). %s"
            % (str(seat), "; ".join(f["text"] for f in facts),
               instead(seat, family)))


def rung(seat, force, family=None):
    """(ok, refusal, note) — the dispatch door's broken-seat rung. `force`
    proceeds unconditionally, like its siblings; the CLI records it."""
    if force:
        return True, None, None
    try:
        facts, notes = reading(seat)
    except Exception as exc:                # noqa: BLE001 — never lose a dispatch
        return True, None, ("broken-seat reading UNKNOWN — the check itself "
                            "failed (%s: %s); admitted unverified"
                            % (exc.__class__.__name__, exc))
    if facts:
        return False, refusal_text(seat, facts, family), None
    return True, None, "; ".join(notes) or None


def force_leg(seat, force, reason):
    """(facts, refusal) for the dispatch CLI. `--force` past a broken fact
    needs `--reason R`, the repair work it admits; the facts it passed are
    returned so the caller records them once the row exists."""
    reason = (reason or "").strip()
    if not force:
        return [], ("--reason rides --force: it names the repair work a "
                    "forced dispatch admits" if reason else None)
    try:
        facts, _notes = reading(seat, walled=True)
    except Exception:                       # noqa: BLE001 — an unread fact refuses nothing
        return [], None
    if facts and not reason:
        return facts, ("--force past %s on %s needs --reason R naming the "
                       "repair work it admits: %s"
                       % ("+".join(f["fact"] for f in facts), seat,
                          "; ".join(f["text"] for f in facts)))
    return facts, None


def _flag(args, name):
    if name not in args:
        return None, args, None
    at = args.index(name)
    if at + 1 >= len(args) or args[at + 1].startswith("--"):
        return None, args, "%s needs a value" % name
    return args[at + 1], args[:at] + args[at + 2:], None


def cmd_hold(argv):
    """`helm seat hold` — list, set or clear an operator HOLD on a seat."""
    args = list(argv or ())
    if "-h" in args or "--help" in args:
        print(_USAGE)
        return 0
    as_json = "--json" in args
    clearing = "--clear" in args
    args = [a for a in args if a not in ("--json", "--clear")]
    reason, args, err = _flag(args, "--reason")
    until, args, err2 = (None, args, None) if err else _flag(args, "--until")
    err = err or err2
    if err or len(args) > 1 or any(a.startswith("--") for a in args) \
            or (clearing and until):
        print("helm seat hold: %s\n%s" % (err or "unexpected arguments",
                                          _USAGE), file=sys.stderr)
        return 2
    if not args:
        if reason or until or clearing:
            print("helm seat hold: name the seat\n" + _USAGE, file=sys.stderr)
            return 2
        return _list(as_json)
    event, err = (clear(args[0], reason) if clearing
                  else hold(args[0], reason, until))
    if err:
        print("helm seat hold: " + err, file=sys.stderr)
        return 2
    if as_json:
        print(json.dumps(event, sort_keys=True))
    elif clearing:
        print("helm seat hold: %s cleared (%s: %s); the clearing stays in %s"
              % (event["seat"], "+".join(event["cleared"]), event["reason"],
                 ledger_path()))
    else:
        print("helm seat hold: %s HELD — the dispatch door refuses it new "
              "work until %s" % (event["seat"], "%s lands" % event["until"]
                                 if event["until"] else
                                 "%s" % event["deadline"]
                                 if event["deadline"] else
                                 "`helm seat hold %s --clear --reason R`"
                                 % event["seat"]))
        for line in _owed_lines(event["seat"]):
            print(line)
    return 0


def _owed_lines(seat):
    """What the held seat already owes, and the verbs that move it: the door
    refuses it NEW work, and its open rows still wait on it."""
    try:
        from . import dispatches, seats
        snap, unavailable = dispatches.snapshot()
        if unavailable:
            return ["  its dispatch rows are UNKNOWN (%s): `helm dispatch "
                    "list --to %s` once the ledger reads" % (unavailable, seat)]
        owed = [(rid, r) for rid, r in sorted((snap or {}).items())
                if isinstance(r, dict)
                and r.get("status") in ("open", "held")
                and seats.recipient_matches(r.get("recipient"), seat)]
    except Exception as exc:                # noqa: BLE001 — the hold is recorded
        return ["  its dispatch rows could not be read (%s: %s)"
                % (exc.__class__.__name__, exc)]
    if not owed:
        return ["  it owes no open or held dispatch row"]
    lines = ["  it still owes %d dispatch row(s), which nobody else will "
             "work until they move:" % len(owed)]
    for rid, r in owed:
        lines.append("    %s %s %s %s" % (rid, r.get("status"),
                                          _text(r.get("kind"), 16) or "-",
                                          _text(r.get("lane"), 80)))
    lines.append("  move them: `helm seat reassign %s --to SEAT --apply` "
                 "(every holding), or `helm dispatch rebind ID --to SEAT` "
                 "(one row). %s" % (seat, instead(seat)))
    return lines


def _storms(latch, cleared):
    """[reading] for every seat the latch records in a drop storm now."""
    out = []
    for name in sorted(latch or {}):
        rd, _unread = _silent_drop().storm(name, latch=latch,
                                           after=cleared.get(name))
        if rd and not rd["healthy"]:
            out.append(rd)
    return out


def _walled():
    """[(family, cause)] for each family whose money axis reads RED now."""
    try:
        from . import burnflags
        flags, _age = burnflags.cached_flags()
    except Exception:                       # noqa: BLE001 — the door says it per row
        return []
    return [(fam, (flag or {}).get("cause") or "no cause recorded")
            for fam, flag in sorted((flags or {}).items())
            if ((flag or {}).get("axes") or {}).get("money") == burnflags.RED]


def _list(as_json):
    """Every seat the door refuses now and why: the operator holds, the drop
    storms, the walled families, and the forced admissions past them."""
    events, unread = _events()
    if unread:
        print("helm seat hold: the hold log cannot be read (%s)" % unread,
              file=sys.stderr)
        return 1
    current, cleared = _fold(events)
    forced = [e for e in events if isinstance(e, dict)
              and e.get("ev") == "forced"]
    out, shown = [], set()
    for seat in sorted(current):
        h = current[seat]
        why, note = lifted(h)
        mine = [e for e in forced if e.get("seat") == seat
                and str(e.get("at")) >= str(h.get("at"))]
        shown.update(id(e) for e in mine)
        out.append(dict(h, lifted=why, note=note, forced=mine))
    latch = _latch()
    storms = _storms(latch, cleared) if latch is not None else []
    walled = _walled()
    rest = [e for e in forced if id(e) not in shown]
    if as_json:
        print(json.dumps({"holds": out, "storms": storms,
                          "walled": [{"family": f, "cause": c}
                                     for f, c in walled],
                          "forced": rest[-FORCED_SHOWN:],
                          "storms_unread": latch is None},
                         indent=1, sort_keys=True))
        return 1 if latch is None else 0
    if not (out or storms or walled or rest):
        print("no seat is held, in a drop storm or walled")
    for h in out:
        print("%s %s" % (_label(h["seat"]), "LIFTED — " + h["lifted"]
                         if h["lifted"] else _hold_text(h, h["note"])))
        for e in h["forced"]:
            print("  " + _forced_line(e))
    for rd in storms:
        print("%s %s" % (_label(rd["seat"]), _storm_text(rd)))
    for fam, cause in walled:
        print("family %s WALLED on MONEY — %s (every seat of it is refused; "
              "`helm burn` reads why)" % (_text(fam, 64), _text(cause, 200)))
    if rest:
        print("forced past a broken fact with no hold in force (the newest "
              "%d of %d):" % (min(len(rest), FORCED_SHOWN), len(rest)))
        for e in rest[-FORCED_SHOWN:]:
            print("  %s: %s" % (_label(e.get("seat")), _forced_line(e)))
    if latch is None:
        print("helm seat hold: drop storms UNKNOWN — the silent-drop latch "
              "cannot be read or is not a JSON object", file=sys.stderr)
        return 1
    return 0


def _forced_line(e):
    return ("forced %s past %s by %s at %s: %s"
            % (_text(e.get("row"), 64), "+".join(e.get("facts") or ()),
               _text(e.get("by"), 64), e.get("at"), _text(e.get("reason"))))
