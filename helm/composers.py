#!/usr/bin/env python3
"""helm seat composers — the fleet probe for HELD-BUT-UNSENT composer text.

THE DEFECT THIS MAKES VISIBLE (owner-reported, plural and recurring: "these
keep landing and not hitting enter"). A seat's own resume directive gets typed
into its composer and never submitted. The seat then sits with its next
instruction on screen doing nothing — and reads IDLE-AND-HEALTHY to
seat_liveness, to proxywatch and to the beacons, because every one of those
asks "is this pane alive?" and the pane is perfectly alive. Until this verb
existed the only instrument that could see it was the owner's eyes.

KEYSTROKE-FREE BY DEFAULT, ON PURPOSE, AND THIS IS A SAFETY PROPERTY. A scan
never sends a keystroke; it may timestamp a matching Helm injection so a later
independent scan can prove persistence. The explicit `--submit HANDLE` actuator
still refuses unless both identity and persistence are proven. Measured on the
live fleet the hour it was written, three panes held
unsent composer text and they were three DIFFERENT things: a stranded helm
directive ("Continue with the OPEN Codex dispatch."), a walled seat's leftover
"hi", and the OWNER MID-SENTENCE. A probe that auto-fired Enter would have
submitted the owner's half-typed thought. Unmatched text is therefore surfaced
only; the guarded actuator is available solely for exact recorded matches.

FIVE ANSWERS, NEVER TWO. `cannot-tell` is a first-class outcome and is never
folded into `clear`:

  held           unmatched text that may be a human draft
  helm-pending   exact recorded injection, seen once
  helm-stranded  the same exact injection on a later independent reading
  clear          empty composer, or known placeholder chrome
  cannot-tell    unreadable pane or unlocatable composer

Measured 2026-08-05: of 26 live panes, TWELVE answered `terminal read` with an
empty tail and every cursor pinned at 0, while connected/writable/orphaned all
said fine — and at least one of the twelve was a seat that had posted to chat
minutes earlier. Their emptiness is a READ-SURFACE LIMITATION, not evidence
about the seat. Reporting them as clear would be an all-clear over half a fleet
nobody looked at, which is the same shape of false negative this bug lives in.
"""
import json
import sys
import time

HELD = "held"
HELM_PENDING = "helm-pending"
HELM_STRANDED = "helm-stranded"
CLEAR = "clear"
CANNOT_TELL = "cannot-tell"
STATES = (HELD, HELM_PENDING, HELM_STRANDED, CLEAR, CANNOT_TELL)

#: what the act door answers about the attempt a repair's stranded text was
#: typed under (`resumeturn._repair_recovery_verdict`) -> the census sentence.
REPAIR_STRANDED = {
    "": ("composer exactly matches a persistent Helm injection and is "
         "eligible for guarded bare-Enter recovery: the repair attempt that "
         "typed it may still act, and --submit re-asks its standing and "
         "horizon, the family pause and the owed row at the moment of action"),
    "stale": ("composer exactly matches a persistent Helm injection, but the "
              "repair attempt that typed it may no longer act ({why}), so "
              "--submit will refuse it and always will. Identity is proven: "
              "read the pane and resolve the held text by hand"),
    "unknown": ("composer exactly matches a persistent Helm injection typed "
                "under a repair authorization that could not be re-read "
                "({why}); --submit re-asks it and refuses while it cannot be "
                "read"),
}

def classify(pane, tail, read_error=None, injection=None, now=None):
    """One pane -> (state, composer_text, why).

    `tail` is the pane's bounded tail text, or None when it could not be read.
    `pane` is a `harness._pane_row` mapping — `last_output_at` is orca's own
    statement that it has never observed output from this pane, and it is the
    discriminator that keeps a blind pane out of the clear bucket. `injection`
    is an unexpired resume-turn record for this exact handle; it changes the
    answer only when the current composer equals its full text exactly.
    """
    from . import harness
    from .seat_lifecycle import _current_prompt_line
    if read_error:
        return CANNOT_TELL, None, "pane read failed: %s" % read_error
    # `last_output_at` null EXPLAINS an unusable tail; it does not replace
    # reading one. It used to return here, ahead of both checks below, so a
    # pane orca had never seen output from was never classified even when its
    # tail was perfectly readable and held a composer — and that pane is the
    # one this module exists to find. It remains the discriminator that keeps a
    # blind pane out of the CLEAR bucket, which is the property the paragraph
    # above promises and the reason it is consulted at all; it is now consulted
    # where an ANSWER IS ACTUALLY MISSING rather than before we have looked.
    blind = pane.get("last_output_at") is None
    if not (tail or "").strip():
        return (CANNOT_TELL, None,
                "the metaharness has never observed output from this pane "
                "(lastOutputAt null) and its tail is empty — that says "
                "nothing about the seat" if blind else
                "pane returned an empty tail")
    line = _current_prompt_line(tail)
    if line is None:
        return (CANNOT_TELL, None,
                "the metaharness has never observed output from this pane "
                "(lastOutputAt null) and no composer could be located in its "
                "tail" if blind else
                "no current composer could be located in the tail (newer "
                "output sits below the last prompt)")
    body = harness.composer_body(line)   # ONE owner — see its docstring
    if not body:
        return CLEAR, "", "composer empty"
    if harness.composer_is_placeholder(body):
        return CLEAR, body, "composer holds only placeholder chrome"
    exact = (harness.composer_exactly_holds(tail, injection.get("text"))
             if injection else False)
    if exact is None:
        return (CANNOT_TELL, body,
                "a recorded injection may be present, but the full composer is "
                "not visible enough to prove exact identity")
    if exact:
        from . import resumeturn
        stamp = time.time() if now is None else now
        if not resumeturn.injection_persistent(injection, now=stamp):
            return (HELM_PENDING, body,
                    "composer exactly matches Helm's recorded injection; a "
                    "later independent reading must prove persistence")
        # A TEXT A REPAIR TYPED IS RECOVERED ONLY THROUGH THAT REPAIR'S ACT
        # DOOR, so the census asks the door's own judges of the attempt before
        # it calls the strand eligible, and neither the eligible sentence nor
        # the horizon sentence below is true of a strand the door refuses.
        kind, verdict = resumeturn._repair_recovery_verdict(injection,
                                                            now=stamp)
        if kind is not None:
            return HELM_STRANDED, body, REPAIR_STRANDED[kind].format(
                why=verdict)
        if injection.get("expired"):
            # IDENTITY PROVEN, AUTHORIZATION LAPSED — and those are different
            # sentences. This used to read as HELD/"a human's unfinished
            # draft" because the record was dropped at the TTL, which is the
            # one classification that makes the pane unrecoverable: autocompact
            # refuses on unsent input, submit refuses because it cannot prove
            # ownership, resume-turn refuses. Three correct guards, no exit.
            # It is still HELM_STRANDED — the state is about whose text is
            # stuck, not about how long ago we said so — and the discriminator
            # carries the part that actually changed.
            return (HELM_STRANDED, body,
                    "composer exactly matches a Helm injection PAST ITS "
                    "FRESHNESS HORIZON — identity is proven: the live "
                    "composer equals the recorded text exactly, the record's "
                    "digest validates against that text, it is the only "
                    "VALID record under this handle and it carries a "
                    "generation — so this is Helm's text and not a human "
                    "draft. The horizon does not refuse recovery; --submit "
                    "re-reads the pane at the moment of action and proceeds "
                    "if it still holds this exact text")
        return (HELM_STRANDED, body,
                "composer exactly matches a persistent Helm injection and is "
                "eligible for guarded bare-Enter recovery")
    return HELD, body, "unmatched text may be a human's unfinished draft"


def scan(adapter=None, limit=200):
    """([row], error) — census rows, with expected read errors CANNOT_TELL.

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
    from . import resumeturn
    now = time.time()
    # Census ownership includes expired records, just as guarded public
    # recovery does. Ambiguous handles have no authoritative associated record.
    injections = resumeturn.recorded_injections(now=now, include_expired=True)
    rows = []
    for p in panes:
        handle = p.get("handle")
        injection = injections.get(handle)
        # last_output_at never suppresses a read. An associated injection DOES
        # require exclusive durable authority first: seeing an edit and then
        # failing to save its invalidation must not leave old persistence
        # reusable. Unassociated/ambiguous panes remain ordinary read-only census.
        token = resumeturn._claim_injection_observation(injection) if injection else None
        state, body, why = (CANNOT_TELL, None,
                            "injection observation reservation unavailable; pane not read")
        if not injection or token:
            tail, err = None, None
            try:
                tail = ad.read(handle, limit=limit)
            except (harness.HarnessError, OSError) as e:
                err = str(e)
            exact = (harness.composer_exactly_holds(tail, injection["text"])
                     if injection and err is None else None)
            observed_at = time.time()
            # Reconcile the actual equality observation BEFORE fallible
            # classification/formatting. No generic finally-release can undo
            # a failed finalization; an unexpected exception retains the token.
            observed = (resumeturn._finish_injection_observation(
                injection, token, exact, observed_at) if token else None)
            if not token or observed is not None:
                state, body, why = classify(p, tail, read_error=err,
                                            injection=observed, now=observed_at)
            else:
                why = ("injection observation finalization failed or reservation changed; "
                       "recovery authority not released by this census")
        rows.append({"handle": handle, "title": p.get("title") or "",
                     "worktree": p.get("worktree") or "",
                     "state": state, "composer": body, "why": why})
    return rows, None


def submit(handle, adapter=None, now=None):
    """Guarded actuator -> (tri-state, detail). Never types or clears text."""
    from . import harness, resumeturn
    stamp = time.time() if now is None else now
    # READ WITH IDENTITY, REFUSE ON FRESHNESS, AND MEASURE FRESHNESS LIVE.
    # include_expired is not a leniency: identity is a property of the RECORD
    # and does not decay, while "is this still what the composer holds" is a
    # property of the PANE and is answered below by reading it. An operator who
    # sees a refusal here should be able to act on the sentence it gives them,
    # which the old age-based wording could not support.
    injection = resumeturn.recorded_injections(
        now=stamp, include_expired=True).get(handle)
    if not injection:
        return harness.UNKNOWN, ("pane %s has no unique Helm injection record"
                                 % handle)
    if not resumeturn.injection_persistent(injection, now=stamp):
        return harness.UNKNOWN, ("pane %s has not remained held across the "
                                 "required persistence interval" % handle)

    # The recovery owner persists its claim BEFORE reading the composer. An
    # observed edit must not outlive a failed persistence reset: that claim
    # stays consumed if reset fails. A pre-claim read cannot provide this.
    # This door brings no admission of its own: a text a repair typed meets
    # the act door of the authorization it was typed under, which the
    # recovery re-reads from the record (`resumeturn._repair_admission`).
    return resumeturn.recover_injection(injection, adapter=adapter)


def cmd_composers(rest):
    """seat composers [--json] [--submit HANDLE] — probe or guarded recovery."""
    if "--submit" in rest:
        i = rest.index("--submit")
        handle = rest[i + 1] if i + 1 < len(rest) else ""
        from . import harness
        state, detail = submit(handle)
        if "--json" in rest:
            print(json.dumps({"handle": handle, "state": state,
                              "detail": detail}, indent=2, sort_keys=True))
        else:
            print("helm seat composers: %s — %s" % (state, detail))
        return 0 if state == harness.DELIVERED else 1
    rows, err = scan()
    if "--json" in rest:
        counts = {s: sum(1 for r in rows if r["state"] == s) for s in STATES}
        print(json.dumps({"panes": rows, "counts": counts, "error": err},
                         indent=2, sort_keys=True))
        unsafe = counts[HELD] + counts[HELM_PENDING] + counts[HELM_STRANDED]
        return 0 if err is None and not unsafe else 1
    if err:
        print("helm seat composers: no pane inventory — " + err,
              file=sys.stderr)
        return 2
    by_state = {s: [r for r in rows if r["state"] == s] for s in STATES}
    held, pending, stranded, clear, blind = (
        by_state[s] for s in
        (HELD, HELM_PENDING, HELM_STRANDED, CLEAR, CANNOT_TELL))
    for r in stranded:
        print("OURS! %-42s %s" % (r["handle"], (r["title"] or "-")[:34]))
        # PRINT THE ROW'S OWN DISCRIMINATOR, NOT A FIXED SENTENCE. The line
        # here used to read "--submit may recover it" for EVERY stranded row —
        # including one whose injection authorization has EXPIRED, where
        # `submit` MUST refuse. So the census recommended a command it knew
        # would not work, and dropped the `why` that already said so. classify writes the correct sentence for each
        # case; the census's job is to carry it, not to summarise it away.
        print("        %s" % (r["why"] or "recorded injection"))
    for r in pending:
        print("OURS? %-42s %s" % (r["handle"], (r["title"] or "-")[:34]))
        print("        recorded injection; waiting for a later persistence read")
    for r in held:
        print("HELD  %-42s %s" % (r["handle"], (r["title"] or "-")[:34]))
        print("        composer: %s" % r["composer"][:110])
    for r in blind:
        print("?     %-42s %s" % (r["handle"], r["why"][:70]))
    print("%d held · %d helm-pending · %d helm-stranded · %d clear · "
          "%d cannot-tell (of %d pane%s)"
          % (len(held), len(pending), len(stranded), len(clear), len(blind),
             len(rows), "" if len(rows) == 1 else "s"))
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
    return 1 if held or pending or stranded else 0
