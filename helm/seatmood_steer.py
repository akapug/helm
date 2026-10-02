#!/usr/bin/env python3
"""A seat's measured mood, fed back to the seat as one steer (task/3899).

MOOD IS AN INPUT, NOT ONLY A DISPLAY. When a seat's own measured state turns
`grinding` or `stuck`, the next turn `helm inject` builds for it carries ONE
line on the reflex lane naming the measured reason and the cheapest next move:

    MOOD: helm measures you stuck: the same refusal by author-gate 4 times in
    a row. Cheapest next move: stop retrying; do what the refusal asks, or
    hand the row back. ...

AT MOST ONCE PER STATE CHANGE PER SEAT. The latch (`<seat>.steer` beside the
mood files) holds the state the last measured turn saw. A steer fires only
when the state just measured is grinding or stuck AND differs from it, so a
seat that stays stuck hears it once, a seat that goes grinding -> stuck hears
the worse news, and a seat that recovers re-arms it. A walled, idle or
blocked-on-owner seat is never steered: a wall is not friction, and an owner
card is the owner's to answer.

WHAT IT MEASURES AT A TURN'S START, and why that is the judge's own answer.
It reads the friction ledger (the refusals the turn's reflex counters already
read), the seat's hook counters, its family's wall, its open decision cards
and a proxy seat's context fill, and hands them to `seatmood.judge`. It skips
the progress sources: the judge starts the quiet clock at the later of the
last progress event and the turn's open, and a turn opening now reads zero
quiet whatever the progress was, so the git and ledger folds behind progress
would change nothing here. It measures at most every STEER_EVERY_S per seat,
so a turn inside that window pays one small file read.

THE SAME LATCH DISCIPLINE AS THE HOURLY ASK. `turn` reads only and returns
the lines plus a payload; `commit` writes the latches, and `helm inject`
calls it from `_apply_mutations` only once the turn is READY, so a turn that
never reached the seat leaves every latch where it was. The steer invites the
seat's check-in itself, so the turn that carries it records the hourly ask as
asked. Each reading taken here is remembered for the roster listing
(`seatmood_surface.remember`).
"""
import os
import time

from . import pk

STEER_ID = "steer:seat-mood-state"
#: How often a seat's mood is measured at its turn start.
STEER_EVERY_S = 5 * 60
STEERED = ("grinding", "stuck")
#: The cheapest next move for each cause the judge can give a grinding or
#: stuck state (`seatmood.FRICTION_CAUSES`).
MOVES = {
    "loop:refusal": "stop retrying; do what the refusal asks, or hand the "
                    "row back",
    "loop:rerun": "stop re-running it; change the input first, or hand the "
                  "row back",
    "loop:failing": "read the last error whole before the next call",
    "refusals": "read the refusal's own door (what it says to do) or hand "
                "the row back",
    "quiet": "post where the work stands, or hand the row back",
    "stalled": "commit what you have, or hand the row back",
    "context": "hand off or compact before the next large read",
}
DEFAULT_MOVE = "say where it stands, or hand the row back"
TAIL = (" If someone must act: `helm seat mood set %s --blocker \"...\"` "
        "reaches your steward.")
#: The reason is clipped so the move and the tail fit the reflex lane's cap.
REASON_MAX = 110


def latch_path(seat):
    from .seatmood import _file
    return _file(seat, ".steer")


def latch(seat):
    """{seat, state, at} the last measured turn of `seat` saw, or None."""
    from .seatmood import STATES, _num
    path = latch_path(seat)
    got = pk.read_json(path, None) if path else None
    if not isinstance(got, dict) or _num(got.get("at")) is None:
        return None
    state = got.get("state")
    return {"seat": got.get("seat"), "at": got["at"],
            "state": state if state in STATES else None}


def reading(seat, session, now):
    """`seat`'s mood as its own turn opens (see the module docstring)."""
    from . import seatmood, seatmood_signals as sig
    k = sig.key(seat)
    refusals, why = sig._refusals({k}, now)
    unread = [why] if why else []
    cards, why = sig._cards({k})
    if why:
        unread.append(why)
    fam = sig.family(seat)
    raw = {"seat": seat, "refusals": refusals.get(k, []),
           "counters": sig._counters(session, now),
           "idle": {"state": "BUSY", "idle_s": None, "turn_opened": now,
                    "why": "its turn is opening"},
           "progress": None, "wall": sig.wall(seat, fam, sig._flags(), now),
           "card": cards.get(k), "context_pct": sig.context_pct(seat, fam),
           "self": seatmood.self_report(seat),
           "unread": [sig.text(u) for u in unread]}
    return seatmood.judge(raw, now)


def line(m):
    """The steer for one measured reading."""
    reason = str(m["reason"])
    if len(reason) > REASON_MAX:
        reason = reason[:REASON_MAX - 1].rsplit(" ", 1)[0] + "…"
    return ("MOOD: helm measures you %s: %s. Cheapest next move: %s.%s" % (
        m["state"], reason, MOVES.get(m.get("cause"), DEFAULT_MOVE),
        TAIL % m["state"]))


def turn(session=None, now=None):
    """([(line, reflex id)], commit payload) for this process's seat at its
    turn start, or None when there is nothing to say or record. READS ONLY.
    The roster is read only when the ask or the measure is due."""
    from . import friction, seatmood
    now = time.time() if now is None else now
    name = friction._seat(session)
    if not name:
        return None
    held = latch(name)
    measure = held is None or now - held["at"] >= STEER_EVERY_S
    ask = seatmood._due(name, now)
    if not (measure or ask):
        return None
    seat, err = seatmood._roster_key(name)
    if err or not seat:
        # throttle the roster read for a name no seat holds
        return ([], {"steer": {"seat": name, "state": None, "at": now}}) \
            if measure else None
    lines, payload = [], {}
    if measure:
        m = reading(seat, session, now)
        payload["steer"] = {"seat": seat, "state": m["state"], "at": now}
        payload["measured"] = m
        if m["state"] in STEERED and m["state"] != (held or {}).get("state"):
            lines.append((line(m), STEER_ID))
            payload["asked"] = (seat, now)
    if ask and "asked" not in payload and seatmood._due(seat, now):
        lines.append((seatmood.ASK_LINE, seatmood.ASK_ID))
        payload["asked"] = (seat, now)
    return (lines, payload) if lines or payload else None


def commit(payload):
    """Write the latches `turn` staged. Fail-open: a lost latch measures or
    asks again on a later turn."""
    from . import seatmood, seatmood_surface
    payload = payload or {}
    if payload.get("asked"):
        seatmood.mark_asked(*payload["asked"])
    steer = payload.get("steer")
    path = latch_path(steer["seat"]) if steer else None
    if path:
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            pk.write_json(path, steer)
        except OSError:
            pass
    if payload.get("measured") and steer:
        seatmood_surface.remember(payload["measured"], steer["at"])
