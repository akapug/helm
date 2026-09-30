#!/usr/bin/env python3
"""helm seats — the review-spiral rung and the pair-meld turn rung.

MOVED VERBATIM OUT OF `seats_stop_signals`, which sat at 986 of its 1000-line
budget with the pair-meld rung just added. Both rungs here ask one question
of the stopping seat: has its review chain become a conversation that belongs
in a meld, and is a meld turn its to take? Neither shares state with the
beacon block or the cheap probes that stay behind, so the cut is clean.
`seats_stop_signals` re-exports every name, so `seats.py`, the stop guard and
the tests keep their import path; the swallow labels keep their old spelling,
so a breadcrumb reads the same after the move.

Downward edges only: this module stands on `seats_stop_fp` (the latch path and
the off switch) and `seats_cursor` (the latch writer), never on the facade it
left.
"""
import hashlib
import os
import re
import time

from . import chat, pk, record, review_door
from .seats_common import SEAT_BYTES, STATUS_BYTES, _clip, _scrub
from .seats_cursor import _write_stop_latch
from .seats_stop_fp import _off, _stop_fp_path


SPIRAL_LATCH = "stopspiral"
# The message's whole point is a command the seat can PASTE, which rules out
# laundering the identities at the sink: mangling a name to make it safe also
# makes the command wrong. The beacon block above solves this the only honest
# way — validate at the seam, then interpolate something provably inert — and
# this rung copies it. `dispatches._TOKEN` is the seat-name shape the ledger
# already enforces on senders; lanes get the same treatment. A name that does
# not match is not scrubbed into something plausible, it yields NO FINDING:
# refusing to speak is always available, and a guard is allowed to be silent.
_SPIRAL_LANE = re.compile(r"[A-Za-z0-9._/-]{1,160}\Z")
def _spiral_meld(peer, lane, prescription="MELD", chain=None):
    """The literal cure command, filled in with the real peer and lane."""
    return review_door.meld_invite(peer, lane, prescription, chain)
def _my_meld_record(st, own):
    """True/False/None: is meld state `st` THIS seat's own record?
    A meld writes ONE FILE PER PARTY, each carrying `self` for its owner
    while `peer`/`peers` name only the OTHER side, so ownership is answered
    by `self` — never by seeking my own name in `peers`, where it never
    appears. Both callers filtered on `peer` alone while `chat_dir()` holds
    EVERY seat's files (task/1112, 1145, 1146: one defect, two sites).
    `own` IS THE SEAT `_spiral_gate` WAS CALLED FOR, threaded from its argument;
    AMBIENT IDENTITY MUST NOT BE CONSULTED HERE — resolving it inside these
    matchers is what let seat A's block be suppressed by seat B's meld, and this
    line said otherwise until a review caught the CONTRACT gone stale while the
    code was already right. None needs no per-caller branch: both skip not-True."""
    if not own:
        return None
    return str(st.get("self") or "").casefold() == str(own).casefold()


def _meld_open_with(peer, span_h, own, now=None):
    """(True, room, age_s) iff an OPEN, un-converged meld with `peer` exists
    inside the spiral's window, else (False, reason, None).

    THIS DOES NOT SUPPRESS THE BLOCK, and that distinction is the whole point.
    `_melded_with` refuses to let a merely-opened meld buy silence — "the gate
    is disarmed by the cheapest possible gesture" — and that law stands. What
    was wrong was the PRESCRIPTION: the block told a seat to run
    `helm chat meld invite <peer> ...` when that seat had already run it and was
    waiting. Measured twice on lr-build-batches-its-git (room opened
    07:52Z, guard prescribed the same invite again at 10:43Z) and once on
    claim-refuses-a-label-whose-row-is-elsewhere.

    So the seat is still walled — it has not converged anything — but it is told
    the TRUE next action (the peer has not entered; chase or escalate) instead
    of an action it already took. A gate that prescribes a completed step reads
    as not having noticed, and a seat that cannot tell "you are ignoring me"
    from "I have not seen you" learns to discount both.

    FAILS CLOSED TO NO-OPEN-ROOM, same as its sibling: an unreadable meld
    directory reports nothing rather than inventing a room, because this text
    goes on screen and a phantom room is worse than a stale prescription.

    Room names are scrubbed and clipped AT THE READ, not at the emit — the
    contract its sibling establishes, so a future caller cannot undo it by
    forgetting.

    ELEMENT TWO IS A ROOM ON SUCCESS AND A REASON ON FAILURE, matching the
    sibling's shape. Only the `if open_room:` guard keeps the two apart, and a
    mutation that removed it printed "room no open meld with codex inside the
    window" into live stop-guard text. Never read element two without checking
    element one — the type is the same and the meaning is not."""
    import glob as _glob
    from . import dispatches
    now = time.time() if now is None else now
    try:
        span_s = max(0.0, float(span_h or 0)) * 3600.0
    except (TypeError, ValueError):
        return False, "unusable span", None
    # THE OBSERVED TIP SPAN IS NOT THIS QUESTION'S WINDOW. review_spiral
    # computes span_h as (newest tip - oldest tip)/3600, so it measures how
    # tightly a chain's rounds CLUSTERED; at zero -- a batch re-dispatch, every
    # tip in one second -- the window collapses to the 1.0s tolerance below and
    # a meld opened seconds ago is invisible, which is the task/981 defect this
    # function exists to cure. The floor is the spiral's own window, the one
    # the block's text cites and the one those tips were collected inside, so
    # span_h can never exceed it. DELIBERATELY NOT APPLIED TO `_melded_with`:
    # that mirror SUPPRESSES the block, so widening it lets an older
    # convergence buy silence, while this one only replaces a prescription
    # with a truer sentence. The asymmetry is about what each AUTHORIZES.
    # Full reasoning and measurements: task/2256.
    span_s = max(span_s, float(dispatches.SPIRAL_WINDOW_H) * 3600.0)
    floor = now - span_s
    try:
        paths = _glob.glob(os.path.join(chat.chat_dir(), "*.meld.*.json"))
    except OSError as e:
        return False, "meld state unreadable (%s)" % type(e).__name__, None
    for path in paths:
        st = pk.read_json(path, None)
        if not isinstance(st, dict):
            continue
        # THE MIRROR OF THE SIBLING'S FILTER: it keeps only converged rooms, this
        # keeps only UN-converged ones. Neither can fire for the same room, so
        # the two can never both speak about one meld.
        if str(st.get("status") or "") in ("done", "done-mutual"):
            continue
        who = [st.get("peer")] + list(st.get("peers") or [])
        if peer not in [str(x) for x in who if x]:
            continue
        # AND IT MUST BE MY OWN RECORD — see `_my_meld_record`.
        if _my_meld_record(st, own) is not True:
            continue
        try:
            when = float(st.get("epoch") or 0)
        except (TypeError, ValueError):
            continue
        if when + 1.0 >= floor:
            return (True,
                    _clip(_scrub(str(st.get("room") or "?")), SEAT_BYTES),
                    max(0.0, now - when))
    return False, "no open meld with %s inside the window" % peer, None


# Moved to review_door, the one owner of what a converged meld is; the
# name stays here for the stop rung and the seats facade.
_melded_with = review_door.melded_with


def _spiral_gate(session, room, seat, dispatch_snapshot=None, spiral=None):
    """(block | None, warn | None); `spiral` is review_spiral's own answer."""
    if _off("STOP_GUARD_SPIRAL") or not seat:
        return None, None
    from . import dispatches
    try:                      # fail-open TOTAL — a Stop rung must never wedge
        info, err = spiral or dispatches.review_spiral(seat, snap=dispatch_snapshot)
    except Exception as _swallowed:
        record.swallow("seats_stop_signals._spiral_gate", _swallowed)
        return None, None
    if err:
        # THE COMMENT WAS ALREADY RIGHT AND THE CODE DID NOT CARRY IT: "UNKNOWN
        # rounds are not zero rounds". Both returned silence, so a seat the
        # detector could not KEY ON looked exactly like a seat with nothing to
        # report — and one seat ran TEN rounds inside that silence. Still
        # fail-open (a Stop rung may never wedge a turn), now audible.
        return None, ("[helm stop-guard] review-spiral rung could not measure "
                      "this seat: %s — rounds are UNKNOWN, not zero."
                      % _clip(_scrub(str(err)).strip(), STATUS_BYTES))
    if not info:
        return None, None     # measured, and genuinely no spiral
    lane = str(info.get("lane") or "")
    peer = str(info.get("peer") or "")
    rounds = int(info.get("rounds") or 0)
    if not _SPIRAL_LANE.fullmatch(lane) or not dispatches._TOKEN.fullmatch(peer):
        return None, None     # cannot be quoted inertly -> not said at all
    if rounds < dispatches.SPIRAL_MELD_ROUNDS:
        return None, None
    cure = _spiral_meld(peer, lane, info.get("prescription") or "MELD",
                        info.get("chain"))
    warn = (
        "[helm stop-guard] two review rounds on lane '%s' (peer %s). At two "
        "rounds the cure is a meld, never round three — if round two comes "
        "back with findings, converge them live instead of sending a "
        "third:\n  %s" % (lane, peer, cure))
    # THE LATCH KEYS ON THE CHAIN, not the lane string — the same defect this
    # rung's detector just stopped making. Two unrelated chains reusing one lane
    # name reach `rounds` independently, and a lane-keyed fingerprint let the
    # first one's latch swallow the second one's block. `chain` is opaque here
    # (an id, or "lane:<name>" for a legacy row) and is hashed, never printed,
    # so nothing needs to quote it.
    # An advisory is not a served block. Same-tip new evidence can change
    # FINISH to UNKNOWN/MELD without adding a round; that transition must fire.
    prescription = info.get("prescription") or "MELD"
    fp = hashlib.blake2b(("%s|%s|%d|%s" % (
        info.get("chain") or lane, lane, rounds, prescription))
        .encode("utf-8"), digest_size=8).hexdigest()
    path = _stop_fp_path(room, seat, session, kind=SPIRAL_LATCH)
    try:
        with open(path) as f:
            last = f.read().strip()
    except OSError:
        last = None
    if last == fp:
        return None, None                 # already pointed at this exact state
    try:
        latched = _write_stop_latch(path, seat, fp, session=session)
    except OSError:
        latched = False
    if rounds < dispatches.SPIRAL_BLOCK_ROUNDS:
        return None, warn
    evidence = str(info.get("finding_evidence") or
                   "finding trajectory UNKNOWN: no typed verdict observations")
    if prescription in dispatches.SPIRAL_ADVISORY_PRESCRIPTIONS:
        return None, ("[helm stop-guard] review tripwire — lane '%s' at "
                      "%d distinct tips in the last %dh: %s — %s. Not "
                      "blocking this stop; this is not landing approval."
                      % (lane, rounds, dispatches.SPIRAL_WINDOW_H,
                         prescription, evidence))
    if prescription == "UNDER-ARMED":   # T3: a bar meld, not an advisory
        evidence = "UNDER-ARMED, so the meld agrees the BAR: " + evidence
    warn += "\n  MELD — " + evidence
    # #70: THE CURE MUST NOT COUNT AS THE DISEASE. A seat that already
    # converged a meld with this peer inside this spiral's window has done
    # exactly what the block prescribes, and the ONE round that carries the
    # convergence is what re-arms the gate. Suppress the BLOCK, keep the WARN
    # — the seat still sees the state, it is simply not walled for complying.
    # A MELD EXEMPTS ONLY THE CHAIN IT WAS ABOUT (review_door.about_chain).
    melded, why = _melded_with(peer, info.get("since_h", info.get("span_h")),
                               seat, chain=info.get("chain"), lane=lane,
                               tips=info.get("tips") or ())
    # A PAIR ROUND'S AGREEMENT MEETS THE SAME READER RULE as its exchange
    # below (the integrator's ruling): a pair round whose reader has no live
    # beacon never silences this rung, whatever it agreed. A meld in a room of
    # its own keeps the #70 exemption above unchanged.
    if melded and review_door.is_pair_room(why) \
            and review_door.live_beacon(peer) is not True:
        melded = False
    if melded:
        return None, (warn + "\n  Meld already converged with %s (room %s) "
                      "inside this window — not blocking: this round is the "
                      "cure, not another spiral." % (peer, why))
    # THE PAIR MELD COUNTS ONLY AS A CONVERSATION (the integrator's ruling):
    # a pair round silences this rung only when its room holds a turn from
    # BOTH this seat and the chain's CURRENT reader inside the window, and
    # that reader's beacon is live. A round the dispatch opened and nobody
    # spoke in, or one whose reader has gone dark, never does.
    talking = review_door.pair_exchange(
        seat, peer, info.get("chain"),
        info.get("since_h", info.get("span_h")))
    if talking and review_door.live_beacon(peer) is True:
        return None, (warn + "\n  The pair meld %s holds turns from you and "
                      "%s inside this window and %s's beacon is live — not "
                      "blocking: the conversation this block asks for is "
                      "happening there." % (talking, peer, peer))
    if not latched:
        return None, warn    # unlatchable -> degrade, never wall the fleet
    # THE PRESCRIPTION MUST NOT NAME A STEP THE SEAT ALREADY TOOK. The block
    # still fires — an opened meld converges nothing and may not buy silence —
    # but telling a waiting seat to open the room it is waiting in reads as not
    # having noticed, and a gate that cannot tell "you ignored me" from "I have
    # not looked" teaches the fleet to discount both. task/981.
    open_room, open_why, open_age = _meld_open_with(peer, info.get("span_h"), seat)
    if open_room and review_door.entry_lapsed(open_why, seat):
        return None, warn + review_door.lapsed_line(peer, open_why)
    if open_room:
        cure = ("a meld with %s is ALREADY OPEN (room %s, %dm) and has not "
                "converged — do NOT open another. Chase the peer in that room, "
                "or escalate to the integrator that it is not entering:\n"
                "  helm chat meld say %s --marker YIELD \"<your side>\""
                % (peer, open_why, int((open_age or 0) // 60), open_why))
    return spiral_block(lane, rounds, dispatches.SPIRAL_WINDOW_H, peer,
                        cure, evidence), None


PAIR_LATCH = "stoppair"


def _pair_turn_gate(session, room, seat):
    """(block | None, warn | None) — a pair-meld round whose floor is THIS
    seat's (review_door.pair_turns_owed).

    A pair meld's chunks carry no @mention by design (both sides are meant to
    sit in recv), so the inbox rung cannot see a turn owed there, and a seat
    that stops over one leaves its partner waiting on a room nobody reads.
    This names the room and the two commands that answer it. BLOCKS ONCE PER
    SET of owed turns, like the inbox rung: a re-stop over the same set
    passes, a new turn re-arms, and an unwritable latch degrades to a warn.
    A seat that never joined a pair meld owes nothing here: the row reaches
    it as any row does. Fail-open total, like every rung on this path."""
    if _off("STOP_GUARD_PAIR") or not seat:
        return None, None
    try:
        owed = review_door.pair_turns_owed(seat)
    except Exception as _swallowed:
        record.swallow("seats_stop_signals._pair_turn_gate", _swallowed)
        return None, None
    if not owed:
        return None, None
    text = review_door.pair_turn_line(owed)
    fp = hashlib.blake2b("|".join(
        "%s@%s#%s" % (o["room"], o["epoch"], o["at"]) for o in owed)
        .encode("utf-8"), digest_size=8).hexdigest()
    path = _stop_fp_path(room, seat, session, kind=PAIR_LATCH)
    try:
        with open(path) as f:
            last = f.read().strip()
    except OSError:
        last = None
    if last == fp:
        return None, None                 # already pointed at this exact set
    try:
        latched = _write_stop_latch(path, seat, fp, session=session)
    except OSError:
        latched = False
    if not latched:
        return None, text + "\n  NOT BLOCKING: the latch is unwritable."
    return text, None


def spiral_block(lane, rounds, window_h, peer, cure, evidence):
    """The review-spiral block's text — a pure renderer at its own door, so a
    budget arm measures the artifact rather than rebuilding its format string.

    THE QUOTED RULE, THE COST ANECDOTE AND THE COUNTING PROSE ARE NOT HERE, and
    none of them is lost: the heuristic this rung enforces is named at the end
    and `helm store get` prints it whole, six-round figure included. What a
    reader cannot reconstruct stays — which lane, how many tips, which peer,
    which round, what the evidence says, and the exact command that converges
    it. The gate's DECISION is decided above; this writes words."""
    return (
        "[helm stop-guard] review spiral — lane '%s' is review-dispatched at "
        "%d distinct tips in the last %dh (latest peer: %s). That is round "
        "%d, and at two rounds the cure is a meld. Do not idle waiting for "
        "the next verdict — converge every open finding in one live "
        "exchange:\n"
        "  %s\n"
        "MELD — %s.\n"
        "Rounds count on the work chain, so renaming the lane does not reset "
        "them. Blocks once per (chain, round-count, prescription); "
        "HELM_STOP_GUARD_SPIRAL=0 disables. Why: helm store get "
        "heuristic:review-begins-with-cat-file"
        % (lane, rounds, window_h, peer, rounds, cure, evidence))
