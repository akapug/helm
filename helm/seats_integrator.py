#!/usr/bin/env python3
"""helm seats_integrator — WHO THE INTEGRATOR IS, resolved and never guessed.

THE INTEGRATOR IS A ROLE AND A HARDCODED NAME IS A CORPSE WAITING TO HAPPEN.
A module that writes `INTEGRATOR = "<a seat name>"` keeps addressing that
name after the roster stops carrying it, and nothing fails loudly: `or
INTEGRATOR` yields a string, a string is truthy, and every surface downstream
reports a delivery it never made. The cure is not a better constant. It is
that the name is READ at the moment it is used, from the roster that decides
it, and that a name which cannot be confirmed produces a REFUSAL rather than
a substitute.

This does not make renames safe; it makes them LOUD — one place to change,
and a refusal instead of a silent misroute once the name stops resolving. A
caller that receives (None, why) owes the reader `why`; a caller that falls
back to a literal has rebuilt the defect it was given a cure for.

BOTH ENTRY POINTS LIVE HERE, TOGETHER, ON PURPOSE. integrator_seat_or_default
calls integrator_seat through this module's own global, so a test that
patches the name must patch it on THIS module — patching a re-export
elsewhere would leave the internal call untouched and the double silently
bypassed. That is the import-binding class, and splitting the pair across
modules would build it in by construction.
"""
from . import home
from .seats_common import _seat_label
from .seats_identity import _warn_once

# The name asked for when nothing else names one. It is a STARTING POINT for
# resolution, never an answer: every path below still requires the roster to
# confirm it before any caller receives it.
INTEGRATOR_SEAT_DEFAULT = "helm-integrator"

# The suffix that makes a seat an integrator. Inference over this suffix is
# the LAST resort and only when it is unambiguous — one candidate resolves,
# two candidates is a question the roster cannot answer for us.
INTEGRATOR_SUFFIX = "-integrator"


def integrator_seat(snapshot=None):
    """(seat, None) for the live integrator, else (None, why) — NEVER a guess.

    ABSENT AND UNREADABLE DO NOT SHARE A VALUE. An unreadable roster cannot
    prove a seat is gone, so it says so rather than handing back a name that
    is merely the default. HELM_INTEGRATOR_SEAT overrides and is validated
    the same way — an override naming a seat that does not resolve is a typo,
    not an authorization.
    """
    override = (home.env("INTEGRATOR_SEAT") or "").strip()
    want = override or INTEGRATOR_SEAT_DEFAULT
    if snapshot is None:
        from .seats_roster import roster_checked
        snapshot, failed = roster_checked()
        if failed:
            # LAUNDERED AT THE DOOR, like every other emit site: an operator's
            # typo'd override is raw input and must not reach a message or a
            # terminal unfiltered.
            return None, ("roster unreadable, so %r cannot be confirmed live "
                          "— refusing to name an unverified integrator"
                          % _seat_label(want))
    if isinstance(snapshot.get(want), dict):
        return want, None
    if override:
        # AN EXPLICIT OVERRIDE THAT DOES NOT RESOLVE IS A TYPO, NOT A REQUEST
        # TO GUESS. Falling through to the suffix inference here would hand an
        # operator who misspelled the variable a DIFFERENT seat, silently, and
        # the inference would look like a resolution. Refusing is the whole
        # point of letting someone name a seat explicitly.
        return None, ("HELM_INTEGRATOR_SEAT names %s, which does not resolve "
                      "in the roster — an explicit override is not a request "
                      "to infer a substitute; fix the value or unset it"
                      % _seat_label(want))
    # THE SAME SHAPE TEST THE DIRECT LOOKUP APPLIES. Without it the two
    # paths disagree about what RESOLVES: the lookup above rejects a
    # malformed row, and this line re-admits THE VERY SAME SEAT, because the
    # default name ends in the suffix it searches for. So a malformed row
    # refuses when reached by an override and resolves when reached by the
    # default — one name, two answers, and the one this module promises to
    # refuse is the one it would hand back.
    live = sorted(n for n in snapshot
                  if str(n).endswith(INTEGRATOR_SUFFIX)
                  and isinstance(snapshot[n], dict))
    if len(live) == 1:
        return live[0], None
    return None, ("integrator seat %r does not resolve in the roster (%d "
                  "candidate(s) ending %s: %s) — set HELM_INTEGRATOR_SEAT or "
                  "fix the roster row"
                  % (_seat_label(want), len(live), INTEGRATOR_SUFFIX,
                     ", ".join(_seat_label(n) for n in live) or "none"))


def integrator_seat_or_default():
    """The integrator seat, NEVER None — and LOUD when it had to guess.

    Some callers key a row or a digest by owner and read `owner or
    integrator()`, so returning None there keys the WRONG row. That invariant
    is real and does not move. What must not happen is the substitution being
    INVISIBLE: a substituted default and a proven-live integrator cannot be
    the same observable, or an integrator that could not be proven live is
    silently replaced by a literal while the code above claims it refuses.

    This keeps the invariant and announces the substitution once per process
    per reason: never blocks, never silent.
    """
    seat, why = integrator_seat()
    if seat:
        return seat
    _warn_once("integrator-unresolved:%s" % (why or "no reason given"),
               "helm: integrator seat could not be proven live (%s) — "
               "addressing %s by default; that seat may be dead or renamed\n"
               % (why or "no reason given", INTEGRATOR_SEAT_DEFAULT))
    return INTEGRATOR_SEAT_DEFAULT


def integrator_mention():
    """('@seat ', None) to prefix an escalation, else ('', why) — never a void.

    AN ESCALATION ADDRESSED TO A NAME NO ROSTER CARRIES IS THE LOUDEST THING
    A SUBSYSTEM CAN SAY AND IT LANDS IN NOBODY'S MENTIONS. The send door
    accepts any token and reports success, so a literal mention is written,
    delivered nowhere, and reported as posted — the failure direction is
    silent-on-danger, which is exactly the direction a safety escalation must
    not fail in.

    THE DOOR DID NOT FAIL TO NOTICE, IT FAILED TO BE ASKED. `helm chat post`
    already resolves addressees and already prints that one is ABSENT with no
    roster row — that diagnostic is not new, and it is true every time. What
    was missing is that nothing READ it: a true sentence on stderr that no
    subsystem consumes changes nothing about where the post goes. So the cure
    is not a better diagnostic, it is moving the question to BEFORE the send,
    where a caller can still act on the answer.

    So a caller about to address the integrator asks HERE, and gets one of two
    things it can act on: a mention that will resolve, or an empty prefix and
    the reason. The reason belongs in the message body — the post still goes
    out carrying its own evidence that it reached no addressee, because
    dropping the text loses the danger along with the delivery.
    """
    seat, why = integrator_seat()
    if seat:
        # LAUNDERED ON THE WAY OUT, like every other roster key this fleet
        # emits. The mention reaches a chat room, which is a terminal, and
        # the key came from a roster row whose spelling nothing here wrote.
        # `_seat_label` leaves a legitimate name BYTE-IDENTICAL, so the
        # mention still resolves; a name carrying ESC or bidi loses it, and
        # a mention that then fails to resolve is the correct outcome for a
        # seat whose name could reshape the reader's terminal.
        return "@%s " % _seat_label(seat), None
    return "", why


def integrator_addressed(text):
    """TEXT addressed to the live integrator, or TEXT that says it is not.

    The one shape every escalation takes, so no sender spells an integrator.
    A resolved integrator is mentioned first; when none resolves, the text is
    sent unchanged with the reason appended (`[UNROSTERED: ...]`), because
    dropping the text loses the danger along with the delivery."""
    mention, why = integrator_mention()
    if mention:
        return mention + text
    return ("%s [UNROSTERED: no integrator could be addressed — %s]"
            % (text, why or "no reason given"))
