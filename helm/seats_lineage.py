#!/usr/bin/env python3
"""What a seat NAME means once it stops being the seat's current key.

Split out of `seats_roster` rather than added to it: that module owns roster
PRESENCE and the admin verbs that mutate identity, and it sits on a line
budget whose whole job is to drain the facade — so a reader that only
INTERPRETS what those verbs wrote belongs beside them, not inside them.

Two things live here, and they are the two halves of one fact. `seat_lineage`
answers who a name means right now; `carry_holdings` is what a rename does
about the rows that still spell the old one.
"""
import contextlib
import threading

from .seats_common import _seat_key, _seat_label, recipient_matches


_LINEAGE_LENS = threading.local()


@contextlib.contextmanager
def lineage_lens(fn):
    """Bind this thread's display reads to one projection; restore on exit."""
    prev = getattr(_LINEAGE_LENS, "fn", None)
    _LINEAGE_LENS.fn = fn
    try:
        yield
    finally:
        _LINEAGE_LENS.fn = prev


# WHAT A SEAT NAME MEANS NOW — one resolver, because there was nearly two.
# `seat_launch_assets._launch_identity` already walked `seat_keys` to answer
# "relaunch this stale asset as WHO", and a second walker for the board would
# have been the same computation under another name, drifting apart on the
# malformed and ambiguous cases exactly where both must fail closed. It is
# lifted here, beside the writer rather than inside it, and split three ways
# because the two callers need different parts of the same answer: the
# launcher only wants the name, the board has to tell a name nothing answers
# to apart from a name that IS a live seat.
SEAT_CURRENT = "CURRENT"      # a live roster row answers to this name
SEAT_RENAMED = "RENAMED"      # no row by this name; its lineage names one
SEAT_ORPHANED = "ORPHANED"    # no row, no lineage — the dead string case
SEAT_LINEAGE_UNKNOWN = "UNKNOWN"


def seat_lineage(seat):
    """Use a bound display snapshot, or read live outside a projection."""
    lens = getattr(_LINEAGE_LENS, "fn", None)
    return lens(seat) if lens is not None else _seat_lineage_uncached(seat)


def _seat_lineage_uncached(seat, checked=None):
    """(name, state, why) — who answers to `seat` right now.

    `checked` is a `roster_checked()` answer the caller already holds, as
    `(rows, unavailable)`. A caller that measures several facts about one seat
    from ONE roster reading passes it, so the lineage and the reachability it
    sits beside cannot describe two different roster states. Omitted, this
    reads the roster itself.

    ORPHANED IS DERIVED AT READ TIME AND MUTATES NOTHING. The population it
    describes is 36 role-slots across seven names on the live board, and there
    is no successor to re-bind most of them to: renames are not how those names
    left. `write_roster` calls `reclaim_seat_key` when it ADMITS a name, the
    SessionStart pool hands the helm-claude-N defaults out and back, and NONE
    of those paths writes a tombstone or a successor. The name does not depart;
    it stops being mentioned. So this answers honestly and leaves the rows
    alone.

    ABSENCE IS NOT RETIREMENT AND THIS STATE MAY NOT EXPIRE ANYTHING. Four
    names moved absent-to-present in thirteen minutes while task/2333 was being
    measured, so "no roster row" is a TIME-VARYING predicate — the same class
    as absence-of-process-evidence-is-not-death, one layer up. ORPHANED is a
    rendering, and a caller that expired a row on it would eventually expire a
    live seat's work while that seat was between sessions.

    UNKNOWN IS ITS OWN ANSWER, never folded into ORPHANED. An unreadable
    roster, malformed lineage on ANY row, and a key claimed by two renamed rows
    all mean the question was not answered — and a caller that read those as
    "nobody answers to this name" would be building the loudest possible claim
    on the weakest possible evidence.

    `roster_checked`, DELIBERATELY, WITH ITS ALL-OR-NOTHING COST ACCEPTED.
    `roster_acquired` warns that pointing a RENDERER at the checked reader
    blanks the owner's whole roster on one type-invalid row. That warning is
    about a renderer whose JOB is to LIST seats, where blanking means showing
    nothing. This one answers a yes/no ABOUT ONE NAME, where the same failure
    means saying nothing — the mark goes quiet and the row renders as it did
    before. Fail-open rows would let one malformed row turn a live seat's
    holdings into an ORPHANED banner, which is the direction that cannot be
    taken back by the reader.
    """
    # ASK THE INPUT, NOT THE DERIVATION. `_seat_key` HASHES what it is given,
    # so it answers a perfectly well-formed key for the empty string — and
    # this guard, written against the key, let "" fall through to the walk and
    # come back ORPHANED. A name nobody gave is not a name nothing answers to.
    if not str(seat or "").strip():
        return None, SEAT_LINEAGE_UNKNOWN, "no seat name was given"
    key = _seat_key(seat)
    # DEFERRED, and it is the thinner leg of the one
    # cycle here: `rename_seat` calls into this module,
    # so the roster read cannot be a module-level import
    # back the other way.
    if checked is None:
        from .seats_roster import roster_checked
        checked = roster_checked()
    rows, unavailable = checked
    if unavailable:
        return None, SEAT_LINEAGE_UNKNOWN, (
            "the roster is unavailable, so whether %s still names a seat "
            "cannot be proven" % _seat_label(seat))
    for name in rows:
        if recipient_matches(name, seat):
            return name, SEAT_CURRENT, "a live roster row answers to this name"
    renamed = []
    for name, row in (rows or {}).items():
        aliases = (row or {}).get("seat_keys")
        if aliases is None:
            continue
        if not isinstance(aliases, list) \
                or not all(isinstance(value, str) for value in aliases):
            # FAIL CLOSED ON ANY MALFORMED ROW, not just the matching one: a
            # walk that skipped it could not have proven the key is absent
            # from the row it could not read.
            return None, SEAT_LINEAGE_UNKNOWN, (
                "roster row %r has malformed seat_keys, so seat lineage "
                "cannot be proven" % name)
        if key in aliases:
            renamed.append(name)
    renamed.sort()
    if len(renamed) > 1:
        return None, SEAT_LINEAGE_UNKNOWN, (
            "seat key %s is claimed by %d renamed rows (%s) — lineage may not "
            "guess" % (key, len(renamed), ", ".join(renamed)))
    if renamed:
        return renamed[0], SEAT_RENAMED, (
            "renamed: %s carries %s in its seat_keys lineage"
            % (renamed[0], key))
    return None, SEAT_ORPHANED, (
        "no roster row answers to %s, by name or by lineage"
        % _seat_label(seat))


def carry_holdings(old_key, new):
    """One line naming what the old NAME still held, after the row has moved.

    THE ROW MOVES AND THE OBLIGATIONS DO NOT, AND THAT IS THE DEFECT. A rename
    rewrites the roster key and every keyed state file; it has never touched a
    dispatch row, a task row or a worktree lease. Those all compare holders by
    exact canonical-token equality, so the instant `codex-8` becomes `gt-codex`
    every open row addressed to `codex-8` is a dead string with no reader:
    measured 36 role-slots across seven names that no roster row answers to.
    `helm seat reassign` has been THE ONE DOOR for exactly this since August
    and nothing has ever invoked it — a correct door with no caller, the same
    shape `stale redispatch` has. This is the caller.

    THE ARGUMENT IS THE NAME, NOT THE KEY, whatever `old_key` is spelled.
    Three spellings of one seat live in seats_roster and the carry needs
    the third: `seat_keys` stores KEYS (name plus durable hash),
    `resolve_source` prefers a SESSION, and every holder field on a
    dispatch, task or lease stores the bare NAME. `holdings` matches with
    `recipient_matches` against that name, so handing this a key made the
    census search for a token nothing holds: it answered a confident zero
    and the wiring was a silent no-op on every rename that had work to
    carry — the exact failure shape this call exists to end.

    THE OLD NAME IS WHAT TRAVELS, NEVER THE SESSION, and the difference is a
    silent no-op rather than an error. `resolve_source` asks SESSION BEFORE
    NAME by design, so handing it this row's session would resolve to whoever
    holds that session NOW — which after a rename is the TARGET — and
    `reassign` would answer "already holds these" and move nothing, at rc 1,
    on a rename that had holdings to carry. The orphaned holder key is the
    only token that names the thing being drained.

    IT RUNS AFTER THE LOCK, AND THE ORDER IS LOAD-BEARING TWICE OVER. The
    roster lock is released, so the four-ledger census cannot deadlock against
    a writer that needs the roster; and the row has ALREADY MOVED, so
    `resolve_source(old_key)` takes its ORPHAN branch and `source_disposition`
    finds no row to call live.

    THAT IS NOT A DODGE OF THE LIVE-SOURCE MUST-MISS, and it would be fair to
    read it as one. `reassign` refuses a measurably LIVE source because moving
    work out from under a running agent puts two builders on one lane. Here
    the source is a NAME nothing answers to and the target is the SAME agent
    under its new key — the work is not leaving anybody, it is following them.
    If that guard DOES fire (a second live seat still answering the old name)
    the refusal STANDS and is reported: this caller never passes `--force`,
    because an automatic caller that overrides the one must-miss converts it
    into a formality.

    AND IT NEVER FAILS THE RENAME. The row is already moved and the lock is
    already gone, so there is no state to roll back and `False` here would be
    a lie about what happened. An unmovable holding is reported in the success
    message, which is where the operator is looking.
    """
    # LAUNDERED FOR DISPLAY, RAW FOR THE DOOR, and the two must not collapse.
    # `old_key` is a roster key a hostile seat name can carry control bytes
    # into, and every string this function returns is appended to a message a
    # terminal prints — so a rename could echo a raw ESC or a bidi override
    # straight out of `seat rename`. Every other sentence in `rename_seat`'s
    # message has always gone through `_seat_label` and mine did not. The
    # census and the reassign call below still take the UNLAUNDERED token:
    # laundering is a DISPLAY property, and rewriting an identity is how a
    # holder stops matching the rows it holds.
    shown = _seat_label(old_key)
    hand = ("Run `helm seat reassign %s --to %s --apply` by hand."
            % (shown, _seat_label(new)))
    try:
        from . import seat_reassign
        # THE CENSUS DECIDES WHETHER TO RUN THE VERB; IT DOES NOT COUNT THE
        # CARRY. `holdings` matches the old name canonically, so it lists
        # rows the target already holds: a lease stored exactly as the new
        # name, a dispatch row already addressed to it. The verb moves none of
        # those and exits 0, and printing the census as "Carried N" reported
        # a carry that moved nothing (task/2529). The count comes from the
        # verb's own result below. Not from its prose either: the apply path
        # prints one line per row and no total, and a first cut that searched
        # that output for "TOTAL" found nothing on every real rename.
        before, unread = seat_reassign.holdings(old_key)
        held = sum(len(v or ()) for v in (before or {}).values())
        if not held and not unread:
            # A PROVEN-EMPTY SOURCE IS NOT REASSIGNED, and the proof is the
            # whole condition. `holdings` reads all four surfaces and reports
            # an unreadable one SEPARATELY from an empty one, so this is the
            # one case where "move nothing" is known rather than assumed — and
            # running a four-ledger mutating verb, a liveness probe and an
            # audit write to move zero rows is cost with no question behind
            # it. `unread` alone sends us down the verb's path, because it
            # already knows how to report what it could not read.
            return (" Nothing open was still spelled %s." % shown)
        # THE TOKENS ARE RAW AND THE REASON IS NOT. The first two are the
        # holder key and the target seat — identities the door matches rows
        # against — while `reason` is prose recorded on the audit event and
        # read back by a human, so it takes the same laundering the rename
        # message does. Same string, two roles, two rules.
        outcome = {}
        rc, lines = seat_reassign.reassign(old_key, new, apply=True,
                                           reason="seat rename %s -> %s"
                                                  % (shown, _seat_label(new)),
                                           outcome=outcome)
    except Exception as exc:                 # noqa: BLE001
        # UNKNOWN, NEVER ZERO. A census that did not run has not proven the
        # old name held nothing, and "nothing moved" is the one reading that
        # would stop an operator from checking.
        return (" Open rows still spelled %s could NOT be carried (%s) — "
                "whether any exist is UNKNOWN. %s" % (shown, exc, hand))
    if rc:
        # rc 1 IS THE WHOLE UNCLEAN FAMILY — a refusal, a residual, an
        # unreadable surface, an unrecorded audit row — and the verb's own
        # last line says which. Re-running it is always safe, so the remedy is
        # one sentence for all four.
        return (" Open rows still spelled %s were NOT fully carried: %s %s"
                % (shown, (lines or ["no reason recorded"])[-1].strip(),
                   hand))
    moved = outcome.get("moved")
    if not isinstance(moved, dict) or not all(
            isinstance(v, list) for v in moved.values()):
        # rc 0 PROVES NOTHING IS LEFT BEHIND; it says nothing about how much
        # moved, and a verb that reported no result has not said. A surface
        # that is not a list of rows has none to count: iterating the audit
        # bound's {count, detail} summary counted its two keys as two rows.
        return (" Open rows spelled %s were carried and nothing was left "
                "behind, but `helm seat reassign` reported no moved rows "
                "this rename could count, so HOW MANY moved is UNKNOWN."
                % shown)
    # A ROW ALREADY ADDRESSED TO THE TARGET IS IN THE MOVER'S LIST, flagged
    # `already`, because the verb counts it toward convergence. Nothing was
    # written for it, so it is not carried.
    rows = [r for v in moved.values() for r in (v or ())]
    already = sum(1 for r in rows if isinstance(r, dict) and r.get("already"))
    carried = len(rows) - already
    text = (" Carried %d open holding%s from %s (`helm seat reassign` ran on "
            "this rename and left nothing behind)."
            % (carried, "s"[:carried != 1], shown))
    if already:
        text += (" %d more already belonged to %s and needed no move."
                 % (already, _seat_label(new)))
    if unread:
        # THE OPENING CENSUS COULD NOT READ EVERY SURFACE. That leaves the
        # count above standing, because the verb read its own census under
        # the lock, and makes only the population before the rename unknown.
        return text + (" The rename's own census could not read every "
                       "surface, so how many were open under that spelling "
                       "before it is UNKNOWN.")
    if held != carried:
        text += (" The rename's own census had counted %d open under that "
                 "spelling; the count above is what the verb moved." % held)
    return text


