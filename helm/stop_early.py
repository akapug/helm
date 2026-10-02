#!/usr/bin/env python3
"""helm stop_early: the stop guard's owed-row check, said at the tool boundary
where a row first becomes owed (task/4019 slice C).

THE COST IT REMOVES. A stop-guard block is one more full-context model
request, made after the agent chose to stop. The owed-row rung
(`seats_stop_owed._owed_rung`) refuses an idle stop at EVERY idle turn while
a dispatch row addressed to the seat is owed and not worked, and nothing on
the tool-call path said so before the stop. Measured over one night, 50 of
235 integrator wakes were stop-guard blocks.

THE CURE. The same check, made at a tool boundary, said once per new owed
row: "you OWE <row>: triage, hold or answer it in this batch". The agent can
clear it inside work it is already doing, so the stop has nothing to refuse.

WHAT IT DOES NOT CHANGE. The stop guard keeps every block. This only adds a
line earlier; a row it names that is still owed at the stop is refused there
exactly as before. A failure anywhere here is silence, never an error at the
tool boundary, because the stop guard still asks the same question.

ONE PREDICATE. The rows are `dispatches.owed_to` over the resident's owed
frontier (`stopfacts.View.owed_pair`), the Stop rung's own predicate, and
each row is worded by `seats_stop_owed.owed_phrases`, so the whisper and the
block cannot name a row two ways. A reading that is not EXACT says nothing.

COST ON THE HOT PATH. This runs at every tool call that has no other
whisper. It reads the stop-facts file (one JSON parse) and keeps the import
of `helm.dispatches` (about 80 modules) off the path unless a frontier row
addressed to this seat has not been said yet.

LATCH. The ids already said, per session, in the toolwhisper latch under
RULE. A row whose id is not in that list fires the line; the commit records
the current set, so a row that leaves and comes back is said again.
`HELM_OWED_EARLY=0` turns the line off.
"""
from . import home, pk

RULE = "owed-early"
#: Rows named on the line; the rest are counted with the verb that lists them.
SHOWN = 2


def _said(session):
    from . import toolwhisper
    latch = pk.read_json(toolwhisper._latch_path(session), {}) or {}
    got = latch.get(RULE) if isinstance(latch, dict) else None
    return set(got) if isinstance(got, list) else set()


def text_for(rows):
    """The one line for these owed rows (oldest first)."""
    from .seats_stop_owed import owed_phrases
    said = owed_phrases(rows[:SHOWN])
    more = len(rows) - len(said)
    return ("[helm stop-guard, early] you OWE %d dispatch row%s, and an idle "
            "stop is refused while %s owed: triage, hold or answer %s in this "
            "batch. %s%s"
            % (len(rows), "" if len(rows) == 1 else "s",
               "it is" if len(rows) == 1 else "they are",
               "it" if len(rows) == 1 else "them", "; ".join(said),
               "; +%d more: helm dispatch list --mine --open" % more
               if more > 0 else ""))


def prepare(session, seat=None, agent=None, view=None):
    """{"session", "rule", "latch", "text"} for a seat that owes a row
    not yet said this session, else None. `seat` defaults to the roster's
    seat for the session; `agent` (the hook's subagent id) says nothing,
    because a subagent's boundary owes none of its lead's rows. Writes
    nothing; never raises."""
    try:
        if not session or agent \
                or (home.env("OWED_EARLY") or "").strip() == "0":
            return None
        if not seat:
            from .seats_roster import seat_for_session
            seat = seat_for_session(session)
        if not seat:
            return None
        from . import stopfacts
        from .seats_common import recipient_matches
        snap = (view or stopfacts.read()).owed_pair(seat)
        frontier, why = snap
        if why or not frontier:
            return None
        said = _said(session)
        if not any(recipient_matches(r.get("recipient"), seat)
                   and str(k) not in said for k, r in frontier.items()):
            return None
        from . import dispatches
        rows, why = dispatches.owed_to(seat, snap=snap)
        ids = [str(r.get("id")) for r in rows or ()]
        if why or not ids or not set(ids) - said:
            return None
        return {"session": session, "rule": RULE, "latch": sorted(ids),
                "text": text_for(rows)}
    except Exception:                       # noqa: BLE001 — see the docstring
        return None


def for_hook(session, seat=None, agent=None):
    """The standalone hook's line: prepare, latch, return the text or None."""
    got = prepare(session, seat, agent)
    if not got:
        return None
    try:
        from . import toolwhisper
        toolwhisper.commit_for_pair(got)
    except Exception:                       # noqa: BLE001 — said, unlatched
        pass
    return got["text"]
