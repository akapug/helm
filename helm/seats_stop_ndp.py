#!/usr/bin/env python3
"""The Stop guard's non-distraction-protocol rung."""
from . import projscope
from .seats_cursor import _write_stop_latch
from .seats_stop_signals import _off, _stop_fp_path
from .seats_work_offer import _solo_load_candidate

NDP_LATCH = "stopndp"


def _ndp_gate(session, room, seat):
    """(block | None, warn | None) for the NON-DISTRACTION-PROTOCOL rung.

    NDP keeps a lead agent's hot context on the critical path: work that
    arrives mid-flight and is not on that path goes to a subagent in the turn
    it arrives, rather than being chased inline or silently parked.

    WHAT IS ENFORCEABLE IS NARROWER THAN THE RULE, deliberately. The
    per-arrival trigger ("this row arrived mid-turn and was neither delegated
    nor dispatched before the turn ended") is NOT computable from state helm
    keeps: nothing binds an arrival to the delegation that handled it, so a
    seat that answered a trivial owner question inline is indistinguishable
    on disk from one that chased a distraction for an hour — and the inject
    fire-ledger's newest row only APPROXIMATES a turn boundary (capped,
    rotated, fail-open by design). A rung built on that would fire on every
    head-down seat that answered one question, which is the false positive
    that gets a blocking rung disabled by the first seat it wrongly blocks.
    So this gate enforces the provable projection: a wide load held with zero
    delegation for the session (`_solo_load_candidate`, one predicate and one
    authority). It owns its emission so higher-salience whisper candidates
    cannot indefinitely hide the condition it measures.

    Latched once per load-state (the predicate's bucket fp, NDP_LATCH lane):
    a re-stop on the same state passes, a doubled queue re-arms, and ONE
    delegation this session silences it outright. An unwritable latch
    DEGRADES to the compact WARN — a gate that cannot remember may not block
    every stop forever. No interpolated value here leaves this module (the
    line is composed from an int), so there is nothing to scrub. Fail-open
    TOTAL; HELM_STOP_GUARD_NDP=0 disables."""
    if _off("STOP_GUARD_NDP"):
        return None, None
    try:                      # fail-open TOTAL — a Stop rung must never wedge
        cand = _solo_load_candidate(session, seat)
    except projscope.Expired:
        raise
    except Exception:
        return None, None
    projscope.spend_or_raise("reading NDP candidate result")
    if not cand:
        return None, None
    fp, line = cand
    path = _stop_fp_path(room, seat, session, kind=NDP_LATCH)
    projscope.spend_or_raise("reading NDP latch")
    try:
        with open(path) as f:
            last = f.read().strip()
    except OSError:
        last = None
    projscope.spend_or_raise("reading NDP latch")
    if last == fp:
        return None, None
    projscope.spend_or_raise("writing NDP latch")
    try:
        latched = _write_stop_latch(path, seat, fp, session=session)
    except OSError:
        latched = False
    if not latched:
        return None, "[helm stop-guard] " + line
    return ndp_block(line), None


def ndp_block(finding):
    """The NDP block's text for one finding — a pure renderer at its own door,
    so a budget arm can measure the artifact rather than rebuild it.

    THE WRONGLY-BLOCKED SEAT'S ESCAPE IS THE PART THAT MUST SURVIVE, and it is
    one sentence rather than the paragraph it was: a seat under a standing
    no-delegate instruction can act on nothing else here, and a gate whose
    escape is buried teaches the fleet to disable it wholesale. The throughput
    argument — hot context on the critical path is a TLA's scarce resource — is
    true and is not something a stop needs to re-argue to be obeyed."""
    return ("[helm stop-guard] non-distraction protocol — " + finding + "\n"
            "  Send the next thing that is not on your critical path to a "
            "subagent in the turn it lands.\n"
            "  Blocks once per load-state — a re-stop passes, a doubled "
            "queue re-arms, one delegation this session ends it. If you are "
            "under a standing instruction not to delegate, this rung cannot "
            "see that and is wrong about you: set HELM_STOP_GUARD_NDP=0 for "
            "the session and say so.")
