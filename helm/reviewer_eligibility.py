"""WHO CAN REVIEW THIS ROW RIGHT NOW — and for every seat that cannot, THE ONE
CONJUNCT THAT SAYS SO.

THE OWNER'S ASK, VERBATIM: "we definitely need to get better at knowing who is
idle and available, i can glance at panes but your version needs to be just as
easy and reliable." Minutes later he routed a blocked review himself by
glancing at the panes, and he was right and he was faster than the fleet.

A LIST OF WHO CAN REVIEW IS HALF AN ANSWER. The half that closes the loop is
WHY EACH OF THE OTHERS CANNOT, because that is the only thing that tells a
reader which of three different actions to take:

    WAIT            the seat is mid-turn and will free itself
    REPAIR          the seat is deaf, or its proxy is in cooldown — both
                    clear, one by a re-arm and one by a bounce or by time
    ROUTE ELSEWHERE the seat is outside the approval tier, or already wrote
                    part of this chain, and no amount of waiting changes it

A surface that prints only the eligible set collapses all three into silence,
and the measured cost of that silence was four ready lanes with no path to an
APPROVE while NONE of the unusable seats was busy reviewing anything.

# THE CONJUNCTS, AND WHY THE LADDER IS ORDERED THE WAY IT IS

Eligibility was hand-computed three times in one night, each time after a
routing mistake, and each time from a DIFFERENT SURFACE that no verb joined:

    tier    the approval-tier policy in the typed store. A seat outside it is
            a valid reviewer whose findings are INPUT; its APPROVE cannot
            close the row.
    mint    whether an authorizing verdict can be written in this seat's name
            at all — it needs immutable verdict-time runtime-family evidence,
            and a seat can read perfectly healthy on every other axis while
            having none.
    chain   whether this seat is already recorded as an author of this chain.
            A contributor's APPROVE is not an independent read.
    awake   whether helm can reach the seat and the seat can complete a turn:
            a gone pane, a DEAF seat with no live beacon, a dark vendor, a
            starved or hung turn.
    budget  whether the credential pool underneath the seat can still pay.
    idle    whether the seat is between turns right now — the pane fact the
            owner reads by looking.

THE LADDER SHORT-CIRCUITS, AND THE ORDER IS AN ARGUMENT ABOUT REPAIR DISTANCE,
not an implementation convenience. It runs from the refusals nothing local can
change (policy, recorded authorship) through the ones a repair clears (deaf,
walled) to the one that clears by itself (busy). So the conjunct a seat is
named under is the FURTHEST-OUT reason it cannot review, which is the one a
reader has to act on first. Naming a busy seat BUSY while it is also outside
the tier would send someone to wait for an answer that could never arrive.

The order also buys the one expensive read its bound: the pane tail is a
subprocess per seat, and it is asked ONLY of the seats that survived every
cheaper conjunct.

# A SECOND CENSUS INHERITS NONE OF THE FIRST ONE'S SCARS

Every conjunct here is already computed somewhere by code that has paid for
edges this module cannot see from the outside — a live-pane census that
refuses to name a pane on contradictory evidence, an attendance register whose
verdicts expire on the census cadence, a dark-state vocabulary that refuses to
assert an origin the state token does not encode, a contributor index that
propagates damage through real chain links and never through lane labels.

So NOTHING IS MEASURED HERE. Every rung CALLS the authority that owns it:

    dispatches.approval_tier(seat)              the tier membership test,
                                                memoised, project-scoped
    dispatches._approval_identity_families      the mint-capability evidence,
                                                which has had no CLI until now
    landreq.chain_contributors(lr)              the chain-authorship join,
                                                folded once per ledger state
    seat_usability.join(...)                    the reach/turn/vendor verdict
                                                `dispatch send` already refuses
                                                on, with its reasons verbatim
    burnflags.cached_flags                      the per-family burn flag
                                                proxywatch writes
    seat.seat_liveness(repair=False)            the pane tail, read-only, and
                                                asked through the seat FACADE
                                                rather than the impl module it
                                                is re-exported from

What is added is the JOIN and the LADDER — which conjunct wins, and the words
that make the answer actionable. Where a reason sentence exists upstream it is
printed VERBATIM rather than paraphrased, because a paraphrase is a second
opinion wearing the first one's authority.

ONE EXCEPTION, AND IT IS A LABEL RATHER THAN A MEASUREMENT: the awake rung
reads `can_take_work is False` from the joined row — the identical predicate
the dispatch write door refuses on — and then reads the SAME FIELDS THE JOIN'S
OWN LADDER READS, IN THE SAME ORDER, to say WHICH shape of unusable it is
(DEAF, WALL, DARK-PANE, TURN). That names a state the join already decided; it
never decides one.

# AN EMPTY OR UNKNOWN RESULT MUST NEVER RENDER AS A NEGATIVE

"No eligible reviewer" and "I could not read the roster" are different worlds
with different repairs, and an incident where the second one read as the first
is what this verb exists to end. So:

  * A CONJUNCT THAT CANNOT BE MEASURED NEVER EXCLUDES. It records UNKNOWN
    against that seat, names THE INPUT IT COULD NOT READ, and the seat stays a
    candidate carrying the caveat. Refusing on absence would downgrade every
    seat whose evidence merely aged out, and on a box where proxywatch has
    never run it would refuse the whole fleet.
  * A REPORT-LEVEL INPUT THAT WOULD NOT READ — the row, the roster, the
    usability join — is stated in the header and sets the exit code. An empty
    eligible list under an unreadable roster prints as UNREADABLE, never as
    "nobody can review this".
  * EXIT 0 MEANS AT LEAST ONE ELIGIBLE SEAT AND EVERY SHARED INPUT READ. Exit
    1 covers BOTH "measured: nobody" and "could not tell", because a caller
    gating on the return code needs the same stop for "no" and for "I do not
    know" — the rule `lr foldcheck` already states for the same reason.

A refusal names THE INPUT THAT FAILED AND ITS VALUE, never the check that
rejected it. Hours have been lost three times to refusals that named the
wrong input.

# WHAT THE BUDGET RUNG DOES *NOT* DO

A seat over the weekly ceiling is a COST decision, not a wall. The flag goes
MONEY-RED only when EVERY account of that family is measured at or past the
ceiling. Measured caps plus an unread account are ORANGE and UNKNOWN to spend:
the unread account is neither headroom nor evidence for a refusal. Inventing a
wall from that partial reading — the guard-fires-on-absence class one layer up
— is not reachable from here. A family on an ORANGE flag stays ELIGIBLE and
carries its cost ON ITS OWN LINE, sorting below every uncaveated seat: a reader
who picks it is making the cost decision knowingly instead of finding out
later. And a family nothing measures is UNKNOWN, never walled — the rung reads
the fold rather than one family's pool, which is what lets it say anything at
all about the other six.
"""

import json
import sys
import time

# ---------------------------------------------------------------------------
# the vocabulary
# ---------------------------------------------------------------------------

TIER = "tier"
MINT = "mint"
CHAIN = "chain"
AWAKE = "awake"
BUDGET = "budget"
PANE = "pane"
IDLE = "idle"

# THE LADDER ORDER IS THE ARGUMENT — see the module docstring. Repair distance
# descending: policy, recorded authorship, reachability, money, a dead pane,
# the clock.
LADDER = (TIER, MINT, CHAIN, AWAKE, BUDGET, PANE, IDLE)

ELIGIBLE = "ELIGIBLE"
EXCLUDED = "EXCLUDED"

# The shapes of "awake says no". These are LABELS for a verdict seat_usability
# already reached, read off the same row fields its own ladder reads, in the
# same order. Nothing here re-decides anything.
DEAF = "DEAF"
WALL = "WALL"
DARK_PANE = "DARK-PANE"
TURN = "TURN"
UNUSABLE_UNNAMED = "UNUSABLE"

USAGE = ("usage: helm reviewers <row-id> [--all-seats] [--json]\n"
         "  who can review this row right now, and for every seat that "
         "cannot, the ONE conjunct that excludes it")


# ---------------------------------------------------------------------------
# the readers — each wrapped so a failure becomes a NAMED REASON, never a
# default. None of them measures anything itself.
# ---------------------------------------------------------------------------

def _label(seat):
    """THE ONE EMIT DOOR FOR A SEAT NAME. Nothing roster-sourced leaves this
    module except through here.

    The candidate set IS the roster's keys, and a roster key is UNVALIDATED at
    the join seam: a hostile `HELM_CHAT_NAME` can carry a screen-clearing CSI
    or a right-to-left override. Every one of those keys reaches a sink here —
    the seat column of an operator's table, the EXCLUDED lines, the pasteable
    `helm seat resume <name>` repair, and the whole `--json` body — so a key
    that reshaped the terminal would reshape the one surface a reader consults
    precisely when they are deciding who to trust with a review.

    ONE DOOR RATHER THAN ONE CALL PER FORMAT SITE: laundering at each site is
    a bet re-placed at every branch, and the next field added to the renderer
    is the one that forgets. The RAW key still drives every internal match —
    the tier policy, the usability join, the pane read all receive it
    unchanged — because only the EMITTED value is the hazard.

    Nothing is lost by laundering: `_seat_label` leaves a legitimate seat name
    BYTE-IDENTICAL, so the resume command still pastes and the eligible list
    still names real seats.
    """
    from .seats_common import _seat_label
    return _seat_label(seat)

def _read_row(rid, get=None):
    """(land-request row, why-not) for one id, through the ledger's own
    resolver so a bad id refuses in the ledger's own words."""
    from . import landreq
    fn = get or landreq.get
    try:
        lr, err = fn(rid)
    except Exception as exc:                            # noqa: BLE001
        return None, ("the land-request ledger did not read for %r (%s: %s)"
                      % (rid, exc.__class__.__name__, exc))
    return (None, err) if err else (lr, None)


def _read_roster(register=None):
    """({seat: register row}, why-not). None is 'helm could not read the seat
    register', which is NOT an empty fleet.

    THE TRI-STATE READER, NOT `roster()`, AND THE DIFFERENCE IS THIS VERB'S
    WHOLE SUBJECT. `seats.roster()` is fail-open by design — it is a
    `read_json(path, {}) or {}`, so a register that is absent, unreadable,
    malformed or not a mapping comes back as an EMPTY DICT and raises nothing.
    A reader built on it cannot tell an empty fleet from a register it could
    not parse, and this one would have answered the second with "no candidate
    seats" and then, one block down, "nobody can review this row" — the exact
    inversion the module docstring says this verb exists to end, reachable
    without any exception ever being raised for the `except` below to catch.
    `roster_checked` is the producer that already separates the two ("for
    truth consumers that cannot accept fail-open {}"), and this is a truth
    consumer. Its all-or-nothing strictness, which is a trap for a renderer
    that must keep listing seats, is the CORRECT reading here: a register helm
    cannot parse is a partial answer, and partial prints as UNREADABLE.
    """
    from . import seats
    try:
        reg, failed = ((register(), False) if register is not None
                       else seats.roster_checked())
    except Exception as exc:                            # noqa: BLE001
        return None, ("the seat register did not read (%s: %s)"
                      % (exc.__class__.__name__, exc))
    if failed:
        return None, ("the seat register did not read: it is unreadable, "
                      "malformed, or not a mapping of seats (an ABSENT "
                      "register is a proven empty fleet and is not this)")
    if not isinstance(reg, dict):
        return None, "the seat register is not readable as a roster"
    return reg, None


def _read_project(cwd=None, project_for_cwd=None):
    """(project, why-not) — the scope `approval_tier` resolves its policy
    against, asked from the same producer so the two cannot disagree about
    which project's rules this answer was computed under."""
    import os
    fn = project_for_cwd
    if fn is None:
        from .inject._ledger import project_for_cwd as fn
    try:
        return fn(cwd if cwd is not None else os.getcwd()), None
    except Exception as exc:                            # noqa: BLE001
        return None, ("the project could not be resolved from the working "
                      "directory (%s: %s)" % (exc.__class__.__name__, exc))


def _read_usability(names, join=None):
    """({seat: joined row}, why-not) — reach, turn, vendor and open-row count.

    ONE call for the whole candidate set: the join costs four reads whatever
    the fleet size, and asking it per seat would turn a display into a storm.
    `health_seats` is deliberately unset so proxywatch walks its own widened
    census — a seat the watch does not cover is itself news, and narrowing it
    here would hide exactly that.
    """
    from . import seat_usability
    fn = join or seat_usability.join
    try:
        rows = fn(seats=sorted(names))
    except Exception as exc:                            # noqa: BLE001
        return None, ("the seat usability join failed (%s: %s)"
                      % (exc.__class__.__name__, exc))
    if not isinstance(rows, dict):
        return None, "the seat usability join returned no seat rows"
    return rows, None


def _read_contributors(lr, chain_contributors=None):
    """(wrote, why-not) — the seats this chain already records as authors.

    UNREADABLE IS NEVER AN EMPTY SET. An empty set says "nobody wrote this
    chain", which would make every stranger eligible exactly when helm knows
    least — the same inversion `independent_review` refuses one layer down.
    """
    from . import landreq
    fn = chain_contributors or landreq.chain_contributors
    try:
        wrote, _approved, err = fn(lr)
    except Exception as exc:                            # noqa: BLE001
        return None, ("this chain's authorship did not read (%s: %s)"
                      % (exc.__class__.__name__, exc))
    if err:
        return None, str(err)
    return frozenset(wrote or ()), None


def _read_budget(cached_flags=None):
    """(family -> burn flag, why-not) — the fold every surface now reads.

    ONE READER FOR THE FLEET. A conjunct bound to one family's pooled
    snapshot can only ever speak about that family; the flag fold answers for
    all of them off readings the watchdog pass already took.

    NO NETWORK, by the same law the dispatch budget rung states: an
    eligibility display must never pay vendor round-trips, and a vendor outage
    must never be able to stall routing. `cached_flags` never probes and
    yields NOTHING past its own staleness bound rather than an old colour, so
    an absent snapshot is UNKNOWN for the families it would have covered and
    never a clear pool.
    """
    from . import burnflags
    fn = cached_flags or burnflags.cached_flags
    try:
        flags, _age = fn()
    except Exception as exc:                            # noqa: BLE001
        return None, ("the burn-flag snapshot did not read (%s: %s)"
                      % (exc.__class__.__name__, exc))
    if not flags:
        return None, ("no fresh burn-flag snapshot — the watchdog pass writes "
                      "it and `helm burn` reads it")
    return flags, None


def _read_pane(seat, liveness=None):
    """(pane row, why-not) — the tail the owner reads by looking at a pane.

    `repair=False` IS LOAD-BEARING. The registered-pane resolver REPAIRS a
    stale handle by default, which rewrites the spawn register; a read-only
    question that silently rewrote state would be lying about itself.

    THROUGH THE FACADE, WHICH IS WHAT EVERY OTHER CALLER OF THIS FUNCTION
    DOES. `seat_liveness` lives in the `seat_lifecycle` impl module and is
    re-exported by `helm.seat`; importing the impl module direct skips the
    facade whose `_seed_impl_modules` binds the names those modules call.
    `planprompt`, `ready`, `proxywatch` and `idle_dispatch` all ask
    `seat.seat_liveness`, and it is the SAME function object, so this reaches
    the identical producer by the tree's own door.
    """
    from . import seat as _seatmod
    fn = liveness or _seatmod.seat_liveness
    try:
        row = fn(seat, repair=False)
    except Exception as exc:                            # noqa: BLE001
        return None, ("the pane read failed for %s (%s: %s)"
                      % (_label(seat), exc.__class__.__name__, exc))
    if not isinstance(row, dict):
        return None, ("the pane read returned no liveness row for %s"
                      % _label(seat))
    return row, None


# ---------------------------------------------------------------------------
# the rungs — each returns (verdict, detail) where verdict is
# "pass" | "exclude" | "unknown". `_rung_mint` returns a third value because
# it RESOLVES the seat's family on its way to an answer, and the budget rung
# two steps down needs that family — asking a second producer for it is how
# two rungs come to decide about different seats under one name.
# ---------------------------------------------------------------------------

def _rung_tier(seat, approval_tier=None):
    """Is this seat's APPROVE capable of CLOSING the row?

    "none" PASSES AND SAYS SO: where no tier is declared there is nothing to
    be outside of, which is the reading the land gate already takes. A
    TierUnknown carries its own measured KIND and is reported with it — the
    kind is the difference between "re-ask" and "this will never clear".
    """
    from . import dispatches
    fn = approval_tier or dispatches.approval_tier
    try:
        state, why = fn(seat)
    except Exception as exc:                            # noqa: BLE001
        return "unknown", ("the approval-tier policy did not read for %s "
                           "(%s: %s)"
                           % (_label(seat), exc.__class__.__name__, exc))
    if state == "ok":
        return "pass", None
    if state == "outside":
        # THE LEAD SAYS WHAT IT MEANS; THE TAIL IS THE POLICY'S OWN SENTENCE,
        # unedited, because that sentence is where the valid set and the
        # owner's reason live and a paraphrase would be a second opinion
        # wearing the first one's authority. Outside the tier is NOT "not a
        # reviewer" — findings from such a seat are still input, and a reader
        # who needs a READ rather than a CLOSE must not be told to go away.
        return "exclude", ("its APPROVE cannot CLOSE this row (its findings "
                           "are still input) — %s"
                           % (why or "outside the approval tier"))
    if state == "none":
        return "pass", None
    kind = dispatches.tier_unknown_kind(state)
    return "unknown", "%s (%s)" % (why or "approval tier unreadable", kind)


def _rung_mint(seat, identity_families=None):
    """Can an AUTHORIZING verdict be written in this seat's name at all?

    THE EVIDENCE IS IMMUTABLE VERDICT-TIME RUNTIME FAMILY, and a seat can
    answer healthy on every other axis while having none — which is the shape
    that rendered a whole fleet USABLE while not one of them could mint.

    ONLY A MEASURED CONTRADICTION EXCLUDES. Two stored family proofs that
    disagree, or a name that resolves to no unique seat, are facts that do not
    yield on a re-read. Everything else — a proxywatch pass that would not
    read, a seat outside the minted-proxy census — is helm not being able to
    tell, and it rides as UNKNOWN on a seat that stays a candidate.
    """
    from . import dispatches
    fn = identity_families or dispatches._approval_identity_families
    try:
        families, why = fn(seat)
    except Exception as exc:                            # noqa: BLE001
        return "unknown", ("the verdict-time runtime family evidence did not "
                           "read for %s (%s: %s)"
                           % (_label(seat), exc.__class__.__name__, exc)), None
    if not why and families:
        return "pass", None, frozenset(families)
    kind = dispatches._kind_of(why)
    if kind in (dispatches.TIER_DAMAGED, dispatches.TIER_UNNAMED):
        return "exclude", "%s (%s)" % (why, kind), frozenset(families or ())
    return "unknown", "%s (%s)" % (why or "no family evidence",
                                   kind or dispatches.TIER_UNCLASSIFIED), \
        frozenset(families or ())


def _rung_chain(seat, wrote, wrote_why):
    """Is this seat already recorded as an author of this chain?

    RECORDED CONTRIBUTORS ARE SUBMISSION PROVENANCE, not proven Git
    authorship, and the comparison is `landreq._same_seat` — the one shared
    rule, whose fallback errs toward calling two spellings the SAME seat. In
    this direction that is the conservative error: it can only ADD an
    exclusion, never quietly admit a contributor as an outsider.
    """
    if wrote is None:
        return "unknown", wrote_why
    from . import landreq
    for name in sorted(wrote):
        if landreq._same_seat(name, seat):
            return "exclude", ("recorded on this chain as an author (as %r) — "
                               "a contributor's APPROVE is not the independent "
                               "read the row needs" % name)
    return "pass", None


def _awake_shape(row):
    """Which SHAPE of unusable this is — a LABEL for a verdict the join
    already reached, read off the same fields in the same order its own
    ladder reads them. It decides nothing."""
    if row.get("pane") is False:
        return DARK_PANE
    if row.get("reachable") is False:
        return DEAF
    if row.get("upstream_dark"):
        return "%s/%s" % (WALL, row.get("upstream") or "?")
    if row.get("turn_state"):
        return "%s/%s" % (TURN, row.get("turn_state"))
    return UNUSABLE_UNNAMED


def _rung_awake(seat, row, join_why):
    """Can helm reach this seat, and can the seat complete a turn?

    THE PREDICATE IS `can_take_work is False` AND NOTHING ELSE — the identical
    value the dispatch write door refuses on, so an eligibility display and
    the door it feeds can never disagree about one seat. None is helm saying
    it could not tell and it must not block; True covers DEGRADED, which can
    work with a caveat.
    """
    if row is None:
        return "unknown", (join_why or "no joined usability row for this seat")
    can = row.get("can_take_work")
    if can is False:
        return "exclude", "%s — %s" % (_awake_shape(row),
                                       row.get("reason") or "no reason recorded")
    if can is None:
        return "unknown", (row.get("reason")
                           or "the usability join reached no verdict")
    return "pass", None


def _rung_budget(seat, family, flags, budget_why):
    """Can the credential pool underneath this seat still pay?

    THE FAMILY FLAG, NOT ONE FAMILY'S POOL. Scoping this rung to the one
    family with a pooled reader passes every other family silently, and a
    family with a measured wall then reads as a family nobody asked about —
    the defect the owner's own routing case paid for. The flag fold answers
    for every family that has a reader, and says GREY for the rest.

    MONEY-RED IS THE ONLY REFUSAL, and it is byte-for-byte the `capped`
    condition this rung already refused on: every pooled account READ and
    every one at or past the ceiling. ORANGE is a cost fact, not a wall — it
    PASSES with the sentence beside it, exactly as the partly-over pool did.
    GREY is UNKNOWN and never excludes: a family nothing measures is not
    walled, it is unmeasured.

    A FAMILY THE FOLD DOES NOT MINT is not a family with an empty pool; it is
    a name outside the metered vocabulary, and it passes for the reason it
    always did — nothing measures a credential pool behind it.
    """
    from . import burnflags
    if family is None:
        # A FAMILY NOBODY RESOLVED IS NOT A FAMILY WITH NO POOL. Passing here
        # silently would let a capped seat through on an unread input, which
        # is the guard's own failure mode inverted — so it says so instead.
        return "unknown", ("this seat's family is unresolved, so helm cannot "
                           "say which credential pool pays for its work")
    if family not in burnflags.families():
        return "pass", None
    if flags is None:
        return "unknown", budget_why
    flag = flags.get(family)
    if not flag:
        return "unknown", ("the burn-flag fold carries no reading for %s, so "
                           "helm cannot say what its credential pool has left"
                           % family)
    colour = flag.get("colour")
    axes = flag.get("axes") or {}
    until = flag.get("expires_at")
    when = (" until %s" % burnflags._when(until, time.time())) if until else ""
    cause = flag.get("cause") or "no cause recorded"
    if axes.get("money") == burnflags.RED:
        return "exclude", ("%s is walled on MONEY%s — %s" % (family, when,
                                                             cause))
    if colour == burnflags.RED:
        # A NON-MONEY RED IS A REACH OR A DECLARATION, and the conjunct that
        # owns reachability is `awake`. Refusing here would name the wrong
        # repair and hide the one the reader has to make.
        return "pass", ("%s is RED on the %s axis%s — %s"
                        % (family, flag.get("axis") or "?", when, cause))
    if colour == burnflags.ORANGE:
        return "pass", ("%s is ORANGE%s — %s; %s"
                        % (family, when, cause,
                           burnflags.BEHAVIOUR[burnflags.ORANGE]["say"]))
    if colour == burnflags.GREY:
        return "unknown", "%s is NOT MEASURED — %s" % (family, cause)
    return "pass", None


# Pane states that mean a live process is sitting between turns — the fact the
# owner reads by looking. An ALLOWLIST, because a blocklist would be wrong in
# the dangerous direction: an unrecognised state would read as idle and this
# verb would recommend a seat nobody measured.
_PANE_IDLE = ("IDLE", "LIVE")
# The one pane state that is a MEASURED occupation.
_PANE_BUSY = ("RUNNING",)
# The one pane state that is a MEASURED ABSENCE — a recorded pid proved dead,
# or no live process names this seat. Not a failed read: the liveness row has
# separate evidence words for those.
_PANE_DEAD = ("GONE",)


def _pane_rungs(seat, liveness=None):
    """[(conjunct, verdict, detail)] for the last TWO rungs, from ONE read.

    THE PANE TAIL IS THE OWNER'S OWN INSTRUMENT, and it answers two different
    questions off one measurement: is there a live process here at all, and is
    it mid-turn. Splitting them into two conjuncts is what lets a reader see
    the difference between "respawn it" and "wait for it"; reading the pane
    twice to ask them would be two instants for one seat.

    IT IS NOT A SECOND PANE CENSUS. The fleet-wide census inside the usability
    join answers for the WHOLE HOST in one /proc walk and is deliberately
    blind wherever two pieces of evidence contradict each other — when it is
    blind, `awake` reads UNKNOWN and, under the no-refusing-on-absence law,
    admits the seat. This is the SAME authority `helm seat where` reads,
    asked of ONE seat where the cost is bounded, and a GONE from it is the
    positive measurement the blind census could not supply. That is the whole
    value of putting it last: it is the cheapest place to recover a fact the
    scan gave up on.

    THE LAST RUNGS, so the subprocess is paid only for seats nothing cheaper
    excluded. Every state that is neither live, busy nor proven dead is
    UNKNOWN with its `evidence` word carried: an empty read, a stale handle
    and an unrecognised tail are three different truths with three different
    repairs, and flattening them would hand the operator the wrong one.
    """
    row, why = _read_pane(seat, liveness=liveness)
    if row is None:
        return [(PANE, "unknown", why)], None
    state = row.get("state")
    detail = "pane reads %s (evidence %s): %s" % (
        state, row.get("evidence") or "none",
        row.get("detail") or "no detail recorded")
    if state in _PANE_DEAD:
        return [(PANE, "exclude",
                 "pane is GONE — no live process holds this seat (`helm seat "
                 "resume %s` relaunches it): %s"
                 % (_label(seat),
                    row.get("detail") or "no detail recorded"))], state
    if state in _PANE_BUSY:
        return [(PANE, "pass", None),
                (IDLE, "exclude",
                 "pane is %s — this seat is mid-turn. It is not blocked and "
                 "needs no repair: it frees itself" % state)], state
    if state in _PANE_IDLE:
        return [(PANE, "pass", None), (IDLE, "pass", None)], state
    return [(PANE, "unknown", detail)], state


# ---------------------------------------------------------------------------
# the join
# ---------------------------------------------------------------------------

def _in_scope(seat, entry, project):
    """(bool, why-not) — does this seat serve the row's project room?

    HOME ROOM, NOT SEAT NAME. The numbered seats of a family are not a review
    pool; what decides where a seat works is the home room on its register
    row, and a seat with NO home room serves every room by construction. A
    seat homed elsewhere is not a defect and is not hidden — the header counts
    it and `--all-seats` admits it — because a silently narrowed candidate set
    is how a verb comes to print a confident "nobody".
    """
    if not isinstance(entry, dict):
        return False, "no readable seat-register row"
    home = str(entry.get("home_room") or "").strip()
    if not home:
        return True, None
    if project and home != str(project):
        return False, "homed in #%s, not #%s" % (home, project)
    return True, None


# THE PUBLIC DOOR ONTO THE SCOPE TEST. `helm route` asks the same question
# this verb asks — does this seat serve that project room — and re-deriving it
# there would put a second copy of the home-room law in the tree, which is the
# class this module exists to close. One name, two callers.
serves_project = _in_scope
# The seat-name laundering door, published for the same reason: a sibling verb
# that emits roster keys must launder them through the SAME function, not
# through its own import of the layer underneath.
label_seat = _label


def bench(project=None, seams=None):
    """({seat: register row}, why-not) — the seats this project's bench holds.

    THE BENCH IS A ROSTER QUESTION, AND THE ROSTER IS READ HERE. Each project
    runs its own bench of families and routing draws only from it; a caller
    that wanted the bench and read `seats.roster()` itself would inherit the
    fail-open read this module already refuses — an unreadable register coming
    back as an empty fleet, which renders as "no family can take this work".

    UNREADABLE IS NOT EMPTY, and the why-not is returned rather than raised so
    a routing answer can be PARTIAL instead of confidently wrong.
    """
    seams = dict(seams or {})
    reg, why = _read_roster(seams.get("register"))
    if why:
        return {}, why
    out = {}
    for seat in sorted(reg):
        ok, _why = serves_project(seat, reg.get(seat), project)
        if ok:
            out[seat] = reg[seat]
    return out, None


def _caveats(seat_row):
    return len(seat_row["unknown"]) + len(seat_row["notes"])


def _rank(seat_row):
    """Most idle first, then least loaded, then least caveated.

    IDLENESS LEADS because it is the owner's own question. A seat carrying a
    caveat — an unreadable conjunct, a pooled budget already over the ceiling
    for some account — sorts BELOW a clean one at the same idleness, so the
    seat a reader takes off the top is the one helm knows most about.
    """
    holding = seat_row.get("holding")
    return (0 if seat_row.get("pane") == "IDLE" else
            1 if seat_row.get("pane") in _PANE_IDLE else 2,
            1 if _caveats(seat_row) else 0,
            holding if isinstance(holding, int) else 1 << 30,
            seat_row["seat"])


def eligibility(rid, all_seats=False, now=None, seams=None):
    """(report, why-not) — the whole answer for one row, under ONE scope.

    THE SCOPE IS CORRECTNESS BEFORE IT IS SPEED, and it is the reason this
    whole answer describes one instant. `approval_tier` measures a LIVE proxy
    runtime — a /proc fd walk plus a canary — and this verb asks it once per
    candidate seat; the policy store, the dispatch fold and every git argv are
    asked again underneath. Without a scope, the tier answer for the first
    seat and the tier answer for the last are minutes apart, and a reader
    comparing two lines of one table would be comparing two different fleets.
    Inside `projscope.scope()` every repeated question is answered once, and
    the scope DROPS on exit so no answer outlives the pass that asked for it.

    THE REPORT IS THE CONTRACT AND THE RENDER IS A CONSUMER OF IT. A caller
    that has to re-derive what a row MEANS is a caller that will eventually
    derive it differently, which is the class this module exists to close.

        report["row"]        the land request, reduced to what a router reads
        report["project"]    the scope the tier policy was resolved under
        report["seats"]      one typed row per candidate, ranked
        report["eligible"]   the ranked seat names that can review
        report["unreadable"] shared input -> why it would not read. NON-EMPTY
                             MEANS THE ANSWER IS PARTIAL, whatever else says.
    """
    from . import projscope
    from .store import load as store_load
    with store_load.read_scope(), projscope.scope():
        return _eligibility(rid, all_seats=all_seats, now=now, seams=seams)


def _eligibility(rid, all_seats=False, now=None, seams=None):
    """The body the scope guards. Split out so the scope wraps EVERY read in
    one place: a rung added later inherits it without anyone remembering."""
    seams = dict(seams or {})
    now = time.time() if now is None else now
    lr, err = _read_row(rid, get=seams.get("get"))
    if err:
        return None, err

    project, project_why = _read_project(
        seams.get("cwd"), project_for_cwd=seams.get("project_for_cwd"))
    reg, reg_why = _read_roster(seams.get("register"))
    report = {"row": {k: lr.get(k) for k in
                      ("id", "lane", "author", "reviewer", "pinned_tip",
                       "chain_root", "repo_id", "kind", "state")},
              "project": project, "measured_at": now,
              "seats": [], "eligible": [], "measured_eligible": [],
              "out_of_scope": {}, "unreadable": {}}
    if project_why:
        report["unreadable"]["project"] = project_why
    if reg_why:
        # THE ROSTER IS THE CANDIDATE SET. Without it there are no candidates
        # to rank, and an empty ranked list under an unreadable roster would
        # be the exact inversion this verb exists to end — so the answer stops
        # here and says which input failed.
        report["unreadable"]["roster"] = reg_why
        return report, None

    candidates = {}
    for seat in sorted(reg):
        ok, why = _in_scope(seat, reg.get(seat), project)
        if ok or all_seats:
            candidates[seat] = reg[seat]
        else:
            report["out_of_scope"][_label(seat)] = why
    if not candidates:
        return report, None

    rows, join_why = _read_usability(candidates, join=seams.get("join"))
    if join_why:
        report["unreadable"]["usability"] = join_why
    wrote, wrote_why = _read_contributors(
        lr, chain_contributors=seams.get("chain_contributors"))
    if wrote_why:
        report["unreadable"]["chain"] = wrote_why
    budget, budget_why = _read_budget(
        cached_flags=seams.get("cached_flags"))

    for seat in sorted(candidates):
        urow = (rows or {}).get(seat)
        # THE ONLY PLACE A ROSTER KEY BECOMES A REPORT VALUE, and it launders
        # on the way in so every consumer of the report — the render, the
        # `--json` body, the ranked lists derived from these rows — inherits
        # the laundered name. The raw `seat` below still drives every match.
        out = {"seat": _label(seat), "state": ELIGIBLE, "conjunct": None,
               "reason": None, "unknown": [], "notes": [],
               "family": (urow or {}).get("family"),
               "holding": (urow or {}).get("holding"),
               "pane": None}
        # THE MINT RUNG RESOLVES A FAMILY THE USABILITY JOIN CANNOT. proxywatch
        # watches MINTED PROXY seats, so every native claude seat comes back
        # with family None there while the verdict-time evidence resolver
        # answers it exactly. Reading that answer is a JOIN of two results this
        # function already holds, not a third resolution — and the budget rung
        # below needs the family to know whether it applies at all.
        tier_state, tier_why = _rung_tier(seat, seams.get("approval_tier"))
        mint_state, mint_why, families = _rung_mint(
            seat, seams.get("identity_families"))
        if out["family"] is None and families and len(families) == 1:
            out["family"] = next(iter(families))
        rungs = [
            (TIER, tier_state, tier_why),
            (MINT, mint_state, mint_why),
            (CHAIN,) + _rung_chain(seat, wrote, wrote_why),
            (AWAKE,) + _rung_awake(seat, urow, join_why),
            (BUDGET,) + _rung_budget(seat, out["family"], budget, budget_why),
        ]
        for name, state, detail in rungs:
            if state == "exclude":
                out["state"], out["conjunct"], out["reason"] = \
                    EXCLUDED, name, detail
                break
            if state == "unknown":
                out["unknown"].append((name, detail))
            elif detail:
                out["notes"].append((name, detail))
        else:
            pane_rungs, pane = _pane_rungs(seat, seams.get("liveness"))
            out["pane"] = pane
            for name, state, detail in pane_rungs:
                if state == "exclude":
                    out["state"], out["conjunct"], out["reason"] = \
                        EXCLUDED, name, detail
                    break
                if state == "unknown":
                    out["unknown"].append((name, detail))
        report["seats"].append(out)

    report["seats"].sort(key=lambda r: (r["state"] != ELIGIBLE,
                                        _rank(r) if r["state"] == ELIGIBLE
                                        else (LADDER.index(r["conjunct"]),
                                              r["seat"])))
    report["eligible"] = [r["seat"] for r in report["seats"]
                          if r["state"] == ELIGIBLE]
    # THE SEATS HELM ACTUALLY MEASURED ALL THE WAY THROUGH. A seat that is
    # eligible only because a conjunct would not read is still eligible — the
    # no-refusing-on-absence law says so — but it is NOT the same answer, and a
    # surface that mixed the two would hand a reader an unmeasured seat at the
    # top of a list they trusted.
    report["measured_eligible"] = [r["seat"] for r in report["seats"]
                                   if r["state"] == ELIGIBLE and not r["unknown"]]
    return report, None


# ---------------------------------------------------------------------------
# the render
# ---------------------------------------------------------------------------

def _row_headline(report):
    row = report["row"]
    tip = str(row.get("pinned_tip") or "")
    return "%s — %s lane %s [%s], author @%s, reviewer @%s, tip %s" % (
        row.get("id") or "?", row.get("kind") or "?", row.get("lane") or "?",
        row.get("state") or "UNKNOWN",
        row.get("author") or "UNKNOWN", row.get("reviewer") or "UNKNOWN",
        tip[:12] if tip else "UNKNOWN")


def _seat_line(r):
    return "    %-22s pane=%-7s holding=%-4s family=%s" % (
        r["seat"], r["pane"] or "UNKNOWN",
        "UNKNOWN" if r["holding"] is None else r["holding"],
        r["family"] or "UNKNOWN")


def render(report):
    """The lines. UNKNOWN PRINTS; it is never withheld and never blank.

    A blank where a conjunct could not be read would say "fine", and a scan
    line that omits a field reads as health — the founding defect of every
    surface this one joins.

    THE ELIGIBLE SET PRINTS IN TWO BLOCKS, and keeping them apart is the whole
    reliability claim. A seat helm measured on every conjunct and a seat that
    is eligible only because something would not read are both eligible and
    they are not the same answer; one list holding both would put an
    unmeasured seat at the top of a list a reader trusts.
    """
    out = ["helm reviewers %s" % _row_headline(report)]
    if report["unreadable"]:
        out.append("  PARTIAL — helm could not read %d of its inputs, so what "
                   "follows is not a complete answer:" % len(report["unreadable"]))
        for field, why in sorted(report["unreadable"].items()):
            out.append("    %-10s UNREADABLE — %s" % (field, why))
    scope = report.get("project") or "UNKNOWN"
    out.append("  scope: %d candidate seat(s) homed in #%s or in every room; "
               "%d homed elsewhere (--all-seats widens)"
               % (len(report["seats"]), scope, len(report["out_of_scope"])))

    eligible = [r for r in report["seats"] if r["state"] == ELIGIBLE]
    measured = [r for r in eligible if not r["unknown"]]
    partial = [r for r in eligible if r["unknown"]]
    if measured:
        out.append("  ELIGIBLE (%d), most idle first — every conjunct MEASURED"
                   % len(measured))
        for r in measured:
            out.append(_seat_line(r))
            for name, why in r["notes"]:
                out.append("      %-7s NOTE — %s" % (name, why))
    elif report["unreadable"]:
        # THE SENTENCE THIS VERB EXISTS FOR. An unreadable input and a measured
        # empty set are different worlds, and printing the second one for the
        # first is what cost the fleet a night.
        out.append("  NO ELIGIBLE SEAT COULD BE COMPUTED — see the UNREADABLE "
                   "inputs above. This is NOT a measured 'nobody can review "
                   "this row'.")
    else:
        busy = [r for r in report["seats"]
                if r["state"] == EXCLUDED and r["conjunct"] == IDLE]
        excluded_n = len([r for r in report["seats"] if r["state"] == EXCLUDED])
        if busy and len(busy) == excluded_n and not partial:
            # BUSY IS NOT A REASON TO STOP EITHER (task/2948). A mid-turn
            # seat frees itself, and that is worth saying; telling the reader
            # to wait on it is what the owner's rule forbids, so the line
            # names the fact and the ladder below names the next move.
            out.append("  NOBODY IS FREE RIGHT NOW, and nobody is BLOCKED: all "
                       "%d candidate(s) are mid-turn and free themselves; do "
                       "not hold the row for them." % len(busy))
        else:
            out.append("  NO FULLY-MEASURED ELIGIBLE SEAT. Every candidate is "
                       "named below with either the conjunct that excludes it "
                       "or the input helm could not read.")
    if not measured:
        # AN EMPTY SEAT LIST IS NOT AN EMPTY REVIEWER LIST (task/2948), on
        # EVERY branch that reaches here — unreadable, busy or excluded. A seat
        # is one way to get another family's or Fable's fresh read, and a
        # surface that stopped at the seats told the reader the row was stuck.
        from . import dispatches
        row = report["row"]
        out.append("  " + dispatches.review_fallback_text(
            row, tip=row.get("pinned_tip")))
    if partial:
        out.append("  ELIGIBLE BUT NOT FULLY MEASURED (%d) — nothing refused "
                   "these seats; helm could not check every conjunct, and an "
                   "unread conjunct is never a refusal" % len(partial))
        for r in partial:
            out.append(_seat_line(r))
            for name, why in r["notes"]:
                out.append("      %-7s NOTE — %s" % (name, why))
            for name, why in r["unknown"]:
                out.append("      %-7s UNKNOWN — %s" % (name, why))

    excluded = [r for r in report["seats"] if r["state"] == EXCLUDED]
    if excluded:
        out.append("  EXCLUDED (%d) — the ONE conjunct that excludes each"
                   % len(excluded))
        for r in excluded:
            out.append("    %-7s %-22s %s" % (r["conjunct"], r["seat"],
                                              r["reason"] or "no reason recorded"))
    out.append(LEGEND)
    return out


LEGEND = (
    "  conjuncts, in ladder order — the seat is named under the FURTHEST-OUT "
    "reason it cannot review: tier=its APPROVE cannot close the row (route "
    "elsewhere) · mint=no immutable verdict-time runtime family evidence, so "
    "no authorizing verdict can be written in its name · chain=already "
    "recorded as an author of this chain, so its APPROVE is not an "
    "independent read (route elsewhere) · awake=helm cannot reach it or it "
    "cannot complete a turn — DEAF is no live beacon (`helm chat wait --seat "
    "S --follow` re-arms it), WALL/<state> is its vendor path not answering "
    "and a PROXY-COOLDOWN or PROXY-LOCAL-403 there is HELM'S OWN proxy and "
    "says nothing about the vendor (repair) · budget=its family is walled on "
    "MONEY, every readable account at or past the ceiling (wait for the "
    "reset) — an "
    "ORANGE family PASSES and prints its cost beside it, and a family "
    "nothing measures is UNKNOWN and never refused · pane=no live process "
    "holds the seat (respawn) · idle=mid-turn right now (frees itself; route the read elsewhere meanwhile) · UNKNOWN is "
    "never a refusal: the seat stays a candidate, prints in its own block and "
    "the input helm could not read is named beside it")


# ---------------------------------------------------------------------------
# the verb
# ---------------------------------------------------------------------------

def cmd_reviewers(args):
    args = list(args or [])
    from .cli import guard_tail
    if not args or args[0] in ("-h", "--help"):
        print(USAGE, file=sys.stderr)
        return 2
    rid, rest = args[0], args[1:]
    rc = guard_tail("helm reviewers", rest, flags=("--all-seats", "--json"),
                    usage=USAGE)
    if rc is not None:
        return rc
    report, err = eligibility(rid, all_seats="--all-seats" in rest)
    if err:
        print("helm reviewers: " + err, file=sys.stderr)
        return 2
    if "--json" in rest:
        print(json.dumps(report, ensure_ascii=False, indent=1, default=str))
    else:
        print("\n".join(render(report)))
    # ONE STOP FOR "NO" AND FOR "I COULD NOT TELL" — a caller gating on this
    # code needs the same refusal for both, because not-measured is not
    # consent. The TEXT above is what distinguishes them, and it always does.
    # The bar is a FULLY MEASURED eligible seat: a seat admitted only because
    # a conjunct would not read is a candidate for a human to look at, never
    # an exit code a script may route on.
    return 0 if (report["measured_eligible"]
                 and not report["unreadable"]) else 1
