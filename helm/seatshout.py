#!/usr/bin/env python3
"""helm seat shout — a seat's voice, with a budget (task/3901).

THE OWNER'S ASK. Each seat sits at a cubicle and can raise its voice the way a
person would: say something in its own room, speak up so its steward comes
over, or shout so the owner walks to its terminal. A shout reaches his phone,
so it must be rare, it must say what the seat needs, and it must say where the
seat is.

THREE VOLUMES, ONE VERB.

  talk      the seat's own room, as today (`helm chat post`). Nothing new.
  speak up  `--speak-up`: one #seats row that @mentions the seat's steward
            (`seatevents.address`), so the steward's beacon rings. The
            steward is the seat's project lead, else the integrator; `--about`
            names a component of `seatevents.STEWARDS` instead.
  shout     the default: one `notify.owner_push` naming the seat, its one
            "what I need" line and its Orca tab, with the seat as the
            `reply_key`, so a Telegram reply comes back to it as a DM.

THE BUDGET. N shouts per seat in any 24 hours, default 2. N is the owner's
dial, kept exactly the way `friction.dial` keeps his: one key in the authored
layer's host block, written with who set it and when, read with a sentence for
a stored value that cannot be used. A seat cannot set it, because the budget
bounds seats. A shout over budget is refused with the time the next one
refills. Only a DELIVERED shout spends: a phone that is not configured costs
nothing. A failed push is refunded only after its cancellation is durable; an
uncertain delivery stays reserved so a retry cannot bypass the budget.

THE DEEP LINK, AND WHY THERE IS NONE. The push cannot focus a pane. ntfy has
no inbound leg; the console binds 127.0.0.1, which on a phone is the phone
(the `helm decide` push names no console for the same reason); Orca has no URL
scheme. `orca terminal switch` can focus a tab from this box, but doing it at
shout time would move the owner's keyboard focus while he types in another
tab. So the push NAMES the pane: the tab titled with the seat's name (the
title is byte-exact the seat name, `orcatitle`) and the worktree from the
process's own ORCA_WORKTREE_ID. The reply leg is the real return path: a
Telegram reply to the push is routed to the seat (`telegram._route`).

THE OPEN QUESTION. Orca has no way to pin text at the bottom of a pane (its
worktree comment is one line per worktree, shared by every seat in it). So a
delivered shout is the seat's open question on the floor view, `--status`,
until the seat closes it with `--answered`. `floor()` is the read a later
floor or mood view uses; nothing here depends on one existing.

THE LEDGER. One JSON line per shout or answer under the helm home, through
`eventledger`'s locked append and checked read, one rotated generation beside
it. The budget check, durable reservation, push and reconciliation run under one
lock, so two shouts cannot both pass a budget of one. An unreadable ledger
refuses the shout and is never read as zero shouts.
"""
import math
import os
import re
import stat
import sys
import time

from . import home

LEDGER_NAME = "shouts.jsonl"
#: The rolling window a budget counts over.
WINDOW_S = 24 * 3600
#: An open question older than this is not shown.
OPEN_KEEP_S = 7 * 86400
#: The longest "what I need" line a push carries.
NEED_MAX = 200
#: How long a shout waits for another shout's lock (a push takes seconds).
LOCK_WAIT_S = 20
#: One generation is kept beside the live file as `<ledger>.1`.
MAX_BYTES = 256 * 1024
#: THE OWNER'S DIAL: its key in the authored layer's host block, its default
#: and the values it admits. Zero is his "no seat shouts"; the ceiling keeps the
#: dial a budget rather than a way to switch it off without saying so.
DIAL_KEY = "shout_dial"
DIAL_DEFAULT = 2
DIAL_MIN, DIAL_MAX = 0, 10
DIAL_REFUSAL = ("The number must be a whole number from %d to %d."
                % (DIAL_MIN, DIAL_MAX))
PUSH_TITLE = "shout: %s"

# The identity seam owns the grammar; the ledger may not invent a stricter one.
_SEAT = home._SEAT_NAME_RE
_TS = "%Y-%m-%dT%H:%M:%SZ"

USAGE = """\
usage: helm seat shout --need "<one line: what I need from you>" [--speak-up [--about COMPONENT]]
       helm seat shout --status [--json] | --answered | --dial N
  A seat's voice has three volumes.
  TALK      your own room, as today: helm chat post "...". This verb adds
            nothing there.
  SPEAK UP  --speak-up: one row in #seats that @mentions your steward (your
            project's lead, else the integrator; --about credentials,
            local-serving, build-lanes or project-seats names the component).
            It costs no budget and never reaches the owner's phone.
  SHOUT     the default: one push to the owner's phone with your name, your one
            --need line and your Orca tab; a Telegram reply to it comes back to
            you as a DM. It costs one of N shouts per seat per 24 h (default %d,
            the owner's dial). Over budget it is refused and says when the next
            one refills. It is refused without exactly one --need line.
  --status   every seat's shouts in the last 24 h, the budget, when it refills
             and each open question (the last shout a seat has not closed).
  --answered closes your open question. It does not refund the shout.
  --dial N   the owner sets the budget (%d to %d); a seat cannot set it.
  For a ruling among options, file a card instead: helm decide file.""" % (
    DIAL_DEFAULT, DIAL_MIN, DIAL_MAX)


def path():
    return os.path.join(home.helm_home(), home.GLOBAL, ".state", LEDGER_NAME)


def _printable(text, limit):
    return "".join(c for c in str(text) if c.isprintable())[:limit]


# ------------------------------------------------------------------ the need

def need_line(values):
    """(line, None) for exactly one usable --need value, else (None, why)."""
    if len(values) != 1:
        return None, ("a shout needs exactly one --need line saying what you "
                      "need from the owner (got %d)" % len(values))
    raw = values[0]
    if "\n" in raw or "\r" in raw:
        return None, "--need is one line; this one has a line break"
    line = raw.strip()
    if not line:
        return None, "--need is empty; say what you need from the owner"
    if len(line) > NEED_MAX:
        return None, ("--need is %d characters; keep it to %d"
                      % (len(line), NEED_MAX))
    if not line.isprintable():
        return None, "--need carries a control character"
    return line, None


def pane_of(seat, environ=None):
    """The sentence that tells the owner where the seat's pane is."""
    env = os.environ if environ is None else environ
    worktree = env.get("ORCA_WORKTREE_ID") or ""
    where = _printable(worktree.split("::", 1)[-1], 120)
    if where:
        return 'the Orca tab "%s", worktree %s' % (seat, where)
    return 'the tab "%s" (no Orca pane recorded for this process)' % seat


# ------------------------------------------------------------------ the dial

def _dial_value(value):
    ok = (isinstance(value, int) and not isinstance(value, bool)
          and DIAL_MIN <= value <= DIAL_MAX)
    return value if ok else None


def dial():
    """THE ONE READ of the owner's dial -> {value, default, min, max, authored,
    by, ts, problem}, `friction.dial`'s shape. NEVER RAISES: a stored value
    that cannot be used answers the default with `problem` saying so."""
    out = {"value": DIAL_DEFAULT, "default": DIAL_DEFAULT, "min": DIAL_MIN,
           "max": DIAL_MAX, "authored": False, "by": "", "ts": None,
           "problem": None}
    try:
        from . import registry
        said = registry.authored_host().get(DIAL_KEY)
    except Exception:
        out["problem"] = ("The saved settings could not be read, so the "
                          "default of %d shouts a day is in use." % DIAL_DEFAULT)
        return out
    if said is None:
        return out
    value = _dial_value(said.get("value")) if isinstance(said, dict) else None
    if value is None:
        out["problem"] = ("The saved number was not a whole number from %d to "
                          "%d, so the default of %d is in use. Setting it "
                          "again replaces it."
                          % (DIAL_MIN, DIAL_MAX, DIAL_DEFAULT))
        return out
    ts = said.get("ts")
    out.update(value=value, authored=True,
               by=said.get("by") if isinstance(said.get("by"), str) else "",
               ts=ts if isinstance(ts, int) and not isinstance(ts, bool)
               else None)
    return out


def set_dial(value, by="", expected=None):
    """Author the dial -> (row, problem, code), `friction.set_dial`'s shape:
    "refused" for a value the dial does not admit, "stale" when `expected` is
    no longer the number in force; both write nothing."""
    n = _dial_value(value)
    if n is None:
        return None, DIAL_REFUSAL, "refused"
    from . import registry
    with registry._write_lock():
        current = dial()
        if expected is not None and current["value"] != expected:
            return None, ("The number was changed to %d while this was showing "
                          "%d, so nothing was saved."
                          % (current["value"], expected)), "stale"
        was = registry.author_host(DIAL_KEY, {"value": n, "by": by or "",
                                              "ts": int(time.time())})
    return {"was": was, "dial": dial()}, None, None


# ------------------------------------------------------------------ the ledger

def read():
    """(rows, unreadable): both generations, oldest first. `unreadable` set
    means the rows say nothing about how many shouts happened."""
    from . import eventledger
    out = []
    for p in (path() + ".1", path()):
        rows, unavailable = eventledger.checked_events(p, strict=True)
        if unavailable:
            return [], unavailable
        for row in rows:
            kind = row.get("kind")
            if kind not in ("shout", "reserved", "answered", "delivered",
                            "failed"):
                return [], "unknown shout ledger row"
            at = row.get("at")
            if not (isinstance(row.get("id"), str) and
                    isinstance(row.get("seat"), str) and
                    _SEAT.fullmatch(row["seat"]) and
                    isinstance(at, (int, float)) and not isinstance(at, bool)
                    and math.isfinite(at)):
                return [], "invalid shout ledger row"
            if kind in ("shout", "reserved") and not (
                    isinstance(row.get("need"), str) and
                    isinstance(row.get("pane"), str)):
                return [], "invalid shout payload"
            if kind == "shout" and not isinstance(row.get("delivered"), bool):
                return [], "invalid shout delivery"
            if kind in ("delivered", "failed") and not (
                    isinstance(row.get("reservation"), str) and
                    row["reservation"]):
                return [], "invalid shout reconciliation row"
            out.append(row)
    return out, None


def _rows(rows, seat=None):
    """The usable rows, optionally for one seat."""
    out = []
    for r in rows:
        at = r.get("at")
        if r.get("kind") in ("shout", "reserved", "delivered", "failed",
                             "answered") \
                and isinstance(at, (int, float)) and not isinstance(at, bool) \
                and isinstance(r.get("seat"), str) \
                and _SEAT.match(r["seat"]) and (seat is None or
                                                r["seat"] == seat):
            out.append(r)
    return sorted(out, key=lambda r: r["at"])


def standing(rows, seat, budget, now):
    """One seat's place on the floor -> {spent, budget, refills_at, open}.

    `spent` counts delivered shouts and unresolved reservations in the last
    WINDOW_S; only a durably cancelled failure releases a reservation. The
    open question needs delivery confirmation, never merely a reservation."""
    mine = _rows(rows, seat)
    failed = {r["reservation"] for r in mine
              if r["kind"] == "failed" and r["at"] <= now}
    delivered = {r["reservation"] for r in mine
                 if r["kind"] == "delivered" and r["at"] <= now}
    active = [r for r in mine if
              (r["kind"] == "shout" and r.get("delivered") or
               r["kind"] == "reserved" and r["id"] not in failed)]
    ats = [r["at"] for r in active if now - WINDOW_S < r["at"] <= now]
    spent = len(ats)
    refills = None
    if budget and spent >= budget:
        refills = ats[spent - budget] + WINDOW_S
    open_q = None
    for r in mine:
        if r["at"] > now:
            continue
        if r["kind"] == "answered":
            open_q = None
        elif r in active and (r["kind"] == "shout" or r["id"] in delivered):
            open_q = {"need": r.get("need"), "at": r["at"],
                      "pane": r.get("pane")}
    if open_q and open_q["at"] <= now - OPEN_KEEP_S:
        open_q = None
    return {"spent": spent, "budget": budget, "refills_at": refills,
            "open": open_q}


def floor(now=None):
    """Every seat that shouted lately -> {budget, window_s, seats, unreadable}.
    The read a floor view uses; `seats` is None when the ledger is unreadable,
    never an empty floor."""
    now = time.time() if now is None else now
    got = dial()
    rows, unreadable = read()
    out = {"budget": got, "window_s": WINDOW_S, "seats": None,
           "unreadable": unreadable}
    if unreadable:
        return out
    seats = {}
    for name in sorted({r["seat"] for r in _rows(rows)}):
        st = standing(rows, name, got["value"], now)
        if st["spent"] or st["open"]:
            seats[name] = st
    out["seats"] = seats
    return out


def _append(dest, row, now):
    from . import eventledger
    row = dict(row, id=os.urandom(8).hex(),
               ts=time.strftime(_TS, time.gmtime(now)), at=now)
    try:
        if os.path.getsize(dest) > MAX_BYTES:
            os.replace(dest, dest + ".1")
    except OSError:
        pass
    return row["id"] if eventledger.append_unlocked(dest, row) else None


def _unreadable(why):
    return ("the shout ledger is UNREADABLE (%s), so the budget cannot be "
            "counted; nothing was sent. This is not zero shouts." % why)


def _no_lock(lock_wait):
    """(code, message) when the ledger's lock was not taken. `locked` answers
    False for a lock another shout holds AND for failed setup. A successful
    independent setup probe cannot take a held lock, but distinguishes setup
    refusal from a timeout without calling a bad lock 'busy'."""
    from . import eventledger
    _seen, unreadable = read()
    if unreadable:
        return "unreadable", _unreadable(unreadable)
    fd = None
    try:
        dest = eventledger._prepare(path(), create=True)
        lock = dest + ".lock"
        fresh = not os.path.exists(lock)
        fd = os.open(lock, eventledger._flags(os.O_RDWR | os.O_CREAT), 0o600)
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
            raise OSError("ledger lock is not a private regular file")
        os.fchmod(fd, 0o600)
        if fresh:
            eventledger._fsync_dir(os.path.dirname(lock))
    except (OSError, ValueError) as exc:
        return "unreadable", _unreadable(exc)
    finally:
        if fd is not None:
            os.close(fd)
    return "busy", ("the shout ledger's lock was not taken within %gs: another "
                    "shout holds it. Nothing was sent, nothing spent."
                    % lock_wait)


def _clock(epoch, now):
    left = max(0, int(epoch - now))
    return "%s (in %dh %02dm)" % (time.strftime("%H:%M", time.localtime(epoch)),
                                  left // 3600, left % 3600 // 60)


# ------------------------------------------------------------------ the acts

def shout(seat, need, pane, now=None, push=None, configured=None,
          lock_wait=LOCK_WAIT_S):
    """One shout -> {ok, code, message, spent, budget, refills_at}.

    `code`: "shouted", "over-budget", "no-phone", "not-delivered", "busy"
    or "unreadable". A reservation is durable before a push; uncertain
    delivery remains charged until the window expires. All steps share the
    ledger's lock."""
    from . import eventledger, notify
    now = time.time() if now is None else now
    push = push or notify.owner_push
    configured = configured or notify.configured
    got = dial()
    budget = got["value"]
    out = {"ok": False, "code": None, "message": "", "spent": None,
           "budget": budget, "refills_at": None}
    dest = path()
    with eventledger.locked(dest, timeout=lock_wait) as held:
        if not held:
            code, message = _no_lock(lock_wait)
            out.update(code=code, message=message)
            return out
        rows, unreadable = read()
        if unreadable:
            out.update(code="unreadable", message=_unreadable(unreadable))
            return out
        st = standing(rows, seat, budget, now)
        out.update(spent=st["spent"], refills_at=st["refills_at"])
        if st["spent"] >= budget:
            if not budget:
                why = ("the owner set the shout budget to 0, so no seat may "
                       "shout until he raises it (helm seat shout --dial N)")
            else:
                why = ("%s has spent %d of %d shouts in the last 24 h; the "
                       "next one refills at %s"
                       % (seat, st["spent"], budget,
                          _clock(st["refills_at"], now)))
            out.update(code="over-budget", message=(
                "refused: %s. Speak up instead (--speak-up), or talk in your "
                "room." % why))
            return out
        if not configured():
            out.update(code="no-phone", message=(
                "no phone is configured (HELM_NTFY_TOPIC or the Telegram "
                "bridge), so a shout reaches nobody; nothing was sent, nothing "
                "spent. Speak up instead (--speak-up)."))
            return out
        n = st["spent"] + 1
        body = "%s needs you: %s\npane: %s\nshout %d of %d in 24 h" % (
            seat, need, pane, n, budget)
        reservation = _append(dest, {"kind": "reserved", "seat": seat,
                                     "need": need, "pane": pane}, now)
        if not reservation:
            out.update(code="unreadable", message=_unreadable(
                "the reservation could not be written"))
            return out
        try:
            delivered = bool(push(body, title=PUSH_TITLE % seat,
                                  receipt=("seat.shout", seat), reply_key=seat))
        except Exception as exc:
            out.update(code="not-delivered", spent=n, message=(
                "push outcome is unknown (%s); reservation still counts until "
                "it expires. Do not retry blindly." % exc))
            return out
        if not delivered:
            released = _append(dest, {"kind": "failed", "seat": seat,
                                      "reservation": reservation}, now)
            out.update(code="not-delivered", spent=st["spent"] if released else n,
                       message=("the push was not delivered; %s. Speak up "
                                "instead (--speak-up)." % (
                                    "nothing spent; try again" if released else
                                    "the cancellation could not be recorded, "
                                    "so the reservation still counts")))
            return out
        confirmed = _append(dest, {"kind": "delivered", "seat": seat,
                                   "reservation": reservation}, now)
        if not confirmed:
            out.update(code="unreadable", spent=n, message=(
                "the owner push was delivered but its confirmation could not "
                "be written; the reservation still counts. Do not retry "
                "blindly."))
            return out
    out.update(ok=True, spent=n, code="shouted",
               message="shouted to the owner: shout %d of %d in 24 h." % (
                   n, budget))
    return out


def answered(seat, now=None):
    """Close the seat's open question -> (need it closed, None) or (None, why)."""
    from . import eventledger
    now = time.time() if now is None else now
    dest = path()
    with eventledger.locked(dest, timeout=LOCK_WAIT_S) as held:
        if not held:
            return None, _no_lock(LOCK_WAIT_S)[1]
        rows, unreadable = read()
        if unreadable:
            return None, "the shout ledger is UNREADABLE (%s)" % unreadable
        open_q = standing(rows, seat, dial()["value"], now)["open"]
        if not open_q:
            return None, "%s has no open question to close" % seat
        if not _append(dest, {"kind": "answered", "seat": seat}, now):
            return None, "the shout ledger could not be written"
    return open_q["need"], None


def _speak_subjects(about, project):
    """The (component, project) pairs whose stewards a speak-up wakes."""
    from . import seatevents
    if about:
        if about == "project-seats" and not seatevents.steward(
                about, project)[0]:
            return [(about, project), ("build-lanes", None)]
        return [(about, project)]
    if not project:
        return [("build-lanes", None)]
    if seatevents.steward("project-seats", project)[0]:
        return [("project-seats", project)]
    # address() names the missing lead in the row's tail and wakes the
    # integrator, so a project with no lead still reaches a steward.
    return [("project-seats", project), ("build-lanes", None)]


def speak_up(actor, seat, need, about=None, project=None):
    """Post one #seats row that @mentions the seat's steward -> (woken, tail):
    the mentions that wake, and the lines naming each steward nobody declared.
    The row is posted as `actor`, the seat's admitted actor: a seat speaking,
    never a machine label (tests/test_delivery_truth UNRESOLVED_SENDERS)."""
    from . import chat, seatevents
    lead, tail = seatevents.address(_speak_subjects(about, project))
    chat.post("%s%s needs: %s (speaking up; not a shout)%s"
              % (lead, seat, need, tail), room=seatevents.ROOM, who=actor)
    return lead.strip(), tail.strip()


# ------------------------------------------------------------------ the verb

def _admit():
    """(actor, None) for this process's admitted seat, else (None, why). A
    shout is an ACT: it spends a seat's budget and puts its name on the
    owner's phone, so a derived or disputed name may not shout."""
    from . import actors
    return actors.resolve_actor(session=home.session_id(), act="shout")


def _name(actor):
    return getattr(actor, "canonical_name", actor)


def _parse(argv):
    """argv -> (opts, None) or (None, why)."""
    opts = {"need": [], "about": None, "dial": None, "flags": set()}
    args = list(argv)
    while args:
        head = args.pop(0)
        if head in ("--need", "--about", "--dial"):
            if not args:
                return None, "%s wants a value" % head
            value = args.pop(0)
            if head == "--need":
                opts["need"].append(value)
            elif opts[head[2:]] is not None:
                return None, "%s is given twice" % head
            else:
                opts[head[2:]] = value
        elif head in ("--speak-up", "--status", "--answered", "--json",
                      "--help", "-h"):
            opts["flags"].add(head)
        else:
            return None, "unknown arg %r" % head
    flags = opts["flags"]
    modes = [m for m in ("--status", "--answered") if m in flags]
    modes += ["--dial"] if opts["dial"] is not None else []
    modes += ["--need"] if opts["need"] else []
    if len(modes) > 1:
        return None, "%s cannot be combined" % " and ".join(modes)
    # A flag its mode would ignore refuses: a dropped flag answers a
    # different question than the one asked.
    mode = modes[0] if modes else "--need"
    if "--json" in flags and mode != "--status":
        return None, "--json goes with --status"
    if "--speak-up" in flags and mode != "--need":
        return None, "--speak-up goes with --need"
    if opts["about"] is not None and "--speak-up" not in flags:
        return None, "--about goes with --speak-up"
    return opts, None


def _cmd_dial(typed):
    value = int(typed) if re.fullmatch(r"[0-9]{1,2}", typed) else None
    if value is None:
        print("helm seat shout: %s\n%s" % (DIAL_REFUSAL, USAGE),
              file=sys.stderr)
        return 2
    try:
        by = home.chat_name() or ""
        from . import seats_identity, seats_roster
        session = home.session_id()
        # A seat's session remains rostered when its shell clears the name.
        # A harness session with no binding is not proof of an owner terminal.
        if session:
            bound = seats_roster.seat_for_session(session)
            print("helm seat shout: only the owner terminal may set the dial; "
                  "this session is %s" % (bound or "unverified"),
                  file=sys.stderr)
            return 1
        if by and by.lower() not in seats_identity.owner_names():
            print("helm seat shout: the shout budget bounds seats, so a seat "
                  "(%s) cannot set it; the owner sets it from his own "
                  "terminal" % by, file=sys.stderr)
            return 1
        row, problem, _code = set_dial(value, by=by)
    except (OSError, ValueError) as exc:
        print("helm seat shout: not saved: %s" % exc, file=sys.stderr)
        return 1
    if problem:
        print("helm seat shout: " + problem, file=sys.stderr)
        return 2
    was = row["was"].get("value") if isinstance(row["was"], dict) else None
    print("helm seat shout — the budget is now %d shout%s per seat per 24 h "
          "(was %s)." % (value, "s"[:value != 1],
                         was if was is not None else "the default"))
    return 0


def _cmd_status(want_json, now):
    import json
    got = floor(now)
    if want_json:
        print(json.dumps(got, sort_keys=True))
        return 1 if got["unreadable"] else 0
    if got["unreadable"]:
        print("helm seat shout: the shout ledger is UNREADABLE (%s); this is "
              "not an empty floor." % got["unreadable"], file=sys.stderr)
        return 1
    d = got["budget"]
    print("helm seat shout — %d shouts per seat per 24 h, %s" % (
        d["value"], "set by %s" % (d["by"] or "an unnamed terminal")
        if d["authored"] else "the default; nobody has set it"))
    if d["problem"]:
        print("  " + d["problem"])
    if not got["seats"]:
        print("  no seat has shouted in the last 24 h, and no question is open")
        return 0
    print("  %-22s %-6s %-22s %s" % ("seat", "spent", "next refill",
                                      "open question"))
    for seat, st in got["seats"].items():
        q = st["open"]
        print("  %-22s %-6s %-22s %s" % (
            seat, "%d/%d" % (st["spent"], st["budget"]),
            _clock(st["refills_at"], now) if st["refills_at"] else "-",
            '"%s" (%d min ago)' % (q["need"], (now - q["at"]) // 60)
            if q else "-"))
    return 0


def cmd_shout(argv, now=None):
    """helm seat shout — a seat's voice: talk, speak up, or shout."""
    now = time.time() if now is None else now
    opts, why = _parse(argv)
    if why:
        print("helm seat shout: %s\n%s" % (why, USAGE), file=sys.stderr)
        return 2
    flags = opts["flags"]
    if flags & {"--help", "-h"}:
        print(USAGE)
        return 0
    if "--status" in flags:
        return _cmd_status("--json" in flags, now)
    if opts["dial"] is not None:
        return _cmd_dial(opts["dial"])
    need = None
    if "--answered" not in flags:
        need, why = need_line(opts["need"])
        if why:
            print("helm seat shout: refused: %s\n%s" % (why, USAGE),
                  file=sys.stderr)
            return 2
    from . import seatevents
    if opts["about"] is not None and opts["about"] not in seatevents.STEWARDS:
        print("helm seat shout: --about names one of %s"
              % ", ".join(sorted(seatevents.STEWARDS)), file=sys.stderr)
        return 2
    actor, why = _admit()
    if why:
        print("helm seat shout: %s" % why, file=sys.stderr)
        return 1
    seat = _name(actor)
    if "--answered" in flags:
        closed, why = answered(seat, now)
        if why:
            print("helm seat shout: %s" % why, file=sys.stderr)
            return 1
        print('helm seat shout — closed %s\'s open question: "%s"'
              % (seat, closed))
        return 0
    if "--speak-up" in flags:
        project = seatevents.project_of([seat]).get(seat)
        woken, tail = speak_up(actor, seat, need, about=opts["about"],
                               project=project)
        if not woken:
            print("helm seat shout: posted to #%s, but it woke nobody. %s"
                  % (seatevents.ROOM, tail), file=sys.stderr)
            return 1
        print("helm seat shout — spoke up in #%s, waking %s%s"
              % (seatevents.ROOM, woken, "\n" + tail if tail else ""))
        return 0
    got = shout(seat, need, pane_of(seat), now=now)
    if not got["ok"]:
        print("helm seat shout: %s" % got["message"], file=sys.stderr)
        return 1
    print("helm seat shout — %s\nOPEN QUESTION to the owner: %s\n(close it "
          "with helm seat shout --answered once he answers)"
          % (got["message"], need))
    return 0
