#!/usr/bin/env python3
"""helm seats — stop_guard: the single decision about whether a seat may idle.

ONE FUNCTION, AND ITS SIZE IS THE POINT. Every rung feeds here: inbox, claims,
leases, beacon, review spiral, seam, NDP, punt, whisper, wiring, claim
evidence, and the mechanical tail. They resolve in ONE place because a seat
either stops or it does not, and a posture resolved twice can disagree with
itself.

THAT LIST IS IN EXECUTION ORDER AND THE ORDER IS LOAD-BEARING. Every rung up
to `whisper` reads state THIS TURN produced and has one moment to fire;
`wiring` reads the shared checkout, so its finding keeps. `seats_stop_budget`
owns why that decides the order and what each rung may spend.

WHAT BLOCKS AND WHAT ADVISES IS THE WHOLE DESIGN. Two things refuse an idle
stop; everything else prints and returns. A guard blocking on every true
observation would be worked around, and an agent that works around a false
refusal learns to work around the real one too — so the blocking set is small,
fingerprinted, and argued for in the code rather than tuned.
THE LEASE ARM'S LATCH IS A SAME-SET FINGERPRINT, not a count: a lease that is
still held, unchanged, must not re-fire on every stop, but the moment the SET
of held leases changes — one expires, one is taken — the fingerprint changes
and the seat hears about it again. Counting would either nag forever or go
silent on a genuinely new lease.
"""

import hashlib
import json
import os
import time

from . import (chat, home, pk, projscope, seats_advice, seats_stop_budget,
               vcs)
from .seats_common import STATUS_BYTES, _clip, _scrub
from .seats_identity import _delivery_pause, derive_seat, identity_disagreement
from .seats_roster import seat_for_session
from .seats_delegation import (_claim_evidence_warning, _lane_stem,
                               _lease_worktree)
from .seats_cursor import _write_stop_latch
from .seats_roomscan import _ESTATE_FAILED
from .seats_stop_signals import (_beacon_block, _off, _pending_all, _rows_fp,
                                 _spiral_gate, _stop_fp_path)
from .seats_stop_ndp import NDP_LATCH, _ndp_gate
from . import seats_stop_seam
from .seats_stop_seam import _seam_gate
# RE-EXPORTED, NOT MERELY IMPORTED: helm/seats.py and tests/test_seats.py
# both read these two FROM THIS MODULE, so the extraction owes them the
# name they have always had here.
from .seats_stop_claims import (LEASE_LATCH,  # noqa: F401
                                LEASE_TTL_ALARM_S, claims_rung)
from .seats_work_offer import _stop_whisper
from .seats_room_advice import (_ROOM_READS, _ROOM_ROWS_SHOWN,
                                _ledger_snapshot, _missed,
                                _room_unfinished)

# The four reads `_room_advice` makes, named so a PARTIAL read can say
# which one is missing rather than shrinking the denominator (task/112).
_ROOM_ROWS_SHOWN = 2      # findings are advice, not a report
INBOX_CLEAN_LATCH = "inboxclean"


def _rearm_rung(session, seat):
    """The line a seat gets on EVERY turn it has no proven wake path, or None.

    WHY A SECOND BEACON RUNG EXISTS BESIDE `_beacon_block`. That one is a
    REFUSAL, so it is built the way a refusal has to be built: it asks the
    LENIENT question (an argv shape, where an unattributable waiter counts as a
    hit, because a wrong block stops a healthy seat), it only speaks when the
    seat also owes dispatch work, and it latches so the same unchanged state
    cannot re-fire. Every one of those three is right for a block and every one
    of them is a way for an unreachable seat to hear nothing.

    THE ADVICE IT REPLACES WAS FALSE, and the refutation is a measurement. The
    readiness gauge told a reader of an unreachable seat to WAIT, because a
    seat re-arms its own beacon on its next turn. Taken over one live census of
    nineteen seats, twelve read DEAF, and five of those twelve had stamped a
    presence beat AFTER their deaf spell began — one of them with its own
    tool-call records written thirteen hours into a fourteen-hour spell. Only a
    turn produces either, so those seats took turns and re-armed nothing.
    Arming is the seat's own explicit act; nothing on the turn path performed
    it, so a beacon died and the seat never learned. This rung is the missing
    step between "a turn happened" and "a wake path exists".

    SO IT ASKS THE STRICT QUESTION AND IT NEVER LATCHES. Strict requires a LIVE
    SESSION behind the shape, which is what makes an orphan waiter from a dead
    session stop reading as coverage; and a seat that cannot be woken has not
    become acceptable by being unreachable for one more turn, so there is no
    state a latch could legitimately spend. It is an ADVISORY, not a block:
    `_beacon_block` remains the only beacon rung that can refuse a stop, so
    adding this cannot wedge a seat that has no Monitor tool to comply with.

    FAIL LOUD, NEVER SILENT-COVERED. Three answers, and the two that are not a
    proven wake path both speak. A probe that cannot answer says UNPROVEN in
    those words rather than returning None, because the one outcome this rung
    exists to prevent is a seat proceeding in the belief that it is reachable.
    Silence from this rung means exactly one thing: a live beacon was PROVEN
    this turn.

    AND THAT IS ALL IT MEANS — the bound is stated because the sentence above
    is one word away from a claim this rung cannot make. A PROVEN live beacon
    is not proof that its wakes arrive: a waiter whose stdout is a file
    consumes addressed rows and reaches nobody, which the census names as its
    own separate verdict with its own repair command. This rung answers "does a
    live beacon exist", the question whose answer was NO for every seat in the
    census that motivated it; the sink is the other verdict's, and borrowing
    its authority here would put two repairs behind one line.
    """
    from .seats_stop_signals import beacon_procs, owes_beacon
    name = None if _off("STOP_GUARD_BEACON") else owes_beacon(seat, session)
    if not name:
        return None                      # not a launched fleet seat: owes none
    try:
        pids, trouble = beacon_procs(name, strict=True)
    except projscope.Expired:
        raise
    except Exception as exc:             # noqa: BLE001 — see FAIL LOUD above
        pids, trouble = [], "the beacon probe raised %s" % type(exc).__name__
    if pids:
        return None                      # a live wake path was PROVEN
    head = ("coverage could not be PROVEN (%s)"
            % _clip(_scrub(str(trouble)), STATUS_BYTES)) if trouble else \
        "NO live `helm chat wait` process is waiting for it"
    return ("[helm stop-guard] NO PROVEN WAKE PATH — seat '%s': %s, so an "
            "addressed row cannot re-invoke this turn loop. A TURN DOES NOT "
            "ARM IT: arming is your own explicit act and nothing on this path "
            "performs it for you, which is why seats in this state go on "
            "taking turns while staying unreachable. Arm it now:\n  %s\n(%s)\n"
            "Monitor missing from your tools means DEFERRED, not absent: "
            "ToolSearch(query: \"select:Monitor\") first. A background shell "
            "is NOT a beacon. This is advice, not a block, and it is NOT "
            "latched — it repeats every turn until a live beacon is proven, "
            "so silence from this rung is the only evidence that arming "
            "worked." % (name, head, seats_advice.beacon_monitor(name),
                         seats_advice.BEACON_EXPIRY_TERSE))


def _inbox_clean_line(room, seat, session):
    """The clean-stop warn: the full arming instruction ONCE per session, the
    short reminder every time after.

    It is the same-state latch the lease and NDP rungs use, keyed on a constant
    because the STATE here is constant — "this seat has been told how to arm
    in this session". Every failure path returns the FULL form: a latch that
    cannot be read or written must never be the reason a seat never learns the
    command. That direction matters more than the bytes."""
    full = ("[helm stop-guard] inbox clean. If you intend to idle-wait, arm "
            "the beacon first: %s — Monitor missing from your tools "
            "means DEFERRED not absent: ToolSearch(query: \"select:Monitor\")"
            % seats_advice.beacon_monitor(seat))
    try:
        path = _stop_fp_path(room, seat, session, kind=INBOX_CLEAN_LATCH)
        try:
            with open(path) as f:
                shown = f.read().strip() == INBOX_CLEAN_LATCH
        except OSError:
            shown = False
        if shown:
            return ("[helm stop-guard] inbox clean; arm the beacon before "
                    "you idle-wait (the command is in this session's start "
                    "banner).")
        _write_stop_latch(path, seat, INBOX_CLEAN_LATCH, session=session)
    except projscope.Expired:
        raise
    except Exception:
        pass
    return full


#: THE PRODUCER'S TWO REASONS, NAMED ONCE. The room-advice wrapper renders
#: them inside its own sentence and the guard's arm pins THE CONSTANT, so a
#: rewording moves both sides together and can never leave the test
#: asserting a string nothing emits (the seam train16 caught red).
LEDGER_RESERVE_SPENT = ("the dispatch ledger exceeded its successor "
                        "reserve; coverage is UNKNOWN")
LEDGER_RAISED = "the dispatch ledger raised"


def _wiring_rung(session, room, seat, blocks=None):
    """THE BUILT-BUT-NOT-WIRED RUNG. You may not go idle having added a module
    nothing can reach. It fires on STOP, so it never interrupts work — only
    finishing while leaving dead code behind.

    APPENDS to `blocks` when given and returns the block either way, the
    append form `_seam_gate` already uses: the ladder reads as one line per
    rung, and this rung's fail-open and its latch live beside the rung.

    LATCHED ONCE PER DEBT, like every other rung here, and that was NOT true
    when this shipped — a cross-review caught it the same night. The
    commit claimed the finding was "scoped to what THIS tree added, so no seat
    is ever blocked on debt it did not create", and on the deployed topology
    that is FALSE: `wiring` derives the repo from its own __file__, and every
    seat's Stop hook runs THIS ONE SHARED CHECKOUT's bin/helm. So "this tree"
    is the same tree for every session on the host. Combined with the repo's
    batch-push-at-slice-end rule, one agent's unpushed unwired module would
    have blocked every seat, on every stop, for hours, with a message reading
    "YOU added N modules". That is exactly the "blocks everyone → switched off
    within a day" failure the commit text names, shipped by the commit that
    names it.

    The latch is one block per DISTINCT debt (fingerprinted on the module
    list), so a real finding still stops a seat once and a standing debt
    cannot become a wall. Narrowing attribution to the ACTING session needs a
    signal helm does not have in a shared checkout, and is filed rather than
    guessed at.

    THE SAME SHARED CHECKOUT IS ALSO WHAT MAKES THIS THE LADDER'S MOST
    EXPENSIVE RUNG. One seat's untracked module makes `added_modules()`
    non-empty for EVERY seat on the host, which re-arms the walk behind the
    memo for all of them at once; the memo's other key input moves on any edit
    to any file in the package. So the cold walk is this rung's ordinary load,
    not its exceptional one, and it is budgeted as such.
    """
    if _off("STOP_GUARD_WIRING"):
        return None
    try:  # fail-open TOTAL — a Stop hook that raises wedges every session
        from . import wiring
        gate = wiring.gate_lines()
    except projscope.Expired:  # the ladder's own bound, not a fault: the
        raise                  # scope above files this rung UNFINISHED
    except Exception:          # noqa: BLE001 — see the fail-open law above
        gate = []
    if not gate:
        return None
    # a plain digest of the finding text, NOT _rows_fp — that helper takes
    # (room, row) chat pairs and bending it here raised ValueError, which the
    # guard's fail-open would have swallowed into a silently disabled rung: a
    # latch that errors reads exactly like a latch that passed.
    fp = hashlib.blake2b("\n".join(gate).encode("utf-8"),
                         digest_size=8).hexdigest()
    fpp = _stop_fp_path(room, seat, session, kind="stopwiring")
    from .seats_cursor import seat_state_lock
    with seat_state_lock(seat, session=session) as current:
        if not current:
            return None
        try:
            with open(fpp) as f:
                last = f.read().strip()
        except OSError:
            last = None
        if last == fp:
            return None
        try:
            chat._ensure_dir()
            pk.atomic_write(fpp, fp)
        except OSError:
            pass  # an unwritable latch must not block
    block = "[helm stop-guard] " + "\n  ".join(gate)
    if blocks is not None:
        blocks.append(block)
    return block


def _unread_rooms_warn(coverage, seat):
    """The WARN for every room the inbox scan could not read, or None.

    `_pending_all` answers an unreadable cursor with no rows, which is right
    for the rows and wrong for the silence: without coverage, a room the scan
    could not read and a room with nothing in it were the same empty list, so
    a stop passed over a room that might hold an owed row and called the
    inbox clean (task/2530). The guard's law still holds -- an unreadable
    inbox is never a hold -- so this is a WARN, and it names each room and the
    reader's own reason.

    A FAILED ROOM LISTING IS THE SAME SILENCE ONE LEVEL UP. The scan then
    reads only the pinned rooms and says so on stderr, which a Stop hook that
    exits 0 never shows the seat, so the stop called the inbox clean over
    foreign rooms it never visited. The estate outcome rides the same
    coverage dict. Only a failed estate is named: a capped rotation is the
    scan covering a bounded slice on purpose, and every room comes up."""
    cov = coverage or {}
    parts = ["%s (%s)" % (_scrub(str(r)), _scrub(str(why)))
             for r, why in sorted((r, why) for r, (outcome, why)
                                  in (cov.get("rooms") or {}).items()
                                  if outcome == "unreadable")]
    outcome, why = cov.get("estate") or (None, None)
    if outcome in _ESTATE_FAILED:
        parts.append("%s, so only the pinned rooms were read"
                     % _scrub(str(why)))
    if not parts:
        return None
    return ("[helm stop-guard] the inbox could not be fully read for seat "
            "'%s': %s. This stop is not blocked on it (an unreadable inbox is "
            "never a hold), and it is not clean either: a row waiting there "
            "is not ruled out. `helm chat read` shows the rooms."
            % (_scrub(str(seat)), "; ".join(parts)))


def _stop_guard(session=None, room="main", seat=None, stop_active=False,
                transcript=None, cwd=None, budget=None, detail=False):
    """-> (blocks, warns) for one Stop event. Posture resolved once (seat via
    the roster's session mapping, else the derived seat); checks are inline:
      (a) BLOCK — undelivered @mentions/owner rows past the seat's cursor,
          once per pending-fingerprint (blake2b of the pending row ids,
          latched in the room dir); a re-stop on the SAME rows passes. An
          unwritable latch degrades to the WARN (the beacon rung's no-latch-
          no-block law) — never an unconditional re-block. A room whose
          cursor cannot be read, or a room listing that fails, blocks nothing
          and is named in one WARN on both stop paths (_unread_rooms_warn).
      (b) BLOCK — live claim leases held by THIS session (session-bound: no
          session in the hook JSON ⇒ no claims check — a display name alone
          must never gate a stop). DELEGATION EXEMPTION: `_delegated_build`
          accepts either an immediate exact-room child or recent source-bound
          activity: a live sampled child, or a documented subagent event under
          the same claim and enclosing Claude holder until SubagentStop.
          The WARN names its proof and retains the lease. Every UNKNOWN blocks;
          HELM_STOP_GUARD_DELEGATION=0 disables both producers and readers.
          LATCHED once per held-set fingerprint (resource + lease id + TTL
          band, kind=stoplease): a re-stop on the SAME set compresses to a
          one-line WARN; a CHANGED set — a new lease, a release, or a
          remainder crossing LEASE_TTL_ALARM_S — re-prints the full block;
          an unwritable latch degrades the block to the WARN, never a wall.
      (b2) BLOCK — a LAUNCHED fleet seat (HELM_CHAT_NAME names it, roster row
          exists) with owed dispatch work or an unreadable obligation ledger
          and NO live `helm chat wait` beacon: an idle seat nothing can wake.
          A measured zero makes the beacon optional and re-arms the gate if
          work later appears. Once per state, absence must be PROVEN;
          HELM_STOP_GUARD_BEACON=0 disables (the restarted-integrator class).
      (b3) BLOCK — a REVIEW SPIRAL (_spiral_gate): this seat has review-
          dispatched ONE lane at 3+ DISTINCT tips inside the window, i.e.
          round three, which the store's own `review-begins-with-cat-file`
          forbids by name. The block quotes the exact `helm chat meld invite
          <peer> "<lane>: …"` cure. Two rounds WARN instead (the rule's stated
          cure point). Latched on (lane, round-count): one block per state, a
          further round re-arms, an unwritable latch degrades to the warn.
          HELM_STOP_GUARD_SPIRAL=0 disables.
      (b4) BLOCK — an UNTESTED COMPOSITION (_seam_gate): the worktree this seat
          stands in (plus any lane rooms it leases) and another LIVE worktree
          of the same repo under a DIFFERENT holder have both authored the same
          code file, both halves are VERIFIED by the strongest evidence that
          repo produces, and nothing has ever run against the two together.
          Owner canon 2026-08-23: "two green halves with an untested
          composition ... the biggest failure mode across projects." The signal
          is GIT + /proc — the worktree registry, cwd occupancy, branch diffs —
          with helm bookkeeping added where it exists rather than required,
          because keying on lane rooms was measured BLIND to the very project
          that asked for the rung. Discharged by an arm run against the
          composed tree (the receipt binds the TREE, so it is a set membership,
          not a story) or by a `Seam: <peer branch>` commit trailer naming who
          owns the seam. An UNKNOWN peer holder never buys the same-seat
          exemption. Latched per seam SET, not per tip; an unwritable latch
          degrades to the WARN; HELM_STOP_GUARD_SEAM=0 disables. It also emits
          a WARN-only SEAM RUNG BLIND SPOT line: this rung is keyed on the
          WORKTREE, so seats sharing ONE room are invisible to it, and a seat
          stopping in such a room is told once per arrangement rather than
          being left to read silence as a clean bill.
      (c) WHISPER — the contextual continuation lane (_stop_whisper): ONE
          budgeted nudge from the live signals (stuck/dirty counters, the
          verify-grounding rungs — red gate, unverified edits, unbanked
          green — the latched-but-unlanded pending set, and, at the BOTTOM,
          the work-offer of the top unowned backlog row to a genuinely idle
          seat — which AUTO-CLAIMS the head instead of offering it when it
          was dispatched to this very seat and its kind is self-assignable),
          once per (signal, level) fingerprint, riding an existing
          block or soft-holding alone; HELM_STOP_GUARD_WHISPER=0 disables;
          fail-closed to nothing.
      (c2) WARN — claim-evidence: settled-sounding count/SHA/proof/landed
          claims whose current turn lacks the matching measurement. One
          transcript snapshot binds finding + latch identity; unreadable skips.
          HELM_STOP_GUARD_CLAIME=0 disables.
      (d) WARN — clean stop: one line reminding to arm the idle-wake beacon.
          A scan that could not read a room or list the estate is not clean
          and does not get this line.
      (e) silent mechanical — `helm index cap --apply` best-effort in-process
          (the documented Stop line, docs/VERBS.md): never blocks, never
          prints; HELM_STOP_GUARD_INDEX=0 disables.
    stop_active (the hook JSON's stop_hook_active) suppresses every BLOCK and
    mechanical leg but still surfaces the inbox and claim-evidence WARNs: the
    harness is already continuing, and a WARN cannot re-enter the stop path."""
    if _off("STOP_GUARD"):
        return [], []
    budget = budget or seats_stop_budget.State()
    blocks, warns = budget.blocks, budget.warns
    timing = budget.timing
    # TWO ANSWERS FROM ONE QUESTION, because this seat name crosses an
    # ACTUATOR and the crossing is invisible from here.
    #
    # LOCALLY this is render: the guard composes WARN TEXT and is "WARN-ONLY
    # here, never a block", so refusing would turn a stop-time advisory into
    # the wedged-turn shape the guard exists to avoid. That reading is what
    # kept `derive_seat` here through a whole review.
    #
    # BUT THE NAME DOES NOT STAY HERE. It is passed to `_stop_whisper`, whose
    # auto-claim rung calls `claim()` — a LEASE, one of the two acts the
    # resolver's docstring forbids by name on a DERIVED identity. The crossing
    # is a property of the CALL GRAPH, not of this line; nothing at this call
    # site looks like acquisition.
    #
    # So the RENDER seat keeps its floor and an ACTOR is resolved separately.
    # A process with no admissible identity still gets every warning; it just
    # cannot reach the actuator two hops down.
    seat = seat or seat_for_session(session) or derive_seat(session)
    from . import actors
    stop_actor, _aerr = actors.resolve_actor(session, cwd, act="auto-claim work")
    # WARN-ONLY here, never a block (a refusal at Stop is the wedged-turn
    # shape below) — but LOUD at every stop, so a disputed pane cannot idle
    # past its own identity conflict while deliver/wait/join refuse quietly
    # around it. Roster-first resolution above stays by design.
    dispute = identity_disagreement(session)
    dispute_warn = (
        "[helm stop-guard] IDENTITY DISPUTE: this process declares %r but "
        "session %.8s is rostered to %r — delivery, the beacon, and join are "
        "REFUSING until the sources agree. Fix: unset/re-export "
        "HELM_CHAT_NAME, or `helm chat seat disown %s %.8s`"
        % (dispute[0], str(session), dispute[1], dispute[1], str(session))
    ) if dispute else None
    delivery_paused = bool(_delivery_pause(seat, session))
    # THE DURABLE RECORD LEARNS WHO IT IS FROM THE RUNG THAT WORKS IT OUT.
    # Re-deriving the seat inside the writer asked the ENVIRONMENT, and the
    # hook process this runs in exports no seat name, so every production
    # record was anonymous. This is the one place the answer exists.
    timing.seat = seat
    timing.done("identity", "continuing-inbox" if stop_active else "inbox")
    if stop_active:
        # #74. THE SHORT-CIRCUIT CONFLATED "MUST NOT BLOCK" WITH "MUST NOT
        # LOOK", and only the first is true. Blocking under stop_hook_active is
        # the infinite-loop shape both reference guards exist to prevent —
        # SURFACING is not, because a warn never re-enters the stop path.
        #
        # MEASURED COST OF THE CONFLATION: a cross-family verdict reached
        # a claude seat MID-TURN at 18:05; that turn ended at 18:11 with
        # stop_hook_active set, so the guard returned before it ever read the
        # inbox; the row was never shown and never latched. The fleet then sat
        # 5.5 hours waiting on the land that verdict authorized, and the owner
        # caught it from the road. A row that arrives mid-turn had exactly one
        # moment to be seen and the guard declined to look.
        #
        # So: read the inbox, say what is waiting, and RETURN NO BLOCKS. Fails
        # open like every other rung — an unreadable inbox is silence, never a
        # hold, because the one thing worse than an unseen row is a wedged turn.
        seen = {}
        try:
            waiting = [] if delivery_paused or _off("STOP_GUARD_INBOX") \
                else _pending_all(room, seat, session, scan_lane="stop",
                                  coverage=seen)
        except projscope.Expired:
            raise
        except Exception:
            waiting = []
        surfaced = warns
        if dispute_warn:
            surfaced.append(dispute_warn)
        unread = _unread_rooms_warn(seen, seat)
        if unread:
            surfaced.append(unread)
        if waiting:
            surfaced.append(
                "[helm stop-guard] %d row(s) arrived DURING this turn and are "
                "still undelivered for seat '%s' — not blocking (the harness "
                "is already continuing), but they will not be shown again by "
                "this path: `helm chat read`. A verdict that lands mid-turn is "
                "how the fleet idled 5.5h on 2026-08-01."
                % (len(waiting), _scrub(str(seat))))
        claim_warn = _claim_evidence_warning(transcript, room, seat, session)
        if claim_warn:
            surfaced.append(claim_warn)
        timing.done("continuing-inbox")
        return [], surfaced
    pending = []
    # One append-only dispatch observation is shared by every stop rung.
    _dispatch_cache = []
    def _dispatch_snapshot():
        # THE FAILURE PAIR OBEYS THE PRODUCER'S OWN CONTRACT: AN EMPTY MAPPING
        # PLUS A REASON, NEVER None. `dispatches.snapshot()` returns `({},
        # reason)` for a ledger it could not read (`_snapshot`'s unavailable
        # arm), and two consumers state that shape in their own comments —
        # `_dispatch_advice`: "AN UNREADABLE LEDGER IS AN EMPTY DICT PLUS A
        # REASON — never None"; the claims rung's gate exemption: "An
        # UNAVAILABLE ledger already folds to {} in `snapshot()` ... so the
        # threaded value never changes an answer, it only stops the second
        # read." THIS FALLBACK WAS THE ONE PLACE None ENTERED, and it made
        # that second sentence false about the only stop it describes.
        #
        # WHAT IT COST, at seats_stop_claims' gate-exemption call site:
        # `snap=_snap if isinstance(_snap, dict) else None`, and
        # `_gate_pending` reads `snap is None` as NO OBSERVATION SUPPLIED and
        # opens its own `dispatches.rows()` — a SECOND whole ledger fold, once
        # per held lane lease, on the exact stop where the first fold had just
        # spent its entire reserve. The rung then has the claims reserve and
        # nothing else to spend it on: measured from the guard's own probe log
        # (`helm/stopprobe.py`), the claims rung ends at its 9.5s wall on every
        # ladder that records it — 131 of 131 in the sample that opened this
        # lane — behind a dispatch-ledger rung ending at its 7.5s wall on 238
        # of 238.
        #
        # AND IT COULD HAVE WIDENED AN AUTHORISATION, which is the half that
        # does not depend on any timing. A second read that SUCCEEDS where the
        # shared one failed lets `_gate_pending` return a triple, and the rung
        # exempts the lease — "stop allowed, lease retained" — on evidence the
        # one shared observation could not supply. Whether the guard allows a
        # stop would then turn on which of two reads of one ledger happened to
        # finish, which is the non-determinism a single read exists to remove.
        # With `{}` the exemption falls to `_gate_pending`'s own stated law:
        # "an unreadable ledger ... returns None and the claims block stands".
        #
        # EVERY OTHER CONSUMER IS UNMOVED, because every one of them reads the
        # REASON before it reads the state: `_dispatch_advice` and
        # `_room_unfinished` (`if note or not isinstance(snap, dict)`),
        # `_beacon_obligation`, `review_spiral` and `stop_candidate` (`if
        # unavailable:`), `_offer_rows` (`elif dispatch_snapshot[1]`). The
        # gate exemption was the only one keyed on the state's TYPE.
        if not _dispatch_cache:
            try:
                snap = timing.measure(
                    "dispatch-ledger", _ledger_snapshot, budget=budget,
                    fallback=({}, LEDGER_RESERVE_SPENT))
            except projscope.Expired:
                raise
            except Exception:
                snap = ({}, LEDGER_RAISED)
            _dispatch_cache.append(snap)
        return _dispatch_cache[0]

    if dispute_warn:
        warns.append(dispute_warn)
    inbox_blocked = False
    unread = None

    if not delivery_paused and not _off("STOP_GUARD_INBOX"):
        seen = {}
        pending = _pending_all(
            room, seat, session, scan_lane="stop",
            coverage=seen)  # EVERY room's inbox gates
        unread = _unread_rooms_warn(seen, seat)
        if unread:
            warns.append(unread)
        if pending:
            fp = _rows_fp(pending)
            fpp = _stop_fp_path(room, seat, session)
            try:
                with open(fpp) as f:
                    last = f.read().strip()
            except OSError:
                last = None
            if last != fp:
                try:
                    latched = _write_stop_latch(fpp, seat, fp, session=session)
                except OSError:
                    # An unwritable latch cannot record "pointed at once", so
                    # the promise this block ends on — a re-stop on the same
                    # rows passes — would be false: the block would re-fire on
                    # EVERY stop forever, the wedge the beacon rung's latch
                    # (above) is forbidden to become. Same degrade: no latch,
                    # no block. The rows still surface, as a WARN — which
                    # never re-enters the stop path.
                    latched = False
                # DEDUPE BY (sender, opening text) BEFORE showing anything.
                # A repeating watchdog posts the SAME sentence every cycle, and
                # each repeat is a separate undelivered row — so one unresolved
                # condition becomes N obligations. Measured 2026-07-30: a seat
                # reached 497 undelivered of which FOUR OF THE FIRST FIVE were
                # one idle-dispatch nag about a single row, repeating every 15
                # minutes for 37 hours. At that size the guard has inverted its
                # own purpose: no seat can act on 497 items, so the only move
                # left is to park them all, and a real obligation buried at #300
                # is parked with the noise.
                # The COUNT is the honest one (obligations are still rows), but
                # what is SHOWN is distinct senders-and-subjects, each carrying
                # how many times it repeated — which is the fact a reader needs
                # to tell "one stuck thing" from "many things".
                # THE COUNT + THE TWO RUNNABLE VERBS ARE THE WHOLE PAYLOAD.
                # The owner never reads a stop-block's sample rows ("i dont
                # think i ever read these stophooks", 2026-08-06), and the seat
                # is told to `helm chat read` for the rows themselves — so the
                # up-to-5 dedup'd sample lines and the ~430B how-to paragraph
                # were per-firing boilerplate on a block that fires at EVERY
                # stop (task 692). Trimmed to the raw count, the read verb, the
                # catchup verb, and the one caveat that carries the whole point
                # of the block — a read does not discharge an obligation (the
                # 2,250-row pileup this rung exists to prevent). The DECISION —
                # the pending set, its fingerprint, the once-per-set latch — is
                # untouched; only the verbosity is. len(pending) stays the
                # HONEST raw-row count (obligations are rows), not a distinct
                # count, because no sample list is shown to reconcile against.
                body = (
                    "[helm stop-guard] %d undelivered message(s) for seat "
                    "'%s' — `helm chat read` SHOWS them but a read does NOT "
                    "discharge an obligation; ACT, or park with `helm chat "
                    "catchup --including-mentions --apply` (WITHOUT --apply it "
                    "is a DRY RUN that parks nothing)."
                    % (len(pending), seat))
                if latched:
                    blocks.append(body + " This blocks once per pending set "
                                  "— a re-stop on the same rows passes.")
                    inbox_blocked = True
                else:
                    warns.append(body + " NOT BLOCKING: the once-per-set "
                                 "latch is unwritable, so a block here would "
                                 "re-fire on every stop.")
    timing.done("inbox", "claims")

    budget.run("claims", lambda: claims_rung(
        session, room, seat, blocks=blocks, warns=warns,
        dispatch_snapshot=_dispatch_snapshot, detail=detail))
    timing.done("claims", "beacon", complete="claims" not in budget.yielded)
    beacon = None
    try:  # the ARMED-BEACON gate — fail-open TOTAL (never wedge a stop)
        beacon = _beacon_block(
            session, room, seat, dispatch_snapshot=_dispatch_snapshot)
    except projscope.Expired:
        raise
    except Exception:
        beacon = None
    if beacon:
        blocks.append(beacon)
    # THE RE-ARM RUNG, inside the beacon boundary because it is the second half
    # of the same question and must be timed with it. It is declared as
    # SURVIVING THE REFUSAL EXIT: `emit_blocks` drops the warn channel whole, so
    # on any stop where another rung refuses, a seat would otherwise learn
    # nothing about the fact that nothing can wake it — and a refused stop is
    # exactly when a seat is most likely to go idle owing work.
    try:                 # fail-open TOTAL, like every rung on this path
        rearm = _rearm_rung(session, seat)
    except projscope.Expired:
        raise
    except Exception:
        rearm = None
    if rearm:
        warns.append(rearm)
        seats_stop_seam.survives_refusal(rearm)
    timing.done("beacon", "spiral")
    # THE REVIEW-SPIRAL RUNG. Serialized rounds on ONE lane where a meld is the
    # cure — the rule was already in context and was not followed, so it becomes
    # a gate. Latched per (lane, round-count); fail-open total.
    try:
        spiral, spiral_warn = _spiral_gate(
            session, room, seat,
            dispatch_snapshot=None if _off("STOP_GUARD_SPIRAL")
            else _dispatch_snapshot())
    except projscope.Expired:
        raise
    except Exception:
        spiral, spiral_warn = None, None
    if spiral:
        blocks.append(spiral)
    if spiral_warn:
        warns.append(spiral_warn)
    timing.done("spiral", "seam")
    # THE UNTESTED-COMPOSITION RUNG. Two green halves on one file, held by two
    # seats, merging cleanly with no arm ever run against the composition —
    # the owner's named biggest failure mode across projects (2026-08-23).
    # Latched per seam set; fail-open total.
    budget.run("seam", lambda: _seam_gate(
        session, room, seat, cwd, blocks=blocks, warns=warns))
    timing.done("seam", "ndp", complete="seam" not in budget.yielded)
    # THE NON-DISTRACTION-PROTOCOL RUNG. A wide load held with zero
    # delegation all session becomes a conditional block (owner canon
    # 2026-08-03: "NDP ... rises to the level of importance to merit a
    # conditional stopbook"). Latched per load-state; fail-open total.
    try:
        ndp, ndp_warn = _ndp_gate(session, room, seat)
    except projscope.Expired:
        raise
    except Exception:
        ndp, ndp_warn = None, None
    if ndp:
        blocks.append(ndp)
    if ndp_warn:
        warns.append(ndp_warn)
    timing.done("ndp", "punt")
    # THE DRESSED DECLINATION. `punt-tell` catches a punt that announces
    # itself ("later", "todo:"); this catches the one that arrives wearing a
    # reason — declined action + an excuse from a named class, in one
    # sentence, with nothing open on the owner's ask ledger. Owner canon
    # 2026-07-29: a reason built on the clock, on where the owner is, or on a
    # disruption you did not MEASURE is a punt 100% of the time.
    if transcript and not _off("STOP_GUARD_PUNT"):
        try:  # fail-open TOTAL — a Stop rung that raises wedges the host
            from . import punt
            gate = punt.gate_lines(punt.last_assistant_text(transcript))
        except projscope.Expired:
            raise
        except Exception:
            gate = []
        if gate:
            fp = hashlib.blake2b("\n".join(gate).encode("utf-8"),
                                 digest_size=8).hexdigest()
            fpp = _stop_fp_path(room, seat, session, kind="stoppunt")
            from .seats_cursor import seat_state_lock
            with seat_state_lock(seat, session=session) as current:
                if current:
                    try:
                        with open(fpp) as f:
                            last = f.read().strip()
                    except OSError:
                        last = None
                    if last != fp:
                        try:
                            chat._ensure_dir()
                            pk.atomic_write(fpp, fp)
                        except OSError:
                            pass  # an unwritable latch must not block
                        blocks.append("[helm stop-guard] " + "\n  ".join(gate))
    timing.done("punt", "whisper")
    if not _off("STOP_GUARD_WHISPER"):
        try:  # fail-closed to NOTHING: whisper trouble = silence, never louder
            w = _stop_whisper(
                session, room, seat, pending, inbox_blocked, cwd,
                actor=stop_actor, dispatch_snapshot=_dispatch_snapshot())
        except projscope.Expired:
            raise
        except Exception:
            w = None
        if w:
            blocks.append(w)   # rides an existing block, or IS the soft hold
    timing.done("whisper", "wiring")
    # THE BUILT-BUT-NOT-WIRED RUNG, LAST OF THE RUNGS THAT CAN BLOCK. Its
    # position and its reserve are `seats_stop_budget`'s to explain; what is
    # local to this call site is that everything above reads state THIS TURN
    # produced and this reads the shared checkout, so this is the one finding
    # on the ladder that keeps until the next stop.
    budget.run("wiring", lambda: _wiring_rung(session, room, seat,
                                              blocks=blocks))
    timing.done("wiring", "claim-evidence",
                complete="wiring" not in budget.yielded)
    # THE CLAIM-EVIDENCE RUNG — an outgoing message full of settled-sounding
    # claims the turn made no measurement to earn. It is evaluated only on an
    # ALLOW exit: block exits discard WARNs, so parsing and latching one there
    # would spend the hot path on output the caller cannot see.
    if not blocks:
        claim_warn = _claim_evidence_warning(transcript, room, seat, session)
        if claim_warn:
            warns.append(claim_warn)

    # GENUINELY CLEAN only: a latched pass (rows still pending, already
    # pointed at) stays silent, and so does a scan that could not read a
    # room -- neither is ever "clean".
    #
    # THE HIGHEST-FREQUENCY STOP MESSAGE HELM HAS, and the only rung on this
    # path with no latch at all: it restated the full Monitor argv and the
    # deferred-tool fallback on EVERY clean stop, all session, after session
    # start had already delivered both. The argv is worth its bytes ONCE per
    # session; after that the seat has it and needs only the reminder.
    # Same-state latch, the mechanism every other rung here already uses.
    if not blocks and not pending and not unread:
        warns.append(_inbox_clean_line(room, seat, session))
    timing.done("claim-evidence", "mechanical")
    if not _off("STOP_GUARD_INDEX"):
        try:  # the documented Stop line — silent, best-effort, never a gate
            from . import store
            store.index_cap(apply=True)
        except projscope.Expired:
            raise
        except Exception:
            pass
        try:  # the scratch reaper's automatic leg — throttled, bounded, silent
            from . import scratch
            scratch.auto_gc()
        except projscope.Expired:
            raise
        except Exception:
            pass
    timing.done("mechanical")
    return blocks, warns


def stop_guard(session=None, room="main", seat=None, stop_active=False,
               transcript=None, cwd=None, budget=None, detail=False):
    """Run the ladder while preserving partial findings on ambient expiry.

    `detail` is RENDERING ONLY — `helm chat stop-guard --detail` sets it, and
    no rung's verdict, read or write depends on it."""
    if _off("STOP_GUARD"):
        return [], []
    budget = budget or seats_stop_budget.State()
    try:
        projscope.spend_or_raise("starting stop rung identity")
        answer = _stop_guard(session=session, room=room, seat=seat,
                             stop_active=stop_active, transcript=transcript,
                             cwd=cwd, budget=budget, detail=detail)
    except projscope.Expired:
        # NOT AN END. An expired ladder is the case the durable record exists
        # to catch, so it declares nothing and its last boundary stands as the
        # last rung seen to finish.
        return budget.expire()
    # THE END IS NOT HERE, AND PUTTING IT HERE WAS WRONG. This function
    # returning is not the ladder finishing: the RESPONSE rung runs after it,
    # published by the caller, and it is the ladder's last rung by
    # construction. On the live log 208 boundary records were written AFTER
    # their own run's end, which makes "the last line names the last rung seen
    # to finish" false about every one of them. The end now belongs to the
    # response publisher, the one place every completing path passes through.
    return answer
