#!/usr/bin/env python3
"""helm — THE OBLIGATION SEAM: helm records every debt and delivers none.

task/928, owner P0, his words: "fix p0 at all levels of the loop, refacotr if
needed, just get us ABLE TO BURN DOWN ALL ROWS".

THE DEFECT IN ONE LINE. `owed_by` is computed, correct, rendered — and nobody
is woken by it. Measured on the live estate: 14 stalled land rows, 12 owed by
an AUTHOR who was handed a revision by a CHANGES_REQUESTED verdict and never
told. It has read as five separate bugs because exactly ONE leg ever got a
delivery mechanism; the other four shipped as state updates with notification
left to whoever happened to be looking.

SO THIS IS A SEAM, NOT A NOTIFIER. Five transitions each inventing their own
notice is precisely how four of them came to invent none. One door, called by
every transition, so the next transition cannot forget — the same argument
that put the foreign-row predicate one function above five observation sites
rather than at each of them.

WHAT THIS MODULE IS NOT. It is not a second chat: an @mention plus the inbox
beacon already wakes a seat, and this wires that up rather than replacing it.
It is not a closer: DELIVERED IS NOT ACKNOWLEDGED AND NEITHER CLOSES A ROW,
the same trap as witnessed-versus-landed. And it is not a sweep — re-delivery
and escalation are task/928 B and deliberately live elsewhere.

THE THIRD STATE, WHICH IS THE PART A REVIEWER SHOULD ATTACK FIRST. A role is
not a person. `OWED_BY` (landreq:707) yields integrator / reviewer / builder /
author / lander, while `_OWED_SEAT_FIELD` (landreq:4401) resolves only three of
those, because THE ROW RECORDS ONLY TWO PEOPLE — "author" is its sender and
"reviewer" is its recipient (landreq:2828). A row has never known who will land
it. So `lander` and `integrator` name a role with NO ADDRESSEE, and against the
live census that is 2 of the 14.

Those two must not share a bucket with "we tried and the post failed". NOBODY
WAS TOLD and THERE IS NOBODY TO TELL are different facts, and merging them
sends a reader hunting a broken poster for a row where delivery was never
possible. Hence `owed_seat` nullable + `unresolved_reason`, and a caller that
fails CLOSED on both: a failed post leaves the obligation UNDELIVERED, because
a fail-open delivery leg reproduces exactly today's silence while looking
healthy — and destroys the evidence that anything is wrong.
"""

import hashlib
import os

from . import landreq

# The four states an obligation's DELIVERY can be in, and the rule that
# governs all of them: NONE OF THEM CLOSES THE ROW. Delivered is not
# acknowledged, acknowledged is not discharged, and the debt is settled only
# when the WORK moves — the same trap as witnessed-versus-landed.
NEVER_DELIVERED = "never-delivered"
DELIVERED_UNACKED = "delivered-unacked"
ACKNOWLEDGED_STILL_OWED = "acknowledged-still-owed"
DELIVERY_UNKNOWN = "unknown"

# The roles that name a debt somebody could act on. `nobody*` is a settled row
# and carries none; `unknown*` is a debt whose HOLDER could not be determined,
# which is a fact worth surfacing rather than a row to skip.
_SETTLED = ("nobody",)
_UNKNOWABLE = ("unknown",)

# Roles that name a real duty for which the row records NO PERSON. Kept as an
# explicit tuple rather than inferred from a missing map entry, so that adding
# a seat field for one of them is a one-line change that this module notices.
_ROLE_WITHOUT_ADDRESSEE = ("integrator", "lander")


def obligation_id(rid, state, owed_since):
    """A stable id for ONE debt — deterministic, recomputable, no storage.

    It must change when a NEW debt is minted on the same row, or an old
    delivery would satisfy a fresh verdict; and it must NOT change on replay,
    or every past delivery reads as stale the moment a projection re-runs. A
    stored counter fails the second. Deriving it from (row, state, the stamp
    that minted the debt) gives both: the same debt recomputes identically
    forever, and a new verdict necessarily moves `state` or `owed_since`.
    """
    seed = "\x1f".join((str(rid or ""), str(state or ""), str(owed_since or "")))
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]


def obligation_of(lr):
    """The debt this land row currently carries -> dict, or None if settled.

    `owed_since` is `entered_ts` UNDER ITS OWN NAME rather than a copy: the row
    already records when it entered its current state, and the debt's age is
    NOT the row's age. Sorting a burn-down by row creation or update time
    reorders an old row that just took a fresh verdict, putting a brand-new
    obligation at the top of a list the owner asked to be oldest-first.
    """
    role, seat = landreq.ball_holder(lr)
    role = str(role or "unknown")
    if role.startswith(_SETTLED):
        return None
    owed_since = lr.get("entered_ts")
    unresolved = None
    if not seat:
        if role.startswith(_UNKNOWABLE):
            unresolved = "the holder of this row could not be determined"
        elif role in _ROLE_WITHOUT_ADDRESSEE:
            unresolved = ("no seat is recorded for the %s role — a row names "
                          "its author and its reviewer and nobody else" % role)
        else:
            unresolved = "role %r resolved to no seat" % role
    age_s = lr.get("dwell_s")
    return {"obligation_id": obligation_id(lr.get("id"), lr.get("state"),
                                           owed_since),
            "row": lr.get("id"),
            "owed_by": role,
            "owed_seat": seat or None,
            "unresolved_reason": unresolved,
            "owed_kind": lr.get("state"),
            "owed_since": owed_since,
            "owed_age_s": age_s,
            "what": _what(lr, role, age_s)}


def _what(lr, role, age_s=None):
    """The ACTIONABLE sentence, never a status word — AND IT CARRIES THE AGE.

    A notice that says "your row is CHANGES_REQUESTED" tells a seat something
    it could already see; what it cannot see is that the ball is now in its
    hand and what the next move is. The state name alone is why four of these
    legs shipped as state updates in the first place.

    THE AGE IS INSIDE THE SENTENCE ON PURPOSE, which makes one seat's second
    acceptance criterion structural rather than hoped for. That seat held the
    word "unread", which reads identically at ten minutes and at a day, and
    the number 17.9h existed nowhere it could see it. A surface can omit any
    field it is handed; it cannot omit a clause of the sentence it renders.
    So the age travels WITH the instruction rather than beside it, and no
    consumer can show this obligation without showing how long it has waited.

    `age_s` is the row's own `dwell_s` — seconds in its current state, already
    computed one floor down. Not recomputed here: two derivations of one fact
    is how HOME and the CLI came to answer different questions.
    """
    from .seats_report import _fmt_age   # lazy: one formatter, not a second
    lane = lr.get("lane") or lr.get("id")
    aged = ""
    if isinstance(age_s, (int, float)) and age_s > 0:
        aged = " — waiting %s" % _fmt_age(age_s)
    if role == "author":
        return ("a reviewer handed this lane back: read the verdict, fix the "
                "findings, re-gate and re-dispatch — lane %s%s"
                % (lane, aged))
    if role == "reviewer":
        return "this lane is waiting on your review — lane %s%s" % (lane, aged)
    if role == "builder":
        return "this lane is waiting on your build — lane %s%s" % (lane, aged)
    if role == "lander":
        return ("this lane is reviewed and waiting to land — lane %s%s"
                % (lane, aged))
    if role == "integrator":
        return "this lane is open and unrouted — lane %s%s" % (lane, aged)
    return "this lane owes %s — lane %s%s" % (role, lane, aged)


def delivery_path():
    """The delivery sidecar, beside the dispatch ledger.

    Same shape as `attests.jsonl`: an append-only event file next to the
    ledger it annotates, rather than a column on the row. The row is a
    projection and gets recomputed; a delivery is something that HAPPENED and
    must survive being recomputed.
    """
    from . import dispatches
    return os.path.join(os.path.dirname(dispatches.ledger_path()),
                        "deliveries.jsonl")


def delivery_of(obligation_id, events=None):
    """(state, detail) for ONE obligation -> one of the four states above.

    AN UNREADABLE SIDECAR IS `unknown`, NEVER `never-delivered`, and that
    distinction is the whole reason this reads through `checked_events`: a
    missing file is a KNOWN empty ledger, while a permission failure or an
    unsafe path is UNKNOWN. Collapsing those would make one bad read re-deliver
    every obligation on the estate — a notification storm produced by a guard
    that was trying to be safe, which is the fail-open shape wearing a helmet.

    ATTEMPTED-AND-FAILED IS STILL NEVER-DELIVERED. An attempt that errored
    leaves the obligation exactly as undelivered as one never tried; the
    attempt count and the error are DETAIL for whoever is debugging the
    poster, never a softer flavour of delivered. A fail-open delivery leg
    reproduces today's silence while looking healthy.

    `events` may be passed by a caller that has already read the sidecar once,
    so a burn-down over N rows costs ONE read rather than N — the same
    one-acquisition rule the roster readers now follow.
    """
    if events is None:
        from . import eventledger
        events, unavailable = eventledger.checked_events(
            delivery_path(), strict=True, skip_blank=True)
        if unavailable:
            return DELIVERY_UNKNOWN, {"unavailable": unavailable}
    # THE LEDGER IS A LIST OF EVENTS, NOT A MAP FROM ID TO EVENTS, and this
    # line read it as a map. `(events or {}).get(...)` is total on an EMPTY
    # ledger — [] is falsy, so `or {}` substitutes a dict and .get succeeds —
    # and raises AttributeError on the FIRST REAL EVENT, because a non-empty
    # list is truthy and lists have no .get. So the defect was invisible until
    # the moment the feature started working, which is the worst possible
    # trigger and exactly why the arms did not catch it.
    #
    # eventledger.checked_events' own docstring says "complete dict events"; its
    # code says `out = []` then `out.append(row)` on every path. I trusted the
    # docstring when I wrote this caller. Found on a re-read (blocker 1 of 6).
    mine = [e for e in (events or [])
            if str(e.get("id") or "") == str(obligation_id)]
    if not mine:
        return NEVER_DELIVERED, {"attempts": 0}
    attempts = [e for e in mine if e.get("event") == "delivery-attempt"]
    ok = [e for e in attempts if e.get("ok")]
    acked = [e for e in mine if e.get("event") == "delivery-ack"]
    detail = {"attempts": len(attempts),
              "last_delivery_attempt_at": attempts[-1].get("ts")
              if attempts else None,
              "delivered_at": ok[-1].get("ts") if ok else None,
              "delivery_error": None if ok else (attempts[-1].get("error")
                                                 if attempts else None),
              "acknowledged_at": acked[-1].get("ts") if acked else None}
    if acked and ok:
        return ACKNOWLEDGED_STILL_OWED, detail
    if ok:
        return DELIVERED_UNACKED, detail
    return NEVER_DELIVERED, detail


UNDELIVERABLE_UNRESOLVED = "undeliverable-unresolved"


def deliver(ob, who=None):
    """Deliver ONE obligation to the seat that holds it -> (state, detail).

    THIS IS WIRING, NOT A SECOND CHAT. A DM plus the inbox beacon already wakes
    a seat; nothing here invents a channel, and the message body is the
    obligation's own `what` sentence so the wake and the record say the same
    thing. Two spellings of one obligation is how a reader comes to mis-compare
    them.

    THE LOCK IS TAKEN BEFORE THE POST, DELIBERATELY. If the sidecar cannot be
    locked we have not sent anything, so the obligation is honestly UNKNOWN and
    unchanged. Posting first and recording second admits the one outcome with
    no honest report: DELIVERED BUT UNRECORDED, which reads as never-delivered
    to every later reader and makes the sweep re-notify a seat that was already
    told. `eventledger.locked` yields falsy when it did not take the lock — the
    same shape as `_flocked`, and the same reason a mint refuses rather than
    inheriting availability semantics.

    NO ADDRESSEE IS NOT A FAILED SEND. A row owed by `lander` or `integrator`
    names nobody — the row records only its author and its reviewer — so it
    returns UNDELIVERABLE_UNRESOLVED WITHOUT an attempt. Recording a failed
    attempt there would invite a sweep to retry forever against a recipient
    that does not exist, and would bury two real rows in retry noise.
    """
    seat = ob.get("owed_seat")
    if not seat:
        return UNDELIVERABLE_UNRESOLVED, {
            "attempts": 0,
            "unresolved_reason": ob.get("unresolved_reason")}
    from . import dispatches, eventledger, seats_delivery
    path = delivery_path()
    # THE IDENTITY IS REFUSED BEFORE THE LOCK, BEFORE THE SEND, BEFORE THE
    # WRITE. An obligation with no id would send its DM and then
    # append an event carrying `id: ""` — append_unlocked validates SIZE, not
    # shape — and the next strict read rejects that line as "not an object with
    # a non-empty id". That makes the whole SHARED sidecar UNKNOWN, and since
    # this function now refuses to send on an unreadable sidecar, one malformed
    # caller silently suppresses delivery for every obligation on the estate.
    #
    # The blast radius is what makes this a refusal rather than a best-effort:
    # the damage is not to the row with the bad id, it is to every row after
    # it, through a store they all share. A write that can poison a shared
    # ledger has to prove its identity first — the same law the repo_id guard
    # in _root_for_repo answers, on a different identity, found the same night.
    raw_id = ob.get("obligation_id")
    obligation_id = raw_id.strip() if isinstance(raw_id, str) else ""
    if not obligation_id:
        return UNDELIVERABLE_UNRESOLVED, {
            "attempts": 0,
            "unresolved_reason": "the obligation carries no id, so a delivery "
                                 "could not be recorded against it — refusing "
                                 "before the send rather than writing a row "
                                 "that would make the shared sidecar "
                                 "unreadable for every later obligation"}
    with eventledger.locked(path) as held:
        if not held:
            return DELIVERY_UNKNOWN, {
                "posted": False, "recorded": False,
                "error": "the delivery sidecar could not be locked — nothing "
                         "was sent, so this obligation is unchanged"}
        # IDEMPOTENCE BELONGS TO THE OBLIGATION ID, AND IT IS CHECKED HERE
        # RATHER THAN AT THE CALLER (review blockers 2+3). Read the list
        # ONCE, inside the lock, before anything is sent: a seat that has
        # already been told is told again by every sweep that runs, and a
        # second DM about one obligation is indistinguishable to the reader
        # from a second obligation. The check must sit INSIDE the lock or it
        # is a race with a cure's shape — two sweeps could both read
        # never-delivered and both post.
        #
        # AN UNREADABLE SIDECAR REFUSES TO SEND rather than sending blind.
        # Treating "cannot look" as "not yet delivered" is what turns one bad
        # read into a notification storm across the whole estate, which is the
        # failure delivery_of's own docstring exists to prevent; inheriting
        # its state here keeps ONE reading of that distinction instead of two.
        prior_events, unavailable = eventledger.checked_events(
            path, strict=True, skip_blank=True)
        if unavailable:
            return DELIVERY_UNKNOWN, {
                "posted": False, "recorded": False,
                "error": "the delivery sidecar could not be read, so whether "
                         "this seat was already told is UNKNOWN — refusing to "
                         "send rather than re-notifying blind: %s"
                         % (unavailable,)}
        prior_state, prior_detail = delivery_of(obligation_id, prior_events)
        if prior_state in (DELIVERED_UNACKED, ACKNOWLEDGED_STILL_OWED):
            detail = dict(prior_detail)
            detail["resent"] = False
            return prior_state, detail
        # THE RESIDUAL, NAMED RATHER THAN PAPERED OVER: chat and
        # deliveries.jsonl are two stores, so a DM that succeeds while the
        # append below fails still leaves no record that the seat was told.
        # This cure closes the REPEAT, not that window; closing it needs a
        # durable outbox, which is a different lane.
        row, reason = seats_delivery.dm(seat, ob.get("what") or "", who=who)
        ok = bool(row) and reason is None
        stamp = dispatches._read_stamp()
        event = {"v": 1, "event": "delivery-attempt",
                 # THE SAME resolved id the idempotence check read with. Two
                 # spellings of one identity is how a written record stops
                 # matching the read that is supposed to find it.
                 "id": obligation_id,
                 "ts": stamp, "seat": seat, "ok": ok,
                 "error": None if ok else str(
                     reason or "the send returned no row and no reason")}
        if not eventledger.append_unlocked(path, event):
            return DELIVERY_UNKNOWN, {
                "posted": ok, "recorded": False,
                "error": "the attempt could not be recorded; a later reader "
                         "cannot tell whether this seat was told"}
    detail = {"attempts": 1, "last_delivery_attempt_at": stamp,
              "delivered_at": stamp if ok else None,
              "delivery_error": event["error"], "acknowledged_at": None}
    return (DELIVERED_UNACKED if ok else NEVER_DELIVERED), detail


# A chain whose latest FIX verdict nobody has answered. Not a delivery state —
# these are DERIVED from the ledger's shape rather than recorded anywhere, and
# nothing in helm has ever written them down.
UNANSWERED_FIX = "unanswered-fix"
CHAIN_FORKED = "chain-forked"
# A verdict that never declared a polarity. NOT a fix and NOT a clearance —
# a third state, because the two-way reading is what made 26 of these vanish
# from a burn-down whose whole job is completeness.
UNDECLARED_VERDICT = "undeclared-verdict"


def _live(row):
    """A row that still counts as a successor. A CANCELLED child does not:
    `rebind` cancels the old row and opens a replacement, so its parent always
    carries a cancelled child and would otherwise read as answered — or as
    forked — for doing exactly the right thing."""
    return str((row or {}).get("status") or "") != "cancelled"


def unanswered_fixes(rows=None, unavailable=None):
    """(items, forks, unavailable) — the lanes sitting CURED BUT UNREVIEWED.

    THE HOLE THIS FILLS, measured on the live estate the night it was written:
    a FIX verdict tells the author to cure, and nothing anywhere tells them
    that CURING CREATES A NEW OBLIGATION — a review dispatch on the new tip.
    No surface shows a lane in that state, so four authors each did the right
    thing, stopped where the loop stopped speaking to them, and re-gated
    instead, because re-gating is the one action the tooling makes obvious.
    Four lanes were found BY HAND. The ledger held 88 rows across 57 lanes.

    IT IS PURE LEDGER SHAPE — no git, no worktree, no tip comparison. A FIX
    row that NO LIVE ROW SUPERSEDES is unanswered, whether the author has
    cured and not re-dispatched or not cured at all. Those two want the same
    next action from the same person, so splitting them would need a tip read
    per lane and would buy nothing.

    AN UNREADABLE LEDGER IS UNKNOWN, NEVER EMPTY. Returning [] on a failed
    read would publish "nothing is owed" from the one moment we cannot see,
    which is the exact fail-open this whole family of readers exists to stop —
    and here it would read as a clean burn-down.

    A FORK IS ONE DEFECTIVE CHAIN, NOT N OBLIGATIONS. A parent with two live
    children is reported ONCE in `forks`, and its branches are excluded from
    `items`: listing them separately sends two people to fix one lane and
    inflates the burn-down that decision-makers read. Which branch is real is
    a human call — the ledger cannot know — so this names the defect and
    refuses to guess.
    """
    # BOTH ARGUMENTS OR NEITHER, AND THE FIRST DRAFT GOT THIS WRONG IN THE ONE
    # WAY THIS MODULE EXISTS TO PREVENT. It tested `rows is None` alone, so a
    # caller passing (None, "ledger unreadable") — a failure, injected
    # deliberately — had that verdict OVERWRITTEN by a fresh live read, and got
    # 89 real debts back from a call that was asking what happens when the
    # ledger cannot be read. A reader that silently substitutes production data
    # for an injected failure is the fail-open this whole file argues against,
    # reproduced inside the cure for it. Caught by its own arm.
    from . import dispatches
    if rows is None and unavailable is None:
        rows, unavailable = dispatches.snapshot()
    if unavailable:
        return [], [], unavailable
    if rows is None:
        return [], [], "no dispatch rows were supplied and none were read"
    all_rows = list(rows.values() if hasattr(rows, "values") else rows)
    by_id = {str(r.get("id")): r for r in all_rows}
    kids = {}
    for r in all_rows:
        parent = r.get("supersedes")
        if parent:
            kids.setdefault(str(parent), []).append(r)
    # ANSWERED MEANS A LIVE ROW EXISTS SOMEWHERE BELOW, NOT AS A DIRECT CHILD,
    # AND THE DIFFERENCE IS 112 ROWS ON THE LIVE LEDGER — measured, after the
    # first draft asked only about immediate children and billed every one of
    # them. A row whose only child was CANCELLED can still be answered: the
    # chain continues through that child's own successor, which is precisely
    # the shape `rebind` produces every time it runs (cancel the old row, open
    # the replacement beneath it). Only 16 such parents are genuine dead ends.
    # Marking the other 112 as owed would have handed authors a burn-down that
    # is 87% work somebody already did — and a list that is mostly wrong is
    # worse than no list, because the first three people to check it stop
    # believing the rest.
    # ANSWERED IS dispatches.carrier's QUESTION AND IT ALREADY OWNS IT. This
    # walked RAW `supersedes` edges with a hop cap and a status != cancelled
    # test, and a reviewer enumerated exactly what that misses: a withdrawn or
    # abandoned successor is a PASS-THROUGH whose own successors may carry, so
    # treating it as terminal hides real debt; a FOREIGN chain cannot take this
    # row's obligation at all, so counting it answers a debt nobody assumed; a
    # self or mutual cycle silently marked itself answered and vanished; and a
    # 10,000-hop truncation bills a valid longer chain.
    #
    # `carrier` answers all four and its docstring records the measurement that
    # forced each: the frozen superseded_by pointer names a CORPSE when a
    # sibling took the work (two live build rows read as owed by a builder who
    # owed nothing), pass-throughs continue the walk, _same_chain refuses a
    # foreign successor, and every unknown — no successors, unreadable row,
    # cycle — resolves toward VISIBLE rather than silently discharged.
    #
    # So the rule is one line: a row is ANSWERED when something CARRIES it.
    # Writing my own version of this was the same mistake as writing a second
    # staleness layer beside clearspan, one floor down.
    index = dispatches._successor_index(rows)
    cycles = dispatches._cycle_components(index)
    answered = {str(r.get("id")) for r in all_rows
                if dispatches.carrier(r, rows, index, cycles) is not None}
    # A BRANCH IS ALIVE IF IT TOOK THE OBLIGATION OR SOMETHING BELOW IT DID —
    # asked through the same authority, so a rebind's cancelled head reads as
    # the pass-through it is instead of a dead branch.
    branch_alive = lambda c: (not dispatches.moved_nothing(c)
                              or str(c.get("id")) in answered)
    forks = []
    forked_branches = set()
    for parent, all_children in sorted(kids.items()):
        # A FORK IS TWO LIVE BRANCHES OF THE SAME WORK — `supersedes` alone
        # cannot say that. It is an edge any writer can put on any row, and
        # `kids` is built from it raw, so a --new-work row that happens to name
        # this parent counted as a branch. That was blocker 6 of 6,
        # and it reproduces in three rows, with both controls clean:
        #
        #   a + legitimate child b            -> debt answered, forks 0   OK
        #   a + FOREIGN child z               -> debt remains,  forks 0   OK
        #   a + b + z (legitimate AND foreign) -> debt SUPPRESSED, forks 1
        #
        # The third line is the defect and it is worse than "an inflated count":
        # the row drops out of the burn-down entirely, because a forked parent is
        # excluded from billing. ONE STRANGER'S EDGE BOTH INVENTS A FORK AND
        # HIDES A REAL OBLIGATION.
        #
        # `_same_chain` is the same predicate `carrier` already refuses foreign
        # successors with, so this asks the fork question through the authority
        # that answers the debt question — the whole point of the carrier swap.
        # It is a module-private of dispatches, and obligation already calls
        # `dispatches._successor_index` above, so this crosses no new boundary.
        # AN UNREADABLE PARENT IS NOT A PERMISSIVE CASE. The first cure admitted
        # every child when `by_id` had no parent row, which reads as being
        # generous with missing data and is the opposite: two rows naming one
        # NONEXISTENT parent then form a fork of work that does not exist.
        # There is no chain identity to compare against, so there is no branch
        # set (the first escape).
        parent_row = by_id.get(parent)
        if parent_row is None:
            continue
        children = [c for c in dispatches.same_chain_children(
            parent_row, all_children) if branch_alive(c)]
        if len(children) < 2:
            continue
        # EVERY DESCENDANT, NOT THE BRANCH HEAD. Excluding only the heads left
        # a hole a reviewer walked: A forks to B and C, B is cancelled and
        # continues to D, and D ends in a FIX with no child. A reports FORKED,
        # B and C are excluded — and D is neither `answered` nor a head, so it
        # was ALSO billed as an ordinary debt. One defective chain became a
        # fork AND a billable row, contradicting the surface's own claim that
        # branches are excluded. The walk is breadth-first over `kids`, which
        # cannot cycle: a child must exist on the ledger before it can name a
        # parent, so no row is its own ancestor.
        # AND SAME-CHAIN AT EVERY HOP, NOT ONLY THE FIRST. Filtering the
        # immediate children while walking their descendants RAW leaves the
        # defect one level down: a foreign row beneath either branch enters
        # forked_branches and is then excluded from billing, so a stranger's
        # edge still hides an unrelated FIX debt — it just needs one more hop
        # to do it (the second escape). The walk now asks the same
        # question at each edge it traverses.
        stack = [str(c.get("id")) for c in children]
        while stack:
            bid = stack.pop()
            if bid in forked_branches:
                continue
            forked_branches.add(bid)
            branch_row = by_id.get(bid)
            if branch_row is None:
                continue
            stack.extend(str(k.get("id")) for k in
                         dispatches.same_chain_children(
                             branch_row, kids.get(bid, ())))
        forks.append({
            "kind": CHAIN_FORKED,
            "row": parent,
            "lane": (by_id.get(parent) or {}).get("lane"),
            "repo_id": (by_id.get(parent) or {}).get("repo_id"),
            "branches": sorted(str(c.get("id")) for c in children),
            "what": ("this row has %d live successors, so the chain disagrees "
                     "with itself about which work is current — one branch can "
                     "never be discharged. Cancel the branch that is not real"
                     % len(children))})
    items = []
    for r in all_rows:
        rid = str(r.get("id") or "")
        if str(r.get("status") or "") != "verdict":
            continue
        # A ROW THE LAND-REQUEST LADDER ALREADY RETIRED IS NOT DEBT, AND ITS
        # STATUS WILL NEVER SAY SO. `status` names how a row FIRST closed, and
        # the projection's transitions are guarded on `status == "open"`, so a
        # `discharge` or `close` arriving AFTER a FIX verdict cannot move it —
        # correctly, because the verdict really is how it closed. The later
        # retirement is recorded in its own fields instead, and nothing here
        # was reading them.
        #
        # MEASURED on the live ledger the day this was written: `helm owed`
        # reported 130 unanswered FIX rows, of which 106 carried a retirement
        # event AFTER their verdict and 24 did not. Row 9641bb987009
        # (chatnode-posture-r1) is the specimen: seq=2 verdict FIX at
        # 2026-07-25T15:31Z, seq=3 discharge TWELVE HOURS LATER, and it has
        # been rendering as unanswered debt ever since — its projection
        # carrying `discharged: True`, `discharge_ts` and a `discharge_ref`
        # naming the successor that landed.
        #
        # ASKED THROUGH `landreq._retired_by`, NOT RE-DERIVED. That function is
        # the ladder's own answer to "what already retired this row", and it
        # reads exactly the five fields the dispatch projection carries
        # (`discharged`, `withdrawn`, `closed_by_landing`, `abandoned`,
        # `close_reason`) — verified by calling it on dispatch rows: it returns
        # "discharge" and "close --reason withdrawn". Writing a second
        # retirement predicate here would be the same mistake this function's
        # own comment names about `carrier` one screen up: a second authority
        # beside the one that owns the question, free to drift from it.
        #
        # IT IS DELIBERATELY NOT `carrier`'S JOB. `carrier` asks whether a
        # SUCCESSOR took the obligation; retirement is the orthogonal case
        # where the ladder closed the row with no successor to carry it. The
        # 499 rows superseded after a FIX are already answered by `carrier`;
        # these are the 230 closed, discharged, withdrawn or abandoned ones it
        # cannot see by construction.
        from . import landreq
        if landreq._retired_by(r):
            continue
        if rid in answered or rid in forked_branches:
            continue
        polarity = str(r.get("polarity") or "").lower()
        if not polarity:
            # AN ABSENT POLARITY IS UNKNOWN, NOT "NOT A FIX", and the first
            # draft of this loop got that wrong in the direction that hides
            # work. `!= "fix"` reads a MISSING field as a decided non-fix and
            # silently drops the row — measured, 26 verdicts on the live ledger
            # whose polarity was never recorded, invisible to a burn-down whose
            # entire job is to be complete. The dispatch layer already knows
            # they exist (28 of its historical verdicts carry no polarity and
            # it reports them in their own bucket rather than guessing); this
            # surface was the one place they vanished.
            #
            # THEY ARE NOT FOLDED INTO THE FIX COUNT EITHER. Nobody can say
            # whether a cure is owed on a verdict that never declared one, so
            # claiming them as debts would over-report exactly as dropping them
            # under-reports. They get their own kind and their own sentence.
            items.append({
                "kind": UNDECLARED_VERDICT,
                "row": rid,
                "lane": r.get("lane"),
                # THE ROW'S OWN REPOSITORY TRAVELS WITH IT. This ledger is
                # GLOBAL — 14 live rows name four repositories other than this
                # one — so a lane NAME cannot be resolved to a worktree without
                # saying WHOSE (review blocker 5).
                "repo_id": r.get("repo_id"),
                "chain_root": r.get("chain_root") or rid,
                "owed_by": "unknown",
                "owed_seat": r.get("sender") or None,
                "reviewer": r.get("recipient") or None,
                "owed_since": r.get("ts"),
                "reviewed_tip": r.get("reviewed_tip") or r.get("ref"),
                # THE SENTENCE NAMES A DOOR THAT NOW OPENS. It said "cancel
                # the row" while `helm dispatch cancel` refused every one of
                # these rows ("a reviewed dispatch is not cancelled") and the
                # door it redirected to, `helm lr close`, answers "no such
                # land request" for a row carrying no exact tip — so the
                # hourly nag asked for an act neither surface would perform,
                # on the one population that cannot close on its own.
                # `dispatches.advisory_close_error` admits exactly this row
                # now, so the sentence names the command and its exact id.
                "what": ("this verdict declared NO polarity, so it authorized "
                         "nothing and demanded nothing — nobody can say a cure "
                         "is owed. Read it: if it should have been a real "
                         "verdict, re-dispatch with an explicit polarity; if "
                         "it was advice, close it as advice with `helm "
                         "dispatch cancel %s \"<what you read, and why "
                         "nothing is owed>\"`, which records an ADVISORY "
                         "CLOSE naming the polarity-less verdict"
                         % rid[:12])})
            continue
        if polarity != "fix":
            continue
        items.append({
            "kind": UNANSWERED_FIX,
            "row": rid,
            "lane": r.get("lane"),
            "repo_id": r.get("repo_id"),   # see UNDECLARED_VERDICT above
            "chain_root": r.get("chain_root") or rid,
            "owed_by": "author",
            "owed_seat": r.get("sender") or None,
            "reviewer": r.get("recipient") or None,
            "owed_since": r.get("ts"),
            "reviewed_tip": r.get("reviewed_tip") or r.get("ref"),
            # THE THIRD ANSWER TO A FIX, AND THE DELIVERY LEG CARRIES THIS
            # SENTENCE VERBATIM (task/2619). Two branches were named here —
            # cure, or re-dispatch what you already cured — and both assume the
            # author will CURE. There is a third answer and it is a real one:
            # you are right, this should not exist. An author who
            # takes it deletes the artifact, records the refutation, and then
            # has no successor tip and never can have one; `helm dispatch
            # cancel` refuses (correctly — a reviewed row is not cancelled) and
            # `--supersedes` would mint a fresh review over nothing. So THIS
            # sentence, re-delivered hourly by owed-bot, was the instrument
            # pressing them to re-dispatch work they had just been correctly
            # told not to build.
            #
            # The door already exists and already works: measured on a scratch
            # ledger, `close --reason withdrawn` on a FIX-verdicted row is
            # accepted and drops the row out of THIS function's own output in
            # the same pass (`landreq._retired_by` one screen up is what
            # excludes it). It was only ever unnamed.
            "what": ("a FIX verdict on this lane is unanswered. Cure it if you "
                     "have not; if you HAVE, re-dispatch for review on the new "
                     "tip with --supersedes %s — curing alone creates no "
                     "review, and nothing else will tell you that. And if you "
                     "AGREE the artifact should not exist, that is the THIRD "
                     "answer and it is not a cancel: `helm lr close %s "
                     "--reason withdrawn --evidence \"<you accept the verdict, "
                     "and where the refutation is recorded>\"` retires this "
                     "row without minting a review over nothing. A reviewer "
                     "you cannot reach is NEVER the reason to wait: another "
                     "family reads it in a fresh context (the openrouter "
                     "seat is always one), or Fable does through a one-agent "
                     "Workflow — never Sonnet or Haiku — and `helm dispatch verdict "
                     "... --reviewer-model M --reviewer-run RUN` records "
                     "that read on the row as ADVISORY — the row stays owed "
                     "until helm can verify the run"
                     % (rid[:12], rid[:12]))})
    items.sort(key=lambda i: str(i.get("owed_since") or ""))
    return items, forks, None


def unanswered_lanes(items):
    """Distinct LANES from `unanswered_fixes` items, oldest debt first.

    THE BURN-DOWN IS COUNTED IN LANES, NOT ROWS, and the gap between the two
    numbers is itself a defect: a lane whose chain was orphaned appears once
    per orphaned root, so the raw row count triple-counts the worst-maintained
    chains. Measured the night this was written: 88 rows, 57 lanes, and two
    lanes contributing SEVEN rows each. A list that triple-counts is a list
    nobody trusts, and the people reading it are deciding what to work on.
    """
    seen, out = set(), []
    for i in items:
        # WORK IDENTITY, NEVER THE LABEL. A lane is FREE TEXT and helm's own
        # reference says so: two unrelated `--new-work` chains may carry the
        # same string, and chain_root is what identifies the work. Keying on
        # the label collapsed two independent FIX verdicts into one line and
        # one count, so a real debt vanished from the default screen — and my
        # arm constructed exactly that pair and called the collapse correct.
        # Falling back to the row id keeps a legacy row without a chain as its
        # own identity rather than merging it with a stranger.
        key = str(i.get("chain_root") or i.get("row") or "")
        if key in seen:
            continue
        seen.add(key)
        out.append(i)
    return out


def _age_s(stamp):
    """Seconds since an ISO row stamp, or None if it will not parse.

    UNPARSEABLE IS None, NEVER ZERO. A zero sorts a debt of unknown age to the
    freshest end of an oldest-first list, which is the one place nobody looks.
    """
    import calendar
    import time
    try:
        return max(0, int(time.time() - calendar.timegm(
            time.strptime(str(stamp), "%Y-%m-%dT%H:%M:%SZ"))))
    except (ValueError, TypeError):
        return None


def cmd_owed(args):
    """helm owed — the lanes whose FIX verdict nobody has answered.

    THE SURFACE THIS EXISTS FOR. A FIX verdict tells an author to cure, and
    nothing tells them that curing creates a NEW obligation: a review dispatch
    on the new tip. So authors cured, then re-gated, because re-gating is the
    action the tooling makes obvious — and the lanes sat. Four were found by
    hand one night; the ledger held dozens. This is the missing screen, and
    without it the derivation behind it is a function nobody calls.
    """
    # TRAILING JUNK REFUSES BEFORE ANY WORK RUNS. A burn-down that silently
    # ignored `--mine` (which does not exist) would answer the FLEET's debts to
    # somebody who asked for their own and believed the screen. guard_tail also
    # owns --seat's value: my hand-rolled parse accepted `--seat` with nothing
    # after it and filtered on None, which quietly answers "you owe nothing".
    from .cli import guard_tail
    rc = guard_tail("helm owed", args, flags=("--rows", "--json"),
                    valued=("--seat",),
                    usage="usage: helm owed [--seat S] [--rows] [--json]")
    if rc is not None:
        return rc
    items, forks, unavailable = unanswered_fixes()
    if unavailable:
        # FAIL CLOSED AND LOUD. An empty burn-down and an unreadable one look
        # identical on a screen, and this screen exists to be believed.
        print("helm owed: the dispatch ledger could not be read (%s) — what is "
              "owed is UNKNOWN, which is NOT the same as nothing" % unavailable)
        return 1
    seat = None
    if "--seat" in args:
        i = args.index("--seat")
        seat = args[i + 1] if i + 1 < len(args) else None
    if seat:
        items = [i for i in items if i.get("owed_seat") == seat]
    rows = items if "--rows" in args else unanswered_lanes(items)
    # THE TIP IS CONSULTED ONLY FOR THE ROWS WE ARE ABOUT TO PRINT. One git
    # call per listed lane, never per ledger row: the derivation above is pure
    # ledger shape and stays that way, and a burn-down of sixty lanes must not
    # cost sixty git spawns to answer "is anything owed".
    # ONE ROOT PER ROW, DERIVED FROM THE ROW. A single cwd-derived root over
    # a GLOBAL ledger places every foreign row inside THIS checkout — 14
    # live rows name another project, CLIProxyAPI, a proxy fork and a tmp
    # dogfood repo, and each was being judged against helm's worktrees.
    # Cached per repo_id so the one-git-call-per-lane budget above holds.
    _roots = {}
    for r in rows:
        r["owed_age_s"] = _age_s(r.get("owed_since"))
        rid_repo = str(r.get("repo_id") or "")
        if rid_repo not in _roots:
            _roots[rid_repo] = _root_for_repo(rid_repo)
        signals, unreadable = parked_signals(r.get("lane"), _roots[rid_repo])
        r["parked_signals"] = list(signals)
        r["parked_unreadable"] = unreadable
    if "--json" in args:
        import json
        print(json.dumps({"owed": rows, "forks": forks}, ensure_ascii=False))
        return 0
    # GROUPED BY KIND, because they ask different things of the reader and a
    # single count that mixed them would be wrong in both directions.
    fixes = [r for r in rows if r.get("kind") == UNANSWERED_FIX]
    undeclared = [r for r in rows if r.get("kind") == UNDECLARED_VERDICT]
    from .seats_report import _fmt_age
    def _lines(group):
        for r in group:
            age = r.get("owed_age_s")
            # A PARKED TIP GETS A DIFFERENT INSTRUCTION, NOT A DIFFERENT
            # COLOUR. Telling an author to dispatch a review on an interrupted
            # edit the actuator rescued asks a reviewer to spend a turn
            # discovering the tip was never a cure — measured on two lanes that
            # read identically here and wanted opposite actions. The EVIDENCE
            # is named because the two signals can disagree and only the author
            # can tell a park from a rescue of finished work.
            mark = ""
            if r.get("parked_signals"):
                mark = "  ⚠ PARKED (%s) — resume this, do not dispatch it" % (
                    ", ".join(r["parked_signals"]))
            elif r.get("parked_unreadable"):
                mark = "  ? tip unreadable (%s)" % r["parked_unreadable"]
            print("  %-38s %-16s waiting %s%s" % (
                str(r.get("lane"))[:38], str(r.get("owed_seat") or "?")[:16],
                _fmt_age(age) if age is not None else "an unknown time", mark))
    who = " for %s" % seat if seat else ""
    if not fixes:
        # "NO UNANSWERED FIX" IS NOT AN ALL-CLEAR WHILE THE UNKNOWN BUCKET IS
        # NONEMPTY. The old sentence said every cure had been re-dispatched and
        # printed it immediately above verdicts whose polarity nobody recorded
        # and which MAY BE FIX. That is a false all-clear standing next to its
        # own refutation. It says DECLARED now, and withholds the clearance
        # entirely while anything undeclared is outstanding.
        print("helm owed: no unanswered DECLARED-FIX verdict%s%s" % (
            who, "" if undeclared else
            " — every cure that landed a declared verdict has been re-dispatched"))
    else:
        # THE SCREEN NO LONGER ASSERTS *CURED*, because the derivation cannot
        # know it. This module's own docstring says a FIX with no successor is
        # EITHER uncured OR cured-and-not-dispatched, and only tip evidence
        # could tell them apart — yet the headline claimed the second and the
        # trailer told every author to dispatch "the cured tip". An author who
        # has NOT cured was being instructed to send an unchanged tip to
        # another reviewer, which spends a reviewer's turn on work nobody
        # changed.
        print("%d lane(s) with an UNANSWERED FIX verdict%s, oldest first:"
              % (len(fixes), who))
        _lines(fixes)
        # THREE BRANCHES, NOT TWO (task/2619). The screen offered `cure` and
        # `re-dispatch the cure` and stopped, so an author who had accepted the
        # verdict IN FULL — deleted the artifact, recorded the refutation —
        # read a list of two moves neither of which was true about their lane,
        # and the only one the instrument made possible was re-submitting the
        # thing the reviewer had just told them not to build.
        print("\nEach needs the AUTHOR's next move, and which one depends on "
              "what you already did:\n  not cured yet   -> cure it, then "
              "re-dispatch for review on the new tip\n  already cured   -> "
              "re-dispatch NOW with --supersedes on the row it answers\n"
              "  verdict ACCEPTED, artifact should not exist ->\n"
              "                     `helm lr close <id> --reason withdrawn "
              "--evidence \"...\"`\n"
              "Curing alone creates no review — that is the step nothing else "
              "will tell you about. And withdrawing is not cancelling: a "
              "reviewed row cannot be cancelled, and --supersedes would mint a "
              "fresh review over nothing.")
        # THE FOURTH LINE, and it answers the question the other three assume
        # away: "and if nobody can review it?" (task/2948).
        from . import dispatches
        print("  no reviewer reachable -> never a reason to wait. "
              + dispatches.review_fallback_text())
    if undeclared:
        # SEPARATE AND NEVER SILENT. These are verdicts that recorded no
        # polarity, so nobody can say whether a cure is owed — reading the
        # absent field as "not a fix" is what hid 26 of them, and reading it as
        # a fix would invent debts. The honest surface is a third list.
        print("\n%d lane(s) carry a verdict with NO DECLARED POLARITY — neither "
              "owed nor clear, and they cannot close on their own:"
              % len(undeclared))
        _lines(undeclared)
    if forks:
        # SURFACED EVEN WHEN THE DEBT LIST IS EMPTY, because a forked chain is
        # not a debt anybody can pay: one branch can never be discharged, and
        # it stays invisible until somebody asks why a lane never closes.
        print("\n%d chain(s) FORKED — two live successors naming one row, so "
              "one branch can never be discharged:" % len(forks))
        for f in forks[:10]:
            print("  %-38s %s" % (str(f.get("lane"))[:38],
                                  " vs ".join(b[:12] for b in f["branches"])))
        if len(forks) > 10:
            print("  ... and %d more (--json for all)" % (len(forks) - 10))
    return 0


# THE TIP CAN CONTRADICT THE ROW, and only the tip knows. A lane's dispatch row
# says a FIX is unanswered; it cannot say whether the lane's HEAD is an author's
# cure or an interrupted edit the lease actuator rescued mid-flight. Two lanes
# read identically on the burn-down and wanted opposite instructions — one a
# review dispatch, one "resume this" — which a seat discovered by opening both.
#
# TWO SIGNALS, DELIBERATELY OR-ED, AND THE SURFACE SAYS WHICH FIRED. Measured
# across sixty worktrees: EIGHT have an actuator-authored or wip-subjected HEAD,
# and the two predicates DISAGREE on one of them — a tip committed by the
# actuator under an ordinary subject, which no metadata can classify as either a
# park with a nice message or a rescue of finished work. The author can tell in
# a glance; the classifier cannot. So it reports both and names the evidence
# rather than collapsing to a verdict it has not earned.
#
# THE OR IS THE SAFE DIRECTION BECAUSE THE ERRORS ARE NOT SYMMETRIC: over-
# flagging costs the author one glance, under-flagging costs a REVIEWER a whole
# turn discovering the tip was never a cure. Same asymmetry that put undeclared
# verdicts in their own bucket rather than in the fix list or in nothing.
_ACTUATOR_AUTHOR = "helm-work@local"
_PARK_SUBJECT = ("wip:",)


def _repo_root():
    """The MAIN repo root, resolved from wherever this is running.

    `--git-common-dir` is shared by every worktree while `--git-dir` is per-
    worktree, so this answers the same path from a lane room as from trunk —
    which is the whole point, since the caller is usually standing in one lane
    while asking about sixty.
    """
    from . import vcs
    # THE TARGET IS PASSED, NEVER cwd-DERIVED. Selecting with no argument
    # re-derives from the process cwd, which is the defect the vcs migration
    # removed: the backend must be chosen for the thing being OPERATED ON. Here
    # that happens to BE the cwd, and saying so explicitly is the point — an
    # implicit match today becomes a silent mismatch the first time a caller
    # runs this from somewhere else.
    #
    # (Deliberately phrased without the literal empty-parens call: the guard
    # that enforces this is a LINE REGEX over helm/, so a comment quoting the
    # forbidden form trips the very rung it is explaining.)
    here = os.getcwd()
    rc, out, _err = vcs.backend(here).text(here, "rev-parse",
                                           "--git-common-dir")
    if rc != 0 or not out:
        return None
    return os.path.dirname(os.path.abspath(out))



def _root_for_repo(repo_id, repo_root=None):
    """The worktree root for a row's OWN repository, or None if we cannot place it.

    A repo_id is a Git COMMON-DIR, and a common-dir is NOT generally invertible
    to a checkout. MEASURED, because the cure this replaces got this wrong in
    a new way: under `git init --separate-git-dir` the link is ONE-DIRECTIONAL —
    the checkout holds a `.git` FILE naming the gitdir, and the gitdir records
    NOTHING pointing back (`core.worktree` unset, `bare = false`). There is no
    information in such a repo_id that names its checkout, so UNKNOWN is not
    caution here, it is the only true answer.

    Asking git from the gitdir does not help and quietly lies. Measured:
      `git --git-dir=X rev-parse --show-toplevel`  -> the PROCESS CWD (/tmp for
          three different real repositories), because --git-dir sets no worktree
      `git -C <a gitdir> rev-parse --git-common-dir` -> "." — a gitdir VERIFIES
          AGAINST ITSELF, which is how the first cure returned a gitdir as a
          checkout root and passed its own verification doing it

    So the candidate comes from persisted `repo_root` when the row carries it,
    or from the one legacy shape that can recover the answer, a plain
    `<root>/.git`. The candidate must then PROVE IT IS A WORK TREE whose top is
    itself and whose common-dir is the id we were handed. Each check kills a
    case the others let through, and the work-tree check is the one whose
    absence made a gitdir look like a checkout.

    A linked worktree is not a separate identity: every worktree shares the
    same common-dir. A carried linked-worktree path may therefore verify and be
    returned; callers that need a unique checkout must resolve that policy above
    this identity check.
    """
    # BOTH persisted path fields are identities at this boundary. Refuse
    # relative, non-string, and NUL-bearing values rather than coercing them into
    # a process-dependent or invented repository.
    if not isinstance(repo_id, str):
        return None
    repo_id = repo_id.strip()
    if not repo_id or "\0" in repo_id or not os.path.isabs(repo_id):
        return None
    if repo_root is not None and not isinstance(repo_root, str):
        return None
    repo_root = (repo_root or "").strip()
    if "\0" in repo_root:
        return None
    roots = [repo_root]
    if os.path.basename(repo_id) == ".git":
        roots.append(os.path.dirname(repo_id))
    from . import dispatches, vcs
    env = {name: None for name in dispatches._GIT_SELECTION_ENV}
    for root in dict.fromkeys(roots):
        if not root or not os.path.isabs(root) or not os.path.isdir(root):
            continue
        git = vcs.backend(root)

        def _ask(*args):
            rc, out, _err = git.text(root, *args, env=env)
            return (out or "").strip() if rc == 0 else None

        if _ask("rev-parse", "--is-inside-work-tree") != "true":
            continue                       # a gitdir is not a checkout
        top = _ask("rev-parse", "--show-toplevel")
        common = _ask("rev-parse", "--git-common-dir")
        if not top or not common or not os.path.isabs(top):
            continue
        if not os.path.isabs(common):
            common = os.path.join(top, common)
        try:
            if os.path.realpath(common) != os.path.realpath(repo_id):
                continue                   # the checkout disowns this gitdir
            top = os.path.realpath(top)
        except OSError:
            continue
        return top                         # normalize a below-toplevel carrier
    return None

def parked_signals(lane, root=None):
    """(signals, unreadable) — why this lane's TIP looks parked, not cured.

    `signals` is a tuple of evidence names, empty when the tip looks like
    ordinary authored work. `unreadable` is the THIRD ANSWER and is not a
    synonym for "not parked": a lane with no worktree, or whose git will not
    answer, is one we could not classify, and saying so beats both silently
    clearing it and silently accusing it.
    """
    lane = str(lane or "").strip()
    if not lane:
        return (), "no lane name on the row"
    # NO CWD FALLBACK. This used to take the cwd-derived root when the caller
    # passed nothing — and the ledger is GLOBAL, so a row from another project or
    # CLIProxyAPI got classified against HELM's worktrees and answered
    # confidently and wrongly. A row we cannot place is one we must not judge
    # (review blocker 5: absent repo_id is UNKNOWN, never a cwd fallback).
    if not root:
        return (), "the row names no repository, so no worktree can be placed"
    from .work._lanes import lane_path
    path = lane_path(root, lane)
    if not os.path.isdir(path):
        # NO ROOM IS NOT AN UNREADABLE TIP, and the first draft said it was.
        # PARKED means "this room's HEAD is an interrupted edit somebody must
        # resume" — a property of a LIVE room. A lane whose room was retired
        # months ago has no such state to be in, so there is nothing to doubt
        # and nothing to report.
        #
        # DOGFOODING IS WHAT CAUGHT IT: on the live burn-down 72 of 80 listed
        # lanes have no room — most are two weeks old and their rooms were gc'd
        # long ago — so "tip unreadable" fired on ninety percent of the screen
        # and buried the ONE lane that was genuinely parked. A caveat printed
        # on every row is not honesty, it is noise that hides the signal it
        # sits beside. The debt is still listed; only the parked hint is
        # silent, which is exactly the scope of what a missing room can tell us.
        return (), None
    from . import vcs
    # the LANE ROOM is the thing being operated on, so it selects the backend
    rc, out, err = vcs.backend(path).text(path, "log", "-1",
                                          "--format=%ae%x1f%s")
    if rc != 0 or not out:
        return (), (err or "git log answered nothing for this lane")
    email, _sep, subject = out.partition("\x1f")
    signals = []
    if email.strip().lower() == _ACTUATOR_AUTHOR:
        signals.append("actuator-authored")
    if subject.strip().lower().startswith(_PARK_SUBJECT):
        signals.append("wip-subject")
    return tuple(signals), None
