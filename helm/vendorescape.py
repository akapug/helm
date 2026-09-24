#!/usr/bin/env python3
"""The vendor-dialog escape — the one blocked state a keystroke can clear.

A seat that hits a vendor limit mid-session does not die and does not go idle.
It parks at a modal dialog with its work half-finished, and every liveness
surface correctly reports a live process at a prompt. The pane is the only
place the fact exists, the dialog waits forever, and the seat is discovered
hours later by a human who presses one key.

WHY THIS IS A SIBLING OF resumeturn AND NOT A BRANCH INSIDE IT. The delivery is
identical and is BORROWED, never re-implemented: `resumeturn.deliver` is the
transaction that records a generation, guards TOCTOU on pid tokens, and refuses
to spell an unreadable pane `resumed`. What differs is arrival and accounting.
resumeturn is woken by an EVENT — a compaction hook fires about one session.
This is woken by an OBSERVATION — a sweep reads pane tails and classifies them.
And the budgets must not be shared: a seat that needed four escapes has a
vendor problem, a seat that needed four resumes has a context problem, and one
counter would let either mask the other.

WHAT IT IS ALLOWED TO PRESS IS NOT DECIDED HERE. `seat_lifecycle`'s
`vendor_escape_choice` owns that, because it reads the same producer bytes the
classifier does. The rule it enforces is a whitelist of one: the option that
continues on another model at no cost. This module's job is to ask, to press
only on a positive answer, and to say out loud why it did not.

NO NEW DAEMON. The sweep is one more step on the timer that already owns
`seat resume --all`, so the escape costs an existing wake rather than a new
one.
"""

import fcntl
import os
import sys
import time

# `seat` IS THE FACADE and seat_lifecycle is an implementation
# module behind it. Reaching the chooser through the facade is not
# ceremony: it is what keeps the split honest, and the tree has a
# rung that refuses an impl import without it.
from . import harness, home, resumeturn, seat

# ---------------------------------------------------------------------------
# the budget — its OWN counters, deliberately
# ---------------------------------------------------------------------------
MAX_ESCAPES = 3
WINDOW_S = 6 * 3600.0
DEBOUNCE_S = 120.0
RELAPSE_S = 900.0

ESCAPED = "escaped"            # a key was pressed and the pane accepted it
UNVERIFIED = "unverified"      # pressed, but the pane could not be re-read
STRANDED = "stranded"          # pressed, landed in the composer, unsubmitted
HELD = "held"                  # deliberately not pressed; detail says why
UNCERTAIN = "uncertain"        # the send failed and cannot say if a key landed

#: WHAT THE `sent` FIELD SAYS WHEN NOBODY CAN SAY. An empty `sent` is a
#: CLAIM — it reads "nothing was placed" — and on an uncertain send that claim
#: is exactly as unfounded as naming a digit would be: the pane may have
#: accepted the keystroke before the reply failed. A negative result and an
#: unreadable one must not share a representation, least of all on the row an
#: operator uses to decide whether to go look at the pane. The attempted
#: option stays visible in the proof text, which is where the delivery states
#: what it tried.
UNCONFIRMED = "UNKNOWN"

_KNOBS = {"max": ("VENDOR_ESCAPE_MAX", MAX_ESCAPES),
          "window": ("VENDOR_ESCAPE_WINDOW_S", WINDOW_S),
          "debounce": ("VENDOR_ESCAPE_DEBOUNCE_S", DEBOUNCE_S),
          "relapse": ("VENDOR_ESCAPE_RELAPSE_S", RELAPSE_S)}


def _knob(name):
    env, default = _KNOBS[name]
    try:
        return type(default)(home.env(env, default))
    except (TypeError, ValueError):
        return default


def enabled():
    """The kill switch, and it is TWO switches by design.

    Its own knob disarms the escape alone. But this leg types through
    resumeturn's transaction, so a fleet that has disarmed THAT has disarmed
    every helm keystroke into a pane, and honouring only the local knob would
    leave a disabled injector still injecting under another name.
    """
    mine = str(home.env("VENDOR_ESCAPE", "1")).lower() not in ("0", "off", "no")
    return mine and resumeturn.enabled()


def state_path():
    return os.path.join(home.helm_home(), home.GLOBAL, ".state",
                        "vendorescape.json")


def _decide(entry, now):
    """(action, detail) from this seat's prior escapes — escape|debounce|
    relapse|capped.

    A ROLLING LIST, not a latch, for the same reason resumeturn keeps one: a
    latch either sticks forever or re-arms on a clock, and a seat may
    legitimately meet a new limit tomorrow.

    RELAPSE IS THE ONE THAT IS NOT A COPY. If a seat parks again shortly after
    an escape, the switch it accepted did not hold — the replacement model is
    walled too, or the dialog returned unchanged. Pressing again would walk the
    seat down a list of options this module is not allowed to finish, so it
    stops and says so.
    """
    at = sorted(t for t in (entry or {}).get("at", [])
                if isinstance(t, (int, float)) and now - t < _knob("window"))
    if not at:
        return "escape", ""
    gap = now - at[-1]
    if gap < _knob("debounce"):
        return "debounce", ("the same dialog — an escape fired %.0fs ago and "
                            "the pane has not settled" % gap)
    if gap < _knob("relapse"):
        return "relapse", ("this seat parked again %.0fs after an escape, so "
                           "the switch it accepted did not hold; a second "
                           "press walks it down options helm may not choose"
                           % gap)
    if len(at) >= _knob("max"):
        return "capped", ("%d escapes already in the last %.0fh"
                          % (len(at), _knob("window") / 3600.0))
    return "escape", ""


def _peek(name):
    from . import pk
    return (pk.read_json(state_path(), {}) or {}).get(name)


def _count(name, now, outcome, seen, sent):
    """Append this escape's stamp under the state lock -> the written entry.

    NEVER RAISES, for resumeturn's reason and this one: the press has already
    happened by the time this runs, so a failed write must cost the budget's
    memory and never the row that tells an operator a key went into their pane.
    """
    try:
        from . import pk
        p = state_path()
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p + ".lock", "a") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            st = pk.read_json(p, {}) or {}
            entry = st.get(name) or {}
            at = [t for t in entry.get("at", [])
                  if isinstance(t, (int, float)) and now - t < _knob("window")]
            at.append(now)
            entry.update({"at": at, "outcome": outcome, "saw": seen,
                          "sent": sent, "last_at": now})
            st[name] = entry
            pk.write_json(p, st)
            return entry
    except Exception as e:                            # noqa: BLE001
        # NAMED BY THE VERB THAT RUNS IT, not by this module. This leg is a
        # step on the fleet sweep and there is no verb of its own to type, so
        # prefixing the module name would shape the line like a command and
        # promise something the parser cannot dispatch — which is exactly what
        # a reader would then go looking for. The tree carries a rung that
        # refuses command-shaped text naming an undispatchable verb, and it
        # reads comments as well as strings — so this note states the rule
        # without spelling the shape it forbids.
        print("helm seat resume --all: vendor-escape budget stamp skipped "
              "(%s) — the escape itself already happened" % e,
              file=sys.stderr)
        return None


def _log(name, outcome, handle, seen, sent, detail):
    """One row per seat per episode, carrying the four facts an operator needs.

    THE PANE IS THE ONLY PLACE THIS EVIDENCE EXISTS and it is a viewport, so it
    is gone by the next screenful. A row that says only "escaped" is unusable
    later: what the dialog offered and what was typed are the difference
    between a working escape and a machine pressing something nobody sanctioned.
    """
    return ("vendor-escape %s seat=%s pane=%s saw=%r sent=%r%s"
            % (outcome.upper(), name, handle or "-", seen or "",
               sent or "", (" — " + detail) if detail else ""))


def consider(name, state, escape, handle=None, session=None, pids=None,
             adapter=None, now=None, deliver=None):
    """(outcome, line) for ONE seat. Presses at most one key.

    NO PANE TEXT ENTERS THIS MODULE. `state` and `escape` both come from the
    liveness row, which computed them where the tail was read; `escape` is that
    row's decision — a digit, a classification and one truncated option label.
    A viewport is raw external content and an actuator is the last place it
    should be re-interpreted, so this module acts on a verdict it cannot
    second-guess rather than on bytes it would have to re-parse.

    `state` is a GATE, not a hint: only BLOCKED_ON_VENDOR_PROMPT is eligible. A
    mid-turn pane classifies RUNNING because live affordances outrank printed
    lines, and a quota wall classifies BLOCKED_ON_QUOTA, which no keystroke
    clears.
    """
    now = time.time() if now is None else now
    if not enabled():
        return HELD, _log(name, HELD, handle, "", "", "escape is disarmed")
    if state != "BLOCKED_ON_VENDOR_PROMPT":
        return HELD, _log(name, HELD, handle, "", "",
                          "state is %s, and only a vendor dialog is escapable"
                          % state)
    # A MISSING DECISION IS NOT AN ABSENT OPTION. The row carries `escape` on
    # exactly this state, so its absence means the row was built by something
    # that does not speak this contract — refuse rather than infer one.
    if not escape:
        return HELD, _log(name, HELD, handle, "", "",
                          "the row claims a vendor dialog but carries no "
                          "escape decision, so nothing here was measured")
    digit = escape.get("digit")
    kind = escape.get("kind")
    seen = escape.get("seen") or ""
    if kind != seat.ESCAPE_CONTINUE:
        return HELD, _log(name, HELD, handle, seen, "",
                          _refusal_text(kind))
    action, detail = _decide(_peek(name), now)
    if action != "escape":
        return HELD, _log(name, HELD, handle, seen, "",
                          "%s — %s" % (action, detail))
    # THE AUTHORITY IS NOT INSPECTED HERE, AND THAT IS THE WHOLE CURE.
    # A presence check over the row's fields cannot answer this question and
    # gets it wrong in BOTH directions: a REGISTERED row legitimately carries
    # no pids (the delivery selects its adopted transaction ON pids, so adding
    # them would route a registered seat down the wrong path), while an
    # ADOPTED row can carry pids that the authorizer then refuses — an
    # unstamped or recycled slot is rejected before a key is typed. Guessing
    # from field presence held every registered seat and mislabelled every
    # adopted refusal.
    #
    # THE OWNER DECIDES, AND IT TELLS US. `deliver` mints a GENERATION at the
    # moment text is actually typed into the pane and hands it to `on_submit`.
    # So `gen is None` means NOTHING WAS TYPED — whatever refused, wherever it
    # refused, including a dirty or unreadable pre-read and a failed send that
    # never reach the typing callback at all. A generation that exists and a
    # submission that cannot be confirmed is the ONLY thing that earns
    # STRANDED, and it is the only case where a key is really sitting in a
    # composer.
    send = deliver or resumeturn.deliver
    typed = {"generation": None, "observed": False, "state": None}

    def on_submit(_ad, _handle, state, _proof, gen):
        typed["observed"] = True
        typed["generation"] = gen
        typed["state"] = state

    # THE DIGIT THIS MODULE HOLDS IS AN ASSESSMENT, NEVER AN INSTRUCTION.
    # `escape` was computed when the pane was READ, and a dialog can repaint,
    # reorder or close between that read and this send. So what travels is the
    # INTENT — the free-continue classification — and the adapter re-derives
    # the option from the pane it is about to type into. If the dialog is gone,
    # or no longer offers a free way out, nothing is typed at all. The digit
    # stays only as evidence for the row.
    # WHAT THE LEDGER AND THE ROW REPORT IS WHAT THE PANE ACTUALLY RECEIVED.
    # `digit` above is the ASSESSED option, read when the pane was classified;
    # the delivery derives its own at the keystroke and can legitimately press
    # a different one. Recording the assessed digit made the operator row
    # contradict the delivery proof printed beside it — the row said sent='2'
    # while the proof said choice '3'. So the placed text is captured HERE, on
    # the same callback that mints the generation, and nothing else is ever
    # reported as sent.
    placed = {"text": ""}

    def choose(ad, handle_, on_typed):
        def note(handle_seen, text):
            placed["text"] = text
            if on_typed is not None:
                on_typed(handle_seen, text)
        return ad.choose_in_modal(handle_, seat.ESCAPE_CONTINUE,
                                  on_typed=note)

    mode, proof = send(name, digit, session, adapter=adapter, pids=pids,
                       on_submit=on_submit, operation=choose)
    # AN UNCERTAIN SEND IS NOT A NO-KEY. The adapter reports UNCERTAIN when the
    # send failed in a way that cannot say whether the pane accepted input —
    # and that failure happens BEFORE the typed callback, so it is
    # indistinguishable here from a clean refusal. Publishing "no key reached
    # the pane and no budget was spent" about it would be a claim nobody
    # measured, so it gets its own outcome and DOES spend budget: a seat whose
    # sends keep failing ambiguously must not be retried without limit.
    if typed["state"] == harness.UNCERTAIN:
        _count(name, now, UNCERTAIN, seen, UNCONFIRMED)
        return UNCERTAIN, _log(name, UNCERTAIN, handle, seen, UNCONFIRMED,
                               "the send failed in a way that cannot say "
                               "whether the key arrived, so this is neither a "
                               "proven press nor a proven refusal — a human "
                               "should look — %s" % proof)
    nothing_typed = not typed["observed"] or typed["generation"] is None
    if nothing_typed:
        # NO BUDGET FOR A KEY THAT NEVER LEFT. The budget rations PRESSES; a
        # refusal upstream of the keystroke is not one, and charging for it
        # would ration a seat out of escapes it never received.
        return HELD, _log(name, HELD, handle, seen, "",
                          "the delivery refused before anything was typed, so "
                          "no key reached the pane and no budget was spent — "
                          "%s" % proof)
    outcome = ESCAPED if mode == "resumed" else (
        STRANDED if mode == "manual" else UNVERIFIED)
    _count(name, now, outcome, seen, placed["text"])
    if outcome == ESCAPED:
        return ESCAPED, _log(name, ESCAPED, handle, seen, placed["text"], proof)
    if outcome == STRANDED:
        return STRANDED, _log(name, STRANDED, handle, seen, placed["text"],
                              "the key WAS typed and its submission was NOT "
                              "confirmed, so the dialog is still up with a "
                              "stray keystroke in front of it — a human should "
                              "look — %s" % proof)
    return UNVERIFIED, _log(name, UNVERIFIED, handle, seen, placed["text"],
                            "the key was typed but the pane could not be "
                            "re-read, so this is NOT a proven escape — %s"
                            % proof)


def _refusal_text(kind):
    if kind == seat.ESCAPE_SPEND:
        return ("the only way out of this dialog spends the owner's money, "
                "which helm never presses")
    if kind == seat.ESCAPE_HUMAN:
        return ("the only way out asks a person for budget, which is not "
                "helm's to send")
    return ("the dialog offered no option that continues at no cost, so the "
            "seat stays parked and visible")


def sweep(seats, apply=False, liveness=None, consider_fn=None):
    """[line] — one row per seat that is parked at a vendor dialog.

    RIDES AN EXISTING WAKE, MINTS NO DAEMON. This is a step on the sweep that
    already walks the fleet after a reboot and on the timer, so the escape
    costs a pane read per live seat rather than a process of its own.

    A SEAT THAT IS NOT PARKED PRODUCES NO ROW. The sweep prints a table an
    operator reads, and a line per healthy seat saying nothing happened is the
    noise that makes a real row invisible.

    DRY RUN REPORTS AND DOES NOT PRESS. Without `apply` this states the
    decision it would act on and never reaches the delivery, so the budget is
    not spent and no key is sent — the same contract the surrounding sweep
    holds for every other act it can take.
    """
    read = liveness or _liveness
    act = consider_fn or consider
    lines = []
    for name in seats:
        try:
            # OBSERVE-ONLY, ALWAYS — not only on the dry run. The registered
            # pane resolver REPAIRS a stale handle by default and that rewrite
            # lands in the spawn register, so a survey that promised to change
            # nothing would have changed the register on exactly the panes
            # whose handles had drifted. The escape's own write is the
            # keystroke and nothing else.
            info = read(name, repair=False)
        except Exception as e:                        # noqa: BLE001
            # A failed read is a fact about the INSTRUMENT. It must not read as
            # "this seat is fine" and must not stop the seats after it.
            lines.append("vendor-escape UNKNOWN seat=%s — could not read the "
                         "pane: %s" % (name, str(e)[:80]))
            continue
        if (info or {}).get("state") != "BLOCKED_ON_VENDOR_PROMPT":
            continue
        escape = info.get("escape")
        if not apply:
            kind = (escape or {}).get("kind") or "unreadable"
            lines.append("vendor-escape WOULD-%s seat=%s saw=%r — DRY RUN"
                         % (kind.upper(), name, (escape or {}).get("seen", "")))
            continue
        _outcome, line = act(name, info["state"], escape,
                             handle=info.get("handle"),
                             session=info.get("session"),
                             pids=info.get("pids"))
        lines.append(line)
    return lines


def _liveness(name, repair=True):
    return seat.seat_liveness(name, repair=repair)
