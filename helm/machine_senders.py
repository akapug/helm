"""The subsystem labels helm posts machine rows under, and what they may reach.

A plain row from one of these is machine state: proxy health, beacon
reachability, autocompact notices, gate queue and gc summaries. It stays
readable in its room and is PULLED, never pushed: the tool-boundary hook, the
stop-guard and the pending counts skip it unless it is addressed to the seat
(an @mention, a reply, a DM, @all). A plain row from a person or a seat,
the owner's included, still reaches its home room's seats between tool calls.

THE SET IS WHAT HELM ACTUALLY POSTS UNDER, not a guess at what is noisy.
Every literal `who=` at a `post`/`dm` call site in helm/ is here, and an arm
in tests/test_delivery_truth.py walks the tree and fails on a label this set
does not carry, so a new subsystem cannot quietly become a pushed row.
NOT HERE, ON PURPOSE: `agent`, the name `chat.whoname` falls back to for any
poster with neither a seat name nor a session. A person posting from a bare
shell gets it too, and an unknown poster reaching the room is the safe
direction, so a subsystem that posts must name itself (the worktree gc
summary posts as `worktree-gc`). A row restored from the journal needs no
entry: `deliverable` drops every restored row before it reads the sender.

THE OWNER IS NEVER INFERRED. `owner_rail` is the one reading of "the owner
posted this": an owner rail origin (seats.OWNER_RAILS, stamped only by his
own doors) AND one of his names (owner_names). Either alone is not him.
"""

SUBSYSTEMS = frozenset((
    "autocompact", "beacons", "chat-durability", "dispatches", "faucet-watch",
    "gate-canary", "helm-rearm",
    "idle-dispatch", "owed-bot", "plan-prompt", "preread", "prompt-stall",
    "proxy-fork-watch", "proxywatch", "resume-turn", "resume-turn/recovery",
    "resume-turn/wake", "rogue-watchdog", "scratch-gc", "silent-drop",
    "stale-bot", "upstream-watch", "watchdog", "worktree-gc",
))


def is_machine(name):
    """True when `name` is a subsystem label rather than a person or a seat."""
    return str(name or "").strip().casefold() in SUBSYSTEMS


def owner_rail(row):
    """True for a row the owner posted through one of his own doors.

    The idle beacon does not wake on the owner's plain home-room row, and a
    pass that crossed it would leave the next tool boundary skipping it as
    already woken: a ruling such as "hold all lands until I say" would reach
    no seat. So the beacon holds such a row for the hook, as it holds a
    muted room's rows."""
    from .seats import OWNER_RAILS
    from .seats_identity import owner_names
    return row.get("origin") in OWNER_RAILS \
        and str(row.get("from") or "").lower() in owner_names()
