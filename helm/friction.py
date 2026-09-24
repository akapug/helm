#!/usr/bin/env python3
"""The friction ledger: every guard refusal, counted.

A hook call is timed whether it allows or refuses, and a refusal leaves no
other trace: nothing can say which guard refuses which seat how often. A guard
that is repeatedly WRONG therefore costs the seats that meet it and is never
repaired, because the cost is invisible to everyone who could repair it. This
module is the count.

ONE JSON LINE PER REFUSAL: `id`, `ts`, `guard`, `seat`, `session`, `reason`.
`id` is random and exists because the shared ledger grammar admits only rows
that carry one; every other field is a short TOKEN checked against one
pattern, or null. The raw command,
the message body and the guard's diagnostic are NEVER written: a diagnostic is
built from raw input and raw input can carry a secret. A value that is not a
token is dropped to null, never truncated into one.

THE WRITE IS FAIL-OPEN AND THE REFUSAL DOES NOT DEPEND ON IT. `record` never
raises and returns False for every failure; a caller ignores the result. The
lock wait is bounded by LOCK_WAIT_S. Filesystem latency itself is not bounded
here, which is the same limit the hook-latency stream states for its own append.

THE APPEND AND THE READ ARE `eventledger`'s: its locked one-line append, and
its checked reader that keeps an ABSENT ledger (known empty) apart from an
UNREADABLE one (unknown). This module adds the row shape, one rotation
generation, and the counting. An unreadable ledger is reported as UNREADABLE
and never as zero refusals.

THE OWNER'S DIAL IS READ AND WRITTEN HERE: how many refusals by one guard in
one day a seat meets before the reflex layer tells it. It is an authored
value, stored with who set it and when in the authored layer's host block,
with the reflex layer's constant as its default. `dial()` is the one read and
`set_dial()` the one write, for the verb and the console card alike.
"""
import calendar
import json
import os
import re
import sys
import time

from . import home

#: One generation is kept beside the live file as `<ledger>.1`.
MAX_BYTES = 512 * 1024
#: How long a refusal path may wait for the ledger lock before it gives up.
LOCK_WAIT_S = 0.05
#: The report's default window.
WINDOW_DAYS = 7
#: The window the reflex counter reads.
STREAK_WINDOW_S = 24 * 3600
#: The counter this module plants for the reflex layer, and the companion key
#: carrying the guard the count belongs to.
STREAK_COUNTER = "refusal-streak"
STREAK_SUBJECT = STREAK_COUNTER + "-subject"
#: THE OWNER'S DIAL: the key it is stored under in the authored layer's host
#: block (`registry.authored_host`), and the values it admits. The floor keeps
#: a single refusal from ever being a streak; the ceiling keeps the dial from
#: being a way to switch the reflex off without saying so.
DIAL_KEY = "friction_dial"
DIAL_MIN, DIAL_MAX = 2, 50
DIAL_REFUSAL = ("The number must be a whole number from %d to %d."
                % (DIAL_MIN, DIAL_MAX))

_TOKEN = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._:-]{0,63}\Z")
_TS = "%Y-%m-%dT%H:%M:%SZ"


def path():
    return os.path.join(home.helm_home(), "_global", ".state", "friction.jsonl")


def _token(value):
    return value if isinstance(value, str) and _TOKEN.fullmatch(value) else None


def _seat(session=None):
    """The seat a refusal belongs to: its DECLARED name, else the one seat the
    roster binds its session to. A pane need not carry its name in the
    environment to be a seat, and a ledger that asked only the environment
    would count those panes' refusals against nobody, so their streak could
    never fire. A session two rows remember names nobody: a guess here would
    hand one seat another's streak."""
    try:
        name = _token(home.chat_name())
        if name:
            return name
        session = session or home.session_id()
        if not session:
            return None
        from . import seats_common, seats_roster
        bound = seats_roster.seats_for_session(session)
        if len(bound) != 1:
            return None
        return _token(seats_common._seat_label(bound[0]))
    except Exception:
        return None


def _rotate(dest):
    try:
        if os.path.getsize(dest) > MAX_BYTES:
            os.replace(dest, dest + ".1")
    except OSError:
        pass


def _record(guard, reason, session, now):
    guard = _token(guard)
    if not guard:
        return False
    from . import eventledger
    row = {"id": os.urandom(8).hex(),
           "ts": time.strftime(_TS, time.gmtime(now)), "guard": guard,
           "seat": _seat(session), "session": _token(session),
           "reason": _token(reason)}
    dest = path()
    with eventledger.locked(dest, timeout=LOCK_WAIT_S) as held:
        if not held:
            return False
        _rotate(dest)
        return eventledger.append_unlocked(dest, row)


def record(guard, reason=None, session=None, now=None):
    """Count one refusal by `guard`. True only for a completed append.

    NEVER RAISES. The caller is a refusal path, and a bookkeeping write must
    not turn a refusal into a crash or an allow."""
    try:
        return _record(guard, reason, session,
                       time.time() if now is None else now)
    except Exception:
        return False


def record_gate(guard, payload):
    """Count a gate hook's refusal from the hook payload it refused.

    Only three facts leave the payload: the session id, the event name and the
    tool name, each as a token. The tool INPUT is never read."""
    try:
        d = json.loads(payload or "")
        d = d if isinstance(d, dict) else {}
    except Exception:
        d = {}
    event, tool = _token(d.get("hook_event_name")), _token(d.get("tool_name"))
    return record(guard, reason=":".join(t for t in (event, tool) if t) or None,
                  session=d.get("session_id"))


def _epoch(ts):
    try:
        return calendar.timegm(time.strptime(ts, _TS))
    except (TypeError, ValueError):
        return None


def read():
    """(rows, unreadable). Both generations, oldest first.

    `unreadable` is None when every generation is either absent or read; else
    the reader's reason, and `rows` then says nothing about how many refusals
    happened."""
    from . import eventledger
    out = []
    for p in (path() + ".1", path()):
        rows, unavailable = eventledger.checked_events(p)
        if unavailable:
            return [], unavailable
        out.extend(rows)
    return out, None


def _counted(rows, since, until):
    """The rows that are refusals inside [since, until]: (guard, seat) pairs,
    and how many rows were not countable."""
    pairs, invalid = [], 0
    for r in rows:
        guard, at = _token(r.get("guard")), _epoch(r.get("ts"))
        if not guard or at is None:
            invalid += 1
        elif since <= at <= until:
            pairs.append((guard, _token(r.get("seat")) or "(no seat)"))
    return pairs, invalid


def report(days=WINDOW_DAYS, now=None):
    """Refusals per guard over the last `days`, each with its per-seat split.

    `unreadable` set means the ledger could not be read: `guards` and `total`
    are then None, never an empty census."""
    now = time.time() if now is None else now
    rows, unreadable = read()
    out = {"days": days, "ledger": path(), "unreadable": unreadable,
           "total": None, "guards": None, "invalid": None}
    if unreadable:
        return out
    pairs, invalid = _counted(rows, now - days * 86400, now)
    guards = {}
    for guard, seat in pairs:
        seats = guards.setdefault(guard, {})
        seats[seat] = seats.get(seat, 0) + 1
    out.update(total=len(pairs), invalid=invalid, guards=[
        {"guard": g, "refusals": sum(s.values()),
         "seats": dict(sorted(s.items(), key=lambda kv: (-kv[1], kv[0])))}
        for g, s in sorted(guards.items(),
                           key=lambda kv: (-sum(kv[1].values()), kv[0]))])
    return out


def streak(seat=None, now=None, session=None):
    """(guard, count): the most refusals ONE guard handed `seat` inside
    STREAK_WINDOW_S. None when the seat is unknown, the ledger is unreadable,
    or no guard refused it -- the three are all "no counter to plant"."""
    seat = seat or _seat(session)
    if not seat:
        return None
    now = time.time() if now is None else now
    rows, unreadable = read()
    if unreadable:
        return None
    counts = {}
    for guard, who in _counted(rows, now - STREAK_WINDOW_S, now)[0]:
        if who == seat:
            counts[guard] = counts.get(guard, 0) + 1
    if not counts:
        return None
    return min(counts.items(), key=lambda kv: (-kv[1], kv[0]))


def counters(seat=None, now=None, session=None):
    """The reflex-layer counters this ledger feeds, {} when there are none.
    NEVER RAISES: it runs inside every injected turn."""
    try:
        worst = streak(seat, now, session)
    except Exception:
        return {}
    return {STREAK_COUNTER: worst[1], STREAK_SUBJECT: worst[0]} if worst else {}


def _dial_value(value):
    """`value` when it is a whole number the dial admits, else None. A bool is
    an int to Python and is not a number anybody typed."""
    ok = (isinstance(value, int) and not isinstance(value, bool)
          and DIAL_MIN <= value <= DIAL_MAX)
    return value if ok else None


def dial():
    """THE ONE READ of the owner's dial -> {value, default, min, max, authored,
    by, ts, problem}. `value` is how many refusals by one guard in a day a seat
    meets before it is told; it is the owner's number when he has set one and
    the reflex layer's default otherwise.

    NEVER RAISES, AND A STORED VALUE THAT CANNOT BE USED IS NEVER GUESSED AT.
    An unreadable settings file, a block of the wrong shape, a number outside
    the bounds: each answers the DEFAULT with `problem` set to a sentence that
    says so. Zero would tell every seat on its first refusal and a huge number
    would tell nobody, and either would look like something the owner chose.
    The sentences are written for the owner, because his card prints them."""
    from . import reflex
    default = reflex.REFUSAL_STREAK_THRESHOLD
    out = {"value": default, "default": default, "min": DIAL_MIN,
           "max": DIAL_MAX, "authored": False, "by": "", "ts": None,
           "problem": None}
    try:
        from . import registry
        said = registry.authored_host().get(DIAL_KEY)
    except Exception:
        out["problem"] = ("The saved settings could not be read, so the "
                          "default of %d is in use." % default)
        return out
    if said is None:
        return out
    value = _dial_value(said.get("value")) if isinstance(said, dict) else None
    if value is None:
        out["problem"] = ("The saved number was not a whole number from %d to "
                          "%d, so the default of %d is in use. Setting it "
                          "again replaces it." % (DIAL_MIN, DIAL_MAX, default))
        return out
    ts = said.get("ts")
    out.update(value=value, authored=True,
               by=said.get("by") if isinstance(said.get("by"), str) else "",
               ts=ts if isinstance(ts, int) and not isinstance(ts, bool) else None)
    return out


def set_dial(value, by="", expected=None):
    """Author the dial -> (row, problem, code); the write half of `dial()`.

    `code` is "refused" for a value the dial does not admit and "stale" when
    `expected` (the number the caller was looking at) is no longer the number
    in force; both write nothing. The check and the write share the authored
    layer's lock, so two presses cannot both be judged against the same number.
    A layer that cannot be read RAISES what `registry.author_host` raises and
    nothing is written: every caller reports that as a failed save."""
    n = _dial_value(value)
    if n is None:
        return None, DIAL_REFUSAL, "refused"
    from . import registry
    with registry._write_lock():
        current = dial()
        now = current["value"]
        if expected is not None and now != expected:
            # a number that moved because the stored one stopped being usable
            # was changed by nobody, and the sentence must not say otherwise
            return None, (current["problem"] + " Nothing was saved."
                          if current["problem"] else
                          "The number was changed to %d while this was showing "
                          "%d, so nothing was saved." % (now, expected)), "stale"
        was = registry.author_host(DIAL_KEY, {"value": n, "by": by or "",
                                              "ts": int(time.time())})
    return {"was": was, "dial": dial()}, None, None


USAGE = ("usage: helm friction [--days N] [--seat] [--json]\n"
         "       helm friction record <guard> [--reason TOKEN] [--session ID]\n"
         "       helm friction dial [N] [--json]\n"
         "  Refusals per guard over the window (default %d days); --seat adds "
         "the per-seat split. `record` counts one refusal and is what a shell "
         "hook calls; it prints nothing and never fails its caller. `dial` "
         "prints how many refusals by one guard in one day a seat meets before "
         "it is told, with who set that and when; with N (%d to %d) it sets it."
         % (WINDOW_DAYS, DIAL_MIN, DIAL_MAX))
DIAL_USAGE = "helm friction dial [N] [--json]  (N is a whole number from %d to %d)" % (
    DIAL_MIN, DIAL_MAX)


def _cmd_dial(argv):
    from .cli import guard_tail
    typed = argv.pop(0) if argv and not argv[0].startswith("-") else None
    rc = guard_tail("helm friction dial", argv, flags=("--json",),
                    usage=DIAL_USAGE)
    if rc is not None:
        return rc
    if typed is not None:
        # `int()` admits "+7", " 7 " and "٧"; the dial is typed as plain digits
        value = int(typed) if re.fullmatch(r"[0-9]{1,3}", typed) else None
        try:
            by = home.chat_name() or ""
        except ValueError as exc:   # a seat name this helm will not record
            print("helm friction dial: %s" % exc, file=sys.stderr)
            return 1
        try:
            row, problem, _code = set_dial(value, by=by)
        except (OSError, ValueError) as exc:
            print("helm friction dial: not saved: the settings could not be "
                  "read (%s)" % exc, file=sys.stderr)
            return 1
        if problem:
            print("helm friction dial: %s\nusage: %s"
                  % (problem, DIAL_USAGE), file=sys.stderr)
            return 2
        if "--json" in argv:
            print(json.dumps(row["dial"], sort_keys=True))
            return 0
        was = row["was"].get("value") if isinstance(row["was"], dict) else None
        print("helm friction dial — set to %d (was %s). A seat is now told "
              "after %d refusals by one guard in one day."
              % (value, was if was is not None else "the default", value))
        return 0
    got = dial()
    if got["problem"]:
        print("helm friction dial: " + got["problem"], file=sys.stderr)
    if "--json" in argv:
        print(json.dumps(got, sort_keys=True))
    else:
        print("helm friction dial — %d refusals by one guard in one day, %s"
              % (got["value"],
                 "set by %s %s" % (got["by"] or "an unnamed terminal",
                                   time.strftime(_TS, time.gmtime(got["ts"]))
                                   if got["ts"] is not None else "(no time recorded)")
                 if got["authored"] else "the default; nobody has set it"))
    return 1 if got["problem"] else 0


def _cmd_record(argv):
    opts, guard = {}, None
    while argv:
        head = argv.pop(0)
        if head in ("--reason", "--session"):
            if not argv:
                print("%s needs a value\n%s" % (head, USAGE), file=sys.stderr)
                return 2
            opts[head[2:]] = argv.pop(0)
        elif head.startswith("-") or guard is not None:
            print("helm friction record: unknown argument '%s'\n%s"
                  % (head, USAGE), file=sys.stderr)
            return 2
        else:
            guard = head
    if not _token(guard):
        print("helm friction record: <guard> must be a short token\n" + USAGE,
              file=sys.stderr)
        return 2
    record(guard, **opts)
    return 0


def cmd(args):
    argv = list(args)
    if argv and argv[0] == "record":
        return _cmd_record(argv[1:])
    if argv and argv[0] == "dial":
        return _cmd_dial(argv[1:])
    days, by_seat, want_json = WINDOW_DAYS, False, False
    while argv:
        head = argv.pop(0)
        if head in ("--help", "-h"):
            print(USAGE)
            return 0
        if head == "--json":
            want_json = True
        elif head == "--seat":
            by_seat = True
        elif head == "--days":
            try:
                days = int(argv.pop(0))
                if days <= 0:
                    raise ValueError(days)
            except (IndexError, ValueError):
                print("--days needs a positive whole number\n" + USAGE,
                      file=sys.stderr)
                return 2
        else:
            print("helm friction: unknown argument '%s'\n%s" % (head, USAGE),
                  file=sys.stderr)
            return 2
    result = report(days=days)
    if want_json:
        print(json.dumps(result, sort_keys=True))
        return 1 if result["unreadable"] else 0
    if result["unreadable"]:
        print("helm friction: UNREADABLE — %s (%s). This is NOT zero refusals."
              % (result["unreadable"], result["ledger"]), file=sys.stderr)
        return 1
    print("helm friction — %d refusal%s in the last %d day%s"
          % (result["total"], "s"[:result["total"] != 1],
             days, "s"[:days != 1]))
    for g in result["guards"]:
        line = "  %5d  %s" % (g["refusals"], g["guard"])
        if by_seat:
            line += "  [" + ", ".join("%s %d" % kv for kv in g["seats"].items()) + "]"
        print(line)
    if result["invalid"]:
        print("  (%d ledger row%s not countable)"
              % (result["invalid"], "s"[:result["invalid"] != 1]))
    return 0
