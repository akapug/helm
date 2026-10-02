"""The subsystem labels helm posts machine rows under, what they may reach,
and which of them are signed.

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

EACH LABEL HAS A SIGNING CLASS (task/3851), and `chat.post` reads it. A signed
chat send costs the chat node about 25 CPU-seconds of proving (measured on
the owner's laptop), and automation made about a fifth of the signed sends. A row earns that only when a seat must be able to check who
sent it. The readers that check a chat row's receipt are the dispatch
verdict attestation (a VERDICT row, which the seat giving the verdict posts
under its own name) and `helm chat verify`, which reports an unsigned row as
unsigned; a land receipt is a coordination turn (landreq), not a chat row.
No reader checks a status row's receipt, so a status label posts unsigned,
as pressure-watch always did. A caller's explicit `sign=` still wins.
"""

# THE SIGNING CLASSES, one arm each: the `sign` chat.post uses for the class.
VERIFY = "verify"   # a seat acts on it: signed whenever a signer is set up
STATUS = "status"   # machine state a reader pulls: never signed
SIGNING = {VERIFY: None, STATUS: False}

SENDERS = {
    # VERIFY: a seat acts on these rows, so it must be able to check them.
    "auto-land": VERIFY,        # a train's INTENT (its veto window), LANDED,
                                # REFUSED, and the land's owed console walk
    "dispatches": VERIFY,       # a verdict's nudge to its lander
    # STATUS: state and alarms. The rows are read; no receipt is checked.
    "autocompact": STATUS,      # a compaction fired; hot, dead, silent seats
    "beacons": STATUS,          # seat reachability census
    "chat-durability": STATUS,  # log-flush watchdog alarm
    "chat-signing": STATUS,     # signing watchdog alarm: signing is what broke
    "checkout-watch": STATUS,   # a stray write on the shared checkout
    "faucet-watch": STATUS,     # a cell's faucet is low
    "gate-canary": STATUS,      # the gate canary failed
    "helm-rearm": STATUS,       # stale beacons and web re-armed at a new head
    "idle-dispatch": STATUS,    # a dispatch's recipient is quiet; a wake
    "owed-bot": STATUS,         # unanswered fix debt, re-delivered
    "plan-prompt": STATUS,      # a seat frozen at a plan prompt was advanced
    "preread": STATUS,          # a pre-read is ready (not a verdict)
    "pressure-watch": STATUS,   # CPU pressure episode
    "prompt-stall": STATUS,     # a seat waits on the owner at a prompt
    "proxy-fork-watch": STATUS,  # upstream debt in the proxy fork
    "proxywatch": STATUS,       # proxy health, codex pace, pool walls, budget
    "remote-relay": STATUS,     # relay notices about remote sessions
    "resume-turn": STATUS,      # a turn restarted after a compaction
    "resume-turn/recovery": STATUS,
    "resume-turn/wake": STATUS,
    "rogue-watchdog": STATUS,   # rogue local compute alarm
    "scratch-gc": STATUS,       # scratch pressure wake
    "seat-events": STATUS,      # #seats: one seat event, @mentioning its
                                # component's steward (seatevents)
    "seat-rehome": STATUS,      # a rehomed seat's wake
    "silent-drop": STATUS,      # an empty completion alarm
    "stale-bot": STATUS,        # stale work digest, task hygiene pings
    "teams": STATUS,            # a team's share of a family changed
    "train-blame": STATUS,      # which car a red train names (the gate holds
                                # the evidence)
    "upstream-watch": STATUS,   # what a new Claude Code release changed
    "watchdog": STATUS,         # a context window near its brick
    "worktree-gc": STATUS,      # worktree gc tallies
}
SUBSYSTEMS = frozenset(SENDERS)


def is_machine(name):
    """True when `name` is a subsystem label rather than a person or a seat."""
    return str(name or "").strip().casefold() in SUBSYSTEMS


def sign_for(name):
    """The `sign` chat.post uses for a row from `name` when its caller names
    none: its class's arm for a subsystem label, and None, the default, for
    a person or a seat."""
    cls = SENDERS.get(str(name or "").strip().casefold())
    return None if cls is None else SIGNING[cls]


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
