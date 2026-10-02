#!/usr/bin/env python3
"""Each seat's mood on the surfaces already read (task/3899).

TWO READERS, NEITHER OF WHICH MEASURES ON ITS OWN HOT PATH:

  `helm chat seats`, the roster listing agents read, prints one CELL per
  row (`cell`): the seat's latest measured state and how long ago it was
  measured, its own word (and rating), and `DIVERGES` when that word says
  the work is going well while the measure says stuck or grinding. The cell
  reads two small files per seat, so the listing costs what it did.

  The console's seats panel draws one dot per seat from `/api/roster/mood`
  (`roster_moods`, a full `seatmood.floor` behind the panel's 60-second side
  channel): coloured by the measured state, dim when the seat's last sign of
  work is older than an hour (`recency`), the reason on hover.

THE LATEST READING IS REMEMBERED by whoever measures (`remember`): every
`seatmood.measure` and `seatmood.floor`, and each seat's own turn start
(`seatmood_steer`). A reading older than SHOW_FOR_S is not today's mood and
the cell does not show it.

ONLY CHECKED TOKENS LEAVE BY THE CELL: a state from `seatmood.STATES`, a word
that is one seat-mood word, a rating from 1 to 5. A record that fails any of
those prints nothing.
"""
import os
import time

from . import pk

SUFFIX = ".measured"
#: A measured reading older than this is not shown as the seat's mood.
SHOW_FOR_S = 24 * 3600
#: (bucket, seconds below which a seat's last sign of work falls in it); the
#: last bucket is "older".
BUCKETS = (("now", 10 * 60), ("under 1 h", 3600), ("today", 24 * 3600))


def remember(m, now=None):
    """Keep `m` as its seat's latest measured reading. Fail-open."""
    from .seatmood import _file
    path = _file((m or {}).get("seat"), SUFFIX)
    if not path:
        return False
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        pk.write_json(path, {
            "seat": m["seat"], "state": m["state"], "score": m.get("score"),
            "reason": m.get("reason"), "cause": m.get("cause"),
            "at": time.time() if now is None else now})
    except (OSError, KeyError, TypeError):
        return False
    return True


def latest(seat):
    """{state, at, ...} the seat's latest measured reading, or None."""
    from .seatmood import STATES, _file, _num
    path = _file(seat, SUFFIX)
    got = pk.read_json(path, None) if path else None
    if not isinstance(got, dict) or got.get("state") not in STATES \
            or _num(got.get("at")) is None:
        return None
    return got


def cell(seat, now=None):
    """The `helm chat seats` row's mood cell, or "" (see the docstring)."""
    from .seatmood import _age, self_report
    from .seatmood_pulse import diverges
    now = time.time() if now is None else now
    m = latest(seat)
    if m and not 0 <= now - m["at"] <= SHOW_FOR_S:
        m = None
    said = self_report(seat)
    words = []
    if m:
        words.append("mood %s (%s ago)" % (m["state"], _age(now - m["at"])))
    if said:
        rating = " %d/5" % said["rating"] if said.get("rating") else ""
        words.append("says %s%s" % (said["word"], rating) if m else
                     "says %s%s (%s ago)" % (said["word"], rating,
                                             _age(now - said["at"])))
    if not words:
        return ""
    gap = m and diverges(m["state"], said, now)
    return " · " + ", ".join(words) + (": DIVERGES" if gap else "")


def recency(m, now):
    """(bucket, seconds since the seat's last dated sign of work or None).
    BUSY can be stale: use the latest of its turn's end, turn's open, last
    call, last progress event and last check-in."""
    s = m.get("signals") or {}
    marks = [s.get("progress_at"), s.get("turn_opened"),
             s.get("last_call"),
             pk.parse_ts_epoch(m.get("self_at")) if m.get("self_at") else None]
    idle_s = s.get("idle_s")
    if isinstance(idle_s, (int, float)) and not isinstance(idle_s, bool):
        marks.append(now - idle_s)
    marks = [t for t in marks if isinstance(t, (int, float))
             and not isinstance(t, bool)]
    if not marks:
        return "older", None
    age = max(0, now - max(marks))
    for bucket, below in BUCKETS:
        if age < below:
            return bucket, age
    return "older", age


def projection(m, now):
    """One seat's mood as the console's dot reads it."""
    c = m.get("checkin") or {}
    bucket, age = recency(m, now)
    return {"state": m["state"], "score": m.get("score"),
            "reason": m.get("reason"), "self": m.get("self"),
            "rating": c.get("rating"), "blocker": c.get("blocker"),
            "win": c.get("win"), "divergent": bool(m.get("divergent")),
            "recency": bucket, "age_s": age}


def roster_moods(now=None):
    """{seat: projection} for every live seat. Raises OSError when the
    roster cannot be read: that is never an empty floor."""
    from . import seatmood
    now = time.time() if now is None else now
    return {m["seat"]: projection(m, now) for m in seatmood.floor(now=now)}


def home_key():
    """What a process cache of `roster_moods` describes: this helm home."""
    from .seatmood import _state_dir
    return _state_dir()
