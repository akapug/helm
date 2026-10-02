#!/usr/bin/env python3
"""helm seats — the council verbs (`verdict`, `reveal`, `council-status`,
`council-abort`) and the argv helpers they share with the dispatcher.

Split out of helm/seats_cli.py, which imports every name back, so the
dispatcher stays under the seats-split line budget. `_flag` moved with the
council leg because that leg calls it, and the dispatcher cannot be imported
from here without a cycle.
"""

import sys

from . import chat


def _flag(args, name, default=None):
    # A FLAG IS NEVER A VALUE: `--seat --apply` minted a seat "--apply".
    i = args.index(name) if name in args else -1
    if i >= 0 and i + 1 < len(args) and not str(args[i + 1]).startswith("-"):
        return args[i + 1]
    return default


_COUNCIL_FLAGS = ("--tip", "--evidence", "--seat", "--threshold")


def _council_positionals(args):
    """Positional args with every known flag AND its value removed — a bare
    split would read `--tip`'s value as the verdict."""
    out, skip = [], False
    for a in args:
        if skip:
            skip = False
            continue
        if a in _COUNCIL_FLAGS:
            skip = True
            continue
        if a.startswith("--"):
            continue
        out.append(a)
    return out


def _cmd_council(verb, args):
    """The council verbs — the FORMAL convergence species (0.3's N-of-M, now
    landed). Identity is AMBIENT (chat._seat_actor): a signal binds MEMBER
    IDENTITY, so a claimed --seat would let one seat cast another's sealed
    judgment — the exact footgun 1f6e5bb ("dm/ack actor-binding: --seat
    asserts ambient, never selects the signer") closed for signing."""
    from . import council
    rest = _council_positionals(args)
    room = rest[0] if rest else None
    if not room:
        print("helm chat %s: needs a council room (helm chat council invite "
              "<members> <topic>)" % verb, file=sys.stderr)
        return 2
    # READS AND CHEAP REFUSALS FIRE BEFORE ANYBODY IS ASKED WHO THEY ARE:
    # admission ahead of dispatch is the regression this lane shipped twice.
    if verb == "council-status":
        print("\n".join(council.status_lines(room)))
        return 0
    if verb == "verdict" and council.registry(room) is None:
        print("helm chat verdict: no council convened for room %s" % room,
              file=sys.stderr)
        return 2
    actor, serr = chat._seat_actor(args)
    if serr:
        print("helm chat %s: %s" % (verb, serr), file=sys.stderr)
        return 2
    # THE CAPABILITY AUTHORIZES, the name only addresses.
    seat = actor.canonical_name
    if verb == "council-abort":
        reason = " ".join(x for x in rest[1:] if not x.startswith("--"))
        reg, err = council.abort(room, seat, reason)
        if err:
            print("helm chat council-abort: " + err, file=sys.stderr)
            return 2
        chat.post("[COUNCIL %s] ABORTED by %s — %s. The embargo is permanent; "
                  "collaborate in a standup, then reconvene on the superseding "
                  "tip." % (room, chat._dsan(seat), reg.get("abort_reason")),
                  room=room, who=actor, sign=False)
        print("COUNCIL %s ABORTED — no reveal, ever (an aborted council's "
              "judgments were not formed independently)" % room)
        return 0
    if verb == "reveal":
        signals, err = council.reveal(room)
        if err:
            print("helm chat reveal: " + err, file=sys.stderr)
            return 2
        label, counts = council.outcome(signals)
        print("COUNCIL %s REVEALED — %d sealed judgment(s), embargo lifted:"
              % (room, len(signals)))
        for s in signals:
            print("  %s: %s @ %s — %s" % (chat._dsan(s["seat"]), s["verdict"],
                                          s["tip"][:12], s["evidence"] or "(no evidence ref)"))
        # quorum is a REVEAL bar, not a decision — say what the judgments add
        # up to rather than let "quorum reached" be misread as "ratified"
        print("OUTCOME: %s (%s)" % (label, ", ".join(
            "%s %d" % (v, counts[v]) for v in council.VERDICTS)))
        chat.post("[COUNCIL %s] QUORUM — embargo lifted, %d judgment(s) on the "
                  "record: %s" % (room, len(signals),
                                  ", ".join("%s %s" % (chat._dsan(s["seat"]), s["verdict"])
                                            for s in signals)),
                  room=room, who=actor, sign=False)
        return 0
    # verdict = SIGNAL (sealed)
    verdict = rest[1] if len(rest) > 1 else None
    tip = _flag(args, "--tip")
    evidence = _flag(args, "--evidence")
    reg, err = council.signal(room, seat, verdict, tip, evidence)
    if err:
        print("helm chat verdict: " + err, file=sys.stderr)
        return 2
    n, k, is_open = council.tally(room)
    print("SEALED — your judgment is recorded and EMBARGOED (%d of %d)" % (n, k))
    # NO PUBLIC PRE-QUORUM PROGRESS ROW — deleted, not patched (a re-gate).
    # Two rounds of trying to publish progress "safely" both leaked:
    #   r1: posted as who=signer — the row's own from field named the seat the
    #       text promised to hide.
    #   r2: posted as who=convener — better, but chat.post still touches the
    #       CALLING seat's presence cross-seat, and a public "1 of 2" identifies
    #       the other signer by elimination anyway.
    # The count was never worth it: `helm chat council-status <room>` already
    # serves the tally on demand to anyone entitled to ask. A guarantee you have
    # to keep narrowing is not a guarantee — so the embargo is now enforced by
    # NOT EMITTING, which is the only version of "zero WHO" that is true.
    # The signer still gets their private local confirmation above.
    if is_open:
        print("QUORUM REACHED — reveal: helm chat reveal %s" % room)
    return 0
