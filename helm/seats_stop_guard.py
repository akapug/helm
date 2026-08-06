#!/usr/bin/env python3
"""helm seats — stop_guard: the single decision about whether a seat may idle.

ONE FUNCTION, AND ITS SIZE IS THE POINT. Every rung in this package feeds
here: the inbox, the claim and lease arms with their delegation exemptions,
the beacon block, the wiring and punt gates, the review-spiral gate, the
whisper ladder, the claim-evidence warning, the clean-stop warning, and the
mechanical index/scratch tail. They have to be resolved in ONE place because
the answer is one answer — a seat either stops or it does not — and a
posture resolved twice is a posture that can disagree with itself.

WHAT BLOCKS AND WHAT ADVISES IS THE WHOLE DESIGN. Two things here refuse an
idle stop, and both mean the seat would otherwise become unreachable or
un-converged. Everything else prints and returns. A guard that blocked on
every true observation would be worked around within a day, and an agent
that has learned to work around a false refusal has learned to work around
the real one too — which is why the blocking set is small, fingerprinted,
and argued for in the code rather than tuned.

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

from . import chat, home, pk, vcs
from .seats_common import (STATUS_BYTES, _clip, _now_mono, _scrub, _sweep,
                           claims_path, roster_path)
from .seats_identity import _delivery_pause, derive_seat, identity_disagreement
from .seats_roster import seat_for_session
from .seats_delegation import (_claim_evidence_warning, _delegated_build,
                               _gate_pending, _lane_stem, _lease_worktree,
                               release_hint)
from .seats_stop_signals import (_beacon_block, _off, _pending_all, _rows_fp,
                                 _spiral_gate, _stop_fp_path)
from .seats_work_offer import _solo_load_candidate, _stop_whisper
from .seats_room_advice import (_ROOM_READS, _ROOM_ROWS_SHOWN,
                                _ledger_snapshot, _missed,
                                _room_advice, _room_unfinished)

# ── the lease arm's same-set latch ─────────────────────────────────────────
# stoplease is the latch lane (the `.state` idiom every other rung uses,
# per (room, seat, session) via _stop_fp_path). LEASE_TTL_ALARM_S is the one
# severity threshold: a held lease whose remainder crosses it renders as
# EXPIRING and the crossing changes the latch fingerprint, so escalation
# re-arms the full block THROUGH the latch instead of being compressed by it.
LEASE_LATCH = "stoplease"
NDP_LATCH = "stopndp"        # the non-distraction-protocol gate's latch lane
LEASE_TTL_ALARM_S = 120
# The four reads `_room_advice` makes, named so a PARTIAL read can say
# which one is missing rather than shrinking the denominator (task/112).
_ROOM_ROWS_SHOWN = 2      # findings are advice, not a report


def _ndp_gate(session, room, seat):
    """(block | None, warn | None) for the NON-DISTRACTION-PROTOCOL rung.

    OWNER CANON, 2026-08-03 by measurement and named 2026-08-05: "NDP really
    drives the relationship between TLAs and their subagents ... it rises to
    the level of importance to merit a conditional stopbook." The rule: work
    that arrives mid-flight and is not your critical path goes to a subagent
    IN THE TURN IT ARRIVES — never chased, never parked — because a TLA's
    scarcest resource is HOT CONTEXT ON THE CRITICAL PATH, and a distraction
    costs the reload of the thread it dropped, not the minutes it takes.

    WHAT IS ENFORCEABLE IS NARROWER THAN THE RULE, deliberately. The
    per-arrival trigger ("this row arrived mid-turn and was neither delegated
    nor dispatched before the turn ended") is NOT computable from state helm
    keeps: nothing binds an arrival to the delegation that handled it, so a
    seat that answered a trivial owner question inline is indistinguishable
    on disk from one that chased a distraction for an hour — and the inject
    fire-ledger's newest row only APPROXIMATES a turn boundary (capped,
    rotated, fail-open by design). A rung built on that would fire on every
    head-down seat that answered one question, which is the false positive
    that gets a blocking rung disabled by the first seat it wrongly blocks.
    So this gate enforces the provable PROJECTION of NDP: a wide load held
    with ZERO delegation all session (_solo_load_candidate — the same
    predicate, one authority), which is exactly the owner-measured incident
    (a wide queue held eight hours, zero subagent calls, three seats
    near-idle, the ask list ~46 -> 73 overnight).

    WHY A GATE, WHEN THE WHISPER ALREADY RODE THE EXIT-2 CHANNEL: the
    whisper ladder speaks ONE line per stop — the highest-salience unlatched
    rung — and solo-load sat second from the bottom. On precisely the seat
    this rung exists for, the rungs above it keep minting fresh fingerprints
    (the incident night's ask ledger alone grew by ~27 rows, each owning a
    stop's only slot), so the throughput nudge could trail the state it
    measures by hours. A gate owns its own emission and fires the same stop
    its predicate becomes true.

    Latched once per load-state (the predicate's bucket fp, NDP_LATCH lane):
    a re-stop on the same state passes, a doubled queue re-arms, and ONE
    delegation this session silences it outright. An unwritable latch
    DEGRADES to the compact WARN — a gate that cannot remember may not block
    every stop forever. No interpolated value here leaves this module (the
    line is composed from an int), so there is nothing to scrub. Fail-open
    TOTAL; HELM_STOP_GUARD_NDP=0 disables."""
    if _off("STOP_GUARD_NDP"):
        return None, None
    try:                      # fail-open TOTAL — a Stop rung must never wedge
        cand = _solo_load_candidate(session, seat)
    except Exception:
        return None, None
    if not cand:
        return None, None
    fp, line = cand
    path = _stop_fp_path(room, seat, session, kind=NDP_LATCH)
    try:
        with open(path) as f:
            last = f.read().strip()
    except OSError:
        last = None
    if last == fp:
        return None, None             # already gated this exact load-state
    try:
        chat._ensure_dir()
        pk.atomic_write(path, fp)
    except OSError:
        # unlatchable -> degrade to the compact advice, never a wall
        return None, "[helm stop-guard] " + line
    return ("[helm stop-guard] NON-DISTRACTION PROTOCOL — " + line + "\n"
            "  You are the only worker your queue has. A TLA's scarcest "
            "resource is HOT CONTEXT ON THE CRITICAL PATH, not tokens: "
            "anything else on your plate goes to a subagent in the turn it "
            "lands, because a distraction costs a full reload of the thread "
            "you dropped, not the minutes it takes. Measured shape of "
            "carrying alone (2026-08-03): a wide queue held 8h, zero "
            "subagent calls, three seats near-idle, the owner ask list grew "
            "~46 -> 73 overnight.\n"
            "  Blocks once per load-state — a re-stop passes, a doubled "
            "queue re-arms, one delegation this session ends it. "
            "HELM_STOP_GUARD_NDP=0 disables.\n"
            "  IF YOU ARE UNDER A STANDING INSTRUCTION NOT TO DELEGATE, this "
            "rung cannot see that and is wrong about you: your operator's "
            "constraint outranks it. Set HELM_STOP_GUARD_NDP=0 for the "
            "session and say so — this is advice about throughput, never a "
            "policy that overrides the human who gave you the constraint."), \
        None


def stop_guard(session=None, room="main", seat=None, stop_active=False,
               transcript=None, cwd=None):
    """-> (blocks, warns) for one Stop event. Posture resolved once (seat via
    the roster's session mapping, else the derived seat); checks are inline:
      (a) BLOCK — undelivered @mentions/owner rows past the seat's cursor,
          once per pending-fingerprint (blake2b of the pending row ids,
          latched in the room dir); a re-stop on the SAME rows passes. An
          unwritable latch degrades to the WARN (the beacon rung's no-latch-
          no-block law) — never an unconditional re-block.
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
          exists) with NO live `helm chat wait` beacon: an idle seat nothing
          can wake. Once per beacon-loss episode, absence must be PROVEN,
          HELM_STOP_GUARD_BEACON=0 disables (the restarted-integrator class).
      (b3) BLOCK — a REVIEW SPIRAL (_spiral_gate): this seat has review-
          dispatched ONE lane at 3+ DISTINCT tips inside the window, i.e.
          round three, which the store's own `review-begins-with-cat-file`
          forbids by name. The block quotes the exact `helm chat meld invite
          <peer> "<lane>: …"` cure. Two rounds WARN instead (the rule's stated
          cure point). Latched on (lane, round-count): one block per state, a
          further round re-arms, an unwritable latch degrades to the warn.
          HELM_STOP_GUARD_SPIRAL=0 disables.
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
      (e) silent mechanical — `helm index cap --apply` best-effort in-process
          (the documented Stop line, docs/VERBS.md): never blocks, never
          prints; HELM_STOP_GUARD_INDEX=0 disables.
    stop_active (the hook JSON's stop_hook_active) suppresses every BLOCK and
    mechanical leg but still surfaces the inbox and claim-evidence WARNs: the
    harness is already continuing, and a WARN cannot re-enter the stop path."""
    if _off("STOP_GUARD"):
        return [], []
    seat = seat or seat_for_session(session) or derive_seat(session)
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
    if stop_active:
        # #74. THE SHORT-CIRCUIT CONFLATED "MUST NOT BLOCK" WITH "MUST NOT
        # LOOK", and only the first is true. Blocking under stop_hook_active is
        # the infinite-loop shape both reference guards exist to prevent —
        # SURFACING is not, because a warn never re-enters the stop path.
        #
        # MEASURED COST OF THE CONFLATION, 2026-08-01: @kimi's verdict reached
        # @helm-claude-2 MID-TURN at 18:05; that turn ended at 18:11 with
        # stop_hook_active set, so the guard returned before it ever read the
        # inbox; the row was never shown and never latched. The fleet then sat
        # 5.5 hours waiting on the land that verdict authorized, and the owner
        # caught it from the road. A row that arrives mid-turn had exactly one
        # moment to be seen and the guard declined to look.
        #
        # So: read the inbox, say what is waiting, and RETURN NO BLOCKS. Fails
        # open like every other rung — an unreadable inbox is silence, never a
        # hold, because the one thing worse than an unseen row is a wedged turn.
        try:
            waiting = [] if delivery_paused or _off("STOP_GUARD_INBOX") \
                else _pending_all(room, seat, session, scan_lane="stop")
        except Exception:
            waiting = []
        surfaced = [dispute_warn] if dispute_warn else []
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
        return [], surfaced
    blocks, warns, pending = [], [], []
    if dispute_warn:
        warns.append(dispute_warn)
    inbox_blocked = False

    if not delivery_paused and not _off("STOP_GUARD_INBOX"):
        pending = _pending_all(
            room, seat, session, scan_lane="stop")  # EVERY room's inbox gates
        if pending:
            fp = _rows_fp(pending)
            fpp = _stop_fp_path(room, seat, session)
            try:
                with open(fpp) as f:
                    last = f.read().strip()
            except OSError:
                last = None
            if last != fp:
                latched = True
                try:
                    chat._ensure_dir()
                    pk.atomic_write(fpp, fp)
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
                groups, order = {}, []
                for rm, r in pending:
                    tag = ("[dm] " if rm.startswith(chat.DM_PREFIX)
                           else "" if rm == room else "[#%s] " % rm)
                    who = chat._dsan(r.get("from") or "?")
                    body = _clip(_scrub(r.get("text") or ""), 120)
                    key = (tag, who, body[:60])
                    if key not in groups:
                        groups[key] = [0, body]
                        order.append(key)
                    groups[key][0] += 1
                lines = []
                for key in order[:5]:
                    n, body = groups[key]
                    lines.append("  %s%s: %s%s" % (
                        key[0], key[1], body,
                        "   (x%d repeats)" % n if n > 1 else ""))
                if len(order) > 5:
                    lines.append("  ... %d more distinct (%d rows total)"
                                 % (len(order) - 5, len(pending)))
                body = (
                    "[helm stop-guard] %d undelivered message(s) for seat "
                    "'%s':\n%s\naddress these before stopping: `helm chat "
                    "read` SHOWS them but does NOT clear them (an addressed row "
                    "is an obligation; reading past one does not discharge it) "
                    "— ACT on them, or `helm chat catchup "
                    "--including-mentions --apply` parks them deliberately "
                    "(WITHOUT --apply catchup is a DRY RUN and parks nothing, "
                    "so this rung would just re-fire)."
                    % (len(pending), seat, "\n".join(lines)))
                if latched:
                    blocks.append(body + " This blocks once per pending set "
                                  "— a re-stop on the same rows passes.")
                    inbox_blocked = True
                else:
                    warns.append(body + " NOT BLOCKING: the once-per-set "
                                 "latch is unwritable, so a block here would "
                                 "re-fire on every stop.")

    if session and not _off("STOP_GUARD_CLAIMS"):
        c = _sweep(pk.read_json(claims_path(), {}) or {})
        now = _now_mono()
        # A lease with a live renewer behind it never lets its remainder
        # fall far: the gate legacy-lock daemon refreshes gatelock:<project>
        # every 5s against a 30s TTL, so a healthy in-flight gate lease
        # displays ~26-29s left INDEFINITELY — and a number that looks like
        # an expiry beside a pre-filled release command made releasing the
        # correct-looking act. Measured twice in one day: a near-miss
        # caught only by sampling twice and seeing the remainder go UP
        # (hc2 ~12:00Z), and gate #29 killed mid-run by its own seat
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
        # (hc2's refutation of the first cut, both worlds measured). The
        # observed symptom was always the tell: 26-29s IS "near full TTL".
        #
        # AND THE TWO-SAMPLE PREMISE ITSELF WAS UNSOUND, proven live by
        # hc2's second trace (2026-08-02 19:47Z, gate provably running
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
        connected = False       # any session-connected row, exempted or held
        # ONE LEDGER READ PER STOP, memoised across every lane lease in this
        # loop and across both rungs that want it (the gate-pending exemption
        # and the release advice's review read). Measured 190ms on a
        # 1,577-event ledger, so N leases x 2 rungs of naive re-reads is the
        # difference between a guard that runs on every stop and one someone
        # switches off. Read LAZILY: a stop with no lane lease pays nothing.
        _snap_cache = []

        def _ledger():
            if not _snap_cache:
                try:
                    _snap_cache.append(_ledger_snapshot())
                except Exception:
                    _snap_cache.append((None, "the dispatch ledger raised"))
            return _snap_cache[0]

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
            # SESSION-CONNECTED ONLY. The RECEIVER half of the
            # claim-on-behalf hazard is deliberately NOT fixed here, and the
            # reason is that the row cannot express it: "my seat name, a
            # different session" is BOTH the receiver of a hand-off AND
            # another session of my own seat that took its own dispatch — and
            # WorkOfferTest::test_new_head_after_a_take_offers_once pins the
            # latter as a NON-block (a lease another session minted is not
            # this turn's obligation). Blocking on holder alone regressed it.
            # Distinguishing the two needs a field the row does not carry:
            # #66's `minted_by`. Until that exists the receiver of a handed
            # lock stays invisible to their own guard, which is stated in the
            # commit and handed forward rather than papered over.
            # THE HAND-OFF IS PROVABLE AFTER ALL, and I said it was not.
            # I compared seat->session (does the HOLDER's roster row name my
            # session) and concluded the row could not distinguish a peer's
            # hand-off from another session of my own seat. @kimi tried the
            # INVERSE and it is decisive: the roster maps session->seat, so
            # `row.session` has exactly ONE owning seat (measured on the live
            # roster: 292 seats carry a session, ZERO sessions are claimed by
            # two). If that owner is not the holder, a DIFFERENT seat minted
            # this grant for someone else — that is a hand-off, proven from
            # the row. If it is the holder, the row is that seat's own claim
            # under one of its sessions, which is the WorkOffer case and must
            # keep passing. No new field, no #66 dependency.
            try:
                minter = (seat_for_session(v.get("session"))
                          if v.get("session") else None)
            except Exception:
                minter = None      # unreadable roster: UNKNOWN, never a guess
            handed = bool(minter) and bool(holder) and minter != holder
            # RECEIVER: I hold it, a peer minted it. Invisible before this —
            # a seat could end a turn holding the fleet's gate mutex with
            # nothing to stop it. Demonstrated live on me at 05:55.
            if holder_is_me and handed:
                mine_by_session = True
            if not mine_by_session:
                continue
            connected = True
            # DISOWN ONLY ON POSITIVE EVIDENCE. A holder that is not my
            # resolved seat is NOT thereby someone else's: the guard resolves
            # roster-first while own_name() can differ, and callers claim
            # under names with no roster row at all (StopGuardGatePendingTest
            # does `claim(res, "alice", session=sid)` fourteen times; treating
            # "no roster evidence" as "not mine" regressed all fourteen —
            # measured: 14 pass before, 14 fail). @helm-claude hit the same
            # wall with a bare `holder != seat` and reverted. The missing
            # clause is not a better name comparison: ABSENCE OF EVIDENCE IS
            # NOT EVIDENCE OF A DIFFERENT OWNER. Disown a session-matched row
            # only when the roster says its holder answers to a DIFFERENT live
            # session — which real peers do, and a bare claimed name does not.
            foreign_holder, roster_read = False, True
            if not holder_is_me and holder:
                try:
                    _r = pk.read_json(roster_path(), {}) or {}
                    hs = (_r.get(holder) or {}).get("session")
                    foreign_holder = bool(hs) and str(hs) != str(session)
                except Exception:
                    roster_read = False
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
                warns.append(
                    ("[helm stop-guard] lease %s was minted by this session "
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
                     "[helm stop-guard] lease %s records holder %s and the "
                     "roster could not be read, so whether it is yours is "
                     "UNKNOWN — this stop is ALLOWED and no release command is "
                     "offered, because the wrong one revokes another seat's "
                     "live hold.")
                    % (_clip(_scrub(str(r)).strip(), STATUS_BYTES),
                       _clip(_scrub(holder), STATUS_BYTES)))
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
                    proof = _delegated_build(r, session)
                except Exception:
                    proof = None
            if proof:
                pid_info = ("pid %d" % proof[0]) if isinstance(proof[0], int) else str(proof[0])
                warns.append(
                    "[helm stop-guard] lease %s held for a live delegated "
                    "build (%s in %s) — stop allowed, lease retained."
                    % (_scrub(str(r)), pid_info, _scrub(str(proof[1]))))
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
                except Exception:
                    gate = None
            if gate:
                warns.append(
                    ("[helm stop-guard] lease %s held for a lane approved, "
                     "awaiting the land window (dispatch %s APPROVED by %s "
                     "with a bound gate at the worktree HEAD; landing is "
                     "the integrator's verb, not the holder's) — stop "
                     "allowed, lease retained."
                     if gate[2] == "approved" else
                     "[helm stop-guard] lease %s held for a lane in gate "
                     "(dispatch %s pending at %s, ref == worktree HEAD) — "
                     "stop allowed, lease retained.")
                    % (_scrub(str(r)), _scrub(gate[0][:12]),
                       _scrub(gate[1])))
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
            # original fix STAYS: no release command is ever offered
            # here, and the false-positive shape — a manual claim inside
            # its first renew-interval — self-expires within one TTL,
            # which an allowed stop cannot make worse.
            if r in renewed:
                warns.append(
                    "[helm stop-guard] %s RENEWED since this stop began — "
                    "a live renewer holds this lease (the gate runner "
                    "heartbeats its lock while a suite runs). Stop allowed, "
                    "lease retained; do NOT release it. The run's completion "
                    "still owes its next leg (receipt, review mint) — stop "
                    "only if that completion can re-invoke you (task "
                    "notification or an armed beacon)."
                    % _clip(_scrub(str(r)).strip(), STATUS_BYTES))
                continue
            left = int(v.get("exp_mono", now) - now)
            # the latch identity: raw resource + lease id (hashed, never
            # printed) + the TTL band — the three coordinates whose change
            # deserves a fresh full sermon.
            lease_fps.append("%s\x1f%s\x1f%s" % (
                r, v.get("lease") or "",
                "expiring" if left <= LEASE_TTL_ALARM_S else "held"))
            # THE ROW, NOT THE PROSE. The hint and its advice are built below,
            # inside the branch that actually PRINTS them: the latch means most
            # stops emit the one-line compression, which never carried a hint,
            # and computing four git-backed reads for text nobody sees is how a
            # stop path acquires cost with no reader.
            held.append((_clip(_scrub(str(r)).strip(), STATUS_BYTES),
                         left, r, v))
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
            fpp = _stop_fp_path(room, seat, session, kind=LEASE_LATCH)
            try:
                with open(fpp) as f:
                    last = f.read().strip()
            except OSError:
                last = None
            if last == fp:
                expiring = sum(1 for _res, left, _r, _v in held
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
                warns.append(
                    "[helm stop-guard] %d lease(s) held by this session, "
                    "unchanged since the last warning%s — the full per-lane "
                    "detail printed then (each line carried its own "
                    "instruction: an exact command, or why none is offered); "
                    "a CHANGED set (a new lease, a release, or a remainder "
                    "crossing %ds) re-prints it. Leases retained."
                    % (len(held),
                       " (%d EXPIRING)" % expiring if expiring else "",
                       LEASE_TTL_ALARM_S))
            else:
                lines = []
                for res, left, _r, _v in held:
                    cmd = release_hint(_r, _v)
                    # Keyed on what the lease CLAIMS TO BE, not on whether its
                    # directory currently resolves: a lane lease whose room is
                    # missing is MORE reason to withhold a blind release, not
                    # less — and `_room_advice` says so in as many words.
                    #
                    # THE ADVICE CAN REVOKE THE COMMAND, and that is the point
                    # of its first return value. A room helm could not measure,
                    # or measured entirely CLEAN, gives the guard no standing
                    # to hand anyone a copy-pasteable release: the reads cannot
                    # see a delegate that has not written a byte yet, and that
                    # is the first minutes of every delegated build. So the
                    # sentence REPLACES the command rather than trailing it.
                    if cmd and str(_r).startswith("worktree:"):
                        _snap, _note = _ledger()
                        keep, advice = _room_advice(_r, snap=_snap,
                                                    ledger_note=_note)
                        cmd = (cmd + advice) if keep else advice.lstrip(" —").strip()
                    lines.append(
                        "  %s (%ds left%s)  ->  %s" % (
                            res, left,
                            " — EXPIRING" if left <= LEASE_TTL_ALARM_S else "",
                            cmd or "this row records NO lease token — it "
                            "cannot be released by any caller; let it expire"))
                sermon = (
                    # The wording deliberately avoids the token "auto-claimed":
                    # the actuator's own whisper owns that phrase, and three
                    # WorkOfferTest cases assert its ABSENCE from a stop's
                    # stderr to prove the rung did not fire. This block rides
                    # the same stream, so reusing the phrase here would forge
                    # the very signal those tests read.
                    #
                    # THE HEADER ASSERTS NOTHING ABOUT CONTENT IT DOES NOT OWN,
                    # and that is the defect CLASS rather than either instance.
                    # It first said "run the EXACT command shown"
                    # unconditionally, which lied once the advice began
                    # REVOKING commands (#66: every line withheld). The obvious
                    # patch — compute the promise from whether ANY line kept
                    # its command — is the SAME coupling at finer granularity,
                    # and it lies the moment one lane is measured and another
                    # is not. That mix is not an edge case: an orchestrating
                    # seat is exactly who holds several lane leases at once,
                    # and eight were held in one session while this was being
                    # written.
                    #
                    # So the division of labour is absolute. The HEADER says
                    # why the stop is held and sends the reader down. Each LINE
                    # owns what to do about its own lane — its exact command,
                    # or plainly why none is offered. No line can inherit a
                    # promise, so a heterogeneous sermon cannot lie in either
                    # direction, and a fifth branch cannot reintroduce this.
                    "[helm stop-guard] live claim lease(s) held by this "
                    "session. EACH LANE BELOW CARRIES ITS OWN INSTRUCTION — "
                    "read the line, not this header: a lane helm could measure "
                    "names the exact command that discharges it (already "
                    "carrying your own lease id, which a lease the actuator "
                    "claimed ON YOUR BEHALF never handed you); a lane it could "
                    "not prove idle says why and offers none. Leases retained; "
                    "act per line, or finish the work, before stopping:\n"
                    + "\n".join(lines))
                latched = True
                try:
                    chat._ensure_dir()
                    pk.atomic_write(fpp, fp)
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
                os.unlink(_stop_fp_path(room, seat, session, kind=LEASE_LATCH))
            except OSError:
                pass

    beacon = None
    try:  # the ARMED-BEACON gate — fail-open TOTAL (never wedge a stop)
        beacon = _beacon_block(session, room, seat)
    except Exception:
        beacon = None
    if beacon:
        blocks.append(beacon)

    if not _off("STOP_GUARD_WIRING"):
        # THE BUILT-BUT-NOT-WIRED RUNG. You may not go idle having added a
        # module nothing can reach. It fires on STOP, so it never interrupts
        # work — only finishing while leaving dead code behind.
        #
        # LATCHED ONCE PER DEBT, like every other rung here, and that was NOT
        # true when this shipped — fable's cross-review caught it the same
        # night. The commit claimed the finding was "scoped to what THIS tree
        # added, so no seat is ever blocked on debt it did not create", and on
        # the deployed topology that is FALSE: `wiring` derives the repo from
        # its own __file__, and every seat's Stop hook runs THIS ONE SHARED
        # CHECKOUT's bin/helm. So "this tree" is the same tree for every
        # session on the host. Combined with the repo's batch-push-at-slice-end
        # rule, one agent's unpushed unwired module would have blocked every
        # seat, on every stop, for hours, with a message reading "YOU added N
        # modules". That is exactly the "blocks everyone → switched off within
        # a day" failure the commit text names, shipped by the commit that
        # names it.
        #
        # The latch is the honest fix available tonight: one block per DISTINCT
        # debt (fingerprinted on the module list), so a real finding still
        # stops a seat once and a standing debt cannot become a wall. Narrowing
        # attribution to the ACTING session needs a signal helm does not have
        # in a shared checkout, and is filed rather than guessed at.
        try:  # fail-open TOTAL — a Stop hook that raises wedges every session
            from . import wiring
            gate = wiring.gate_lines()
        except Exception:
            gate = []
        if gate:
            # a plain digest of the finding text, NOT _rows_fp — that helper
            # takes (room, row) chat pairs and bending it here raised
            # ValueError, which the guard's fail-open would have swallowed into
            # a silently disabled rung: a latch that errors reads exactly like
            # a latch that passed.
            fp = hashlib.blake2b("\n".join(gate).encode("utf-8"),
                                 digest_size=8).hexdigest()
            fpp = _stop_fp_path(room, seat, session, kind="stopwiring")
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
                    pass      # a latch that cannot be written must not block
                blocks.append("[helm stop-guard] " + "\n  ".join(gate))

    # THE REVIEW-SPIRAL RUNG. Serialized rounds on ONE lane where a meld is the
    # cure — the rule was already in context and was not followed, so it becomes
    # a gate. Latched per (lane, round-count); fail-open total.
    try:
        spiral, spiral_warn = _spiral_gate(session, room, seat)
    except Exception:
        spiral, spiral_warn = None, None
    if spiral:
        blocks.append(spiral)
    if spiral_warn:
        warns.append(spiral_warn)

    # THE NON-DISTRACTION-PROTOCOL RUNG. A wide load held with zero
    # delegation all session becomes a conditional block (owner canon
    # 2026-08-03: "NDP ... rises to the level of importance to merit a
    # conditional stopbook"). Latched per load-state; fail-open total.
    try:
        ndp, ndp_warn = _ndp_gate(session, room, seat)
    except Exception:
        ndp, ndp_warn = None, None
    if ndp:
        blocks.append(ndp)
    if ndp_warn:
        warns.append(ndp_warn)

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
        except Exception:
            gate = []
        if gate:
            fp = hashlib.blake2b("\n".join(gate).encode("utf-8"),
                                 digest_size=8).hexdigest()
            fpp = _stop_fp_path(room, seat, session, kind="stoppunt")
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
                    pass      # a latch that cannot be written must not block
                blocks.append("[helm stop-guard] " + "\n  ".join(gate))

    if not _off("STOP_GUARD_WHISPER"):
        try:  # fail-closed to NOTHING: whisper trouble = silence, never louder
            w = _stop_whisper(session, room, seat, pending, inbox_blocked, cwd)
        except Exception:
            w = None
        if w:
            blocks.append(w)   # rides an existing block, or IS the soft hold

    # THE CLAIM-EVIDENCE RUNG — an outgoing message full of settled-sounding
    # claims the turn made no measurement to earn. It is evaluated only on an
    # ALLOW exit: block exits discard WARNs, so parsing and latching one there
    # would spend the hot path on output the caller cannot see.
    if not blocks:
        claim_warn = _claim_evidence_warning(transcript, room, seat, session)
        if claim_warn:
            warns.append(claim_warn)

    if not blocks and not pending:   # genuinely clean — a latched-pass (rows
        warns.append(                # still pending, already pointed at) stays
            "[helm stop-guard] inbox clean. If you intend to idle-wait, arm "
            "the beacon first: Monitor(command: \"helm chat wait --seat %s "
            "--follow\", persistent: true) — Monitor missing from your tools "
            "means DEFERRED not absent: ToolSearch(query: \"select:Monitor\")"
            % seat)  # silent, never "clean"

    if not _off("STOP_GUARD_INDEX"):
        try:  # the documented Stop line — silent, best-effort, never a gate
            from . import store
            store.index_cap(apply=True)
        except Exception:
            pass
        try:  # the scratch reaper's automatic leg — throttled, bounded, silent
            from . import scratch
            scratch.auto_gc()
        except Exception:
            pass
    return blocks, warns
