#!/usr/bin/env python3
"""Publish the Stop guard's response after naming the killable stage."""
import sys

from . import seats_stop_seam, seats_stop_timing
from .seats_stop_signals import _off


def _begin(budget):
    return budget.timing.begin("response") if budget \
        else seats_stop_timing.begin("response")


def _done(budget, started):
    """The response rung ends, AND WITH IT THE LADDER.

    THE ONE PLACE THE LIFECYCLE COMPLETES. `response` is the last rung in
    RUNGS, and every path that publishes a decision passes through here. The
    end was declared one call earlier, when the guard function returned —
    which is BEFORE this rung begins, so the durable log carried boundary
    records written after their own run's end.

    AN EXPIRED LADDER STILL DECLARES NOTHING. `publish_expired` does not route
    through this function at all: it times its own `response-fallback` rung on
    the module-level timer, holds no run identity, and so cannot end a run it
    was never part of. That is the case the durable record exists to catch, and
    an end nobody observed stays unobserved."""
    if budget:
        budget.timing.done("response")
        budget.timing.finish()
    else:
        seats_stop_timing.done("response", started)


def publish(blocks, warns, budget=None):
    """Publish one completed guard decision and return its hook status."""
    if _off("STOP_GUARD"):
        return 0
    started = _begin(budget)
    if blocks:
        seats_stop_seam.emit_blocks(blocks)
        _done(budget, started)
        return 2
    seats_stop_seam.emit_warns(warns)
    _done(budget, started)
    return 0


def publish_failure(exc, budget=None):
    """Allow a crashed guard loudly; this is unchecked, never a clean bill."""
    # THE RC AND THE MEANING PART COMPANY HERE, so the meaning takes its own
    # channel. This returns 0 because never wedging a stop is the law, and a
    # reader of the rc alone cannot tell this from a guard that looked and
    # found nothing. The aggregate must not mint corroboration on it.
    from . import hookoutcome
    hookoutcome.declare(hookoutcome.UNCHECKED,
                        "stop-guard crashed (%s)" % type(exc).__name__)
    started = None if _off("STOP_GUARD") else _begin(budget)
    # FAIL-OPEN, AND LOUD. Never wedging a stop is right — a guard that can
    # wedge the fleet is worse than one that misses a row. Returning 0 silently
    # made a crashed guard indistinguishable from a clean one: nothing here says
    # the inbox, claims, or lanes are clear.
    print("[helm stop-guard] THE GUARD COULD NOT RUN (%s: %s) — this stop is "
          "ALLOWED and UNCHECKED: your inbox, your claim leases and your lanes "
          "were NOT examined. Nothing here says they are clear."
          % (type(exc).__name__, exc), file=sys.stderr)
    if started is not None:
        _done(budget, started)
    return 0


def publish_expired(blocks, warns, stop_active=False):
    """Publish UNKNOWN without starting latch, chart, or receipt work.

    EXPIRY IS AN UNCHECKED ANSWER, not a clean one: the ladder ran out of
    coverage budget partway, so some rungs never looked. It can still return 0
    — fail-open does not move — and the outcome channel carries the fact that
    nothing certified the rungs that were skipped.
    """
    from . import hookoutcome
    hookoutcome.declare(hookoutcome.UNCHECKED,
                        "stop-guard coverage budget expired mid-ladder")
    # BOTH LISTS, BECAUSE THE COVERAGE LINE MOVED AND THIS SELECTOR WOULD HAVE
    # EATEN IT. `budget.expire()` now files its COVERAGE UNKNOWN as a WARN
    # (seats_stop_budget: an absence is not a finding and may not refuse), and
    # this rendered `blocks` alone whenever the harness was not already
    # continuing — so the one line explaining why the ladder stopped early
    # would have been built, drained, and never printed. Measured blocks still
    # refuse below; both are printed because both are true.
    #
    # AND THE SURVIVORS RIDE EVERY EXIT NOW. `include_survivors` was false
    # under stop_active while `fallback_lines` cleared `_SURVIVES_REFUSAL`
    # unconditionally, so a disclosure ARMED to survive any exit was discarded
    # unprinted on exactly the path that takes it. Their own law is that they
    # are true no matter which rung, if any, blocks.
    lines = seats_stop_seam.fallback_lines(
        list(blocks or ()) + list(warns or ()), include_survivors=True)
    payload = "\n".join(line for line in lines
                        if isinstance(line, str) and line.strip())
    started = seats_stop_timing.begin("response-fallback")
    if payload:
        sys.stderr.write(payload + "\n")
        sys.stderr.flush()
    seats_stop_timing.done("response-fallback", started)
    return 0 if stop_active or not blocks else 2
