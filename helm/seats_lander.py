#!/usr/bin/env python3
"""helm seats_lander — WHO FOLDS, resolved and never spelled.

THE LANDER IS THE INTEGRATOR UNLESS SOMEONE NAMES OTHERWISE, and that is a
RELATIONSHIP rather than a coincidence of spelling. A module that writes
`os.environ.get("HELM_LANDER") or "<a seat name>"` has two defects in one
line: it keeps addressing that literal after the roster stops carrying it,
and it says nothing about why that name and not another. Reading the
integrator instead makes the relationship the code, so the day the integrator
changes the lander follows without anyone remembering to.

AN OVERRIDE IS OBEYED, AND THAT IS WHAT AN ESCAPE HATCH MEANS. `HELM_LANDER`
exists for the case where the ordinary resolution is WRONG — including the
case where the roster itself is wrong — so confirming it against the roster
before honouring it takes the hatch away at exactly the moment it is needed.
An operator who types a seat name has made a decision, and this module is not
the authority that overrules it.

A TYPO IS NOT SILENT EITHER, because the door that SENDS already reports one:
`helm chat` resolves every addressee and says "ABSENT — no current roster row"
for a name nothing answers to, and the flush-alert path picks its channel from
membership for exactly that reason. Re-asking that question here would put a
second, weaker copy of it in front of the one that is already right, and would
make this module a roster consumer for no gain.

WHY THE PAIR AND NOT ONE FUNCTION. `lander_seat` REFUSES, and its callers are
the ones that can act on a refusal. `lander_seat_or_default` CANNOT return
empty, because its caller's fallback is addressed by construction — an
unaddressed room post is the exact defect that fallback exists to cure — so
it announces the substitution instead of making it silently. Both entry
points live here together and the second calls the first through this
module's own global, so a test that patches the name patches the call too.
"""
from . import home, seats_integrator
from .seats_common import _seat_label
from .seats_identity import _warn_once

# The env var an operator exports to point the fold at a different seat.
# `home.env` prefixes it, so this is the HELM_LANDER everything else names.
LANDER_ENV = "LANDER"


def lander_seat(snapshot=None):
    """(seat, None) for the seat that FOLDS, else (None, why) from the integrator.

    AN OVERRIDE SHORT-CIRCUITS AND IS RETURNED AS GIVEN. With no override this
    IS the integrator question, asked of the one door that answers it: the
    fold belongs to the integrator role, and duplicating its resolution here
    would start at zero on every edge that module already handles — an
    unreadable roster, a malformed row, an ambiguous suffix. The refusal
    travels with it: this resolves nowhere the integrator could not.

    `snapshot` is passed straight through and is read only by that door, so
    this module never reads the roster itself.
    """
    override = (home.env(LANDER_ENV) or "").strip()
    if not override:
        return seats_integrator.integrator_seat(snapshot)
    # LAUNDERED ON THE WAY OUT, like every other name this fleet emits: the
    # value is raw operator input and it reaches a mention, a DM and a
    # terminal. `_seat_label` leaves a legitimate name BYTE-IDENTICAL, so an
    # ordinary override still resolves at the send door; one carrying ESC or
    # bidi loses its resolution, which is the correct outcome for a name that
    # could reshape a reader's terminal.
    return _seat_label(override), None


def lander_seat_or_default():
    """The lander seat, NEVER empty — and LOUD when it had to guess.

    ITS CALLER'S FALLBACK IS ADDRESSED BY CONSTRUCTION. `dispatches._nudge`
    falls back to a ROOM POST addressed to this name when a DM does not
    deliver, and an unaddressed room post is the exact defect that fallback
    exists to cure. So this may not return empty and may not refuse. What it
    must not do instead is substitute INVISIBLY: a guessed name and a
    proven-live one cannot be the same observable, or the fold is announced to
    a seat nobody confirmed while the module above claims it refuses.
    """
    seat, why = lander_seat()
    if seat:
        return seat
    # THE DEGRADED PATH HANDS OFF RATHER THAN NAMING A CONSTANT, and that is
    # the difference between one guess and two. A literal here would reproduce
    # this module's own thesis inside its cure: the fallback would name a seat
    # the roster does not carry, `_nudge` would address a ROOM POST to it, and
    # the send door would write that post to nobody while reporting success.
    #
    # THE INTEGRATOR DOOR ALREADY OWNS THIS DECISION, so it makes it. That
    # matters most in the case this branch actually reaches today: an
    # HELM_LANDER override that does not resolve refuses ABOVE while the
    # integrator resolves fine, and handing off then names a LIVE seat where a
    # constant would have named a dead one. When the integrator cannot be
    # resolved either, nothing rostered can be named by anyone and its own
    # degraded answer is the fleet's single policy for that — one door to fix
    # rather than two to keep in step.
    _warn_once("lander-unresolved:%s" % (why or "no reason given"),
               "helm: lander seat could not be proven live (%s) — falling back "
               "to the integrator door's answer; that seat may be dead or "
               "renamed\n" % (why or "no reason given"))
    return seats_integrator.integrator_seat_or_default()
