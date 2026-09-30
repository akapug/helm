"""The OWNER PAUSE: `helm seat rest <seat>` — a seat that is up and is
deliberately not listening.

THE OWNER'S VOCABULARY. A seat whose turn has ended is in one of three
states:

    IDLE     its beacon is armed and it is available: any addressed row
             wakes it.
    RESTING  its beacon is off because the OWNER paused it: only an explicit
             resume wakes it.
    DEAF     its beacon is missing by accident: a fault to repair.

WHY IT HAD TO BE A RECORDED STATE. While the owner had a seat paused ("drain
to pause for the time being"), helm woke it or gave it work anyway: the router
named it an idle approval-tier reader, the resume-turn nudge typed "Catch up"
into it twice, the stop-whisper auto-claimed a dispatch for it, the stop-guard
demanded a beacon on every turn, and the beacons census called it a DEAF SEAT.
Each one cost a turn on a seat the owner paused to save credit. Nothing was
wrong with any of those readers: a RESTING seat and a DEAF seat look the same
to every instrument, because the difference is an INTENT, and nothing recorded
it.

ONE RECORD PER SEAT, UNDER THE HELM HOME:
`<helm home>/_global/.state/seat-rest/<seat, casefolded>.json`, written whole
by `pk.atomic_write`, private (0600):

    {"v": 1, "seat": S, "by": NAME, "role": "self"|"integrator",
     "because": THE OWNER'S WORDS, "at": EPOCH, "until": EPOCH|null,
     "ended": null | {"by": NAME, "role": ..., "at": EPOCH,
                      "because": TEXT|null}}

One file per seat, so a damaged record can speak for one seat and never for
the fleet. Durable, because an owner pause outlives a reboot. `--end` does not
delete the record: it stamps `ended`, so who ended a rest, and why, stays on
disk until the next rest replaces it. Both are written under the lock every
delivery holds from its pause read to its commit (`_ordered`), so a rest is
ordered with every delivery and an end with every rest.

EVERY READER GOES THROUGH THE SEAM THAT ALREADY EXISTED. A resting seat's
delivery pause (`proxywatch.delivery_pause`) is truthy, with state RESTING,
read BEFORE the proxy family is resolved, because the seat the owner paused was
a native one with no family. So every existing reader of the credential-wall
pause holds for a rest with no parallel path. The readers that never asked the
pause (the usability join the router and the reviewer bench read, the
stop-whisper's work offer, idle-dispatch's wake leg, the beacons census) ask
this module directly.

AN UNREADABLE RECORD HOLDS THE WAKERS AND IS NEVER CALLED RESTING. A record
that exists and will not read (a corrupt file, a planted symlink, a record for
another seat, a field named twice, a time that is not a finite number) answers
three ways at once:
  * the delivery pause is state UNKNOWN, so every waker holds, as it does for
    an unreadable proxy latch: a seat the owner may have paused is not woken
    on a file helm cannot read;
  * no surface the owner reads calls it RESTING: the census keeps its own
    verdict (DEAF stays DEAF, with the unreadable record named), and the
    delivery receipt says UNKNOWN;
  * every sentence names the repair: `helm seat rest <seat> --end` rewrites
    it.
This is the opposite choice from `seat_down`'s unreadable marker, and the two
are consistent: that marker can only keep a seat DEAD, which the fleet cannot
afford on a guess; this one can only keep a seat QUIET, which is what the
owner asked for when he wrote it.

WHO MAY SET OR END IT. The seat itself (resting on the owner's recorded word)
or the integrator, resolved through the identity layer (`actors`), never from
a bare environment name. The owner's words are required (`--because`); the
owner does not type helm verbs (`ownerasks`), so his authority reaches the
record through one of those two seats and his words are what it records.
"""
import json
import math
import os
import re
import sys
import time

from . import home, pk

STATE = "RESTING"
VERSION = 1
WORDS_CAP = 400
_SUBDIR = os.path.join(".state", "seat-rest")
_SEAT = re.compile(r"\A[A-Za-z0-9._-]{1,64}\Z")
_DURATION = re.compile(r"\A(\d+)([smhd])\Z")
_UNIT_S = {"s": 1, "m": 60, "h": 3600, "d": 86400}
USAGE = ("usage: helm seat rest <seat> --because \"<the owner's words>\" "
         "[--until <ISO-8601|NNs|NNm|NNh|NNd>]\n"
         "       helm seat rest <seat> --end [--because \"<why>\"]")


def path(seat):
    """Where this seat's rest record lives, or None for a name no record can
    have (the verb refuses to write one for it)."""
    seat = str(seat or "")
    if not _SEAT.match(seat):
        return None
    return os.path.join(home.global_dir(), _SUBDIR,
                        "%s.json" % seat.casefold())


def _text(value, cap):
    """One line of display-safe text, bounded."""
    from .seats_common import _clip
    return _clip(pk.launder(str(value)).replace("\t", " ").strip(), cap)


def _iso(epoch):
    return pk.epoch_ts(epoch) if isinstance(epoch, (int, float)) else None


def _number(value):
    """A time: a finite number the renderer can print. NaN crashed every
    reader that printed the rest, an `until` of -Infinity expired a rest
    nobody ended, and JSON's `1e400` parses as an infinity."""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:        # a JSON integer float() cannot hold raises here, not False
        if not math.isfinite(value):
            return False
        pk.epoch_ts(value)
    except (ValueError, OverflowError, OSError):
        return False
    return True


def _unique(pairs):
    """A JSON object whose field is named twice is not one statement: one
    reader keeps the copy that ends the rest and another the copy that does
    not (seat_down's record reads the same way)."""
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError("duplicate JSON field %s" % key)
        out[key] = value
    return out


def _constant(name):
    raise ValueError("non-finite JSON number %s" % name)


def _invalid(rec, seat):
    """Why `rec` is not this seat's rest record, or None."""
    if not isinstance(rec, dict):
        return "not a JSON object"
    if type(rec.get("v")) is not int or rec["v"] != VERSION:   # not True, 1.0
        return "version %r, want %d" % (rec.get("v"), VERSION)
    if str(rec.get("seat") or "").casefold() != str(seat).casefold():
        return "names seat %r" % (rec.get("seat"),)
    for key in ("by", "because"):
        if not isinstance(rec.get(key), str) or not rec[key].strip():
            return "field %r missing or not text" % key
    if not _number(rec.get("at")):
        return "field 'at' is not a time"
    if rec.get("until") is not None and not _number(rec["until"]):
        return "field 'until' is not a time"
    ended = rec.get("ended")
    if ended is not None and not (
            isinstance(ended, dict) and isinstance(ended.get("by"), str)
            and _number(ended.get("at"))):
        return "field 'ended' is not an ending"
    return None


def _load(seat):
    """(record as written, error) — (None, None) when the seat has no file."""
    where = path(seat)
    if where is None:
        return None, None
    try:
        with pk.open_regular(where, encoding="utf-8") as f:
            rec = json.load(f, object_pairs_hook=_unique,
                            parse_constant=_constant)
    except FileNotFoundError:
        if os.path.islink(where):
            return None, "%s is a dangling symlink" % where
        return None, None
    except Exception as exc:                # noqa: BLE001 — every failure is a reason
        return None, "%s unreadable (%s: %s)" % (
            where, exc.__class__.__name__, exc)
    why = _invalid(rec, seat)
    if why:
        return None, "%s is not a rest record (%s)" % (where, why)
    return rec, None


def read(seat, now=None):
    """(record, error). (None, None): not resting — no record, a rest that was
    ended, or one whose --until has passed. (record, None): RESTING.
    (None, why): a record exists and cannot be read; see the module
    docstring for what each reader does with that."""
    rec, why = _load(seat)
    if rec is None:
        return None, why
    return (None, None) if _over(rec, now) else (rec, None)


def _over(rec, now=None):
    """Is this readable record a rest that has ended or run out?"""
    now = time.time() if now is None else now
    return rec.get("ended") is not None or (
        rec.get("until") is not None and now >= rec["until"])


def phrase(rec):
    """RESTING (owner pause since T: "<words>"; recorded by B[; until U]) —
    the one rendering every surface prints."""
    return 'RESTING (owner pause since %s: "%s"; recorded by %s%s)' % (
        _iso(rec["at"]), _text(rec["because"], WORDS_CAP),
        _text(rec["by"], 64),
        "; until %s" % _iso(rec["until"]) if rec.get("until") is not None
        else "")


def end_hint(seat):
    return "end it with `helm seat rest %s --end`" % _text(seat, 64)


def unreadable_text(seat, why):
    """The sentence for a record helm cannot read — and what it did."""
    return ("the owner-rest record for %s is UNREADABLE (%s): every waker "
            "holds as if the seat were resting, and no surface calls it "
            "RESTING; `helm seat rest %s --end` rewrites it"
            % (_text(seat, 64), _text(why, 240), _text(seat, 64)))


def pause(seat, now=None):
    """The DELIVERY-PAUSE record for a resting seat, the shape every consumer
    of `proxywatch.delivery_pause` already renders — or None.

    state RESTING carries the record (`rest`), who recorded it, the owner's
    words and `since`; a record that cannot be read is state UNKNOWN with
    `rest_error`, which every waker holds on and no surface calls RESTING."""
    now = time.time() if now is None else now
    try:
        rec, why = read(seat, now)
    except Exception as exc:                # noqa: BLE001 — cannot invent a wake
        rec, why = None, "the rest record could not be read (%s)" % (
            exc.__class__.__name__)
    if why:
        return {"state": "UNKNOWN", "seat": seat, "stale": True,
                "rest_error": why, "reason": unreadable_text(seat, why)}
    if rec is None:
        return None
    return {"state": STATE, "seat": seat, "by": rec["by"],
            "because": rec["because"], "since": _iso(rec["at"]),
            "until": _iso(rec.get("until")),
            "age_s": max(0.0, now - rec["at"]), "stale": False, "rest": rec,
            "reason": phrase(rec)}


def combine(rest, wall):
    """One delivery pause from the owner's rest and a credential wall: either
    alone is itself, and both together is the rest carrying the wall, with
    BOTH reasons named. Ending one leaves the other, because each is re-read
    on every call."""
    if not rest:
        return wall
    if not wall:
        return rest
    return dict(rest, wall=wall, reason="%s; and %s" % (
        rest["reason"], wall.get("reason") or "delivery is paused (state %s)"
        % (wall.get("state") or "UNKNOWN")))


def display(seat, now=None):
    """The line a seat's own surfaces print (`helm seat where`), or "": the
    rest and how to end it, or the sentence for a record helm cannot read."""
    held = pause(seat, now)
    if not held:
        return ""
    return "%s; %s" % (held["reason"], end_hint(seat)) if held.get("rest") \
        else held["reason"]


def holds(seat, now=None):
    """The sentence a WAKER holds on — resting, or a record it cannot read —
    or "" when nothing holds this seat."""
    return (pause(seat, now) or {}).get("reason") or ""


def rename_refusal(old, new):
    """Why renaming `old` to `new` would drop or re-stale an owner's rest, or
    "" (task/3280). The record lives under the NAME and a rename carries no
    record, so renaming a resting seat would wake it under its new name, and
    taking a name that carries a live rest would hand the seat a pause the
    owner never gave it. The rename refuses while either name holds (an
    unreadable record included). A case-only rename keeps its one file, the
    casefolded name, and is never refused."""
    if str(old).casefold() == str(new).casefold():
        return ""
    for name, then in (
            (old, "a rename does not carry the rest, so the seat would wake "
                  "under its new name"),
            (new, "the seat would inherit a rest recorded for that name")):
        held = pause(name)
        if held:
            said = ("%s is %s" % (_text(name, 64), held["reason"])
                    if held.get("rest") else held["reason"])
            return ("refusing to rename %s -> %s: %s; %s. End it first "
                    "(`helm seat rest %s --end`), then rename, and rest the "
                    "seat again under its new name if the owner still wants "
                    "it paused" % (_text(old, 64), _text(new, 64), said,
                                   then, _text(name, 64)))
    return ""


def stop_line(seat, held):
    """The stop-guard's line for a seat whose delivery pause `held` names a
    rest, or "" — said IN PLACE of any beacon demand."""
    held = held if isinstance(held, dict) else {}
    if held.get("rest"):
        return ("[helm stop-guard] %s; %s. No beacon is owed while the seat "
                "rests: nothing wakes it until the rest ends."
                % (phrase(held["rest"]), end_hint(seat)))
    if held.get("rest_error"):
        return "[helm stop-guard] %s. No beacon is demanded while it holds." \
            % held["reason"]
    return ""


def authority(seat):
    """The rest record BY VALUE, for an act door's before/after capture: ""
    when the seat has no file, so a seat that never rested reads exactly as
    it did before this module existed."""
    rec, why = _load(seat)
    if rec is None and why is None:
        return ""
    return json.dumps(rec, sort_keys=True) if rec is not None \
        else "unreadable: %s" % why


def parse_until(text, now=None):
    """(epoch, None) or (None, why): an ISO-8601 UTC instant or NNs/NNm/NNh/
    NNd from now, and in the future either way."""
    now = time.time() if now is None else now
    raw = str(text or "").strip()
    got = _DURATION.match(raw.lower())
    epoch = (now + int(got.group(1)) * _UNIT_S[got.group(2)] if got
             else pk.parse_ts_epoch(raw))
    if epoch is None:
        return None, ("--until is an ISO-8601 UTC instant or NNs/NNm/NNh/NNd "
                      "(got %r)" % raw)
    if epoch <= now:
        return None, "--until %s is not in the future" % _iso(epoch)
    return float(epoch), None


def _write(seat, rec):
    """Write the record whole. The caller holds `_ordered()`."""
    try:
        pk.atomic_write(path(seat), json.dumps(rec, sort_keys=True) + "\n",
                        mode=0o600)
    except OSError as exc:
        return None, "%s could not be written (%s)" % (path(seat), exc)
    return rec, None


def _ordered():
    """THE LOCK EVERY DELIVERY HOLDS from its pause read to its commit,
    `proxywatch.delivery_state_guard`, taken with no deadline as the wall's
    own writer takes it. A rest is an input of that pause, so it is written
    under it (task/3280): once `mark` returns, no delivery that read the seat
    unpaused is still to commit its wake, and every later one reads the
    rest. `end` reads and writes inside it, so a rest recorded meanwhile is
    never written over with the one it read."""
    from . import proxywatch
    return proxywatch.delivery_state_guard()


def _named():
    """THE ROSTER LOCK, taken OUTSIDE `_ordered()` by `mark` and `end`: the
    order roster, then the delivery lock, is the one chat's append already
    takes. A rename holds the roster lock from its rest refusal through its
    publish (seats_roster.rename_seat), so a rest recorded meanwhile waits
    for the publish instead of landing unseen between the two (review N1)."""
    from .seats_common import _flocked, roster_path
    return _flocked(roster_path() + ".lock")


def _renamed_to(seat):
    """The seat a rename moved `seat`'s row to, while that rename's window is
    open and no row holds `seat` itself, else None. Read under `_named()`;
    an unreadable roster answers None and the rest is written as before."""
    from .seats_common import rename_aliases, roster_path
    try:
        rows = pk.read_json(roster_path(), {})
    except Exception:
        return None
    if not isinstance(rows, dict):
        return None
    key = str(seat).casefold()
    if any(str(name).casefold() == key for name in rows):
        return None
    for name, row in rows.items():
        if any(str(old).casefold() == key for old, _ in rename_aliases(row)):
            return str(name)
    # THE ACTOR STORE REMEMBERS WHAT THE ROSTER FORGOT: a rename with no alias
    # window leaves no event on the row, but the actor keeps the old name as
    # an alias of the name it now holds. That counts only when the new name
    # is a live row, so a name nobody holds under any name may still be rested.
    from . import actors
    actor = actors.lookup(seat)
    canon = actor.get("canonical_name") if isinstance(actor, dict) else None
    if isinstance(canon, str) and canon.casefold() != key:
        return next((str(name) for name in rows
                     if str(name).casefold() == canon.casefold()), None)
    return None


def _row_identity(seat):
    """(present, incarnation) for `seat`'s roster row right now (casefold):
    (True, id) or (True, None) for a row with no incarnation, (False, None)
    for no row, and (None, None) when the roster cannot be read, which never
    refuses a rest."""
    from .seats_common import roster_path
    try:
        # STRICT: a missing roster is no rows, but one that cannot be parsed
        # is UNKNOWN, never read as the row having gone (review of CURE3).
        rows = pk.read_json(roster_path(), {}, strict=True)
    except Exception:
        return None, None
    if not isinstance(rows, dict):
        return None, None
    key = str(seat).casefold()
    for name, row in rows.items():
        if str(name).casefold() == key:
            inc = row.get("incarnation") if isinstance(row, dict) else None
            return True, inc if isinstance(inc, str) else None
    return False, None


def _unordered(exc):
    return None, ("the delivery lock could not be taken (%s), so nothing was "
                  "written" % exc)


def mark(seat, because, by, role, until=None, now=None):
    """(record, error) — record that the owner rested this seat. Written
    under the roster lock and then the delivery lock (`_named`, `_ordered`);
    a name a rename just moved away is refused, naming the new one."""
    rec = {"v": VERSION, "seat": seat, "by": by, "role": role,
           "because": _text(because, WORDS_CAP),
           "at": time.time() if now is None else now,
           "until": until, "ended": None}
    # THE ROW THE REST WAS ASKED FOR must still be the row once the lock is
    # held. A rename with no alias window (`--alias-hours 0`) leaves no event
    # `_renamed_to` can read, and the name may be gone or already taken by a
    # different seat that joined under it; the row's incarnation, which a
    # rename carries with the row and a join mints fresh, tells the two apart
    # where presence cannot (reviews N1 of CURE2 and CURE3). Unlocked on
    # purpose: it only has to predate the wait.
    before = _row_identity(seat)
    try:
        with _named(), _ordered():
            moved = _renamed_to(seat)
            if moved:
                return None, ("%s was renamed to %s, so a rest recorded "
                              "under %s would pause no seat; nothing was "
                              "written. Rest it by its new name: `helm seat "
                              "rest %s --because \"<the owner's words>\"`"
                              % (_text(seat, 64), _text(moved, 64),
                                 _text(seat, 64), _text(moved, 64)))
            after = _row_identity(seat)
            if before[0] is True and after[0] is not None and (
                    after[0] is False or before[1] != after[1]):
                return None, ("%s was renamed, removed or taken by another "
                              "seat while this rest waited for it, so a rest "
                              "recorded under %s could pause the wrong seat or "
                              "none; nothing was written. Rest the seat by its "
                              "current name (`helm seat list`)."
                              % (_text(seat, 64), _text(seat, 64)))
            return _write(seat, rec)
    except OSError as exc:
        return _unordered(exc)


def end(seat, by, role, because=None, now=None):
    """(the rest it ended, error). A rest already over — none recorded, ended,
    or past its --until — is (None, None) and writes nothing. An unreadable
    record is REPLACED by an ended one, which is the repair its sentence
    names. The read and the write are one step under `_ordered()`."""
    now = time.time() if now is None else now
    try:
        with _named(), _ordered():
            rec, why = _load(seat)
            live = rec if rec is not None and not _over(rec, now) else None
            if live is None and why is None:
                return None, None
            base = rec if rec is not None else {
                "v": VERSION, "seat": seat, "by": by, "role": role,
                "because": "an unreadable record (%s)" % _text(why, 200),
                "at": now, "until": None}
            ended = {"by": by, "role": role, "at": now,
                     "because": _text(because, WORDS_CAP) if because
                     else None}
            got, err = _write(seat, dict(base, ended=ended))
    except OSError as exc:
        return _unordered(exc)
    return (None, err) if err else (live or base, None)


def _authority(seat):
    """(who, role, refusal) — the seat itself or the integrator, admitted by
    the identity layer; anyone else is refused, and so is an identity that
    does not resolve."""
    from . import actors, seats_integrator
    from .seats_identity import safe_cwd
    actor, err = actors.resolve_actor(home.session_id(), safe_cwd(),
                                      act="record a seat's rest")
    if err:
        return None, None, err
    who = actor.canonical_name
    if who.casefold() == seat.casefold():
        return who, "self", None
    integrator, _why = seats_integrator.integrator_seat()
    if integrator and who.casefold() == integrator.casefold():
        return who, "integrator", None
    return None, None, ("%s may not record %s's rest: only the seat itself or "
                        "the integrator does, on the owner's word"
                        % (_text(who, 64), _text(seat, 64)))


def _args(args):
    """(seat, {flag: value}, refusal)."""
    if not args or args[0].startswith("-"):
        return None, None, USAGE
    seat, rest, flags = args[0], list(args[1:]), {}
    while rest:
        flag = rest.pop(0)
        if flag == "--end" and flag not in flags:
            flags[flag] = True
        elif flag in ("--because", "--until") and flag not in flags \
                and rest:
            flags[flag] = rest.pop(0)
        else:
            return None, None, "helm seat rest: unknown or repeated argument " \
                "%r\n%s" % (_text(flag, 64), USAGE)
    try:
        seat = home.validate_seat_arg(seat)
    except home.SeatNameError as exc:
        return None, None, "helm seat rest: %s" % exc
    return seat, flags, None


def cmd_rest(args):
    """seat rest <seat> --because W [--until T] | --end [--because W]."""
    seat, flags, refusal = _args(list(args))
    if refusal:
        print(refusal, file=sys.stderr)
        return 2
    because = str(flags.get("--because") or "").strip()
    if flags.get("--end"):
        if "--until" in flags:
            print("helm seat rest: --until sets a rest, --end ends one\n"
                  + USAGE, file=sys.stderr)
            return 2
    elif not because:
        print("helm seat rest: --because \"<the owner's words>\" is "
              "required — a rest records WHY the owner paused the seat\n"
              + USAGE, file=sys.stderr)
        return 2
    until = None
    if "--until" in flags:
        until, why = parse_until(flags["--until"])
        if why:
            print("helm seat rest: " + why, file=sys.stderr)
            return 2
    who, role, why = _authority(seat)
    if why:
        print("helm seat rest: refused — " + why, file=sys.stderr)
        return 2
    if flags.get("--end"):
        was, err = end(seat, who, role, because or None)
        if err:
            print("helm seat rest: " + err, file=sys.stderr)
            return 1
        print("helm seat rest: %s" % (
            "%s was not resting; nothing to end" % seat if was is None else
            "%s's rest ENDED by %s (%s); it was %s. Its wakers apply again: "
            "arm its beacon (`helm chat wait --seat %s --follow`) to make it "
            "IDLE" % (seat, who, role, phrase(was), seat)))
        return 0
    rec, err = mark(seat, because, who, role, until=until)
    if err:
        print("helm seat rest: " + err, file=sys.stderr)
        return 1
    held = _row_identity(seat)[0]
    if held is None:
        print("helm seat rest: %s is %s, but the roster could not be read, so "
              "whether a seat holds %s now is UNKNOWN; check `helm seat list` "
              "before you rely on it; %s." % (seat, phrase(rec), seat,
                                              end_hint(seat)))
        return 0
    if held is False:
        # RECORDED, BUT NEVER SAID TO PAUSE A SEAT IT CANNOT SEE (the ruling
        # on N1 of CURE4): a released or not-yet-joined name may be rested
        # ahead of the seat that takes it, and a renamed seat is rested by
        # the name it holds now.
        print("helm seat rest: %s is %s, but no seat holds %s right now, so "
              "this rest pauses the seat that next joins as %s and nothing "
              "before that. A seat that was renamed away from %s is rested by "
              "the name it holds now (`helm seat list`); %s."
              % (seat, phrase(rec), seat, seat, seat, end_hint(seat)))
        return 0
    print("helm seat rest: %s is %s. Nothing wakes it and no work is offered "
          "to it; %s." % (seat, phrase(rec), end_hint(seat)))
    return 0
