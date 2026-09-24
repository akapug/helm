#!/usr/bin/env python3
"""Helm's own Stop dispatch ALLOWED. Corroboration only — never acceptance.

WHAT THIS RECORDS, AND THE NAME HAD TO CHANGE. An earlier revision called
this a completion stamp and let it prove a turn finished. IT CANNOT, and the
reason is structural rather than a bug to fix: helm's aggregated allow is the
point where every HELM Stop handler has answered and none vetoed, and helm
sees its own hook dispatch and nothing after it. A foreign plugin Stop hook
registered outside helm's dispatcher can veto after helm has allowed, the
harness continues the turn, and this stamp would sit on disk asserting a
completion that never happened.

So the record means exactly one thing: AT TIME T, HELM'S STOP DISPATCH
ALLOWED. It is not in `ownership.TERMINAL_RESPONSE_SOURCES`, it can support a
HOLDING answer as corroboration, and it can never carry a row to DARK. The
source that does prove a terminal response is `helm/turnresponse.py`, which
reads the session transcript.

WHY IT IS STILL WRITTEN. A seat whose transcript cannot be read — a family
that writes none, a session id the roster does not carry — has no terminal
response evidence at all, and this is then the only thing standing between
that seat and an unqualified UNKNOWN. Corroboration that cannot revert a row
is cheap and occasionally the whole answer.

WHERE IT IS CALLED FROM, AND WHY NOT ANYWHERE EASIER. At the AGGREGATED allow
in `hookrun.run_event`: the point after every Stop handler has answered.
Not at hook invocation (the hook fires on refusals too), and not inside a
single guard (`hookrun` keeps running later handlers after one returns 2, so
that guard's allow is not the dispatch's).

THE BINDING IS RECONCILED AT THE WRITER, not asserted by it. A stamp claims
"this seat, this session", and both halves arrive from the environment and
the hook payload — two sources that can disagree after a rename, a pane
reuse, or a seat whose roster row moved on. A disputed or unreconcilable
binding WRITES NOTHING, because a stamp filed under the wrong seat is worse
than no stamp: it is another seat's liveness wearing this one's name.
"""
import json
import os
import time

from . import home, pk

#: This name is deliberately ABSENT from
#: `ownership.TERMINAL_RESPONSE_SOURCES`. Kept beside the writer so the two
#: cannot drift apart silently, and worded so a reader who meets it in a
#: rendered sentence learns what it actually observed.
SOURCE = "helm Stop-dispatch allow"


def stamp_dir():
    return home.surface_dir("TURNSTAMP_DIR", "turnstamps",
                            os.path.join(home.helm_home(), "turnstamps"))


def stamp_path(seat):
    """Use the same casefold-exact identity as the roster's other state files.

    Legacy raw-name stamps are deliberately not read as a fallback: two case
    spellings could hold different records. Missing evidence remains UNKNOWN
    until this writer records again; this path change proves nothing.
    """
    if not seat:
        return None
    from .seats_common import _seat_key
    return os.path.join(stamp_dir(), "%s.json" % _seat_key(seat))


def reconcile(seat, session, roster_rows=None):
    """-> (canonical_seat, session, None) or (None, None, why-not).

    THE WRITER'S OWN CHECK, and it refuses rather than guesses. The seat name
    comes from the environment and the session from the hook payload; this
    asks the ROSTER whether those two belong together right now. Three ways
    it refuses, and each one would otherwise file a record under a seat that
    did not earn it:

      * the roster cannot be read, or names this seat ambiguously — the
        binding is unverifiable, not merely unusual;
      * there is no roster row for the name — nothing says this session
        answers to it;
      * the row's CURRENT session is a different one. A pane that was reused,
        or a seat whose incarnation moved on, still carries the old name in
        the environment. Writing then would stamp the live seat's file with a
        dead incarnation's dispatch.
    """
    if not seat or not session:
        return None, None, "a stamp needs both a seat and a session"
    if roster_rows is None:
        try:
            from .seats_roster import roster_checked
            roster_rows, unreadable = roster_checked()
        except Exception as e:                      # noqa: BLE001
            return None, None, "roster unreadable (%s: %s)" % (
                e.__class__.__name__, e)
        if unreadable:
            roster_rows = None
    if roster_rows is None:
        return None, None, "the roster did not read; the binding is unverified"
    try:
        from .seats_common import canonical_seat
        key, kerr = canonical_seat(seat, roster_rows)
    except Exception as e:                          # noqa: BLE001
        return None, None, "identity unresolved (%s: %s)" % (
            e.__class__.__name__, e)
    if kerr:
        # THE VARIANT LIST IS NOT RE-EMITTED HERE, deliberately.
        # `canonical_seat`'s message interpolates the roster's OWN key
        # spellings, which are unvalidated at the join seam; passing it
        # through would make this module a rendering site for hostile roster
        # content that reaches an operator's terminal. This module reads the
        # roster to RESOLVE a binding and emits nothing the roster supplied —
        # the repair verb is where the variants belong.
        return None, None, ("the roster holds case-variant rows for this "
                            "seat; repair with `helm chat seat rename` "
                            "before this source can be bound")
    if key is None:
        return None, None, "no roster row claims this seat name"
    row = roster_rows.get(key) or {}
    current = str(row.get("session") or "")
    if not current:
        return None, None, "the roster row carries no current session"
    if current != str(session):
        return None, None, "the roster's current session for this seat is a " \
                           "different one; this payload is not its live turn"
    return key, str(session), None


def record(seat, session, now=None, roster_rows=None):
    """Write ONE Stop-dispatch-allow stamp. -> (path, None) or (None, why-not).

    SESSION-BOUND, because a seat name outlives the process that answered to
    it. A stamp with no session cannot later be told apart from one left by a
    previous incarnation, and the census would read a dead seat's last
    dispatch as the live one's.
    """
    key, session, err = reconcile(seat, session, roster_rows=roster_rows)
    if err:
        return None, err
    path = stamp_path(key)
    if not path:
        return None, "seat name yields no usable stamp path"
    row = {"seat": str(key), "session": str(session),
           "allowed_at": float(now if now is not None else time.time()),
           "source": SOURCE}
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        pk.atomic_write(path, json.dumps(row, sort_keys=True) + "\n")
    except OSError as e:
        return None, "%s: %s" % (e.__class__.__name__, e)
    return path, None


def last_allowed(seat, session=None):
    """-> (row, err). `row` is None when nothing is stamped for this seat.

    THE THREE ANSWERS STAY APART. A missing file is "this source has no
    record" (the seat may never have run a Stop hook at all — every
    proxy-family seat is in that position). An unreadable or malformed file
    is an ERROR. Only a well-formed row is evidence.

    A `session` argument NARROWS, and the CENSUS IS REQUIRED TO SUPPLY IT
    from the verified roster row: a stamp from a different session is not
    this seat-incarnation's dispatch, and is reported as no record rather
    than quietly accepted. Reading with `session=None` answers "what is on
    disk under this name", which is a repair question, not a liveness one.
    """
    path = stamp_path(seat)
    if not path:
        return None, "seat name yields no usable stamp path"
    try:
        with pk.open_regular(path, encoding="utf-8") as fh:
            row = json.load(fh)
    except FileNotFoundError:
        return None, None
    except (OSError, ValueError) as e:
        return None, "%s: %s" % (e.__class__.__name__, e)
    if not isinstance(row, dict):
        return None, "stamp is not an object"
    ts = row.get("allowed_at")
    if not isinstance(ts, (int, float)):
        return None, "stamp carries no numeric allowed_at"
    if session is not None and str(row.get("session") or "") != str(session):
        return None, None
    return row, None
