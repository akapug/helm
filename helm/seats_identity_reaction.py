#!/usr/bin/env python3
"""helm seats — the wake for a burst of reactions: which reactions group, and
the one delivery body a grouped burst renders as.

MOVED VERBATIM OUT OF `seats_identity`, which reached 1011 lines of its
1000-line budget when task/4019 added the wake tier's addressed-elsewhere rule
to `deliverable`. These two helpers answer no identity question: the
delivery loop (`seats_delivery`) calls them after `deliverable` has already
said a reaction is FOR the seat. `seats_identity` re-exports both and declares
the move in `_OWNER_NAMES`, so `seats.py`, `seats_delivery` and the tests keep
their import path.

Downward edges only: this module stands on `chat` and `seats_common`, never on
the module it left.
"""
from . import chat
from .seats_common import MAX_BYTES, _clip, _scrub, recipient_matches


def _same_reaction_target(a, b):
    """Same canonical target as chat's signed reaction + `_react_state`:
    `(tts, tfrom)`. Reactions do not carry a room ordinal, rendered text, or a
    target row id, so the beacon composes with their existing durable identity
    rather than inventing a second grouping key."""
    return a.get("tts") == b.get("tts") \
        and recipient_matches(a.get("tfrom"), b.get("tfrom"))
def _reaction_wake_body(rows, room="main"):
    """One bounded, honest delivery body for a contiguous reaction burst.

    The target comes FIRST, so clipping can never erase which row woke the seat.
    Include as many reactor+emoji pairs as fit, then name the omitted count and
    the exact pull surface — never silently truncate identities behind an
    ellipsis or point a foreign-room wake at main."""
    target = rows[0]
    prefix = "%s@%s ← " % (
        chat._dsan(target.get("tfrom") or "?"),
        _scrub(str(target.get("tts") or "?")))
    acts = ["%s %s %s" % (
        chat._dsan(r.get("from") or "?"),
        "un-reacted" if r.get("un") else "reacted",
        _scrub(str(r.get("react") or "?"))) for r in rows]
    pull = ("helm chat read --dm" if room.startswith(chat.DM_PREFIX)
            else "helm chat read" if room == "main"
            else "helm chat read --room %s" % _scrub(str(room)))
    shown = []
    for i, act in enumerate(acts):
        rest = len(acts) - i - 1
        suffix = " (+%d more reactions — %s)" % (rest, pull) if rest else ""
        candidate = prefix + "; ".join(shown + [act]) + suffix
        if len(candidate.encode("utf-8")) > MAX_BYTES:
            break
        shown.append(act)
    rest = len(acts) - len(shown)
    suffix = " (+%d more reactions — %s)" % (rest, pull) if rest else ""
    return _clip(prefix + "; ".join(shown) + suffix)
