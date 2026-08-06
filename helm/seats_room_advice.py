#!/usr/bin/env python3
"""The room read the stop-guard speaks: what is UNFINISHED in this room, and
what helm could not measure about it.

EXTRACTED FROM seats_stop_guard BECAUSE OF ITS OWN BUDGET. These four
functions arrived from task/112 during the split's merge with trunk, landed
next to their only consumer, and pushed seats_stop_guard.py to 1230 lines
against the split's 1000-line finish line — the ratchet caught it, which is
what a ratchet is for. They are a coherent unit on their own terms (one READ
made of four sub-reads, one partial-read law, one advice renderer), so they
get a module rather than a smaller consumer.

THE ONE LAW WORTH RESTATING HERE: a read helm could not make rides `unknowns`
and NEVER shrinks the denominator. `_missed` exists so a partial read says
WHICH of the four is missing; `_ROOM_READS` is that denominator and it is a
constant, not a count of whatever happened to succeed.
"""
import os

from . import vcs
from .seats_common import STATUS_BYTES, _clip, _scrub
from .seats_delegation import _lane_stem, _lease_worktree

_ROOM_READS = ("working tree", "landedness", "review", "gate")
_ROOM_ROWS_SHOWN = 2


def _missed(reads, why):
    """One unknown entry PER READ, never one entry covering several.

    The advice quotes "N of 4 reads could not be made" and derives N by
    counting these entries, so a single string standing for three failed
    reads would under-report the blindness — an error in the direction of
    reassurance, printed under a destructive command."""
    return ["%s: %s" % (read, why) for read in reads]


def _ledger_snapshot():
    """(state by id, unavailable) — the dispatch ledger, read ONCE per stop.

    A named seam rather than an inline call because two rungs of one stop
    (`_gate_pending`'s exemption test and `_room_unfinished`'s review read)
    ask the same 190ms question, and the second one arriving later is exactly
    how a stop path doubles its cost without anybody noticing."""
    from . import dispatches
    return dispatches.snapshot()


def _room_unfinished(resource, snap=None, ledger_note=None):
    """(findings, unknowns) — the UNFINISHED WORK BOUND TO THIS ROOM, named.

    THE QUESTION THIS REPLACES WAS DELEGATION, AND DELEGATION WAS BOTH THE
    WRONG QUESTION AND AN UNANSWERABLE ONE. Wrong: measured over four live
    holds in one night (2026-08-04, @helm-claude-2) the correct answer was
    HOLD every time, and delegation explained exactly ONE of them — an
    outstanding review, a gate in flight, and a cure awaiting re-review after
    a FIX verdict are all correct holds for reasons no subagent probe can
    see, so the guard's basis was right 1 time in 4. Unanswerable: probed
    2026-08-04 against 7 concurrently-live builder subagents, a completed
    subagent transcript ends in an ordinary assistant row (live-vs-done
    reduces to mtime, which `_delegated_build`'s own law rejects) and every
    row records the PARENT's cwd, so nothing binds a subagent to THIS room.
    A guard may not prescribe a destructive act on a premise it cannot
    establish — and it may not ask the narrow question when the wide one is
    deterministically computable from state helm already keeps.

    FOUR READS, each through the primitive that ALREADY OWNS IT. A second
    authority for any of these would be the defect, not the feature:

      * WORKING TREE — `work._lanes._room_status`, the one status authority
        (`work._gc._dirty` is its boolean projection; `vcs.dirty` is the
        second oracle that authority exists to retire). Its write age is a
        live worker's signature, and this is the read that covers a delegate
        genuinely building — the one specimen delegation got right.
      * LANDEDNESS — `work._gc._merge_state`, ancestry OR patch identity,
        printed through `_gc._proof_word`. A hand-rolled `is_ancestor` here
        would repeat the measured error that predicate exists to correct: of
        107 lane branches, 7 carried work that was entirely on trunk under
        rebased shas and invisible to ancestry alone.
      * REVIEW — `dispatches.owed` over the SAME snapshot `_gate_pending`
        already read this stop; that read measured 190ms on a 1,577-event
        ledger, so reading it twice on the stop path would be the real
        regression. Deliberately WIDER than `_gate_pending`'s test: that one
        demands ref == HEAD because it grants an UN-GUARDING, while this one
        only NAMES what is outstanding — and a review row whose ref no
        longer matches the tip is outstanding work too. It is precisely the
        specimen where the holder committed on top of a review in flight.
        REPO-SCOPED THREE WAYS (task/142's settled Stop rule, applied at
        this read): the row's writer-stamped `repo_id` is compared against
        `dispatches._repo_info(room)` — the ONE scrubbed identity
        authority; a second derivation here would be the second-oracle
        defect this lane cures. A KNOWN same-repo row counts; a row
        POSITIVELY scoped to another repo is excluded (@codex-2's two-repo
        fixture: the only open review on this lane stem lived in a
        DIFFERENT project and was named as THIS room's unfinished work);
        a legacy row carrying no `repo_id` is relevant-but-unprovable and
        contributes UNKNOWN, never same-project proof.
      * GATE — `gate.inflight_census`, a LIVE pid in this room's own marker
        directory. Chosen over "a receipt minted recently": a receipt's
        recency is an mtime heuristic, this is a liveness proof, and it is
        room-scoped by construction where `gatelock:<project>` is not. The
        TYPED census, not the bare `inflight()` projection: the projection
        returns None for BOTH "no gate" and "could not look", and consuming
        it here laundered an unreadable marker dir into "no gate running"
        (@codex-2's reproduction). UNREADABLE lands in `unknowns` with its
        reason; only a measured empty stays silent.

    EVERY READ DEGRADES TO UNKNOWN AND SAYS SO. A read that cannot be made
    lands in `unknowns` carrying its reason; none of them may collapse to a
    silent "nothing here", because the sentence this feeds sits directly
    under a destructive command."""
    findings, unknowns = [], []
    room = _lease_worktree(resource)
    if not room:
        return findings, _missed(_ROOM_READS,
                                 "this lease resolves to no lane room")
    head = None
    if not os.path.isdir(room):
        # A MISSING ROOM IS NOT AN IDLE ROOM. Three of the four reads need the
        # checkout; the review read is pure ledger and still answers below.
        unknowns += _missed(("working tree", "landedness", "gate"),
                            "the claimed room is not on disk")
    else:
        try:
            from .work import _lanes
            st = _lanes._room_status(room)
        except Exception:
            st = None
        if not isinstance(st, dict):
            unknowns.append("working tree: the room status read failed")
        elif st.get("dirty") and st.get("wrote_ago") is None and st.get("unknown"):
            # `_room_status` reports an UNREADABLE room as dirty (its callers
            # must fail toward rescue). Here that would print a specific claim
            # helm did not measure, so the one shape with no measurable write
            # clock AND a stated reason degrades to UNKNOWN instead.
            unknowns.append("working tree: %s" % st["unknown"])
        elif st.get("dirty"):
            findings.append(
                "UNCOMMITTED changes in the room (newest write %ds ago%s)%s"
                % (st.get("wrote_ago") or 0,
                   ", %d conflicted path(s)" % st["conflicts"]
                   if st.get("conflicts") else "",
                   " [read incomplete: %s]" % st["unknown"]
                   if st.get("unknown") else ""))
        try:
            head = vcs.backend(room).head_sha(room)
        except Exception:
            head = None
        if not head:
            # ONE READ PER ENTRY, always — `_room_advice` reports "N of 4",
            # and a single entry standing for two reads makes that count a
            # lie in the direction of reassurance.
            unknowns += _missed(("landedness", "gate"),
                                "the room's HEAD could not be resolved")
        else:
            try:
                from .work import _gc
                state = _gc._merge_state(room, head)
                word = _gc._proof_word(state)
            except Exception:
                state = word = None
            if state is None or state == vcs.UNKNOWN:
                unknowns.append("landedness: %s" % (word or "unreadable"))
            elif state == vcs.NOT_ANCESTOR:
                findings.append("the room's tip %s is %s vs the trunk"
                                % (head[:12], word))
            try:                        # a LIVE pid, never a marker's mtime
                from . import gate as _gate
                census = _gate.inflight_census(room)
            except Exception:
                census = None
                unknowns.append("gate: the in-flight marker could not be read")
            if census is not None and census.reason:
                # THE TYPED FORM, NOT THE PROJECTION. `gate.inflight()`
                # collapses unreadable into None, and this read consuming
                # that projection was the third instance of one class found
                # in one night (gate.inflight / seat_homes.walk /
                # dispatches.stop_candidate): a primitive swallowed "could
                # not look" and its consumer printed "nothing there".
                # Reproduced by @codex-2 — a PermissionError on the marker
                # dir read as a clean census and the advice asserted "no
                # gate running" about a directory helm never saw. The
                # reason rides even beside a LIVE owner: a partial census
                # is still a read not fully made.
                unknowns.append("gate: %s" % census.reason)
            if census is not None and census.live:
                findings.append("a GATE IS RUNNING in this room (pid %s, "
                                "started %s)" % census.live)
    if snap is None and not ledger_note:
        try:
            snap, ledger_note = _ledger_snapshot()
        except Exception:
            snap, ledger_note = None, "the dispatch ledger raised"
    if ledger_note or not isinstance(snap, dict):
        unknowns.append("review: the dispatch ledger could not be read (%s)"
                        % (ledger_note or "no snapshot"))
        return findings, unknowns
    family = _lane_stem(str(resource).split(":", 2)[2])
    try:
        from . import dispatches
        owed = [row for row in dispatches.owed(snap)
                if isinstance(row, dict) and row.get("kind") == "review"
                and _lane_stem(row.get("lane")) == family]
    except Exception:
        owed = None
    if owed is None:
        unknowns.append("review: the owed-frontier read failed")
        return findings, unknowns
    if owed:
        # REPO SCOPE, THREE-WAY, through the ONE identity authority. The
        # stem filter above accepted every owed review carrying this lane's
        # NAME, whatever project it lived in — @codex-2's two-repo fixture:
        # the only open review named `lane-u` sat in a DIFFERENT repo and
        # this read called it THIS room's unfinished work with unknowns=[].
        # The row already carries the writer's canonical `repo_id` (the
        # scrubbed git-common-dir `_base` stamps), and `dispatches._repo_info`
        # is the one authority that derives it. Three arms, none collapsible:
        # a KNOWN same-repo row counts; a row POSITIVELY scoped to another
        # repo is not this room's work at all; a LEGACY row carrying no
        # scope is relevant-but-unprovable and lands in `unknowns` — never
        # laundered into same-project proof, never silently dropped.
        # ONE unknown entry for the whole set, per `_missed`'s law: this is
        # one READ partially made, not N failed reads, and the advice's
        # "N of 4" count must never inflate past the reads that exist (the
        # gate census set the precedent: a partial read rides `unknowns`
        # with its reason). Paid only when same-family rows exist at all —
        # `_repo_info` is two git subprocesses, and the common stop has
        # zero rows to classify.
        try:
            room_id = (dispatches._repo_info(room) or {}).get("repo_id")
        except Exception:
            room_id = None
        if not room_id:
            unknowns.append(
                "review: %d open review row(s) on this lane family could "
                "not be tied to this room (its repo identity is unreadable)"
                % len(owed))
            return findings, unknowns
        # EXCLUSION IS THE ONE OUTCOME THAT LEAVES NO TRACE, so it must be
        # earned by a POSITIVE identity rather than by inequality. The first
        # cut read every truthy `repo_id` unequal to the room's as a known
        # foreign scope and dropped it silently — @codex-2's repro rewrote a
        # real row's `repo_id` to the integer 123, replay still owed it, and
        # this read answered findings=[] unknowns=[]: a FALSE CLEAN built out
        # of a value nothing had validated. `_repo_info` mints `repo_id` as
        # `os.path.realpath` of the git-common-dir, so canonical means an
        # ABSOLUTE PATH STRING and nothing else. A row that is not canonical
        # is not foreign, it is UNREADABLE, and it rides `unknowns` with the
        # rows that predate the field — same one-entry-per-READ law.
        def _canonical_repo_id(value):
            if not isinstance(value, str):
                return None
            v = value.strip().rstrip(os.sep)
            # BOTH SIDES OF THE `==` MUST NORMALISE THE SAME WAY, or the
            # comparison excludes by SPELLING and the law above is a live
            # counter-example to itself. `room_id` arrives through
            # `_repo_info`'s `os.path.realpath`; this side only stripped. So
            # `…/helm/../helm/.git` and `…//helm/.git` — THIS room, both of
            # them — failed the `==` and were dropped with no trace: an
            # exclusion earned by inequality (@helm-claude-2, T2 on this
            # lane). Resolving here makes the two sides commensurable. The
            # `rstrip` stays AHEAD of the guard so a bare `/` still collapses
            # to unreadable rather than resolving to the filesystem root.
            return os.path.realpath(v) if v.startswith(os.sep) else None

        # ONE resolution per row: `_canonical_repo_id` now stats the path, so
        # the two-comprehension form would walk every row's symlinks twice.
        scoped = [(_canonical_repo_id(r.get("repo_id")), r) for r in owed]
        unscoped = [r for cid, r in scoped if cid is None]
        owed = [r for cid, r in scoped if cid == room_id]
        if unscoped:
            unknowns.append(
                "review: %d open review row(s) on this lane family carry no "
                "READABLE repo identity (absent, or not a canonical path) — "
                "whether they are this room's is UNKNOWN" % len(unscoped))
    for row in owed[:_ROOM_ROWS_SHOWN]:
        ref = str(row.get("ref") or "")
        if head and ref and (head == ref or head.startswith(ref)
                             or ref.startswith(head)):
            where = "at this exact tip"
        elif head and ref:
            where = "at %s, NOT the room's current tip" % ref[:12]
        else:
            where = "tip correspondence UNKNOWN"
        findings.append("an OPEN review dispatch %s to %s (%s)"
                        % (str(row.get("id") or "?")[:12],
                           str(row.get("recipient") or "?"), where))
    if len(owed) > _ROOM_ROWS_SHOWN:
        findings.append("%d further open review dispatch(es) on this lane"
                        % (len(owed) - _ROOM_ROWS_SHOWN))
    return findings, unknowns


def _room_advice(resource, snap=None, ledger_note=None):
    """(may_print_the_release_command, sentence) for one lane lease.

    The sentence is WHAT IS ACTUALLY UNFINISHED IN THIS ROOM, or an honest
    account of what could not be measured. Never empty — silence under a
    destructive command reads as endorsement.

    THE FIRST ELEMENT IS THE WHOLE RULING. The reads are DIAGNOSTIC, NEVER
    PERMISSIVE: they earn their keep by NAMING what is there when something
    is there, and they have no standing to authorise a release when nothing
    is, because their blind spot — a delegate that has not written a byte
    yet — is the FIRST MINUTES OF EVERY DELEGATED BUILD rather than a rare
    shape. So the all-clean branch returns False and the caller prints NO
    copy-pasteable release command at all. See that branch's comment for
    the two 2026-08-05 measurements that settled it.

    NO MTIME RUNG, DELIBERATELY, and this is the place someone will try to
    add one. It fails in BOTH directions on a fresh room (measured
    2026-08-05): read naively, every one of 510 files in a 15-minute-old
    room carries the CHECKOUT second and screams frantic activity for a
    room nobody has touched; read correctly as the newest AUTHORED write,
    it says idle for a room with a live delegate. `_room_status`'s
    `wrote_ago` appears below only as a DESCRIPTOR on a room already proven
    dirty by its own status read — it is never consulted for liveness, and
    widening it into one would make the clean case worse, not better.

    COST, A/B'd against the pre-#112 tree with the live 1,586-row ledger
    and two lane leases held. This leg ADDS ~37ms median / 46ms p90 per
    lane lease (R1 4.3 / head_sha 4.1 / R2 19.8 on the `git cherry`
    branch / R3 7.5 / R4 7.1) and it is paid ONLY when the sermon actually
    prints — a latched re-stop measured a call-count delta of ZERO. It
    also RETIRES two of five ledger folds per stop (~136ms each), so the
    whole stop path came out FASTER: sermon 1033ms -> 789ms (-245ms),
    latched 1003ms -> 761ms (-242ms). WORST CASE, stated because the win
    is ledger-shaped: against a trivial/empty ledger there is no fold to
    retire, and the sermon stop is +36-48ms for two leases while the
    latched stop is unchanged. (An earlier reading of +176ms was
    contention from this lane's own concurrent mutation run and did NOT
    reproduce on a quiet box — it is noise, not a measurement.)"""
    try:
        findings, unknowns = _room_unfinished(resource, snap, ledger_note)
    except Exception:
        # measured NOTHING: the weakest possible standing, so the command
        # goes with it.
        return False, ("   — helm could not measure this room at all, so "
                       "whether releasing it strands work is UNKNOWN, and NO "
                       "release command is offered. Confirm the lane by hand.")
    # SCRUBBED AND CLIPPED PER ITEM, not per sentence: lane names, dispatch
    # recipients and git's own error text all reach this string, and the
    # claims surface's rule is that no interpolated value may carry control
    # bytes or unbounded length to a terminal. Clipping the JOINED string
    # would let one long item eat the others; clipping each keeps every
    # finding visible.
    def _fmt(items):
        return "; ".join(_clip(_scrub(str(i)), STATUS_BYTES) for i in items)

    # FINDINGS KEEP THE COMMAND. This is the one branch with standing to
    # print it: helm MEASURED what is in the room and named it, so a holder
    # who knows that work is theirs to abandon can act on a fully-informed
    # line. The caveat is the naming, not the withholding.
    if findings:
        return True, ("   — do NOT run that blind: this room has UNFINISHED "
                      "WORK BOUND TO IT — %s.%s Releasing it opens the lane "
                      "on that work; finish it or hand it off instead."
                      % (_fmt(findings),
                         " (%d of %d reads could not be made: %s.)"
                         % (len(unknowns), len(_ROOM_READS), _fmt(unknowns))
                         if unknowns else ""))
    if unknowns:
        return False, ("   — %d of %d reads could not be made (%s), so an "
                       "IDLE room is UNPROVEN and NO release command is "
                       "offered. Confirm the lane by hand."
                       % (len(unknowns), len(_ROOM_READS), _fmt(unknowns)))
    # ALL FOUR CLEAN AUTHORISES NOTHING, and this branch is the one the
    # owner overruled me on. I had it print "no unfinished work" and invite
    # the release; two measurements taken after I proposed that killed it.
    #
    # FIRST, 2026-08-05: the guard fired on THREE lease-held rooms at once
    # WHILE A LIVE AGENT-TOOL SUBAGENT WAS BUILDING IN ALL THREE, and
    # `_delegated_build` fired for NONE of them — an Agent-tool subagent
    # does not present the in-room cwd/ancestry shape that exemption looks
    # for, so whatever it catches, it is not this. Two of the three were
    # caught by a SINGLE read (one by unlanded commits alone at
    # uncommitted=0; one by uncommitted delta alone at ahead=0).
    #
    # SECOND, the case that is not an extrapolation: a room 12 minutes old
    # with a live spawned subagent measured uncommitted=0, ahead=0, no
    # bound dispatch, no gate — CLEAN ON ALL FOUR. That is the first
    # minutes of EVERY delegated build, so the blind spot is the COMMON
    # state a held-but-clean room is in, not a rare one.
    #
    # AND NOTHING ELSE IN HELM DISTINGUISHES IT. mtime was tried on that
    # same room and is confounded in both directions (see this function's
    # docstring). At that moment there was no computable signal separating
    # "delegate alive and orienting" from "abandoned room, safe to
    # release" — so the only honest line is one that reports and withholds.
    # THE TOKEN "delegated" IS RESERVED and may not appear in this string.
    # Two StopGuardDelegationTest cases read its ABSENCE from a stop's stderr
    # as proof the delegation EXEMPTION did not fire, so using the word here
    # forges exactly the signal they read — the same forged-token defect the
    # sermon's own comment warns about for "auto-claimed", which I
    # reintroduced one string over and the regression sweep caught.
    return False, ("   — %d reads found nothing bound to this room YET "
                   "(clean tree, tip landed, no open review, no gate "
                   "running) — and NO release command is offered, because "
                   "they CANNOT SEE a subagent that has not written a file "
                   "yet, which is the first minutes of every build a "
                   "subagent runs. Lease retained; confirm the lane by hand."
                   % len(_ROOM_READS))
