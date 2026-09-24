#!/usr/bin/env python3
"""helm seats — stop signals: the two HARD blocks and the cheap probes.

THIS IS THE FIRST OF FOUR MODULES OUT OF ONE 3,143-LINE SECTION, and the cut
is at the point where a stop stops being a REFUSAL and becomes ADVICE.

TWO THINGS HERE ACTUALLY BLOCK AN IDLE STOP, and both are deliberate: an
un-armed beacon (_beacon_block) while the seat owes dispatch work means the
agent cannot be woken, so stopping makes it unreachable rather than idle; and
the review spiral (_spiral_gate)
means the same lane has been review-dispatched at three distinct tips, where
the cure is one live exchange rather than a fourth round. Everything else in
this file only SUPPLIES SIGNAL to the whisper ladder, which advises and never
refuses.

PRECISION IS THE WHOLE GAME for the blocking pair. A guard that blocks on a
bad signal is worse than no guard, because an agent that learns to work
around a false refusal has learned to work around the true one too. That is
why the latches are fingerprinted rather than counted: a latch keyed on a
count re-fires on the same unchanged state, and re-firing is how a guard
teaches itself to be ignored.

The candidate probes (_ask_candidate, _dispatch_candidate, _gate_candidate,
_staged_deletions, _unverified_candidate, _unbanked_candidate) are grouped
with them because each is a cheap per-turn read the ladder asks EVERY stop,
and their cost is the reason they are cheap: anything expensive here is paid
on every turn of every seat.
"""

import hashlib
import json
import os
import re
import time

from . import chat, home, pk, projscope, record, seats_advice, vcs
from .seats_common import (SEAT_BYTES, STATUS_BYTES, _clip, _scrub, _seat_key,
                           own_name, recipient_matches, roster)
# Downward edges only: stop_signals -> delivery -> roster -> identity ->
# common. The stop ladder READS the world these modules maintain and writes
# none of it, which is why nothing here needs deferring.
from .seats_identity import acting_seat, deliverable, safe_cwd, seat_scope
from .seats_cursor import (_occurrence, _sid8, _write_stop_latch,
                           seat_state_lock)
from .seats_stop_fp import (_off, _pending_all,  # noqa: F401
                            _pending_rows, _rows_fp, _stop_fp_path)
from .seats_delivery import _cursor, _room_dirty, _scan_rooms, _tail

BEACON_LATCH = "stopbeacon"
def beacon_procs(seat, proc_dir="/proc", strict=False):
    """([pids], trouble) — live `helm chat wait` waiters attributable to
    `seat`. Argv shape comes from rearm.py (the same exact-shape gate the
    land-to-live pass signals on): a standalone `helm` token — or `-m helm` —
    followed by `chat wait`. A bash `-c 'helm chat wait …'` wrapper carries
    that text inside ONE argument with no standalone `helm` token, so the
    wrapper alone is never mistaken for the beacon; the wrapper execs the real
    process, which IS matched.

    Attribution, in order: the `--seat` flag (casefold), else the process's own
    HELM_CHAT_NAME/MELD_CHAT_NAME (a beacon armed without --seat resolves its
    seat from that env), else UNATTRIBUTABLE. The stop guard's default is
    deliberately lenient: unattributable counts as a hit because a wrong BLOCK
    stops a healthy seat.

    `strict=True` IS A POSITIVE CLAIM, AND A SHAPE CANNOT MAKE ONE. An argv
    shape says a `helm chat wait` process EXISTS; it says nothing about whether
    anything is still listening at the other end. An ORPHAN waiter left behind
    by a DEAD session satisfies the shape perfectly, and that is not a corner
    case — a live seat measured two of them on itself, detached shells whose
    claude sessions had exited (chat #helm row 623). While this function
    answered from shape alone, `resumeturn`'s recovery line — "It has an armed
    inbox beacon and will wake on its next @mention" — was satisfiable by a
    zombie, so a compacted seat covered only by an orphan stayed dark forever
    under an alert that said it was covered.

    So strict now requires a LIVE SESSION behind the shape (`beacons.classify`):
    a hit counts only when a live process still holds the session the waiter
    was armed under. The three outcomes stay distinct, because collapsing them
    is the whole bug — PROVEN LIVE returns pids; UNPROVABLE returns trouble (an
    alert must say UNKNOWN, never promise a wake); and a seat covered ONLY by
    proven ghosts returns a clean, deliberate EMPTY, which is the true and
    previously unsayable answer "nothing will wake this seat."

    FAIL-OPEN: trouble is a string and the caller must not block or claim armed
    on it."""
    from . import rearm
    needles = [("%s=%s" % (v, seat)).encode("utf-8") + b"\0"
               for v in ("HELM_CHAT_NAME", "MELD_CHAT_NAME")]
    want = str(seat or "").casefold()
    try:
        me = os.getuid()
        pids = [n for n in os.listdir(proc_dir) if n.isdigit()]
    except OSError as exc:
        return [], "process table unlistable (%s)" % exc
    hits, opaque, mypid = [], [], os.getpid()
    for pid in pids:
        pdir = os.path.join(proc_dir, pid)
        try:
            ipid = int(pid)
            if ipid == mypid or os.stat(pdir).st_uid != me:
                continue
        except OSError:
            continue                     # exited between listdir and stat
        try:
            with open(os.path.join(pdir, "cmdline"), "rb") as f:
                raw = f.read(64 * 1024)
        except (FileNotFoundError, ProcessLookupError, NotADirectoryError):
            continue                     # exited mid-scan: proven not-live
        except PermissionError:
            continue      # not-dumpable (systemd --user is uid-1000 and
                          # opaque): opaque by kernel design, and never a
                          # plain `helm chat wait` python process. Treating
                          # this as trouble would disable the guard on every
                          # real host — found on the first live run.
        except OSError as exc:
            return [], ("process %s cmdline unreadable (%s)"
                        % (pid, exc.__class__.__name__))
        argv = [p.decode("utf-8", "replace") for p in raw.split(b"\0") if p]
        sub = rearm._helm_subargv(argv) if argv else None
        if not sub or sub[:2] != ["chat", "wait"]:
            continue
        flag = rearm._flag(sub, "--seat")
        if flag is not None:
            if str(flag).casefold() == want:
                hits.append(ipid)
            continue
        try:                             # no --seat: its env names its seat
            with open(os.path.join(pdir, "environ"), "rb") as f:
                env = f.read(64 * 1024)
        except OSError:
            (opaque if strict else hits).append(ipid)
            continue
        named = b"HELM_CHAT_NAME=" in env or b"MELD_CHAT_NAME=" in env
        if any(n in env for n in needles):
            hits.append(ipid)
        elif not named:
            (opaque if strict else hits).append(ipid)
    if strict:
        return _strict_live(seat, hits, opaque, proc_dir)
    return hits, None
def _strict_live(seat, hits, opaque, proc_dir):
    """The positive half of `beacon_procs`: which of these shapes is a beacon
    somebody is still listening to. Every waiter is asserted, not the first
    match and not the newest — taking the newest arm time is exactly how a seat
    with five waiters, four of them stale, was read as 'current' (#helm 622).
    The session census opens every credential home and NO zero-hit return below
    reads it, so a seat with no shape at all does not pay to take one."""
    from . import beacons
    live = beacons.live_sessions() if hits else None
    proven, unproven = [], []
    for pid in hits:
        row = beacons.classify(pid, seat, None, live, proc_dir)
        if row["state"] == beacons.LIVE:
            proven.append(pid)
        elif row["state"] != beacons.GHOST:
            unproven.append(row)        # a PROVEN ghost is a proven absence
    if proven:
        return proven, None
    if opaque:
        return [], ("%d live beacon waiter%s could not be attributed to seat %s"
                    % (len(opaque), "" if len(opaque) == 1 else "s", seat))
    if unproven:
        return [], ("%d beacon waiter%s for seat %s could not be proven live "
                    "(%s)" % (len(unproven), "" if len(unproven) == 1 else "s",
                              seat, unproven[0]["why"]))
    # Zero proven, zero unprovable: either there is no waiter at all, or every
    # one of them is a proven ghost. Both are the same honest answer — nothing
    # will wake this seat — and it is the answer this function could not give
    # before, because a ghost used to satisfy it.
    return [], None
def owes_beacon(seat, session=None):
    """The VALIDATED seat name when this process owes an armed beacon, else
    None. Only a LAUNCHED fleet seat owes one: HELM_CHAT_NAME names this very
    seat (the launch seam's stamp — launch.build_env / seat.launch_line; an
    ad-hoc session that auto-named itself has none) AND the seat is
    roster-registered (its join actually ran, so a delivery lane exists to go
    dark). Fail-open to None on every uncertainty — never block on a guess.

    Returning the name (not a bool) is load-bearing for the display-launder
    law: home.chat_name() is THE validated ingestion seam ([A-Za-z0-9._-] only,
    SeatNameError on ESC/C0/bidi), so the name this returns is provably inert
    and can be interpolated into the block message — including into the
    copy-pasteable Monitor command, which laundering would corrupt. The roster
    read below is INTERNAL-MATCHING-ONLY: a membership test whose key never
    reaches a sink."""
    try:
        stamped = home.chat_name()
    except Exception:
        return None                      # a hostile name is not a seat at all
    if not stamped or not seat:
        return None
    if str(stamped).casefold() != str(seat).casefold():
        return None
    try:
        return stamped if seat in roster() else None
    except Exception:
        return None


def _beacon_obligation(seat, dispatch_snapshot=None):
    """True when `seat` sends or receives owed dispatch work, False for a
    measured empty fold, None when the obligation ledger cannot be read.

    One snapshot owns both direction checks AND every dispatch-reading stop rung.
    `dispatches.owed` owns successor and terminal semantics; hand-spelling
    OPEN/HELD here made this gate disagree with every other obligation surface.
    UNKNOWN fails closed at the caller — unreadable work is not evidence that
    becoming unreachable is safe."""
    from . import dispatches
    try:
        snap, unavailable = dispatches.snapshot() \
            if dispatch_snapshot is None else dispatch_snapshot
        if unavailable:
            return None
        # THE OUTBOUND HALF READS CUSTODY, NOT AUTHORSHIP (review, who
        # caught that my "both obligation readers" census was FALSE — it
        # enumerated callers of `_sent_by` inside dispatches.py and this
        # reader spells the field itself, in another module). Keyed on raw
        # `sender`, a transferred delivery leg leaves the DEAD author owing
        # the beacon while the live custodian owes nothing: the exact
        # inversion the transfer exists to repair.
        return any(
            recipient_matches(dispatches.custodian_of(row), seat)
            or recipient_matches(row.get("recipient"), seat)
            for row in dispatches.owed(snap))
    except projscope.Expired:
        raise
    except Exception as _swallowed:
        record.swallow("seats_stop_signals._beacon_obligation", _swallowed)
        return None


def _beacon_block(session, room, seat, dispatch_snapshot=None):
    """The armed-beacon gate -> one block line, or None. Latches the OBSERVED
    state (armed|optional|missing-owed|missing-unknown) per (seat, session): a
    measured zero makes the beacon optional and re-arms the gate if work later
    appears; each missing state blocks once. Probe trouble touches nothing."""
    name = None if _off("STOP_GUARD_BEACON") else owes_beacon(seat, session)
    if not name:
        return None
    pids, trouble = beacon_procs(name)
    if trouble:
        return None                      # absence unproven -> never block
    obligation = True if pids else _beacon_obligation(
        name, dispatch_snapshot() if dispatch_snapshot is not None else None)
    state = "armed" if pids else "optional" if obligation is False \
        else "missing-owed" if obligation else "missing-unknown"
    path = _stop_fp_path(room, seat, session, kind=BEACON_LATCH)
    with seat_state_lock(seat, session=session) as current:
        if not current:
            return None
        try:
            with open(path) as f:
                last = f.read().strip()
        except OSError:
            last = None
        if state != last:
            try:
                chat._ensure_dir()
                pk.atomic_write(path, state)
            except OSError:
                # An unlatchable gate would block EVERY stop forever — the wedge
                # this guard is forbidden to become. No latch, no block.
                return None
    if state in ("armed", "optional") or last == state:
        return None                      # safe, or already pointed at once
    why = "holds owed dispatch work" if obligation else (
        "has an UNREADABLE dispatch ledger, so absence of owed work is unproven")
    return (
        "[helm stop-guard] NO ARMED BEACON — seat '%s' %s and has no live "
        "`helm chat wait --seat %s` process, so NOTHING can wake it and its "
        "owed dispatch work stalls (the restarted-integrator incident: 98 "
        "undelivered rows, ~1.5h blind, a cross-family "
        "gate verdict missed). Arm it BEFORE ending this turn:\n"
        "  %s\n(%s)\n"
        "Monitor missing from your tools means DEFERRED, not absent: "
        "ToolSearch(query: \"select:Monitor\") first. A background shell is "
        "NOT a beacon — it cannot wake your turn loop. This blocks once per "
        "unchanged missing state (a re-stop passes; HELM_STOP_GUARD_BEACON=0 "
        "disables it)." % (name, why, name, seats_advice.beacon_monitor(name),
                           seats_advice.BEACON_EXPIRY_TERSE))
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
def _spiral_meld(peer, lane):
    """The literal cure command, filled in with the real peer and lane."""
    return ('helm chat meld invite %s "%s: converge every open review finding '
            'in ONE exchange"' % (peer, lane))
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


def _melded_with(peer, span_h, own, now=None):
    """(True, room) iff this seat CONVERGED a meld with `peer` inside the
    spiral's own window, else (False, reason).

    #70, and it is the guard punishing a seat for taking the guard's own cure.
    review_spiral counts every distinct reviewed tip and consults NO meld
    state, and the latch fingerprint includes the round count — so the ONE
    post-meld round that IS the convergence re-arms the gate against the seat
    that just did what the block told it to do. The remedy becomes evidence.

    CONVERGED, NOT MERELY OPENED: an `active` meld is a conversation in
    progress and proves nothing about a cure; only a done/done-mutual state
    says the exchange closed. Opening a meld must never buy silence, or the
    gate is disarmed by the cheapest possible gesture.

    INSIDE THE WINDOW, because a meld from last week is not this spiral's cure.
    The caller passes review_spiral's `since_h` (first tip to now); `span_h`
    (first to newest tip) ended at the last round and rejected a meld made
    after it, measured as a whole-suite flake. This asks the question the rule
    exists to ask — did this seat meld DURING this spiral — and says so
    plainly rather than implying a precision it does not have.

    FAILS CLOSED TO NOT-MELDED. An unreadable meld directory suppresses
    nothing: a suppression that fires on absent evidence un-guards the spiral
    rung fleet-wide, which is the same failure the delegation exemption next
    door forbids by name."""
    import glob as _glob
    now = time.time() if now is None else now
    try:
        span_s = max(0.0, float(span_h or 0)) * 3600.0
    except (TypeError, ValueError):
        return False, "unusable span"
    floor = now - span_s
    try:
        paths = _glob.glob(os.path.join(chat.chat_dir(), "*.meld.*.json"))
    except OSError as e:
        return False, "meld state unreadable (%s)" % type(e).__name__
    for path in paths:
        st = pk.read_json(path, None)
        if not isinstance(st, dict):
            continue
        if str(st.get("status") or "") not in ("done", "done-mutual"):
            continue                      # active != converged
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
        # ONE SECOND OF SLACK, and it is a real boundary rather than a fudge.
        # `epoch` is integer seconds; `floor` is a float. When a spiral's
        # rounds land inside one second — which is exactly the shape of a fast
        # ping-pong, and of every test that stages rounds in a loop — span_h is
        # 0.0 and the window collapses to a POINT that integer truncation puts
        # the meld just outside. The cure would then be rejected for being
        # simultaneous with the disease.
        if when + 1.0 >= floor:
            # LAUNDERED AT THE READ, never at the emit. This room name comes
            # out of a meld file ANOTHER seat wrote, and _spiral_gate
            # interpolates it straight into displayed stop-guard text — so an
            # unscrubbed value here reshapes the frame it rides in. Scrubbing
            # at the read makes the contract "element two is always safe to
            # display", which a future caller cannot undo by forgetting;
            # laundering at the one emit site would have fixed one caller and
            # left the next exposed. SEAT_BYTES because a room name is a
            # glance, exactly like a seat label.
            return True, _clip(_scrub(str(st.get("room") or "?")), SEAT_BYTES)
    return False, "no converged meld with %s inside the window" % peer
def _spiral_gate(session, room, seat, dispatch_snapshot=None):
    """(block | None, warn | None) for the review-spiral rung."""
    if _off("STOP_GUARD_SPIRAL") or not seat:
        return None, None
    from . import dispatches
    try:                      # fail-open TOTAL — a Stop rung must never wedge
        info, err = dispatches.review_spiral(seat, snap=dispatch_snapshot)
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
    cure = _spiral_meld(peer, lane)
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
    warn += "\n  MELD — " + evidence
    # #70: THE CURE MUST NOT COUNT AS THE DISEASE. A seat that already
    # converged a meld with this peer inside this spiral's window has done
    # exactly what the block prescribes, and the ONE round that carries the
    # convergence is what re-arms the gate. Suppress the BLOCK, keep the WARN
    # — the seat still sees the state, it is simply not walled for complying.
    melded, why = _melded_with(peer, info.get("since_h", info.get("span_h")), seat)
    if melded:
        return None, (warn + "\n  Meld already converged with %s (room %s) "
                      "inside this window — not blocking: this round is the "
                      "cure, not another spiral." % (peer, why))
    if not latched:
        return None, warn    # unlatchable -> degrade, never wall the fleet
    # THE PRESCRIPTION MUST NOT NAME A STEP THE SEAT ALREADY TOOK. The block
    # still fires — an opened meld converges nothing and may not buy silence —
    # but telling a waiting seat to open the room it is waiting in reads as not
    # having noticed, and a gate that cannot tell "you ignored me" from "I have
    # not looked" teaches the fleet to discount both. task/981.
    open_room, open_why, open_age = _meld_open_with(peer, info.get("span_h"), seat)
    if open_room:
        cure = ("a meld with %s is ALREADY OPEN (room %s, %dm) and has not "
                "converged — do NOT open another. Chase the peer in that room, "
                "or escalate to the integrator that it is not entering:\n"
                "  helm chat meld say %s --marker YIELD \"<your side>\""
                % (peer, open_why, int((open_age or 0) // 60), open_why))
    return spiral_block(lane, rounds, dispatches.SPIRAL_WINDOW_H, peer,
                        cure, evidence), None


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
STOP_WHISPER_CAP = 240   # one line's byte budget (inject.py WHISPER_CAP kin)
_WHISPER_FIRED_CAP = 20  # fired-set entries kept per (seat, session) latch
STUCK_AT = 3    # reflex.py stuck-commonsense threshold (re-fires per bucket)
DIRTY_AT = 8    # reflex.py uncommitted-drift threshold
SOLO_LOAD_AT = 3  # owner canon 2026-08-03: "when you mention 3 things in one
                  # sentence, 2 of them should go to SA or TLA". The structural
                  # analogue of holding: 3 leases at once is the width at which
                  # carrying them alone stops being a choice and starts being a
                  # backlog. Buckets like STUCK_AT/DIRTY_AT (N // SOLO_LOAD_AT).
PENDING_STALE_S = 600  # unlanded rows must have AGED to whisper — a fresh set
                       # was just pointed at by the inbox block (echo ≠ context)
RUNNER_TAIL_ROWS = 40  # bounded command-log lookback (newest rows win)
# A row bound is not a clock: a low-activity session can retain 40 rows for
# days. One work-and-sleep stretch is the live-red horizon; older positive
# exits remain visible as historical rather than prescribing a current fix.
RED_GATE_FRESH_S = 12 * 3600
# Code-ish edit targets only — a doc-only session must never arm the verify
# rungs (specificity law: a whisper that fires on prose edits is wallpaper).
_CODE_EDIT_RE = re.compile(
    r"\.(py|pyi|ts|tsx|js|jsx|mjs|cjs|rs|go|rb|sh|bash|zsh|c|h|cc|cpp|hpp"
    r"|java|kt|kts|swift|php|pl|lua|sql|proto|toml|yaml|yml|json)$", re.I)
def _ask_candidate():
    """The OWNER-ASK rung — TOP of the salience ladder. One cheap local read
    of the owner-ask ledger (ownerasks.py): any row not yet REPORTED to the
    owner (open OR done-but-unreported — `done` without `report` stays open,
    owner-surface-is-the-bar) whispers the OLDEST such ask, never the list
    (one ask per whisper — no wallpaper). The fp carries the row's status as
    its level, so an open→done transition re-fires exactly once. Fail-closed
    to None: ledger trouble = silence."""
    try:
        from . import ownerasks
        r, unavailable = ownerasks.stop_candidate()
        if unavailable:
            return ("ask:ledger-unavailable",
                    "owner-ask ledger UNAVAILABLE — owner debt is UNKNOWN, not "
                    "zero; repair/read `helm asks list` before stopping")
        if not r:
            return None
        return ("ask:%s:%s" % (r.get("id"), r.get("status")),
                "owner ask %s is %s: '%s' — report it to the owner (then: "
                "helm asks report %s <chat-post-id>)"
                % (r.get("id"),
                   "done-UNREPORTED" if r.get("status") == "done" else "open",
                   _clip(_scrub(str(r.get("ask") or "")), 48), r.get("id")))
    except Exception as _swallowed:
        record.swallow("seats_stop_signals._ask_candidate", _swallowed)
        return None
def _dispatch_candidate(dispatch_snapshot=None):
    """The DISPATCH rung — sits directly under the owner-ask rung: work you
    handed to another seat and have not checked on. One cheap local read of
    the dispatch ledger; whispers the OLDEST OVERDUE row, never the list.

    This is the durable half of the owner's ask (2026-07-21): "we definitely
    need some timer fallback for anything that is sent to them, to make sure
    it is remembered to check on their progress". Per-session Monitor
    watchdogs die at compaction; this rung re-fires from disk in whatever
    session is running.

    The whisper says CHECK IN, never reassign — an overdue row means the
    deadline passed, not that the seat is dead, and tonight a lane quiet 47
    minutes turned out to be a long turn. The fp carries status/delivery so a
    state transition re-fires exactly once. Fail-closed to None."""
    try:
        from . import dispatches
        # THIS seat's own identity, through the one law (own_name -> session
        # row -> derive). Without it the rung offered every seat the same
        # globally-oldest row — the docstring above says "work YOU handed",
        # and the filter that makes that true needs a seat to compare against.
        r, kind, unavailable = dispatches.stop_candidate(
            seat=acting_seat(cwd=safe_cwd()), snap=dispatch_snapshot)
        if unavailable:
            return ("dispatch:ledger-unavailable",
                    "dispatch ledger UNAVAILABLE — obligations are UNKNOWN, not "
                    "zero; repair/read `helm dispatch list` before stopping")
        if not r:
            return None
        tip = str(r.get("tip") or r.get("ref") or "<reviewed-tip>")
        lane = _clip(_scrub(str(r.get("lane") or "")), 32)
        if kind == "redispatch":
            return ("dispatch:%s:needs-redispatch" % r.get("id"),
                    "dispatch %s to @%s (%s) NEEDS REDISPATCH — this historical "
                    "row lacks an exact tip so no verdict can ever close it; "
                    "redispatch the work with `helm dispatch send <recipient> <lane> "
                    "\"<message>\" --ref <tip> --kind <build|review> "
                    "--supersedes <old-id>` (the old row stays visible as history)"
                    % (r.get("id"), r.get("recipient"), lane))
        if kind == "confirm":
            return ("dispatch:%s:needs-confirmation" % r.get("id"),
                    "dispatch %s to @%s (%s) delivery is NEEDS CONFIRMATION — "
                    "confirm at @%s that the hand-off actually arrived; do NOT "
                    "resend automatically (one operation = at most one send)"
                    % (r.get("id"), r.get("recipient"), lane,
                       r.get("recipient")))
        return ("dispatch:%s:%s:%s" % (r.get("id"), r.get("status"), tip),
                "dispatch %s to @%s (%s) NEEDS CHECK-IN (OVERDUE) and is "
                "PENDING VERDICT — verify at the exact recipient; do NOT "
                "reassign on age alone. Close the exact reviewed tip with: "
                "helm dispatch verdict %s %s --approve|--fix|--supersede "
                "<evidence>   (the polarity flag is REQUIRED — without it "
                "the verb exits 2)"
                % (r.get("id"), r.get("recipient"), lane, r.get("id"),
                   _clip(_scrub(tip), 16)))
    except Exception as _swallowed:
        record.swallow("seats_stop_signals._dispatch_candidate", _swallowed)
        return None
def _runner_identity(token, cwd=None):
    """Compatibility façade; record.py owns the persisted identity grammar."""
    return record.runner_identity(token, cwd)


def _runner_latest(session, cwd=None):
    """{identity: row} — latest run per record-time semantic identity.

    New rows carry the identity minted beside the invocation cwd. Legacy rows
    are normalized without consulting the Stop cwd or current filesystem, so
    a checkout disappearing cannot change a prior row's bucket."""
    if not session:
        return {}
    try:
        p = os.path.join(record.session_dir(session), "command-log.jsonl")
        with open(p, encoding="utf-8") as f:
            tail = f.readlines()[-RUNNER_TAIL_ROWS:]
    except Exception as _swallowed:
        record.swallow_unless_unwritten(
            "seats_stop_signals._runner_latest", _swallowed)
        return {}
    latest, owners = {}, {}
    for ln in tail:
        try:
            r = json.loads(ln)
        except ValueError:
            continue
        if not isinstance(r, dict) or not r.get("token"):
            continue
        identity = r.get("identity")
        if not isinstance(identity, str) or not identity:
            identity = record.runner_identity(r["token"])
        aliases = r.get("aliases") if isinstance(r.get("aliases"), list) else []
        ids = [identity] + [a for a in aliases if isinstance(a, str) and a]
        matches = {owners[a] for a in ids if a in owners}
        key = next(iter(matches), identity)
        for old in matches - {key}:
            latest.pop(old, None)
            for alias, owner in list(owners.items()):
                if owner == old:
                    owners[alias] = key
        for alias in ids:
            owners[alias] = key
        latest[key] = r
    return latest
def _edited_code(session):
    """Basenames of CODE-ish files this session actually edited (record.py's
    edit-targets log — real landed edits, failed ones never appended). A
    doc-only session returns [] and never arms the verify rungs. [] on any
    trouble = fail-closed."""
    if not session:
        return []
    try:
        p = os.path.join(record.session_dir(session), "edit-targets.log")
        with open(p, encoding="utf-8") as f:
            names = [ln.strip() for ln in f]
    except Exception as _swallowed:
        record.swallow_unless_unwritten(
            "seats_stop_signals._edited_code", _swallowed)
        return []
    return [n for n in names if n and _CODE_EDIT_RE.search(n)]
GATE_GREEN, GATE_RED, GATE_EXPIRED, GATE_NONE = (
    "green", "red", "expired", "none")


def _gate_verdict(row, now=None):
    """One row's measured state; numeric exits are never reinterpreted.

    A positive exit is live only inside RED_GATE_FRESH_S. Missing or malformed
    clocks stay live because silently aging against an unknown timestamp would
    manufacture an all-clear. Negative returns and non-integers carry no
    verdict; subprocess signal returncodes such as -9/-2 are not failures."""
    e = row.get("exit")
    if not isinstance(e, int) or e < 0:
        return GATE_NONE
    if e == 0:
        return GATE_GREEN
    ts = row.get("ts")
    if not ts:
        return GATE_RED
    try:
        age = (now if now is not None else time.time()) - float(ts)
    except (TypeError, ValueError):
        return GATE_RED
    return GATE_EXPIRED if age > RED_GATE_FRESH_S else GATE_RED


def _gate_candidate(latest, now=None):
    """Surface a live positive exit, or report the historical state once.

    The log proves an exit number and that no later green row for the same
    semantic identity was found. It does not prove why the process exited or
    that no run exists outside the bounded log, so both branches state only
    those measured facts. A later green silences either branch."""
    live, old = [], []
    for r in latest.values():
        verdict = _gate_verdict(r, now)
        if verdict == GATE_RED:
            live.append(r)
        elif verdict == GATE_EXPIRED:
            old.append(r)
    if live:
        r = max(live, key=lambda x: x.get("ts") or 0)
        return ("redgate:%s:%s" % (r.get("digest"), r["exit"]),
                "last recorded run of `%s` exited %s; no later green run of "
                "the same runner found — rerun or surface before stopping"
                % (_clip(_scrub(str(r.get("token") or "?")), 40), r["exit"]))
    if old:
        r = max(old, key=lambda x: x.get("ts") or 0)
        return ("redgate-expired:%s:%s" % (r.get("digest"), r["exit"]),
                "historical run of `%s` exited %s over %dh ago; no later green "
                "run of the same runner found — rerun only if it still matters"
                % (_clip(_scrub(str(r.get("token") or "?")), 32), r["exit"],
                   RED_GATE_FRESH_S // 3600))
    return None
STAGED_DEL_NAMED = 2    # deleted paths NAMED in a whisper ahead of "+N more"
_STAGED_DEL_PATH_CLIP = 20   # per-path byte clip — measured with the templates
_STAGED_DEL_TOK_CLIP = 12    # token clip in the deletion wording — measured
def _staged_deletions(cwd):
    """(shared, deleted-paths) probed from the checkout at `cwd`, or None =
    UNKNOWN (no cwd, not a repo, git absent/failed/timed out). Two bounded git
    reads: rev-parse separates the MAIN checkout — git-dir IS the common dir,
    the tree the whole fleet shares — from a linked worktree (lane-owned,
    single-lease); `diff --cached` lists paths whose DELETION is already
    staged, `--no-renames` so a delete paired with a similar add elsewhere
    still surfaces as the delete it is. Tri-state on purpose (vcs.ancestry's
    law): a probe that cannot SEE returns UNKNOWN, never a verdict — and the
    callers SAY "UNKNOWN" on UNKNOWN, keeping the read-then-decide advice
    without asserting a venue the probe never measured."""
    if not cwd:
        return None
    try:  # through the vcs seam (probe: rc!=0/timeout/no-git → None), never a
        from . import vcs  # direct spawn — the spawn-audit pin holds at 20
        be = vcs.backend(cwd)
        rp = be.probe(cwd, "rev-parse", "--path-format=absolute",
                      "--git-dir", "--git-common-dir", timeout=3)
        if not rp:
            return None
        dirs = [ln.strip() for ln in rp.splitlines() if ln.strip()]
        if len(dirs) != 2:
            return None
        dz = be.probe(cwd, "diff", "--cached", "--name-only",
                      "--diff-filter=D", "--no-renames", "-z", timeout=3)
        if dz is None:  # '' is a LEGITIMATE clean answer, only None is UNKNOWN
            return None
        return (os.path.realpath(dirs[0]) == os.path.realpath(dirs[1]),
                [p for p in dz.split("\0") if p])
    except Exception as _swallowed:
        record.swallow("seats_stop_signals._staged_deletions", _swallowed)
        return None
def _deletions_named(paths):
    """The bounded name-what-is-at-stake clause: the first STAGED_DEL_NAMED
    paths (each byte-clipped) plus an EXPLICIT '+N more'. The omitted count is
    part of the clause and sits AHEAD of the advice in both templates — a
    truncation that hides its own truncation is the bug class this fix
    belongs to, so the tail clip may never be what discloses it."""
    named = ", ".join(_clip(_scrub(p), _STAGED_DEL_PATH_CLIP)
                      for p in paths[:STAGED_DEL_NAMED])
    more = len(paths) - STAGED_DEL_NAMED
    return named + (" +%d more" % more if more > 0 else "")
def _deletions_fp(paths):
    """8-hex fingerprint of the staged-deletion SET, so the latch re-arms when
    the set changes and the legacy wording (a different fp) still gets its one
    shot once the deletions resolve."""
    return hashlib.blake2b("\0".join(paths).encode("utf-8"),
                           digest_size=4).hexdigest()
def _unverified_candidate(dirty, edits, latest):
    """The UNVERIFIED rung: code edits landed, tree still dirty, and NO
    test/gate ran this session at all — `compiles` ≠ done; stopping here is
    stopping before the work was ever proven. Level bucket escalates per
    DIRTY_AT further edits (reflex escalate law), so one whisper per stretch,
    never wallpaper."""
    if not (dirty and edits) or latest:
        return None
    return ("unverified:%d" % (len(edits) // DIRTY_AT),
            "%d code edit(s) landed with NO test/gate run this session — run "
            "the gate before stopping (pull: git diff --stat, then the suite)"
            % len(edits))
def _unbanked_candidate(dirty, edits, latest, staged=None, root=None,
                        here=None):
    """The UNBANKED-GREEN rung: edits landed, EVERY latest gate run is green,
    tree still dirty — the next step is unambiguous: commit. The sharper,
    earlier cousin of the dirty-streak rung (no eight-op wait when the state
    already reads 'proven green, unbanked'). fp = the newest green digest:
    each newly-proven green state whispers once. `staged` is the tri-state
    _staged_deletions probe (None = UNKNOWN): staged deletions in a shared
    checkout swap the advice for the refusal branch below."""
    if not (dirty and edits and latest) or not record.observed_here(root, here):
        return None
    if any(not (isinstance(r.get("exit"), int) and r["exit"] == 0)
           for r in latest.values()):
        return None   # a red/no-verdict gate stands — the red rung owns this stop
    g = max(latest.values(), key=lambda x: x.get("ts") or 0)
    # THIS RUNG CANNOT KNOW WHOSE DIRT IT IS, so it must not say "yours".
    #
    # The dirty signal is a bare boolean — `backend.dirty()` returns True/False
    # with no file list — and the fleet shares one main checkout. Two facts are
    # known here: a green gate ran, and the tree is dirty. NEITHER implies the
    # dirty files were in that green, and neither implies they are this seat's.
    #
    # THE FIRST VERSION SAID `git add -u`, NOT `add -A`, for a reason that was
    # true but INCOMPLETE. `-A` was the danger because untracked files (another
    # lane's audit doc, live 2026-07-26) were never in the green, so `-u` was
    # called safe on the grounds that it stages tracked modifications only.
    #
    # IT IS NOT SAFE. Live 2026-07-27: another seat staged lane content into this
    # shared tree, REVERTING three pushed commits. Those were TRACKED
    # modifications, so `-u` stages them exactly as `-A` would, and this rung
    # fired telling the integrator to bank them as "the proven slice." Following
    # it would have silently reverted pushed work belonging to three seats. `-u`
    # narrows the blast radius from untracked to tracked; it does not make the
    # claim true.
    #
    # So the fix is at the CLAIM, not the flag — state what is known, and name the
    # check the reader must run. Same repair as `dispatch add`'s loud silence: a
    # surface that cannot know says so instead of guessing confidently.
    #
    # ORDER IS WHAT PROTECTS THE INSTRUCTION. STOP_WHISPER_CAP is 240 bytes for
    # the whole line and _clip truncates the TAIL, so whatever sits last is what
    # overflow eats. The first draft of this wording put the advice last and
    # shipped as `bank it with \`git ad…` — the reader got the alarm and not the
    # repair. Keeping the actionable text ahead of the trailing "holds once per
    # state" boilerplate means an overlong gate token spends the budget on the
    # note, never on the verb.
    #
    # The token clip is 16 rather than 40 to buy back room for the trailing
    # note. MEASURED, and narrower than I first claimed: at clip 40 this body
    # reached 247 bytes and lost 7, which fell in the boilerplate, not the
    # instruction. So 16 protected the NOTE; the ordering protects the ADVICE.
    # Stated precisely because the mutation that restored clip 40 left the test
    # green, and a comment calling the clip load-bearing would have been the
    # third documented-exclusion-untested that day.
    #
    # RE-MEASURED 2026-08-05, and the NOTE's protection is now SPENT: adding the
    # third ownership state costs 31 bytes, so this arm composes to 247 bytes
    # (UNKNOWN: 243) and loses its tail at ANY token — even a 6-byte `pytest`,
    # well under the clip. 16 no longer buys the note back; only the ORDERING
    # still holds, and it is now the sole thing keeping the verb on screen. Do
    # not read the surviving "…" in the pinned test as a typo. Budget is now
    # ZERO: any further wording growth eats `never commit` next, so a FOURTH
    # state cannot simply be appended — it costs a re-cut of the whole line.
    #
    # WHILE ANY STAGED DELETION EXISTS IN A SHARED CHECKOUT, `add -u` IS NOT
    # OFFERED AT ALL — the deleted paths are named instead. Do not "simplify"
    # this back into the one-branch advice: the ownership question the legacy
    # wording asks the reader to answer — "is this dirt yours?" — is PRECISELY
    # the question a seat cannot answer reliably in a shared tree, and that
    # unanswerable question is the whole reason this guard exists. A seat that
    # edited here all evening reasonably concludes the dirt is its own, follows
    # the `add -u` arm, and stages every tracked deletion with it. Live
    # 2026-07-29: eight owner-gated doc deletions (~1648 lines of landed
    # documentation, a deliberate public-readiness scrub) sat staged in the
    # shared checkout, worktree matching, while this whisper suggested exactly
    # that branch. The guard cannot distinguish a deliberate owner-gated scrub
    # from an accidental `git rm` — the check-that-cannot-see-a-case class
    # (return UNKNOWN, never a verdict: vcs.ancestry fe9c606, seat.py proxy
    # liveness 9bc3061), except this instance laundered the UNKNOWN as an
    # INSTRUCTION rather than a verdict. So in the one state where a wrong
    # ownership guess is destructive, the guard refuses to name the destructive
    # branch and hands the reader the real information instead: a bounded list
    # of the paths at stake plus the exact count omitted. The fallback arms
    # below SAY WHAT WAS MEASURED, never more: the probe returned a bool, so
    # a tree it measured NOT shared (a lane worktree, single-lease — it owns
    # its own index) must not be called SHARED, and a probe that returned
    # None must say UNKNOWN rather than mint a venue from blindness — the
    # legacy wording asserted SHARED in both states, which was this same
    # laundered-verdict class one arm over.
    # MEASURED: token clip 12 + per-path clip 20 + 2 named
    # paths peaks this body at ~237 bytes, so count, first path, '+N more'
    # and the prohibition all clear the 240 cap; overflow can only eat the
    # trailing boilerplate.
    if staged and staged[0] and staged[1]:
        return ("unbanked-del:%s:%s" % (g.get("digest"),
                                        _deletions_fp(staged[1])),
                "gate GREEN (`%s`) + %d STAGED deletion(s) in this SHARED "
                "checkout: %s — scrub or accident? one seat can't tell: do "
                "NOT stage/commit; act per-path"
                % (_clip(_scrub(str(g.get("token") or "?")),
                         _STAGED_DEL_TOK_CLIP),
                   len(staged[1]), _deletions_named(staged[1])))
    tok = _clip(_scrub(str(g.get("token") or "?")), 16)
    if staged is not None and not staged[0]:
        return ("unbanked:%s" % g.get("digest"),
                "gate GREEN (`%s`) + dirty in this LANE room — a lane owns "
                "its own index: `git add -u` and commit in your room" % tok)
    if staged is None:
        return ("unbanked:%s" % g.get("digest"),
                "gate GREEN (`%s`) + dirty; shared or lane? UNKNOWN — read "
                "`git diff --stat HEAD`: yours -> add -u; a live DELEGATE's -> "
                "LEAVE IT; NOT yours -> stash create, never commit" % tok)
    # THREE STATES, NOT TWO. yours/not-yours was exhaustive while every agent
    # edited in a CLAIMED LANE — and the delegation machinery below is
    # lane-claim-scoped by construction (_delegated_build needs a lease;
    # _record_subagent_event requires "the exact canonical lane root whose
    # current claim binds this process's seat and session"). A Task SUBAGENT
    # working in the SHARED checkout holds neither, so it is invisible here BY
    # DESIGN, and the binary then prescribes confidently over a case it cannot
    # see: `add -u` commits a delegate's half-finished edit, `stash create`
    # steals the file out from under a live writer.
    # MEASURED 2026-08-05: this whisper fired three times at an integrator whose
    # subagent held the two dirty files; the correct action was NEITHER branch.
    # Naming the third state is the honest floor — the guard cannot detect it
    # (that needs lease-free subagent visibility, a real lane, not a string), so
    # it must not answer as though only two exist.
    return ("unbanked:%s" % g.get("digest"),
            "gate GREEN (`%s`) + dirty, but this checkout is SHARED — read "
            "`git diff --stat HEAD`: yours -> add -u; a live DELEGATE's -> "
            "LEAVE IT; NOT yours -> stash create, never commit" % tok)
