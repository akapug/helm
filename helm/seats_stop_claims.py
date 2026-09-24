#!/usr/bin/env python3
"""helm seats — the CLAIMS rung of the stop guard, and only that rung.

EXTRACTED THE WAY `_seam_gate` WAS, AND FOR THE SAME REASON. seats_stop_guard
opens "ONE FUNCTION, AND ITS SIZE IS THE POINT ... they resolve in ONE place
because a seat either stops or it does not, and a posture resolved twice can
disagree with itself" — and two hundred lines below that sentence it already
calls a rung living in a sibling module, through the same `budget.run` door.
The sentence is about POSTURE resolving once, not about every rung's code
sitting inline; this rung resolves at exactly the door it always did.

WHY IT LEFT: it was a 476-line closure inside a 914-line function inside a
996-line module with four lines of headroom against the split budget, so the
next one-line addition anywhere in that file reddened a whole-suite gate.

THE SIGNATURE IS THE PRECEDENT'S. `_seam_gate(session, room, seat, cwd=None,
blocks=None, warns=None)` takes the identity triple and the two accumulators;
this takes the same plus the dispatch snapshot.

`dispatch_snapshot` IS PASSED RATHER THAN MOVED, and that is not a shortcut:
it memoises over `_dispatch_cache`, `budget` and `timing` — per-invocation
state of one _stop_guard call — so moving it would drag the budget machinery
with it. A caller-supplied reader keeps the memo exactly one stop wide.

THE TWO CONSTANTS CAME WITH IT because only this rung reads them, and
seats_stop_guard re-exports them because helm/seats.py and the suite both
import them FROM there. That is the split contract's founding case: a name
that looks internal turning out to have an external caller.

THE SIXTH STOP-GUARD RUNG HOLE (task/2388, after 1069 1098 1418 1874 2290),
AND THE CLASS IS THE SHAPE OF THE LADDER ITSELF: rung eligibility predicates
composed without a coverage matrix. Each exemption here was added on its own
measured case — a live delegated build, a lane in gate, a lane approved, a
renewing lock, a lease minted for a peer — and each was written as
`warns.append(...); continue`, which is correct in isolation and wrong in
composition, because `seats_stop_response.publish` emits blocks OR warns and
never both. Five locally-correct predicates therefore compose into a surface
that can print two of three held leases and drop the third with no marker at
all. Until a matrix exists that enumerates (every exemption) x (every exit),
the next rung or exemption reopens this hole; what this module can enforce
meanwhile is that an exemption has exactly ONE door out of the loop
(`_exempt`) and that door cannot emit without also printing.
"""
import hashlib

from . import pk, projscope
from .seats_common import (STATUS_BYTES, _clip, _now_mono, _scrub, _sweep,
                           claims_path)
from .seats_roster import roster_acquired, roster_indexes, seat_for_session_in
from .seats_delegation import (_delegated_build, _gate_pending, proc_scan,
                               release_hint)
from .seats_cursor import _remove_stop_latch, _write_stop_latch
from .seats_stop_signals import _off, _stop_fp_path
from .seats_room_advice import _dispatch_advice, _room_advice

# stoplease is the latch lane (the `.state` idiom every other rung uses,
# per (room, seat, session) via _stop_fp_path). LEASE_TTL_ALARM_S is the one
# severity threshold: a held lease whose remainder crosses it renders as
# EXPIRING and the crossing changes the latch fingerprint, so escalation
# re-arms the full block THROUGH the latch instead of being compressed by it.
LEASE_LATCH = "stoplease"
LEASE_TTL_ALARM_S = 120

# THE COMMAND THE SHORT BLOCK SENDS ITS READER TO, SPELLED ONCE. The footer
# and the latched one-liner both print it, and `chat.HELP["stop-guard"]` must
# offer the flag it names or the block points at a verb whose own usage denies
# it — a synopsis omission reads as absence to the next reader who checks
# before running. Two hand-kept spellings of one instruction is how a surface
# starts advertising a flag nothing else admits to having.
DETAIL_VERB = "helm chat stop-guard --detail"


def _ttl_human(left):
    """A remainder a person reads at a glance: "3h58m", "58m", "45s".

    A FIFTH `_fmt_age` IN THIS TREE, and the four that exist cannot answer this
    question. stalebot/seats_report/storage_matrix all roll up to DAYS and
    round a sub-minute remainder to "<1m"; seat_usability's renders "0h00m" for
    forty-five seconds. This number is read against LEASE_TTL_ALARM_S, which is
    120 SECONDS, so a formatter that cannot say "45s" erases the one band where
    the remainder is the whole message. Seconds are what the reader compares;
    hours are what a four-hour lease actually has.
    """
    left = max(0, int(left or 0))
    if left >= 3600:
        return "%dh%02dm" % (left // 3600, (left % 3600) // 60)
    if left >= 60:
        return "%dm" % (left // 60)
    return "%ds" % left


def _ttl_band(left):
    """THE LATCH'S SEVERITY COORDINATE, SPELLED ONCE FOR BOTH KINDS OF LEASE.

    A fingerprint is a claim about what the sermon SAYS, and both shapes say
    EXPIRING: the held line renders it and so does the exempt accounting line.
    The exempt coordinate hashed resource, token and state and NOT the band,
    so a steady held lease beside an unchanged gate-exempt lease crossing
    LEASE_TTL_ALARM_S fingerprinted identically — no full reprint, and the
    compressed one-liner promising "a remainder crossing 120s re-prints it"
    was false for exactly the lanes that carry no command (task/2388 round
    one, finding 3). Two spellings of one band is how they drifted apart, so
    there is one, and both `lease_fps` writers call it.
    """
    return "expiring" if left <= LEASE_TTL_ALARM_S else "held"


def claims_rung(session, room, seat, blocks=None, warns=None,
                dispatch_snapshot=None, detail=False):
    """The claims/leases rung: append to `blocks` and `warns`, return None.

    `detail` RENDERS THE LONG FORM AND CHANGES NOTHING ELSE — no read, no
    write, no verdict moves. It is what `helm chat stop-guard --detail` sets,
    so the per-lane diagnostic the hook's one-line summary replaces stays
    reachable by a named verb instead of being printed at every stop. It also
    bypasses the same-state compression, because a reader who asked for the
    detail is asking about the state the latch says they have already seen.

    APPENDS RATHER THAN RETURNS, which is the contract it already had as a
    closure and the one `_seam_gate` keeps too. The accumulators are the
    caller's, so the order rungs speak in stays the caller's decision.
    """
    if session and not _off("STOP_GUARD_CLAIMS"):
        c = _sweep(pk.read_json(claims_path(), {}) or {})
        now = _now_mono()
        # ONE ROSTER READ PER CLAIMS RUNG. The old loop called
        # seat_for_session(row.session) and then read roster_path() again for
        # each foreign-looking holder: 1+C to 1+2C full JSON parses. Capture
        # one tri-state snapshot and project both directions from it so every
        # claim is judged against the same bytes. A failed read makes every
        # ownership inference UNKNOWN; it never becomes an empty roster.
        try:
            roster_rows, roster_failed = roster_acquired()
        except projscope.Expired:
            raise
        except Exception:
            roster_rows, roster_failed = {}, True
        session_seats, holder_sessions = (
            roster_indexes(roster_rows) if not roster_failed else ({}, {}))
        resolved_sessions = {}

        def _resolve_roster_session(sid):
            if not sid or roster_failed:
                return None
            sid = str(sid)
            if sid not in resolved_sessions:
                resolved_sessions[sid] = seat_for_session_in(
                    session_seats, sid)
            return resolved_sessions[sid]

        lease_seat = _resolve_roster_session(session) or seat
        # A lease with a live renewer behind it never lets its remainder
        # fall far: the gate legacy-lock daemon refreshes gatelock:<project>
        # every 5s against a 30s TTL, so a healthy in-flight gate lease
        # displays ~26-29s left INDEFINITELY — and a number that looks like
        # an expiry beside a pre-filled release command made releasing the
        # correct-looking act. Measured twice in one day: a near-miss
        # caught only by sampling twice and seeing the remainder go UP
        # (~12:00Z), and gate #29 killed mid-run by its own seat
        # releasing the runner's lock (kimi 18:29Z — the renewer failed
        # "not claimed; suite killed", gate closed UNKNOWN).
        #
        # THE DISCRIMINATOR IS THE REMAINDER ITSELF, one read, no sleep:
        # a renewed lease's remaining stays within (renew interval +
        # margin) of its full TTL — renewed every 5s against 30s, it never
        # drops below ~25s — while a genuinely lapsing lease falls toward
        # 0. The double-read probe this replaced could not fire in
        # production: consecutive renews of a minutes-old lease differ by
        # the RENEW INTERVAL (5s), not by any pause the guard chooses, and
        # a 0.6s window over a 5s cycle usually contains no renew at all
        # (a refutation of the first cut, both worlds measured). The
        # observed symptom was always the tell: 26-29s IS "near full TTL".
        #
        # AND THE TWO-SAMPLE PREMISE ITSELF WAS UNSOUND, proven live by
        # a second trace (19:47Z, gate provably running
        # under the lease): two samples 3s apart read 28s then 25s — the
        # remainder FELL, because a renewing lease spends most of its
        # life between renews decaying exactly like a lapsing one. A
        # "rose or held flat = renewing" rule reads that lease as lapsing
        # and offers the command that kills the gate; the one-read floor
        # reads both samples correctly. The rising first trace (27->29)
        # confirmed the mechanism; the falling second trace is the one
        # that discriminates the methods — the two-sample form would
        # have been wrong ~88% of the time (a 3s sample lands between
        # renews 3/5 of the time, and between any two renews the
        # remainder always falls).
        #
        # Constants come from the renewer's own module (gate._GATE_LEGACY_*)
        # with a fallback to the measured values if gate is unimportable;
        # only gatelock: leases are checked — the one renewed claim shape
        # on the box (the FIFO serializes by queue file, not claims).
        # Kill-switch HELM_STOP_GUARD_LEASE_TTL=0 restores the bare
        # release command.
        renewed = set()
        lease_fps = []
        if not _off("STOP_GUARD_LEASE_TTL"):
            try:
                from helm import gate as _gate
                _ttl = float(_gate._GATE_LEGACY_TTL_S)
                _interval = float(_gate._GATE_LEGACY_RENEW_S)
            except projscope.Expired:
                raise
            except Exception:
                _ttl, _interval = 30.0, 5.0
            _floor = _ttl - _interval - 1.0   # one interval + 1s margin
            for _r, _v in c.items():
                if _r == "_fence" or not isinstance(_v, dict):
                    continue
                if not str(_r).startswith("gatelock:"):
                    continue
                # Only a lease THIS SESSION MINTED gets the no-release
                # hint — the trap shape is the runner claiming on my
                # behalf (session mine, holder me or the runner's name).
                # A HANDED lease (holder me, session a peer's) must keep
                # its release command: releasing it is the receiver's
                # documented move, and its renewer keeps it above the
                # floor forever — the receiver arm of the handoff fix
                # (test_lease_recovery) red-ed on exactly this.
                if str(_v.get("session")) != str(session):
                    continue
                if _v.get("exp_mono", 0) - now > _floor:
                    renewed.add(_r)
        held = []
        exempt = []            # (res, left, reason, text) — printed, not held
        connected = False       # any session-connected row, exempted or held

        def _exempt(r, res, left, v, state, reason, prefix="lease "):
            """THE ONE DOOR an exempted lease leaves the loop through.

            `state` IS BOTH THE PRINTED WORD AND THE LATCH COORDINATE, in the
            ladder's own currency: a state word is spoken only where a
            measurement was made, and the branch that reached this door IS that
            measurement. Short and upper-case on purpose — see the sermon.

            AN EXEMPTION WRITTEN AS A `warns.append(...); continue` PAIR IS
            A HOLE, which is why none of them is written that way: the guard's
            publisher emits blocks OR warns, never both, so on any stop where
            one lane is held the sermon becomes a block and every exempted
            lane's only line is discarded unprinted. Measured on a seat holding
            three leases: the sermon named two and the third — nine dirty files
            in a lane with a live in-room process — was ABSENT, which is the
            one reading that makes a seat walk away from uncommitted work.

            So an exemption is no longer an exclusion from the printed set: it
            is an ANNOTATION on a line that still appears. This door cannot
            emit the warn without also queueing the printed line and the
            fingerprint coordinate, because it does all three in one call."""
            warns.append("[helm stop-guard] %s%s%s %s" % (
                prefix, res,
                # DISPLAY SEVERITY AND LATCH TRUTH SAY THE SAME THING. The
                # coordinate below re-fires the full sermon on the crossing, so
                # the sentence that re-fires has to name what changed; without
                # this the exempt warning carried no TTL at all and a re-print
                # read as a repeat.
                " (%ds left, EXPIRING)" % left
                if left <= LEASE_TTL_ALARM_S else "", reason))
            exempt.append((res, left, state, reason, warns[-1]))
            # THE EXEMPTION IS A FINGERPRINT COORDINATE. A delegate dying or a
            # gate clearing changes what the sermon says about that lane, so
            # the latch must not compress it away: `state` is the branch that
            # exempted it, and a change of branch re-prints the full set. THE
            # BAND IS A COORDINATE HERE FOR THE SAME REASON IT IS ON A HELD
            # LEASE — this line renders EXPIRING, so a crossing is a change of
            # what the sermon says and owes a reprint (see `_ttl_band`).
            lease_fps.append("%s\x1f%s\x1fexempt:%s\x1f%s" % (
                r, v.get("lease") or "", state, _ttl_band(left)))

        # ONE LEDGER READ PER STOP, memoised across every lane lease in this
        # loop and across both rungs that want it (the gate-pending exemption
        # and the release advice's review read). Measured 190ms on a
        # 1,577-event ledger, so N leases x 2 rungs of naive re-reads is the
        # difference between a guard that runs on every stop and one someone
        # switches off. Read LAZILY: a stop with no lane lease pays nothing.
        def _ledger():
            return dispatch_snapshot()

        # AND ONE PROCESS-TABLE READ PER STOP, for the same reason and on the
        # same lazy terms. The delegation exemption below asks the table one
        # question per lane -- who is standing in THIS room -- and every step
        # before that comparison is identical across lanes: the pid list, the
        # uid filter, this process's own ancestry, the cwd of each candidate.
        # Run inside the loop it is O(leases x processes); MEASURED at 0.030s
        # per walk over 1156 pids, a seat holding 18 lane leases paid ~0.54s of
        # table-scanning per stop before a single git read, inside a rung whose
        # reserve is a constant that knows nothing about the lease count.
        #
        # MEMOISED, NOT PRECOMPUTED. A stop with no lane lease never takes the
        # walk at all, and a stop with one pays exactly what it paid before.
        # The cell holds the scan object itself -- including an UNREADABLE one,
        # which every lane must see as UNKNOWN rather than re-attempting per
        # lane and possibly disagreeing with its neighbour about the same host.
        _scan_cell = []

        def _scan():
            if not _scan_cell:
                _scan_cell.append(proc_scan())
            return _scan_cell[0]

        for r, v in sorted(c.items()):
            if r == "_fence" or not isinstance(v, dict):
                continue
            mine_by_session = v.get("session") == str(session)
            holder = str(v.get("holder") or "").strip()
            holder_is_me = bool(seat) and holder == str(seat).strip()
            # A row must be CONNECTED to this seat before anything else runs:
            # by the session that minted it, or by naming this seat as holder.
            # (holder-only is the RECEIVER of a claim-on-behalf hand-off, who
            # was invisible to their own guard before this.) Nothing below may
            # ADOPT a row connected to neither — an earlier cut of this fix
            # did, and GuardInstructionRefusesTheWrongCallerTest caught it.
            # The inverse session mapping below distinguishes the receiver.
            # THE HAND-OFF IS PROVABLE AFTER ALL, and I said it was not.
            # I compared seat->session (does the HOLDER's roster row name my
            # session) and concluded the row could not distinguish a peer's
            # hand-off from another session of my own seat. A review tried the
            # INVERSE and it is decisive: the roster maps session->seat, so
            # `row.session` has exactly ONE owning seat (measured on the live
            # roster: 292 seats carry a session, ZERO sessions are claimed by
            # two). If that owner is not the holder, a DIFFERENT seat minted
            # this grant for someone else — that is a hand-off, proven from
            # the row. If it is the holder, the row is that seat's own claim
            # under one of its sessions, which is the WorkOffer case and must
            # keep passing. No new field, no #66 dependency.
            minter = _resolve_roster_session(v.get("session"))
            handed = bool(minter) and bool(holder) and minter != holder
            # RECEIVER: I hold it, a peer minted it. Invisible before this —
            # a seat could end a turn holding the fleet's gate mutex with
            # nothing to stop it. Demonstrated live on me at 05:55.
            if holder_is_me and handed:
                mine_by_session = True
            if not mine_by_session:
                continue
            connected = True
            # RENDERED ONCE, FOR EVERY BRANCH BELOW. The scrub/clip pair is
            # the claim-surface law this module already states below ("SCRUBBED
            # like every other claim surface"), and four branches each spelling
            # it differently is how one of them ends up raw. `left` comes up
            # here too because an exempted line carries its remainder exactly
            # like a held one.
            res = _clip(_scrub(str(r)).strip(), STATUS_BYTES)
            left = int(v.get("exp_mono", now) - now)
            # DISOWN ONLY ON POSITIVE EVIDENCE. A holder that is not my
            # resolved seat is NOT thereby someone else's: the guard resolves
            # roster-first while own_name() can differ, and callers claim
            # under names with no roster row at all (StopGuardGatePendingTest
            # does `claim(res, "alice", session=sid)` fourteen times; treating
            # "no roster evidence" as "not mine" regressed all fourteen —
            # measured 14 pass on b5d02c0, 14 fail). Another seat hit the same
            # wall with a bare `holder != seat` and reverted. The missing
            # clause is not a better name comparison: ABSENCE OF EVIDENCE IS
            # NOT EVIDENCE OF A DIFFERENT OWNER. Disown a session-matched row
            # only when the roster says its holder answers to a DIFFERENT live
            # session — which real peers do, and a bare claimed name does not.
            roster_read = not roster_failed
            hs = holder_sessions.get(holder) if roster_read else None
            foreign_holder = (not holder_is_me and bool(holder) and
                              bool(hs) and str(hs) != str(session))
            mine_by_holder = holder_is_me or (roster_read
                                              and not foreign_holder)
            if mine_by_session and not mine_by_holder:
                # THE GIVER. The old code BLOCKED their stop and printed a
                # pre-filled `release --lease <id> --seat <holder>` as the way
                # to end the turn — a guard instructing a seat to revoke
                # another seat's LIVE gate mutex. A seat that complied would
                # have killed a running whole-suite. Never block on someone
                # else's hold, and never print a cure that is destructive to
                # the holder; say what happened and allow the stop.
                _exempt(
                    # NEGATIVE AND UNREADABLE NEVER SHARE A VALUE WITH A KNOWN
                    # ANSWER. The reason below says UNKNOWN when the roster
                    # could not be read, but the state word — which is both the
                    # sermon's whole account of this lane and the latch
                    # coordinate — said PEER HOLD either way, so the guard
                    # asserted KNOWN peer ownership of a lease whose owner it
                    # could not name and ONE fingerprint covered both states
                    # (task/2388 round one, finding 2).
                    #
                    # MEASURED WHILE CURING IT, because the finding's mechanism
                    # is narrower than it reads: an unreadable roster also makes
                    # seat_incarnation None, so the latch cannot be written and
                    # this sermon degrades to a WARN — the state word and the
                    # sentence that contradicted it arrive TOGETHER, rather than
                    # the sentence being dropped. That narrows the damage and
                    # not the falsehood: a block raised by any other rung
                    # discards both (task/2390), and the latch compared equal
                    # across a real change of state either way.
                    r, res, left, v,
                    "PEER HOLD" if roster_read else "OWNER UNKNOWN",
                    ("was minted by this session "
                     "FOR %s, who holds it — not yours to release, and this "
                     "stop is ALLOWED. (If it needs releasing, that is the "
                     "holder's call.)" if roster_read else
                     # UNCERTAINTY MAY NOT PRINT A DESTRUCTIVE CURE. With the
                     # roster unreadable we cannot tell an alias of ourselves
                     # from another seat, and the two want opposite actions.
                     # Say UNKNOWN and allow: the cost of allowing a stop on a
                     # lease that was ours is a lease held slightly longer; the
                     # cost of printing `release --seat <holder>` on a lease
                     # that was NOT ours is killing another seat's live gate.
                     "records holder %s and the "
                     "roster could not be read, so whether it is yours is "
                     "UNKNOWN — this stop is ALLOWED and no release command is "
                     "offered, because the wrong one revokes another seat's "
                     "live hold.")
                    % _clip(_scrub(holder), STATUS_BYTES))
                continue
            if not mine_by_holder and not mine_by_session:
                continue
            # THE RECEIVER falls through to the block below on holder alone.
            # Before this, a handed lease was INVISIBLE to its holder's guard —
            # they could end a turn holding the fleet's gate mutex with nothing
            # to stop them, which is the orphaned-HOLDER class the guard exists
            # to prevent, reintroduced by the fix for orphaned QUEUERS.
            # THE DELEGATION EXEMPTION (board row stop-guard-delegation-
            # exemption): a session with delegated work is waiting, not idle —
            # but waiting must be a VERIFIABLE SATURATED STATE, never a
            # declaration. Immediate exact-room child proof and recent active-
            # subagent evidence are independently bound and the warn SAYS which
            # passed; anything less blocks exactly as before.
            proof = None
            if not _off("STOP_GUARD_DELEGATION"):
                try:  # UNKNOWN → BLOCK: a raise here is not proof of anything
                    proof = _delegated_build(r, session, scan=_scan())
                except projscope.Expired:
                    raise
                except Exception:
                    proof = None
            if proof:
                pid_info = ("pid %d" % proof[0]) if isinstance(proof[0], int) else str(proof[0])
                _exempt(
                    r, res, left, v, "LIVE DELEGATE",
                    "held for a live delegated "
                    "build (%s in %s) — stop allowed, lease retained."
                    % (pid_info, _scrub(str(proof[1]))))
                continue
            # STAGES 2+3, one lifecycle stage later each: the build is
            # FINISHED and the lane sits IN GATE (an open review dispatch
            # whose --ref is the claimed worktree's current HEAD), or the
            # gate CLEARED — a bound APPROVE at that same HEAD — and the
            # lane awaits the integrator's land window. Release opens the
            # lane mid-gate/mid-land; finishing is the reviewer's move and
            # landing the integrator's. Same law: positive proof only,
            # re-derived every stop; a fix/supersede verdict re-blocks.
            gate = None
            if not _off("STOP_GUARD_DELEGATION"):
                try:
                    # An UNAVAILABLE ledger already folds to {} in
                    # `snapshot()`, which is the same empty mapping
                    # `rows()` handed this rung before — so the threaded
                    # value never changes an answer, it only stops the
                    # second read.
                    _snap, _note = _ledger()
                    gate = _gate_pending(
                        r, snap=_snap if isinstance(_snap, dict) else None)
                except projscope.Expired:
                    raise
                except Exception:
                    gate = None
            if gate:
                _exempt(
                    r, res, left, v,
                    "GATE APPROVED" if gate[2] == "approved" else "GATE PENDING",
                    ("held for a lane approved, "
                     "awaiting the land window (dispatch %s APPROVED by %s "
                     "with a bound gate at the worktree HEAD; landing is "
                     "the integrator's verb, not the holder's) — stop "
                     "allowed, lease retained."
                     if gate[2] == "approved" else
                     "held for a lane in gate "
                     "(dispatch %s pending at %s, ref == worktree HEAD) — "
                     "stop allowed, lease retained.")
                    % (_scrub(gate[0][:12]), _scrub(gate[1])))
                continue
            # SCRUBBED like every other claim surface. `claims_list` documents
            # the reason and this block was the one publisher that skipped it:
            # it interpolated the RAW resource key into the operator's stderr,
            # so a claim("evil\x1b[2J…") reached the terminal through the guard
            # even though `helm chat claims` had been safe for months.
            # #66. A LEASE ON A LANE WORKTREE CANNOT BE PROVEN IDLE, and the
            # guard was prescribing release as if it had been. Measured on the
            # integrator's live work-peek lease 2026-08-01: _delegated_build
            # returns None, and BOTH its proofs are unreachable for that
            # delegation shape — 0 live processes have cwd inside the claimed
            # room (a subagent inherits its parent's cwd, it does not chdir
            # into the lane), and the documented-subagent activity file was
            # never written. UNKNOWN, not idle.
            #
            # UNKNOWN STILL HOLDS THE STOP — widening the exemption on absent
            # evidence is the fleet-wide un-guarding the delegation docstring
            # forbids by name, and that stays. What changes is the ADVICE: a
            # guard may not prescribe a DESTRUCTIVE action on a premise it
            # could not establish. Releasing a lease held for a live subagent
            # opens the lane mid-build.
            #
            # #112's COMPUTABLE HALF, PROBED 2026-08-04, and the answer is
            # the honest none: the harness records every subagent under
            # <transcript-dir>/<session>/subagents/ (one .jsonl + one
            # .meta.json), but a COMPLETED subagent transcript ends in an
            # ordinary assistant row — no terminal marker, so live-vs-done
            # reduces to mtime, which this rung's own law rejects — and,
            # measured on 7 concurrently-LIVE builder subagents, every row
            # records the PARENT's cwd, never the lane room, so nothing
            # binds a live subagent to THIS lease's room either. The meta
            # carries only {agentType, description, toolUseId, spawnDepth};
            # the ~/.claude/tasks/<sid>/N.json rows are a tasklist's
            # declarations, not process measurements; and an in-process
            # subagent is not a separate OS process, so /proc cannot see it
            # between tool calls. The proofs _delegated_build already
            # consumes (in-room live scan + documented PostToolUse activity)
            # remain the ONLY reliable signals, so no "HELD FOR LIVE
            # DELEGATE" line is printed here — a state word without a
            # measurement would be the forged signal, not a feature.
            # A RENEWING lease is somebody's live hold — the gate runner's
            # heartbeat is the measured case — and the release command is
            # the act that kills it. Name the renewer, offer nothing, and
            # ALLOW THE STOP: the renewal is proof of an OUT-OF-TURN holder
            # (it heartbeats while this stop is already underway), so
            # holding the turn hostage protects nothing the renewer does
            # not already protect — it only forces the seat into a poll
            # loop against its own suite (three blocked stops in one
            # session, measured 2026-08-03). The fail-safe half of the
            # b4485c30 fix STAYS: no release command is ever offered
            # here, and the false-positive shape — a manual claim inside
            # its first renew-interval — self-expires within one TTL,
            # which an allowed stop cannot make worse.
            if r in renewed:
                _exempt(
                    r, res, left, v, "LIVE RENEWER",
                    "RENEWED since this stop began — "
                    "a live renewer holds this lease (the gate runner "
                    "heartbeats its lock while a suite runs). Stop allowed, "
                    "lease retained; do NOT release it. The run's completion "
                    "still owes its next leg (receipt, review mint) — stop "
                    "only if that completion can re-invoke you (task "
                    "notification or an armed beacon).",
                    prefix="")
                continue
            # the latch identity: raw resource + lease id (hashed, never
            # printed) + the TTL band — the three coordinates whose change
            # deserves a fresh full sermon.
            #
            # AND, FOR A DISPATCH CLAIM, THE ADVICE ITSELF. Those three
            # coordinates are all properties of the LEASE, and a dispatch
            # claim's instruction depends on the ROW: another actor can rebind,
            # cancel or hold it between two stops while the resource, the lease
            # and the TTL band all stay put. The fingerprint then said
            # "unchanged", the compressed line referred the seat back to
            # earlier instructions, and those instructions were the ones this
            # rung exists to correct. Folding the SENTENCE in makes every
            # transition that alters the advice alter the latch, which is the
            # property the compression promises.
            #
            # COST, stated because the line below deliberately does NOT do this
            # for a lane lease: the dispatch advice is one LEDGER read, already
            # memoised across both rungs and across every lease in this loop,
            # so a sermon stop pays nothing new and a latched stop holding a
            # dispatch claim pays that one read (~190ms on a 1,577-event
            # ledger) it previously skipped. `_room_advice` is four git-backed
            # reads per lane lease and stays out of the latch path.
            advice_fp = ""
            if str(r).startswith("dispatch:"):
                _snap, _note = _ledger()
                _sentence = _dispatch_advice(
                    r, seat, snap=_snap, ledger_note=_note)
                advice_fp = "\x1f" + hashlib.blake2b(
                    _sentence.encode("utf-8"), digest_size=8).hexdigest()
            lease_fps.append("%s\x1f%s\x1f%s%s" % (
                r, v.get("lease") or "", _ttl_band(left), advice_fp))
            # THE ROW, NOT THE PROSE. The hint and its advice are built below,
            # inside the branch that actually PRINTS them: the latch means most
            # stops emit the one-line compression, which never carried a hint,
            # and computing four git-backed reads for text nobody sees is how a
            # stop path acquires cost with no reader.
            held.append((res, left, r, v))
        # THE RESIDUAL, STATED WHERE THE NEXT READER WILL BE: the exempt lines
        # ride the sermon when there is one, and the WARN channel otherwise —
        # and `seats_stop_response.publish` emits blocks OR warns, so a refusal
        # raised by ANOTHER rung still discards them. That is not this
        # exemption's hole and it is not narrower than this rung: the
        # COMPRESSED one-liner is a warn too, so a foreign block hides HELD
        # leases by the same mechanism. It wants the fix `emit_blocks` already
        # describes (delivery decided at the one refusal door, via the
        # survives-refusal arming) applied to this rung's whole output, which
        # is a separate row rather than a wider version of this cure.
        if held:
            # THE SAME-STATE LATCH (owner-surfaced 2026-08-04): this arm
            # re-printed its full multi-line sermon on EVERY stop while the
            # held set was UNCHANGED — the owner watched the identical wall
            # twice in a row and asked whether that was intended. Every
            # sibling rung already latches per state (inbox on the pending
            # fp, beacon on armed|missing, wiring/punt on the finding text,
            # spiral on chain|rounds); this was the one publisher without a
            # memory. The fingerprint keys on (resource, lease id, TTL band)
            # so the full sermon re-fires on exactly the events that deserve
            # it — a new lease, a released lease, or a remainder crossing
            # LEASE_TTL_ALARM_S (severity escalates THROUGH the latch, it is
            # never compressed by it) — while a re-stop on the same set
            # compresses to ONE line that still carries the count and the
            # expiring tally. The compressed line is a WARN, not a block: the
            # inbox rung's own law ("a re-stop on the SAME rows passes")
            # covers an obligation already pointed at, and a block that
            # re-fires forever on unchanged state is the poll-loop wedge the
            # renewed-lease fix measured live (three blocked stops in one
            # session, 2026-08-03). An unwritable latch degrades the sermon
            # to a WARN as well — the spiral rung's law: a gate that cannot
            # remember must never become a wall.
            fp = hashlib.blake2b("|".join(lease_fps).encode("utf-8"),
                                 digest_size=8).hexdigest()
            fpp = _stop_fp_path(room, lease_seat, session, kind=LEASE_LATCH)
            try:
                with open(fpp) as f:
                    last = f.read().strip()
            except OSError:
                last = None
            if last == fp and not detail:
                # THE TALLY COUNTS THE EXEMPT LANES TOO, for the same
                # reason the count above does: the exempt coordinate now
                # carries the TTL band, so a crossing re-prints — and a tally
                # blind to the exempt half would name zero EXPIRING on the
                # very stop that escalated.
                expiring = sum(1 for left in ([_h[1] for _h in held]
                                              + [_e[1] for _e in exempt])
                               if left <= LEASE_TTL_ALARM_S)
                # THE HEADER RULING REACHES THE LATCH PATH TOO. This line
                # said "the full detail (exact release commands) printed
                # then" UNCONDITIONALLY — the sermon's original coupling
                # defect surviving one path over, the path the heterogeneous
                # witness never drives: a first stop on rooms helm could not
                # prove idle withholds EVERY command, so the latched re-stop
                # pointed its reader back at commands that were never
                # printed. Same division of labour as the sermon header: a
                # summary may describe the sermon's STRUCTURE (each line
                # owned its own instruction, or said why none was offered —
                # the exhaustiveness `_room_advice`'s branch arm pins), and
                # may assert NOTHING about content it does not own.
                # AND THE COUNT COVERS THE EXEMPT LANES TOO, because a count
                # that silently excludes them is the same omission one surface
                # smaller: a seat comparing "2 lease(s) held" against three
                # rows in `helm work list` reads the missing one as released.
                # THE SUMMARY MAY DESCRIBE THE SERMON'S STRUCTURE AND ASSERT
                # NOTHING ABOUT CONTENT IT DOES NOT OWN. It once said "the
                # exact release commands printed then" unconditionally, which
                # is false the moment a lane helm could not prove idle
                # withholds every command — so it names the verb that reprints
                # instead of describing what that reprint will say.
                warns.append(
                    "[helm stop-guard] %d lease(s) held%s%s, unchanged. "
                    "Reprint: %s"
                    % (len(held),
                       " +%d exempt" % len(exempt) if exempt else "",
                       " (%d EXPIRING)" % expiring if expiring else "",
                       DETAIL_VERB))
            else:
                lines = []
                for res, left, _r, _v in held:
                    cmd = release_hint(_r, _v)
                    # Keyed on what the lease CLAIMS TO BE, not on whether its
                    # directory currently resolves: a lane lease whose room is
                    # missing is MORE reason to withhold a blind release, not
                    # less — and `_room_advice` says so in as many words.
                    #
                    # THE ADVICE REVOKES THE COMMAND, and that is the point
                    # of its first return value. NO room read gives the guard
                    # standing to hand anyone a copy-pasteable release: the
                    # four reads see ARTEFACTS, never PRESENCE, so a clean room
                    # cannot prove nobody is in it and a room with findings has
                    # PROVEN there is work to strand. So the sentence REPLACES
                    # the command rather than trailing it. The boolean stays
                    # because the shape is the audit: the exhaustiveness arm
                    # reads it against "NO release command is offered", and a
                    # branch that ever earns a True must pass through here.
                    if cmd and str(_r).startswith("worktree:"):
                        _snap, _note = _ledger()
                        keep, advice = _room_advice(
                            _r, snap=_snap, ledger_note=_note, brief=not detail)
                        cmd = (cmd + advice) if keep else advice.lstrip(" —").strip()
                    # A DISPATCH CLAIM GETS THE SAME TREATMENT AND NEVER DID.
                    # It was listed with its resource and its TTL and nothing
                    # about the ROW, so a seat whose row had been cancelled or
                    # rebound read a discharged claim as work it still owed —
                    # measured twice in one session, on two seats, from one
                    # rebind of mine.
                    elif str(_r).startswith("dispatch:"):
                        # THE SENTENCE RIDES, IT DOES NOT REPLACE. Unlike a
                        # lane lease, releasing a dispatch claim strands
                        # nothing — the row keeps its recipient and status —
                        # so there is no branch where withholding the command
                        # protects anything, while withholding it denies the
                        # seat the only printing of its own lease token.
                        _snap, _note = _ledger()
                        advice = _dispatch_advice(
                            _r, seat, snap=_snap, ledger_note=_note,
                            brief=not detail)
                        cmd = (cmd + advice) if cmd \
                            else advice.lstrip(" —").strip()
                    lines.append(
                        "  %s %s%s — %s" % (
                            res, _ttl_human(left),
                            " EXPIRING" if left <= LEASE_TTL_ALARM_S else "",
                            cmd or "no lease token on this row; let it expire"))
                # THE EXEMPT LANES ARE LINES, NOT ABSENCES — the second
                # promised shape, which this header has promised all along:
                # "a lane it could not prove idle says why and offers none".
                # An exemption stopped being an exclusion at `_exempt`.
                #
                # COMPACT, BECAUSE THE OTHER OWNER OBSERVATION IS ALSO TRUE.
                # Claude Code renders every exit-2 emission as a red "Stop hook
                # error:", so "stop allowed, lease retained" printed beside the
                # one real demand glows red as an error. Accounting and
                # reassurance are different jobs: the red block gets ONE
                # scannable line per lane, marked NO ACTION OWED and carrying
                # its measured state word and nothing else, so nothing in it
                # reads as a demand and no lane's absence reads as a release.
                # The full sentence still rides the warn channel.
                for res, left, state, _reason, _text in exempt:
                    lines.append(
                        "  %s %s%s — NO ACTION OWED (%s)" % (
                            res, _ttl_human(left),
                            " EXPIRING" if left <= LEASE_TTL_ALARM_S else "",
                            state))
                # THE HEADER IS TWELVE WORDS AND ASSERTS NOTHING ABOUT CONTENT
                # IT DOES NOT OWN, and the second half of that sentence is the
                # defect CLASS rather than either instance. It first said "run
                # the EXACT command shown" unconditionally, which lied once the
                # advice began REVOKING commands. The obvious patch — compute
                # the promise from whether ANY line kept its command — is the
                # SAME coupling at finer granularity, and it lies the moment
                # one lane is measured and another is not. That mix is not an
                # edge case: an orchestrating seat is exactly who holds several
                # lane leases at once.
                #
                # So the division of labour is absolute. The HEADER says why
                # the stop is held and sends the reader down. Each LINE owns
                # what to do about its own lane — its exact command, or plainly
                # why none is offered. No line can inherit a promise, so a
                # heterogeneous sermon cannot lie in either direction.
                #
                # THE WORDING DELIBERATELY AVOIDS THE TOKEN "auto-claimed": the
                # actuator's own whisper owns that phrase, and three
                # WorkOfferTest cases assert its ABSENCE from a stop's stderr
                # to prove the rung did not fire. This block rides the same
                # stream, so reusing the phrase here would forge the very
                # signal those tests read.
                #
                # AND THE DETAIL IS ONE FOOTER, NOT ONE CLAUSE PER LANE. The
                # long form is what the owner reported reading on every blocked
                # stop; naming the verb that reprints it costs one line however
                # many lanes are held, and `--detail` is that verb's flag
                # rather than a second surface that could answer differently.
                sermon = ("[helm stop-guard] leases held by this session — "
                          "act per line, then stop:\n" + "\n".join(lines)
                          + ("" if detail else
                             "\n  why, in full: " + DETAIL_VERB))
                try:
                    latched = _write_stop_latch(fpp, lease_seat, fp, session=session)
                except OSError:
                    latched = False
                if latched:
                    blocks.append(sermon)
                else:
                    warns.append(sermon)  # unlatchable -> degrade, never wall
        elif connected:
            # AN ALL-EXEMPT STOP CLEARS THE EMISSION MEMORY. The hole it
            # closes: sermon (latched) → a delegation/gate proof appears →
            # the proof DIES. Without this clear, the third stop's held set
            # fingerprints identically to the latched one and a delegate's
            # death — the closest thing this arm sees to a stranded lease —
            # would compress into the one-liner. The stop after an exemption
            # lapses is a fresh event and must be loud again; the latch
            # compresses repetition, never severity escalation.
            try:
                _remove_stop_latch(
                    _stop_fp_path(room, lease_seat, session, kind=LEASE_LATCH),
                    lease_seat, session=session)
            except OSError:
                pass
