#!/usr/bin/env python3
"""helm seat mood set's CHECK-IN: a rating, a blocker and a win (task/3899).

THE SHAPE is a 15five-style pulse check-in, cut to what a seat can answer in
one turn: beside its one word, a seat may give

  --rating   how its work is going, 1 (worst) to 5 (best), the scale the
             console draws red to green
  --blocker  one line naming what stops it
  --win      one line naming what went well

A BLOCKER IS ANSWERED, NOT FILED. It is posted ONCE to #seats through
`seatevents`, with the seat's STEWARD @mentioned, and an @mention wakes that
steward's seat in any room. The steward is the lead of the project the seat
serves (`seatevents.STEWARDS["project-seats"]`), else the build-lanes steward
(the integrator) when the seat serves no project, its project names no single
lead, or the seat is that lead. The same blocker words, re-said, post nothing:
the event's identity is the seat and its words, so a seat that reports one
conflict many times costs its steward one row. A post that fails stays owed
in the seat-events ledger and goes out on the next seat event.

A WIN is recorded only: it rides the check-in and the console's hover.

THE RATING JOINS THE HONESTY SIGNAL (`diverges`): a seat that rates its work
4 or 5 while helm measures it stuck or grinding is divergent. With no rating,
its word decides, as before (`seatmood.POSITIVE`).
"""
import hashlib
import unicodedata

RATINGS = (1, 2, 3, 4, 5)
#: A rating at or above this says the work is going well.
WELL_AT = 4
LINE_MAX = 200
KIND = "blocker"


def _line(value, flag):
    """(the line, refusal) for one --blocker / --win value."""
    if value is None:
        return None, None
    raw = str(value)
    text = " ".join(raw.split())
    if not text or len(text) > LINE_MAX or any(
            unicodedata.category(c) in ("Cc", "Cf", "Zl", "Zp") for c in raw):
        return None, ("%s is one printable line of %d characters or fewer"
                      % (flag, LINE_MAX))
    return text, None


def check(rating=None, blocker=None, win=None):
    """({rating, blocker, win}, None) or (None, refusal). `rating` is an int
    or the one digit a command line carries."""
    r = rating
    if isinstance(r, str) and len(r) == 1 and r.isdigit():
        r = int(r)
    if r is not None and (isinstance(r, bool) or r not in RATINGS):
        return None, "--rating is a whole number from 1 to 5 (5 is best)"
    b, refusal = _line(blocker, "--blocker")
    if refusal:
        return None, refusal
    w, refusal = _line(win, "--win")
    if refusal:
        return None, refusal
    return {"rating": r, "blocker": b, "win": w}, None


def checkin(said):
    """The check-in beside the word: {rating, why, blocker, win}, or None."""
    if not isinstance(said, dict):
        return None
    return {k: said.get(k) for k in ("rating", "why", "blocker", "win")}


def says_well(said):
    from .seatmood import POSITIVE
    rating = said.get("rating")
    if rating in RATINGS and not isinstance(rating, bool):
        return rating >= WELL_AT
    return said.get("word") in POSITIVE


def diverges(state, said, now):
    """A fresh check-in that says the work is going well, beside a measured
    stuck or grinding."""
    from .seatmood import SELF_FRESH_S, _num
    if not isinstance(said, dict) or state not in ("stuck", "grinding"):
        return False
    at = _num(said.get("at"))
    return at is not None and now - at <= SELF_FRESH_S and says_well(said)


# ------------------------------------------------------------------ the post

def subject(seat):
    """(component, project) whose steward answers `seat`'s blocker."""
    from . import seatevents
    project = seatevents.project_of([seat]).get(seat)
    if project:
        lead, _why = seatevents.steward("project-seats", project)
        if lead and str(lead).casefold() != str(seat).casefold():
            return "project-seats", project
    return "build-lanes", None


def _ident(seat, blocker):
    words = " ".join(str(blocker).casefold().split())
    return hashlib.sha256(("%s\0%s" % (str(seat).casefold(), words))
                          .encode("utf-8")).hexdigest()[:16]


def post_blocker(row, now=None):
    """Post `row`'s blocker to #seats once -> (outcome, text).

    outcome: `posted` (text is the row sent now), `already` (these words were
    posted before), `owed` (claimed, the room post failed, retried on the
    next seat event) or `failed` (the seat-events ledger could not be
    written; nothing was claimed)."""
    from . import pk, seatevents
    seat, blocker = row["seat"], row["blocker"]
    component, project = subject(seat)
    said = row["word"] + (" %d/5" % row["rating"] if row.get("rating") else "")
    body = ("%s is blocked: %s (it says %s). Answer it here; `helm seat mood "
            "%s` shows what helm measures." % (seat, blocker, said, seat))
    key, ident = "mood:%s" % str(seat).casefold(), _ident(seat, blocker)
    got = seatevents.announce([seatevents.event(
        component, key, KIND, ident, body, project=project)], now=now)
    mine = [t for t in got["posted"] if body in t]
    if mine:
        return "posted", mine[0]
    ledger = pk.read_json(seatevents.ledger_path(), default={})
    rec = ((ledger.get("rows") or {}) if isinstance(ledger, dict) else {}
           ).get("%s|%s|%s" % (key, KIND, ident))
    if not isinstance(rec, dict):
        return "failed", None
    return ("already" if rec.get("chat") else "owed"), None


def post_line(outcome, text):
    """The CLI's one line for `post_blocker`'s answer."""
    return {
        "posted": "blocker posted to #seats: %s" % text,
        "already": "blocker already posted to #seats: the same words post "
                   "once, and its steward has the row",
        "owed": "blocker owed: the #seats post failed and goes out on the "
                "next seat event",
        "failed": "blocker NOT posted: the seat-events ledger could not be "
                  "written; it is recorded with the check-in",
    }[outcome]
