#!/usr/bin/env python3
"""Ownership of a row is a LEASE ON LIVENESS, not a name written down.

A dispatch row's recipient and a task row's owner are bare seat names, and a
name does not stop being a name when the seat behind it dies.

WHY THIS MODULE TAKES ITS EVIDENCE AS AN ARGUMENT, AND WHY THE ARGUMENT IS
TYPED. A presence field is not a response field, and the two are easy to
confuse because the honest-sounding name belongs to the wrong one:
`seats_roster.last_seen` reads the mtime of `.seen`, which the beacon poll
touches every cycle (`seats_delivery` states it — "presence even when every
room is quiet"). A predicate fed that field measures BEACON LIVENESS while
reporting seat activity, and on a live fleet it classifies seats with a
polling beacon as working regardless of whether they have done anything.

TAKING NO BEACON PARAMETER DOES NOT PREVENT THIS. The dependency arrives
through whatever evidence argument IS accepted, so the signature proves
nothing and the data path is the only thing worth checking. Hence a TYPED
answer that must declare what it covers and whether its event is a TERMINAL
RESPONSE; a source that cannot cover a seat yields UNKNOWN rather than a
confident number. A field is verified at its PRODUCER, never by its name.

WHAT NO SOURCE IN THIS TREE CLAIMS, AND THE HISTORY IS THE POINT. An earlier
revision had a `proves_completion` flag and a Stop-hook writer that set it.
It was RETIRED (integrator ruling, task/2293) because nothing helm can
observe proves the HARNESS ACCEPTED a stop: helm's own aggregated allow is
not acceptance (a foreign plugin Stop hook can veto after every helm handler
allows), and a terminal transcript record cannot be distinguished from a
vetoed stop whose continuation has not been appended yet. So the strongest
honest claim is a TERMINAL RESPONSE at a known time, and that is what the
type, the sources and the rendered sentences all say.

DEAD IS A CONJUNCTION AND BOTH HALVES ARE LOAD-BEARING: no terminal response
inside the threshold, AND no live descendant work. The second half is what
makes the first safe — a seat waiting 34 minutes on a remote gate has emitted
no terminal response and is emphatically working.

UNKNOWN NEVER REVERTS, and it is the answer to every question this module
cannot measure: an unreadable roster, an ambiguous identity, an evidence
source that does not cover this seat, a malformed timestamp, a response too
young to have settled, a descendant probe that raises or declines. Judging a
live seat dead hands its work to somebody else, and two seats doing one row
is worse than one row waiting.
"""
import math

HOLDING = "holding"          # working, or reachable with live descendant work
DARK = "dark"                # dead by the conjunction below — revertible
UNKNOWN = "unknown"          # something could not be read; never reverts

# WHY TWO HOURS, AND THE HONEST STATE OF THAT NUMBER. It was first chosen
# from a measured bimodal distribution of `last_seen` — busiest seat 0.0
# minutes quiet, quietest dark seat 11.2 hours, nothing between. THAT
# MEASUREMENT IS WITHDRAWN: every 0.0 was a seat whose BEACON was polling, so
# the distribution described beacon state rather than seat activity, and a
# threshold derived from it was calibrated against the wrong population.
#
# It is kept as a deliberately conservative placeholder, not as a measured
# value, and it is chosen for the failure that costs more: reverting a
# WORKING seat's row gives one piece of work two owners, while leaving a dead
# seat's row costs delay on an advisory surface. Two hours sits far above any
# plausible single turn (the longest whole-suite gate on the slow node is ~34
# minutes). RE-DERIVE IT against genuine terminal-response evidence before any
# caller uses this predicate to WRITE, and the census gap tripwire is the
# surface that has to report the distribution it is actually calibrated on.
DEAD_AFTER_S = 2 * 3600


#: THE ONLY SOURCES ALLOWED TO DECLARE `proves_terminal_response`, and the
#: list is here rather than at the call site ON PURPOSE (integrator ruling,
#: task/2293): the flag binds to the WRITER, never to a value a caller
#: supplies. A caller that passes proves_terminal_response=True with any other
#: source name is DOWNGRADED to corroboration rather than trusted, because the
#: failure this revision cures was a source being believed on the strength of
#: its name. Adding a name here is a deliberate act with a reviewer attached.
#:
#: THE STOP-HOOK STAMP IS DELIBERATELY NOT IN THIS SET. It records that helm's
#: own Stop dispatch allowed — which is neither harness acceptance nor a
#: terminal response — so it corroborates a live answer and can never revert a
#: row. Putting it back here would restore the exact defect this task cured.
TERMINAL_RESPONSE_SOURCES = frozenset((
    # The seat's own session transcript, read by session id: the last
    # assistant record whose stop_reason is `end_turn`, with the record's own
    # timestamp. See `helm/turnresponse.py` for what it cannot distinguish.
    "session transcript terminal response",
))


class TurnEvidence(object):
    """What a source can say about a seat's last TERMINAL RESPONSE.

    FOUR OUTCOMES, NEVER TWO, and the extra ones are the reason this is a
    class rather than a timestamp. `ts` present means the source saw a
    terminal response. `covered=False` means THIS SOURCE CANNOT SEE THIS SEAT
    at all — which is not the same as "this seat has done nothing", and
    collapsing them is exactly the error the beacon field made. `err` means
    the source failed to read. `pending` means the source SAW something but it
    is too young to settle, which is neither evidence of life nor of death.

    A source is required to say which it means, because the caller cannot tell
    from a bare `None` and the difference decides whether a row may be
    reverted.

    AND A FACT THAT IS NOT ABOUT THIS SEAT AT ALL: whether the source's EVENT
    IS ACTUALLY A TERMINAL RESPONSE. Declaring coverage cannot repair
    different event semantics — a chat post proves a seat EMITTED something in
    one room, and a Stop-hook stamp proves helm's own guard allowed; neither
    is a response terminating. So a source states
    `proves_terminal_response`, and one that does not may CORROBORATE a live
    answer but can never carry a row to DARK. That is enforced in
    `owner_state` rather than trusted to the caller, because the caller is
    where I got it wrong last time.
    """

    __slots__ = ("ts", "covered", "err", "source", "proves_terminal_response",
                 "pending", "incomplete")

    def __init__(self, ts=None, covered=True, err=None, source=None,
                 proves_terminal_response=False, pending=None,
                 incomplete=None):
        self.ts = ts
        self.covered = covered
        self.err = err
        self.source = source or "unnamed source"
        # A SEEN-BUT-UNSETTLED ANSWER, which is its own outcome. The producer
        # can see a terminal record that is too young for either write order
        # to be settled; reporting that as a timestamp would certify it and
        # reporting it as absence would be a lie in the other direction.
        self.pending = pending or None
        # PARTIAL COVERAGE, AND IT IS DIRECTIONAL. A source that could not
        # read part of its evidence can only have MISSED activity, never
        # invented it — so the seat may be MORE recently active than this
        # reading says and can never be less. Such a reading may therefore
        # support a LIVE answer and may never carry a row to DARK.
        self.incomplete = incomplete or None
        # DEFAULT FALSE, DELIBERATELY, AND NOT TAKEN ON TRUST. A source must
        # CLAIM that its event is a terminal response AND be one of the
        # writers allowed to make that claim; forgetting either costs an
        # UNKNOWN, which is the safe direction. Asking the caller would
        # reproduce the original defect exactly — a source believed because of
        # what it called itself.
        self.proves_terminal_response = bool(proves_terminal_response) and \
            self.source in TERMINAL_RESPONSE_SOURCES


def _finite(value):
    """A timestamp is only a timestamp when it is a finite number.

    A roster row is a JSON file anybody can hand us, so `last_seen` can be a
    string, None, NaN or an infinity. Subtracting it crashed the census;
    treating NaN as an ordinary quiet duration is worse, because every
    comparison against it is False and the seat silently reads HOLDING.
    """
    try:
        f = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return f if math.isfinite(f) else None


def owner_state(seat, roster_rows, turn_evidence, descendants, now=None):
    """-> (HOLDING | DARK | UNKNOWN, why). The ONE door every caller asks.

    `roster_rows` is the tri-state roster: None means the read FAILED and
    every seat comes back UNKNOWN.

    `turn_evidence(seat, row) -> TurnEvidence` answers about TERMINAL
    RESPONSES. It must not be a presence or beacon field; see the module
    docstring for why that distinction is the whole point.

    `descendants(seat) -> True | False | None` answers whether live work
    hangs off this seat (a subagent, or a remote gate). None means the probe
    could not look, which is UNKNOWN and never free.
    """
    import time

    name = str(seat or "").strip()
    if not name:
        return UNKNOWN, "no seat name on the row"
    if roster_rows is None:
        return UNKNOWN, "the roster did not read; no seat can be judged"

    # IDENTITY IS CASEFOLD-EXACT, and an exact-case lookup called a live seat
    # absent: roster `alice` queried as `Alice` read "no roster row" and went
    # DARK. Ambiguity is NOT absence either — two rows naming one identity is
    # a repair question, not a death sentence.
    try:
        from .seats_common import canonical_seat
        key, kerr = canonical_seat(name, roster_rows)
    except Exception as e:                  # noqa: BLE001
        return UNKNOWN, "identity could not be resolved (%s: %s)" % (
            e.__class__.__name__, e)
    if kerr:
        return UNKNOWN, "the roster names %r ambiguously: %s" % (name, kerr)
    if key is None:
        return DARK, "no roster row for %r" % name
    row = roster_rows.get(key)

    ev = turn_evidence(key, row)
    if ev.err:
        return UNKNOWN, "turn evidence unreadable (%s): %s" % (ev.source, ev.err)
    # A SOURCE THAT CANNOT SEE THIS SEAT SAYS SO, and that is UNKNOWN. Reading
    # "this source has no record" as "this seat has done nothing" is the
    # beacon error with the sign flipped, and it would revert on silence.
    if not ev.covered:
        return UNKNOWN, "%s does not cover %r; its activity is unmeasured" % (
            ev.source, key)
    # SEEN BUT NOT SETTLED. The producer found a terminal record too young for
    # the write order to be decidable, so it is neither a response to trust
    # nor an absence to revert on. This costs at most the pending window and
    # removes the cheapest false positive there is.
    if ev.pending:
        return UNKNOWN, "%s is not settled yet: %s" % (ev.source, ev.pending)

    ts = None if ev.ts is None else _finite(ev.ts)
    if ev.ts is not None and ts is None:
        return UNKNOWN, "turn evidence for %r is not a finite timestamp" % key

    at = now if now is not None else time.time()
    if ts is not None:
        quiet = at - ts
        if quiet < DEAD_AFTER_S:
            # THE SENTENCE MATCHES THE SOURCE'S SEMANTICS, which is the whole
            # defect this revision exists for wearing its smallest coat. A
            # corroboration-only source saw the seat EMIT something; calling
            # that a terminal response would relabel the evidence exactly as
            # `last_seen` did, and the next reader would quote it.
            return HOLDING, ("last terminal response %.0fm ago (%s)"
                             if ev.proves_terminal_response else
                             "emitted activity %.0fm ago (%s), which is not a "
                             "terminal response but is inconsistent with "
                             "dead") % (quiet / 60.0, ev.source)
    else:
        quiet = None

    # BOTH HALVES, AND A MISSING TIMESTAMP DOES NOT SKIP THE SECOND.
    # Returning DARK here without calling the probe reverts a seat that has
    # recorded no response but holds live work — out from under itself. "No
    # response recorded" is the weaker half of the conjunction, never the
    # whole of it.
    try:
        busy = descendants(key)
    except Exception as e:                  # noqa: BLE001
        return UNKNOWN, "descendant probe failed (%s: %s)" % (
            e.__class__.__name__, e)
    if busy is None:
        return UNKNOWN, "descendant work for %r is unreadable" % key
    if busy:
        return HOLDING, ("no terminal response %s but holds live descendant "
                         "work" % ("recorded" if quiet is None
                                   else "in %.1fh" % (quiet / 3600.0)))

    # THE LAST GATE, AND IT IS ABOUT THE SOURCE RATHER THAN THE SEAT. Both
    # halves of the conjunction now say "dead", but a source whose event is
    # not a TERMINAL RESPONSE cannot carry that: old posts and absent posts
    # are equally consistent with a seat working silently. Such a source
    # corroborates a live answer and never justifies a revert.
    if not ev.proves_terminal_response:
        return UNKNOWN, (
            "%s is not a TERMINAL RESPONSE, so silence cannot mean dead "
            "(no live descendant work either; this is unmeasured, not clear)"
            % ev.source)

    # AND THE COVERAGE GATE, WHICH ONLY BITES IN THIS DIRECTION. Everything
    # above already concluded "dead"; a source that could not read part of its
    # evidence may have missed exactly the recent activity that would refute
    # that, so it cannot carry the revert. The same reading was allowed to
    # support HOLDING above, because a hole can only hide activity and never
    # manufacture it.
    if ev.incomplete:
        return UNKNOWN, ("%s could not read all of its evidence: %s"
                         % (ev.source, ev.incomplete))

    if quiet is None:
        return DARK, "%s records no terminal response, and no live descendant work" % (
            ev.source,)
    return DARK, "silent %.1fh and no live descendant work" % (quiet / 3600.0)
