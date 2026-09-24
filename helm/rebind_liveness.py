"""The rebind door's LIVE-READER rung: a row somebody is measurably reading
right now may not be moved off them on the default path.

WHY THIS IS NOT THE EVIDENCE GATE ALREADY IN `dispatches.rebind`. That gate
asks about the recipient's CAPACITY — is proxywatch starving it, has its
context window run out — and answers UNKNOWN for a perfectly healthy seat. Its
refusal therefore says the recipient "is not measurably unable to act" and
advertises the override in the next clause, so a seat reading it learns how to
take the row before it learns whose work the row is. This rung asks the other
question, the one the capacity gate cannot phrase: is anybody READING this row
at this moment. When the answer is yes it names the reader, when the reader was
last measured, and the state the row is in, so the override that follows is a
decision about a person rather than about a missing measurement.

LIVENESS IS NOT ROUTING AUTHORITY, and this is the one direction that premise
leaves open. Alive-and-idle says a seat CAN take work, never that anyone MAY
route work to it, so a liveness reading may not authorize a move. Here the
same reading only ever REFUSES one, which is why it is safe to compose the
recipient's liveness with a routing decision at all.

THREE SURFACES FOR ONE QUESTION, in the order `idle_dispatch` already ranks
them, because a reading seat can be visible on any one of them and invisible
on the others:

  * a live pane, through the seat facade's own classifier. This is the
    positive reading `idle_dispatch._live_pane` exists for: presence measures
    TIME SINCE A TOOL BOUNDARY, which grows without bound for a live seat
    sitting between turns, so a working reader goes quiet for the same reason
    a departed one does and only the pane can tell them apart.
  * an armed inbox beacon. A seat with a live waiter is reachable and parked,
    which is exactly the posture of somebody between reading and minting.
  * a tool boundary inside the freshness window. The weakest of the three and
    the only one a caller can produce without a pane, so it is asked last.

A recipient none of the three can see is ABSENT — the STRANDED evidence
`idle_dispatch` publishes — and this rung says nothing about such a row: the
capacity gate above it keeps whatever answer it had.

THE READING HALF IS DELIBERATELY NARROW. Only a row whose delivery was
OBSERVED counts as being read, because that is the one fact the ledger holds
about whether the brief reached the recipient at all. A recipient who has
POSTED about the row is a second, stronger reading and is NOT implemented
here: it needs a chat search keyed on the row, and a wrong answer from a
fuzzy text match would refuse a rebind that should pass. Named so the gap is
a decision rather than an oversight.
"""

# The refusal's two doors. `--force --reason` is deliberately spelled the same
# way the capacity gate above spells it, because they are the same override.
_REFUSAL = (
    "rebind REFUSED: @%(who)s is READING this row — %(activity)s — and the "
    "row is %(state)s. A move now mints a second reader against work somebody "
    "is holding, and the reader loses the row mid-read without being told. "
    "Two doors: WAIT for the verdict, which discharges the row by itself, or "
    "--force --reason '<why the live reader must lose it>'. The override and "
    "@%(who)s are recorded on the ledger event, so the reader can see who "
    "took it.")

# Recorded on the cancel event when the override is used. The overridden
# recipient is in it BY NAME: the cancel already says where the obligation
# went, and the party who needs this sentence is the one it was taken from.
_OVERRIDE = "OVERRIDE took it from @%(who)s (%(activity)s); %(why)s"

# The pane states that are a POSITIVE liveness reading, as an ALLOWLIST for
# the same reason `idle_dispatch` keeps one: under a blocklist an unrecognised
# state would read as live and refuse a rebind that should pass, and this rung
# must fail toward letting the capacity gate decide.
_LIVE_PANE_STATES = ("IDLE", "RUNNING", "LIVE")

# Presence buckets that count as a recent tool boundary. `quiet` and `absent`
# are what admits a row to the stranded sweep in the first place, so neither
# may count as activity here or this rung would refuse every rebind.
_ACTIVE_PRESENCE = ("fresh",)

# How much of a pane classifier's evidence sentence survives into the fact.
_EVIDENCE_CHARS = 60


def _live_pane(recipient):
    """"" or a short fact naming a live pane for this recipient."""
    try:
        from helm import seat
        row = seat.seat_liveness(recipient) or {}
    except Exception:
        return ""
    if not isinstance(row, dict):
        return ""
    state = str(row.get("state") or "")
    if state not in _LIVE_PANE_STATES:
        return ""
    # The evidence sentence the classifier writes is unbounded and this fact
    # is recorded inside a capped cancel reason, so a long one would push the
    # caller's own words off the end of the record. Cut, and SAY it was cut.
    ev = str(row.get("evidence") or "no evidence")
    if len(ev) > _EVIDENCE_CHARS:
        ev = ev[:_EVIDENCE_CHARS] + "... (cut)"
    return "pane %s (%s)" % (state, ev)


def _armed_beacon(recipient, proc_dir):
    """"" or a short fact naming an armed inbox beacon.

    FAIL-QUIET: `beacon_procs` answers with trouble text rather than raising
    when the process table cannot be listed, and an unlistable process table
    is not evidence that nobody is reading. It is simply no fact, and the next
    surface is asked."""
    try:
        from . import seats
        from . import procid
        pids, _trouble = seats.beacon_procs(
            recipient, proc_dir=procid.proc_root(proc_dir))
    except Exception:
        return ""
    if not pids:
        return ""
    return "armed inbox beacon (pid %s)" % pids[0]


def _recent_boundary(recipient):
    """"" or a short fact naming a tool boundary inside the freshness window."""
    try:
        from . import seats
        seen = seats.last_seen(recipient)
        bucket = seats.presence_of(seen)
    except Exception:
        return ""
    if bucket not in _ACTIVE_PRESENCE:
        return ""
    return "presence %s (last tool boundary inside the window)" % bucket


def recipient_activity(recipient, proc_dir=None):
    """"" or ONE short fact that the recipient is measurably live.

    Short by construction: the fact is carried into a cancel reason the ledger
    caps, so a long evidence string would push the caller's own words off the
    end of the record."""
    who = str(recipient or "").strip()
    if not who:
        return ""
    return (_live_pane(who) or _armed_beacon(who, proc_dir)
            or _recent_boundary(who))


def reading_state(row):
    """"" or the row's state, when the row is one a recipient can be reading.

    An OPEN row whose delivery was OBSERVED is the shape: the brief is proven
    to have reached the recipient and no verdict has closed it. Every other
    status is either not yet delivered, already answered, or parked, and none
    of those is a read in progress."""
    if not isinstance(row, dict) or str(row.get("status") or "") != "open":
        return ""
    ref = str(row.get("delivery_ref") or "").strip()
    if str(row.get("delivery") or "") != "observed" and not ref:
        return ""
    return "PENDING VERDICT with delivery observed"


def live_reader(row, proc_dir=None):
    """None, or {who, activity, state} for a recipient reading this row now.

    BOTH HALVES ARE REQUIRED. A live seat holding a row nobody delivered has
    nothing to lose by the move, and an undelivered-to absent seat is the
    stranded case the capacity gate is built for. Only the conjunction is the
    harm this rung exists to refuse."""
    state = reading_state(row)
    if not state:
        return None
    who = str((row or {}).get("recipient") or "").strip()
    activity = recipient_activity(who, proc_dir=proc_dir)
    if not activity:
        return None
    return {"who": who, "activity": activity, "state": state}


def refusal(fact):
    """The refusal text for a live reader, naming the reader and the evidence."""
    return _REFUSAL % fact


def override_reason(fact, why):
    """The reason to record when --force moves a row off a live reader."""
    return _OVERRIDE % {"who": fact["who"], "activity": fact["activity"],
                        "why": str(why or "").strip()}
