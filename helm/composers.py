#!/usr/bin/env python3
"""helm seat composers — the fleet probe for HELD-BUT-UNSENT composer text.

THE DEFECT THIS MAKES VISIBLE (owner-reported, plural and recurring: "these
keep landing and not hitting enter"). A seat's own resume directive gets typed
into its composer and never submitted. The seat then sits with its next
instruction on screen doing nothing — and reads IDLE-AND-HEALTHY to
seat_liveness, to proxywatch and to the beacons, because every one of those
asks "is this pane alive?" and the pane is perfectly alive. Until this verb
existed the only instrument that could see it was the owner's eyes.

READ-ONLY, ON PURPOSE, AND THIS IS A SAFETY PROPERTY. It never sends a
keystroke. Measured on the live fleet the hour it was written, three panes held
unsent composer text and they were three DIFFERENT things: a stranded helm
directive ("Continue with the OPEN Codex dispatch."), a walled seat's leftover
"hi", and the OWNER MID-SENTENCE. A probe that auto-fired Enter would have
submitted the owner's half-typed thought. Surfacing is the whole job; the human
or the integrator decides.

THREE ANSWERS, NEVER TWO. `cannot-tell` is a first-class outcome and is never
folded into `clear`:

  held         the composer holds text nobody submitted
  clear        the composer is empty, or holds only known placeholder chrome
  cannot-tell  the pane could not be read, or its composer could not be located

Measured 2026-08-05: of 26 live panes, TWELVE answered `terminal read` with an
empty tail and every cursor pinned at 0, while connected/writable/orphaned all
said fine — and at least one of the twelve was a seat that had posted to chat
minutes earlier. Their emptiness is a READ-SURFACE LIMITATION, not evidence
about the seat. Reporting them as clear would be an all-clear over half a fleet
nobody looked at, which is the same shape of false negative this bug lives in.
"""
import json
import sys

HELD = "held"
CLEAR = "clear"
CANNOT_TELL = "cannot-tell"

# Placeholder text Claude renders INSIDE the composer row while the composer is
# actually empty. MEASURED, not guessed — each entry was read off a live pane.
# An unrecognised non-empty composer is reported HELD rather than clear: this
# verb surfaces to a human and never acts, so the safe direction is to show one
# line too many, and folding an unknown string into "clear" is how a probe
# learns to lie.
_PLACEHOLDERS = ("press up to edit queued messages",)


def classify(pane, tail, read_error=None):
    """One pane -> (state, composer_text, why).

    `tail` is the pane's bounded tail text, or None when it could not be read.
    `pane` is a `harness._pane_row` mapping — `last_output_at` is orca's own
    statement that it has never observed output from this pane, and it is the
    discriminator that keeps a blind pane out of the clear bucket.
    """
    from . import harness
    from .seat_lifecycle import _current_prompt_line
    if read_error:
        return CANNOT_TELL, None, "pane read failed: %s" % read_error
    if pane.get("last_output_at") is None:
        return (CANNOT_TELL, None,
                "the metaharness has never observed output from this pane "
                "(lastOutputAt null) — its empty tail says nothing about the "
                "seat")
    if not (tail or "").strip():
        return CANNOT_TELL, None, "pane returned an empty tail"
    line = _current_prompt_line(tail)
    if line is None:
        return (CANNOT_TELL, None,
                "no current composer could be located in the tail (newer "
                "output sits below the last prompt)")
    body = harness.composer_body(line)   # ONE owner — see its docstring
    if not body:
        return CLEAR, "", "composer empty"
    if body.lower() in _PLACEHOLDERS:
        return CLEAR, body, "composer holds only placeholder chrome"
    return HELD, body, "composer holds text that was never submitted"


def scan(adapter=None, limit=200):
    """([row], error) — every pane, classified. Never raises for a pane.

    A missing metaharness is ([], reason): honest absence, not an empty fleet.
    """
    from . import harness
    ad = adapter if adapter is not None else harness.detect()
    if ad is None:
        return [], harness.RECOMMENDATION
    try:
        panes = ad.list()
    except (harness.HarnessError, OSError) as e:
        return [], "metaharness pane list unavailable: %s" % e
    rows = []
    for p in panes:
        handle = p.get("handle")
        tail, err = None, None
        if p.get("last_output_at") is not None:
            try:
                tail = ad.read(handle, limit=limit)
            except (harness.HarnessError, OSError) as e:
                err = str(e)
        state, body, why = classify(p, tail, read_error=err)
        rows.append({"handle": handle, "title": p.get("title") or "",
                     "worktree": p.get("worktree") or "",
                     "state": state, "composer": body, "why": why})
    return rows, None


def cmd_composers(rest):
    """seat composers [--json] — the fleet composer probe."""
    rows, err = scan()
    if "--json" in rest:
        counts = {s: sum(1 for r in rows if r["state"] == s)
                  for s in (HELD, CLEAR, CANNOT_TELL)}
        print(json.dumps({"panes": rows, "counts": counts, "error": err},
                         indent=2, sort_keys=True))
        return 0 if err is None and not counts[HELD] else 1
    if err:
        print("helm seat composers: no pane inventory — " + err,
              file=sys.stderr)
        return 2
    held = [r for r in rows if r["state"] == HELD]
    blind = [r for r in rows if r["state"] == CANNOT_TELL]
    clear = [r for r in rows if r["state"] == CLEAR]
    for r in held:
        print("HELD  %-42s %s" % (r["handle"], (r["title"] or "-")[:34]))
        print("        composer: %s" % r["composer"][:110])
    for r in blind:
        print("?     %-42s %s" % (r["handle"], r["why"][:70]))
    print("%d held · %d clear · %d cannot-tell (of %d pane%s)"
          % (len(held), len(clear), len(blind), len(rows),
             "" if len(rows) == 1 else "s"))
    if blind:
        # The number that keeps this from being read as an all-clear. An
        # unreadable pane is not a clean pane, and the summary line above is
        # the exact place that distinction gets lost.
        print("  NOTE: %d pane%s could NOT be inspected — this is NOT a clean "
              "fleet, it is a partial one" % (len(blind),
                                              "" if len(blind) == 1 else "s"))
    if held:
        # Held text is not automatically a stranded directive. Say so here,
        # because the remedy for a stranded directive (a bare Enter) would
        # SUBMIT a human's half-typed thought, and both were live on this
        # fleet at once.
        print("  a held composer may be a stranded directive OR a human's "
              "unfinished draft — READ THE PANE before submitting anything")
    return 1 if held else 0
