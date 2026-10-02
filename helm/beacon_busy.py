"""Bounded proof that an un-beaconed seat is working, not unreachable.

The pane alone is not proof: a hung RUNNING pane retains the same tail. A
fresh same-session turn start may provisionally count for ten minutes before
its first call; afterward a recent call in that turn is required. An
unreadable record, a foreign session, or stale hook evidence cannot silence an
alarm.
"""

PROGRESS_S = 10 * 60


def proof(seat, now):
    """Reason for a RUNNING pane with recent same-session progress, else None."""
    from . import seat as seat_facade, seat_idle

    pane = seat_facade.seat_liveness(seat, repair=False)
    if pane.get("state") != "RUNNING" or "pane-tail" not in pane.get("evidence", ""):
        return None
    activity = seat_idle.reading(seat, now=now)
    call = activity.get("last_call")
    opened = activity.get("turn_opened")
    if activity.get("state") != seat_idle.BUSY or not pane.get("session") \
            or pane["session"] != activity.get("session") or opened is None:
        return None
    if call is not None and call >= opened \
            and 0 <= now - call <= PROGRESS_S:
        return "RUNNING pane and a tool call in its current session %s ago" % (
            int(now - call))
    if (call is None or call < opened) \
            and 0 <= now - opened <= PROGRESS_S \
            and (activity.get("turn_ended") is None
                 or activity["turn_ended"] < opened):
        return "RUNNING pane and a fresh turn opened %s ago (provisional)" % (
            int(now - opened))
    return None
