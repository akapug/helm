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

from . import projscope, vcs
from .seats_common import STATUS_BYTES, _clip, _scrub
from .seats_delegation import (_lane_stem, _lease_worktree,
                               lease_foreign_project)

_ROOM_READS = ("working tree", "landedness", "review", "gate")
_ROOM_ROWS_SHOWN = 2


def _missed(reads, why):
    """One unknown entry PER READ, never one entry covering several.

    The advice quotes "N of 4 reads could not be made" and derives N by
    counting these entries, so a single string standing for three failed
    reads would under-report the blindness — an error in the direction of
    reassurance, printed under a destructive command."""
    return ["%s: %s" % (read, why) for read in reads]


# THE CLOCK IS AN INPUT, AND IT IS THE ONE INPUT A ROOM READ CANNOT SEE.
# These reads run under the Stop guard's cooperative budget, and the
# primitives they call spend against it BEFORE doing any work:
# `vcs.Backend.run` opens with `projscope.spend_or_raise("git memo lookup")`
# and `dispatches._repo_info` with its own. So a spent budget arrives here as
# `projscope.Expired`, a bare `except Exception` catches it, and the read
# files it under the reason ITS AUTHOR had in mind — "the room status read
# failed", "the room's HEAD could not be resolved". Every one of those
# sentences is a claim about the ROOM, and the room was never opened.
#
# MEASURED, one control and one arm on ONE fixture room in ONE process, the
# scope deadline the only difference: unbudgeted the room reads findings=[]
# unknowns=[] (it is clean and readable); with the budget already spent the
# SAME room yields "working tree: the room status read failed", "landedness:
# the room's HEAD could not be resolved", "gate: the room's HEAD could not be
# resolved" — and the sentence a seat reads then says an IDLE room is
# UNPROVEN and sends it to confirm a lane that was never in question. That is
# not a hypothetical shape: read the guard's own probe log
# (`helm/stopprobe.py`) and the dispatch-ledger rung ends at its 7.5s reserve
# wall on every ladder of a busy run — 238 of 238 in the sample that opened
# this lane — so every such stop enters these reads with the clock gone.
#
# THE VERDICT DOES NOT MOVE. An unmeasured room is still UNPROVEN, still gets
# no release command, and the denominator is still four — `Expired` is the
# weakest possible standing, not a licence. What changes is WHICH INPUT the
# sentence accuses, which is the only half a reader can act on: a budget is
# repaired in the guard, a room is repaired in the lane, and for a week the
# guard has been naming the wrong one.
_BUDGET_SPENT = "the stop guard's budget expired before this read"


def _clock_lost(reads_owed):
    """One entry for every read the guard's own budget killed unmade.

    ONE DOOR, because the alternative is the same sentence spelled at six
    handlers, and six spellings of one rule is how a seventh read arrives
    saying something slightly different about the same clock."""
    return _missed(tuple(reads_owed), _BUDGET_SPENT)


def _clock_spent():
    """Is the guard's budget already gone? — `spend_or_raise`'s own test,
    asked without raising.

    Needed because not every primitive below RAISES its expiry outward:
    `_lease_worktree` resolves the lane room through `work._lanes.find_root`,
    a git read, under a bare `except Exception` that returns None — so a spent
    clock arrives at its caller as a resolved-to-nothing LEASE and cannot be
    told apart by an exception that never travels. Asking the clock is an
    INFERENCE and it is stated as one: what it establishes is that the read
    could not have been made reliably whatever else was true, which is exactly
    the sentence `_BUDGET_SPENT` says. The ruling does not move either way."""
    left = projscope.remaining()
    return left is not None and left <= 0


def _room_unfinished(resource, snap=None, ledger_note=None, cwd=None):
    """(findings, unknowns) — the UNFINISHED WORK BOUND TO THIS ROOM, named.

    THE QUESTION THIS REPLACES WAS DELEGATION, AND DELEGATION WAS BOTH THE
    WRONG QUESTION AND AN UNANSWERABLE ONE. Wrong: measured over four live
    holds in one night the correct answer was
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
        POSITIVELY scoped to another repo is excluded (a review's two-repo
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
        (a review's reproduction). UNREADABLE lands in `unknowns` with its
        reason; only a measured empty stays silent.

    EVERY READ DEGRADES TO UNKNOWN AND SAYS SO. A read that cannot be made
    lands in `unknowns` carrying its reason; none of them may collapse to a
    silent "nothing here", because the sentence this feeds sits directly
    under a destructive command.

    WHO CALLS IT. The stop resident (`helm/stopfacts_resident.py`), once per
    change of an input, anchoring each lease at `cwd` — the repository its
    claim recorded. The Stop hook reads the result from the stop facts and
    never calls this: four git-backed reads per lease per stop was the cost
    the resident exists to take off the hook."""
    findings, unknowns = [], []
    # THE READS STILL OWED, so an expiry can name exactly what it did not
    # reach. Each read strikes its own name the moment it has ANY answer —
    # a finding, a measured silence, or an unknown of its own — and the
    # expiry handler reports the remainder. A read added later that forgets
    # to strike itself over-reports blindness, which is the safe direction
    # and the one `_missed` already chose.
    reads_owed = list(_ROOM_READS)
    room = _lease_worktree(resource, cwd=cwd)
    if not room:
        # NAME THE SCOPE LIMIT RATHER THAN THE ROOM'S ABSENCE. All four reads
        # are cwd-scoped, so a lease held in ANOTHER repository is unreadable
        # from here by construction — the room is ordinary and this process is
        # simply somewhere else. Rendering that as "no lane room" sends the
        # holder looking for a missing directory and repeats every stop for
        # the lease's whole TTL.
        foreign = lease_foreign_project(resource, cwd=cwd)
        # A LEASE IN ANOTHER REPOSITORY AND A CLOCK THAT RAN OUT ARE TWO
        # DIFFERENT SENTENCES WITH TWO DIFFERENT REPAIRS, and only one of them
        # is about the lease. The foreign read is a pure upward path walk, so
        # it still answers on a spent clock and keeps its own instruction;
        # everything else that lands here on a spent clock is the budget.
        if not foreign and _clock_spent():
            return findings, _clock_lost(reads_owed)
        return findings, _missed(
            _ROOM_READS,
            # SHORT, BECAUSE IT IS SAID FOUR TIMES. `_missed` writes one
            # entry per read so the denominator stays honest, and the full
            # explanation belongs once in the instruction below rather than
            # four times inside the parenthetical.
            ("in repository %s, not this one" % foreign)
            if foreign else "this lease resolves to no lane room")
    head = None
    if not os.path.isdir(room):
        # A MISSING ROOM IS NOT AN IDLE ROOM. Three of the four reads need the
        # checkout; the review read is pure ledger and still answers below.
        unknowns += _missed(("working tree", "landedness", "gate"),
                            "the claimed room is not on disk")
        for _read in ("working tree", "landedness", "gate"):
            reads_owed.remove(_read)
    else:
        try:
            from .work import _lanes
            st = _lanes._room_status(room)
        except projscope.Expired:
            return findings, unknowns + _clock_lost(reads_owed)
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
        reads_owed.remove("working tree")
        try:
            head = vcs.backend(room).head_sha(room)
        except projscope.Expired:
            return findings, unknowns + _clock_lost(reads_owed)
        except Exception:
            head = None
        if not head:
            # ONE READ PER ENTRY, always — `_room_advice` reports "N of 4",
            # and a single entry standing for two reads makes that count a
            # lie in the direction of reassurance.
            unknowns += _missed(("landedness", "gate"),
                                "the room's HEAD could not be resolved")
            reads_owed.remove("landedness")
            reads_owed.remove("gate")
        else:
            try:
                from .work import _gc
                state = _gc._merge_state(room, head)
                word = _gc._proof_word(state)
            except projscope.Expired:
                return findings, unknowns + _clock_lost(reads_owed)
            except Exception:
                state = word = None
            if state is None or state == vcs.UNKNOWN:
                unknowns.append("landedness: %s" % (word or "unreadable"))
            elif state == vcs.NOT_ANCESTOR:
                findings.append("the room's tip %s is %s vs the trunk"
                                % (head[:12], word))
            reads_owed.remove("landedness")
            try:                        # a LIVE pid, never a marker's mtime
                from . import gate as _gate
                census = _gate.inflight_census(room)
            except projscope.Expired:
                return findings, unknowns + _clock_lost(reads_owed)
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
                # Reproduced — a PermissionError on the marker
                # dir read as a clean census and the advice asserted "no
                # gate running" about a directory helm never saw. The
                # reason rides even beside a LIVE owner: a partial census
                # is still a read not fully made.
                unknowns.append("gate: %s" % census.reason)
            if census is not None and census.live:
                findings.append("a GATE IS RUNNING in this room (pid %s, "
                                "started %s)" % census.live)
            reads_owed.remove("gate")
    if snap is None and not ledger_note:
        try:
            from . import dispatches
            snap, ledger_note = dispatches.snapshot()
        except projscope.Expired:
            return findings, unknowns + _clock_lost(reads_owed)
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
    except projscope.Expired:
        return findings, unknowns + _clock_lost(reads_owed)
    except Exception:
        owed = None
    if owed is None:
        unknowns.append("review: the owed-frontier read failed")
        return findings, unknowns
    if owed:
        # REPO SCOPE, THREE-WAY, through the ONE identity authority. The
        # stem filter above accepted every owed review carrying this lane's
        # NAME, whatever project it lived in — a review's two-repo fixture:
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
        except projscope.Expired:
            # `_repo_info` spends before it derives, so a spent budget lands
            # here and the handler below would report it as a room whose
            # REPO IDENTITY is unreadable — a claim about the checkout, made
            # about a checkout nothing asked.
            return findings, unknowns + _clock_lost(reads_owed)
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
        # foreign scope and dropped it silently — a review's repro rewrote a
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
            # exclusion earned by inequality (T2 on this
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


def _dispatch_advice(resource, seat, snap=None, ledger_note=None, brief=False):
    """The sentence for one `dispatch:` claim — what the ROW has become.

    `brief` PICKS THE SHORT SPELLING OF THE SAME SENTENCE, for the same reason
    `_room_advice` takes it: a blocked Stop prints the short form — the owner
    reads it in his terminal every time a seat is held — and `helm chat
    stop-guard --detail` prints the long one. Every branch chooses through
    `_say`, so the two spellings of one ruling stay side by side.

    IT RETURNS A SENTENCE AND NOT A RULING, unlike `_room_advice`. A lane
    lease's release can strand a delegate's unwritten work, so that helper may
    revoke its release command. Releasing a dispatch CLAIM removes the claim
    and its delegation-activity markers, not the dispatch row: its recipient,
    status and owed work are unchanged. The sentence RIDES the holder's
    release command rather than deciding whether to offer it.

    The offer layer can take a dispatch claim on the holder's behalf. Some
    lifecycle doors release the caller's own claim, but a rebound or otherwise
    stale claim can remain. The Stop line must explain the row as well as name
    the lease; the holder can also retrieve their token through `chat claims`.

    THE FIX BELONGS HERE AND NOT AT THE REBIND DOOR, which is the first place
    anyone reaches for. `_release_autoclaim` can only release the CALLER'S own
    lease, on purpose: a release is a durable mutation, so the identity layer
    resolves the actor through the admission door rather than trusting a name,
    and another seat's claim is invisible to it. Making rebind clear the
    previous recipient's claim would be a cross-seat write against exactly that
    rule. What the holder lacks is not authority — they have the lease — but
    KNOWLEDGE, and this is the surface that owes it to them.

    THE READS ARE DIAGNOSTIC, NEVER PERMISSIVE about what is OWED: a row this
    cannot resolve is reported as unreadable rather than guessed at, because
    an unreadable ledger and a discharged row look identical from here.

    WHAT COUNTS AS DISCHARGED IS NOT THIS MODULE'S TO DECIDE, and spelling it
    here as "any status that is not open" got it wrong in BOTH directions. The
    lifecycle owner says a dispatch is closed by a VERDICT, a CANCEL or a
    CLOSE; a HOLD is an acknowledged PAUSE that `mark_cancel` still admits as
    a live row, and an ADMINISTRATIVE RETIREMENT is terminal while leaving the
    status word `open` untouched. So a held row was called discharged and
    offered a release command for an obligation the holder still owes, and a
    retired row was called a LIVE obligation and sent its holder to answer
    something the door had already cleared. `query_is_open` owns active row
    status; `dispatches.carrier` owns whether a successor took that row's work.
    An OPEN or HELD parent is not itself owed when the canonical carrier walk
    finds a successor. Name that carrier without claiming the work discharged
    or inventing a second successor-status classification here.
    """
    from . import query
    from .dispatches import CLOSED_STATES, carrier

    def _say(short, long_):
        """The two spellings of ONE branch, chosen here so they cannot drift.

        A brief sentence kept anywhere but beside the long one is a second
        account of the same ruling, free to say something else the day one of
        them is edited."""
        return short if brief else long_

    rid8 = str(resource or "").split(":", 1)[-1].strip().lower()
    if not rid8:
        return _say(
            " — this claim names no row",
            "   — this claim names no row, so whether it is still "
            "owed cannot be read here")
    # THE PRODUCER RETURNS A PAIR, AND AN UNREADABLE LEDGER IS AN EMPTY DICT
    # PLUS A REASON — never None. Reading only for None meant a ledger helm
    # could not parse arrived here as a readable ledger CONTAINING NOTHING,
    # and every claim then matched zero rows and was reported as a row that is
    # not there. That is the one substitution this surface must never make:
    # absence and all-clear are the same observable, and only the reason word
    # tells them apart. `_room_unfinished` already reads the pair correctly;
    # this sibling did not.
    note = str(ledger_note or "").strip()
    if note or not isinstance(snap, dict):
        return _say(
            " — the dispatch ledger is unreadable, so what you owe is UNKNOWN",
            "   — the dispatch ledger could not be read (%s), so "
            "whether this row is still owed is UNKNOWN"
            % (note or "no snapshot"))
    matches = [row for rid, row in snap.items()
               if str(rid).lower().startswith(rid8)]
    if len(matches) != 1:
        return _say(
            " — %s matching row%s, so this claim binds to no one obligation"
            % (len(matches) or "NO", "s" if len(matches) != 1 else ""),
            "   — %s row%s in the ledger match this claim, so it "
            "cannot be bound to one obligation"
            % (len(matches) or "NO", "s" if len(matches) != 1 else ""))
    row = matches[0]
    status = str(row.get("status") or "").strip().lower()
    recipient = str(row.get("recipient") or "").strip()
    lane = str(row.get("lane") or "").strip() or "an unnamed lane"

    mine = bool(recipient) and recipient == str(seat or "").strip()

    if recipient and not mine:
        return _say(
            " — REBOUND to @%s; your claim is STALE" % recipient,
            "   — this row now names @%s, not you: it was REBOUND and "
            "your claim is STALE. Only you can clear it, because a "
            "lease is released by its holder" % recipient)
    # RETIREMENT IS TERMINAL AND LEAVES THE STATUS WORD ALONE, so it is asked
    # about BEFORE the status word is read at all.
    if query.query_is_retired_admin(row):
        return _say(
            " — %s was RETIRED at the door; your claim is STALE" % lane,
            "   — %s was RETIRED at the door: the obligation is "
            "discharged and your claim is STALE, whatever its "
            "status word still says. Only you can clear it, because "
            "a lease is released by its holder" % lane)
    if query.query_is_open(row):
        kid = carrier(row, snap)
        if kid is not None:
            return _say(
                " — %s is CARRIED by dispatch %s; this parent is not itself "
                "owed" % (lane, str(kid.get("id") or "?")[:12]),
                "   — %s is CARRIED by dispatch %s: this parent row is "
                "not itself owed. Supersession is not discharge; follow "
                "the carrier for the work's outcome"
                % (lane, str(kid.get("id") or "?")[:12]))
        if status == "held":
            # A HOLD IS A PAUSE, NOT AN ENDING. With no carrier, the row
            # remains owed. Claim release still leaves that obligation alone.
            return _say(
                " — %s is HELD, a PAUSE and not a discharge; still yours "
                "(helm dispatch release %s when it clears)" % (lane, rid8),
                "   — %s is HELD: a hold is an acknowledged PAUSE, "
                "not a discharge, so this obligation is STILL "
                "YOURS and the claim is not stale. Return it with "
                "`helm dispatch release %s` when the dependency "
                "clears" % (lane, rid8))
        if mine:
            return _say(
                " — %s is OPEN and yours: helm dispatch triage %s"
                % (lane, rid8),
                "   — %s is OPEN and addressed to you: this is a "
                "LIVE obligation, not a stale claim. Answer it "
                "with `helm dispatch triage %s`" % (lane, rid8))
        return _say(
            " — %s is OPEN with no recipient this surface can read; UNKNOWN"
            % lane,
            "   — %s is OPEN but names no recipient this surface can "
            "read, so whether it is still owed by you is UNKNOWN"
            % lane)
    if status in CLOSED_STATES:
        return _say(
            " — %s is %s; your claim is STALE" % (lane, status.upper()),
            "   — %s is %s: the obligation is discharged and your "
            "claim is STALE. Only you can clear it, because a lease "
            "is released by its holder" % (lane, status.upper()))
    return _say(
        " — %s records no status this surface can classify; UNKNOWN" % lane,
        "   — %s records no status this surface can classify (%s), "
        "so whether it is still owed is UNKNOWN"
        % (lane, status.upper() or "empty"))
def _room_advice(resource, snap=None, ledger_note=None, brief=False,
                 reads=None, foreign=None):
    """(may_print_the_release_command, sentence) for one lane lease.

    `reads` is `(findings, unknowns)` ALREADY MADE — the Stop hook passes the
    resident's stop facts here, so the ruling below is the same ruling over
    the same four reads, made by the process that could afford them. With
    `reads` given, `foreign` is the scope limit the caller already knows (the
    resident anchors each lease in its own repository, so it is None there)
    and nothing below reads git.

    `brief` PICKS THE SHORT SPELLING OF THE SAME RULING, never a different
    one. Every branch below returns both renderings from ONE expression, side
    by side, because two sentences for one branch held in two places is how a
    surface comes to say one thing and mean another; the ruling — the first
    element — is computed once and is the same either way. The short form is
    what a blocked Stop prints (the owner reads it in his terminal every time
    a seat is held), the long form is what `helm chat stop-guard --detail`
    prints. NO READ IS SKIPPED for a brief rendering: the four reads decide
    the ruling, and a cheaper ruling is a different ruling.

    The sentence is WHAT IS ACTUALLY UNFINISHED IN THIS ROOM, or an honest
    account of what could not be measured. Never empty — silence under a
    destructive command reads as endorsement.

    THE FIRST ELEMENT IS THE WHOLE RULING, AND NO BRANCH OF THIS READ EARNS
    A TRUE. The reads are DIAGNOSTIC, NEVER PERMISSIVE. They name what is in
    the room; they cannot say whether anyone is in it, because every one of
    them reads an ARTEFACT (uncommitted bytes, an unlanded tip, an owed
    ledger row, a live gate pid) and an agent that is reading, gating or
    thinking leaves no artefact for minutes at a time. The blind spot — a
    delegate that has not written a byte yet — is the FIRST MINUTES OF EVERY
    DELEGATED BUILD rather than a rare shape.

    So the all-clean branch withholds because idleness is UNPROVEN, the
    partial branch withholds because it is unproven on strictly less
    evidence, and the findings branch withholds because work is PROVEN
    BOUND — which is the strongest reason of the three. That last one is the
    inversion this function carried for a month: it printed "do NOT run that
    blind" together with the exact runnable string, on the one branch whose
    own evidence most contraindicates it.

    THE DOOR KEEPS THE RUNGS THIS READ LACKS, so withholding costs a paste
    and nothing else: `work._claims.release_lane` refuses a dirty room,
    refuses a room with live cwd occupants, and keeps room and branch for
    work that has not landed. A guard that pre-composes the command is
    claiming a standing the door owns.

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
        findings, unknowns = reads if reads is not None else \
            _room_unfinished(resource, snap, ledger_note)
    except Exception:
        # measured NOTHING: the weakest possible standing, so the command
        # goes with it.
        return False, (
            " — helm could not read this room; check the lane by hand"
            if brief else
            "   — helm could not measure this room at all, so "
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

    # ANY FAILED READ IS THE UNKNOWN EXIT, AND IT IS TESTED FIRST — BEFORE
    # `findings`. THE ORDER IS THE GUARANTEE. Testing findings first keeps the
    # command whenever ANYTHING was measured, so a room read on three axes out
    # of four renders MORE confidently than a room read on none: strictly
    # weaker evidence, strictly more assertive surface, and the copy-pasteable
    # release command printed at 3 of 4 that 4 of 4 correctly withholds. Two
    # live lanes reproduced it at once, at 1 of 4 and at 3 of 4, both with a
    # delegate building in them.
    #
    # THE FOUR READS ARE NOT INTERCHANGEABLE, which is why no partial count is
    # good enough. Each one proves a DIFFERENT disjunct of "work is bound
    # here" — uncommitted bytes, an unlanded tip, an open review, a live gate
    # — and a lane can be silent on three and loud on the fourth. Naming the
    # three that answered does not bound what the fourth would have said, so a
    # holder deciding "yes, that work is mine to abandon" is deciding against
    # an INCOMPLETE manifest, and the axis missing from it is exactly where
    # the surprise lives. The failure is not random either: these reads die
    # when the guard's budget expires, which happens on BUSY stops — so the
    # blindness is CORRELATED with the very activity that makes surrendering
    # the lane wrong, rather than distributed evenly over quiet rooms.
    #
    # THE FINDINGS ARE NOT DISCARDED, they travel into this branch's sentence.
    # A partial failure with measured work and a partial failure with none are
    # different situations and read differently; collapsing them would be the
    # same loss one level up from the one being cured.
    if unknowns:
        # THE INSTRUCTION FOLLOWS THE SCOPE. "Confirm the lane by hand" is the
        # right ask when a read failed and might succeed next time; for a lease
        # in another repository nothing here will EVER succeed, so the holder
        # is told where the answer lives instead of being asked again at every
        # stop. Recomputed rather than threaded through `_room_unfinished`: it
        # is an upward path walk, the signature is shared with other readers,
        # and a second parameter on it would be paid by every caller to serve
        # one branch.
        if reads is None:
            foreign = lease_foreign_project(resource)
        # ONE RETURN, TWO SPELLINGS. A second `return` for the brief form would
        # be a fifth branch in a function whose exhaustiveness arm counts them,
        # and that arm is what keeps the header's promise true. NEVER THE WORD
        # RELEASE IN EITHER SPELLING, and this branch is why: it is the exit
        # that WITHHOLDS the command because an IDLE room is UNPROVEN, so an
        # instruction to release is the one sentence it has no standing to say.
        # A true scope limit is not a licence to add one — naming WHERE the
        # reads can be made is the whole permissible content, and what to do
        # once there belongs to the branch that measured the room.
        if brief:
            text = (" — this lease lives in %s; answer it from a checkout "
                    "there" % _clip(_scrub(str(foreign)), STATUS_BYTES)
                    if foreign else
                    # THE REASON RIDES THE SHORT LINE TOO, BOUNDED. The
                    # unknowns ARE the producers' own sentences ("the room
                    # status read failed", "the dispatch ledger could not be
                    # read (<why>)") and compressing them to a count drops
                    # the one fact a reader can act on — including the
                    # expired-clock lane's attribution, which lives IN the
                    # reason and nowhere else. One per read, clipped to the
                    # same byte budget every other field on this line obeys.
                    " — %s%d of %d reads failed (%s), so %s; "
                    "check the lane by hand"
                    % ("unfinished work is bound to this room and "
                       if findings else "",
                       len(unknowns), len(_ROOM_READS),
                       "; ".join(_clip(_scrub(str(u)), 96)
                                 for u in unknowns),
                       "WHAT ELSE is bound is UNKNOWN" if findings
                       else "IDLE is unproven"))
        else:
            text = ("   — %s%d of %d reads could not be made (%s), so %s "
                    "and NO release command is offered. %s"
                    % (("this room has UNFINISHED WORK BOUND TO IT — %s — and "
                        % _fmt(findings)) if findings else "",
                       len(unknowns), len(_ROOM_READS), _fmt(unknowns),
                       "WHAT ELSE is bound to it is UNKNOWN" if findings
                       else "an IDLE room is UNPROVEN",
                       ("Inspect and confirm the lane from a checkout of "
                        "%s — no stop in this repository can answer for it."
                        % _clip(_scrub(str(foreign)), STATUS_BYTES))
                       if foreign else "Confirm the lane by hand."))
        return False, text
    # MEASURED WORK IS THE STRONGEST REASON TO WITHHOLD, NOT THE ONE REASON
    # TO OFFER — and this branch spent a month asserting the opposite. It
    # printed "do NOT run that blind" and handed over the runnable string in
    # the same line: a surface that states the ruling and supplies the means
    # to contradict it is two surfaces, and the reader pastes the one that
    # fits on a line. Observed on a lane with a live delegate and ZERO failed
    # reads, so this is not the partial-read defect one branch up; it is the
    # evidence and the command pointing opposite ways.
    #
    # WHAT THE FOUR READS PROVE, EXACTLY. They read ARTEFACTS in the room —
    # uncommitted bytes, an unlanded tip, an owed ledger row, a live gate pid.
    # None of them reads PRESENCE. So the read set answers "what is bound
    # here" and cannot answer "is anyone here", which is the question a
    # release actually turns on. The all-clean branch below says exactly that
    # in as many words, which is the standard every other branch here has to
    # meet: findings present or absent, the standing to offer the command is
    # not something this read can earn.
    #
    # AND NOTHING IS LOST BY WITHHOLDING, because the DOOR holds the rungs
    # this read does not: `work._claims.release_lane` refuses a DIRTY room
    # outright ("two exits, no third"), refuses a room with live cwd
    # occupants, and keeps both room and branch for work that has not landed.
    # The guard's job is the sentence — what is bound, which reads were made,
    # what to check by hand. Pre-composing the command claimed a standing the
    # door owns and the read cannot support.
    #
    # CLEARING A LANE THAT REALLY IS FINISHED STILL WORKS, and this branch
    # says where. That case is the one this hint was built for, and a lease
    # would rot if the TOKEN went with the command. It does not: the lane
    # census prints every lease this seat holds with its token, the recovery
    # path `release_hint` already documents. So the cost of withholding is
    # one READ between a holder and a destructive act, which is the whole
    # difference between a surface that reports and one that prescribes.
    if findings:
        return False, (
            " — NOT blind: unfinished work is bound to this room; "
            "finish it or hand it off"
            if brief else
            "   — this room has UNFINISHED WORK BOUND TO IT — %s — so "
            "NO release command is offered: releasing the lane opens it "
            "on that work. Finish it or hand it off instead. If you KNOW "
            "it is finished, `helm work list` carries this lane's lease."
            % _fmt(findings))
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
    return False, (
        " — nothing bound to this room YET, but a subagent that has not "
        "written a file is invisible to these reads; check the lane by hand"
        if brief else
        "   — %d reads found nothing bound to this room YET "
        "(clean tree, tip landed, no open review, no gate "
        "running) — and NO release command is offered, because "
        "they CANNOT SEE a subagent that has not written a file "
        "yet, which is the first minutes of every build a "
        "subagent runs. Lease retained; confirm the lane by hand."
        % len(_ROOM_READS))
