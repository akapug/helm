"""DERIVE a land-request row's state from artifacts — never from a remembered flag.

THE DEFECT THIS REPLACES. `landreq._lr` computes state by folding the dispatch
EVENT LEDGER: a row reads AWAITING_BUILD because somebody once ran `dispatch
mark-delivered` and nobody has run a closing verb since. The ledger records what
an agent REMEMBERED to declare. Measured 2026-08-05 on the live estate: 17 of 31
AWAITING_BUILD rows had their work already folded to trunk, and the owner's own
row (`a1f5aee91903`, lane fleet-notes-freshness) sat AWAITING_BUILD while trunk
commit `fa0915b22104` named it by id. Git is only consulted for rows a verb has
ALREADY closed (`landreq.py:1985-1989`), so the world moving is structurally
invisible to an open row.

THIS MODULE ASKS THE ARTIFACTS INSTEAD. Branches, commits, trees, patch-ids,
gate receipts and verdict polarity are written by machines and cannot be
forgotten. `derive()` is PURE — it performs no I/O, spawns nothing, writes
nothing, and returns the same answer for the same `World`. Every read lives in
`helm.rowworld`, which builds the immutable `World` once per population. That
split is the performance story as much as the purity one: the artifacts are
SET-SHAPED, so one bulk read answers a thousand rows.

WHAT AN ABSENT ARTIFACT MEANS. Nothing. `UNKNOWN` is a real state and is
returned whenever the evidence is missing rather than negative — a reaped lane
branch is indistinguishable from one that never existed, and calling that
UNSTARTED would invent a claim about a row that may well have landed. Every
state below is backed by POSITIVE evidence, carried in `Derivation.evidence` so
a reader can check the derivation instead of trusting it.
"""

import calendar
import re
import time

from .verdicts import WORK_POLARITIES

# ---------------------------------------------------------------- vocabulary
#
# TWO AXES, NOT ONE. The ledger's own terminal records — a cancel event, a
# verdict's polarity, a structured close — are MACHINE-WRITTEN artifacts with
# exactly the same standing as a commit, and they take PRECEDENCE over any
# progress inferred from git (review blocker 2, gate:9b5029474fb63d13:
# measured live, 128 terminal FIX/SUPERSEDE rows and 96 cancelled rows derived
# LANDED because the ancestry rung outranked the row's own recorded ending).
# What this module refuses to trust is the FOLD'S INTERPRETATION of missing
# verbs ("open because nobody closed it"), never the recorded events.

UNSTARTED = "UNSTARTED"     # branch exists, nothing past the merge-base
BUILDING = "BUILDING"       # commits exist, no receipt binds the head's tree
BUILT = "BUILT"             # closed delivered-report: the artifact WAS delivery
GATED = "GATED"             # a receipt binds the head commit's EXACT tree
APPROVED = "APPROVED"       # GATED + a cross-family approve on that exact tip
REVIEWING = "REVIEWING"     # an open review row: the obligation is a verdict
REVIEWED = "REVIEWED"       # the verdict is recorded; it authorized no landing
LANDED = "LANDED"           # trunk carries the work (ancestry, content, or id)
SUPERSEDED = "SUPERSEDED"   # a successor row carries the obligation
CANCELLED = "CANCELLED"     # the ledger withdrew the row: nobody owes anything
UNKNOWN = "UNKNOWN"         # the artifacts do not answer — never a claim

STATES = (UNSTARTED, BUILDING, BUILT, GATED, APPROVED, REVIEWING, REVIEWED,
          LANDED, SUPERSEDED, CANCELLED, UNKNOWN)

# Ordered most-resolved first. A row is reported at the furthest stage its
# artifacts positively support, and `derive` walks this order exactly once.
RESOLVED_ORDER = (LANDED, CANCELLED, BUILT, REVIEWED, SUPERSEDED, APPROVED,
                  GATED, REVIEWING, BUILDING, UNSTARTED, UNKNOWN)

_HEX = re.compile(r"[0-9a-f]{8,64}\Z")


class Evidence(tuple):
    """One artifact-backed reason, `(kind, detail)`. A tuple so it compares and
    hashes by value — tests assert on evidence, not on prose."""

    __slots__ = ()

    def __new__(cls, kind, detail):
        return tuple.__new__(cls, (str(kind), detail))

    @property
    def kind(self):
        return self[0]

    @property
    def detail(self):
        return self[1]

    def __repr__(self):
        return "Evidence(%r, %r)" % (self[0], self[1])


class Derivation(tuple):
    """`(state, evidence, unknown)` — the answer plus why it is the answer.

    `evidence` is a tuple of `Evidence`, never empty except for UNKNOWN.
    `unknown` is None, or the sentence naming the artifact that was missing.
    Both halves are always present: a caller that renders the state without the
    evidence is trusting this module, which is the habit that produced the
    stale board in the first place."""

    __slots__ = ()

    def __new__(cls, state, evidence=(), unknown=None):
        return tuple.__new__(cls, (state, tuple(evidence), unknown))

    @property
    def state(self):
        return self[0]

    @property
    def evidence(self):
        return self[1]

    @property
    def unknown(self):
        return self[2]

    def because(self, kind):
        """The detail of the first evidence entry of `kind`, or None."""
        return next((e.detail for e in self[1] if e.kind == kind), None)

    def __repr__(self):
        return "Derivation(%s, %r, %r)" % (self[0], list(self[1]), self[2])


def _hex(value):
    """A sha/token or None. Refuses anything that is not lowercase hex — an id
    read out of a ledger is untrusted text until it looks like one."""
    text = str(value or "").strip().lower()
    return text if _HEX.fullmatch(text) else None


def _lane_ref(row):
    lane = str((row or {}).get("lane") or "").strip()
    return ("lane/" + lane) if lane else None


def _retipped_tip(row):
    """The tip a VERIFIED retip bound to this build row, or None.

    A build row's `tip` is normally the base it was cut from, which is useless
    as evidence because a base is an ancestor of trunk by construction. A
    `retip` is the exception the ledger records honestly: the builder rebinds
    the row to the object it actually produced, and stamps the entry
    `identity: "verified"`. Only the newest entry counts, and only when it still
    agrees with the row's current tip — a retip that a later one replaced is
    history, not a claim about now. Unverified entries are ignored outright."""
    retips = (row or {}).get("retips")
    if not isinstance(retips, (list, tuple)) or not retips:
        return None
    latest = retips[-1]
    if not isinstance(latest, dict) or latest.get("identity") != "verified":
        return None
    tip = _hex(latest.get("tip"))
    return tip if tip and tip == _hex(row.get("tip")) else None


# ----------------------------------------------------------- lifecycle
#
# THE LEDGER'S OWN TERMINALS COME FIRST. A cancel event, a structured close
# and a recorded verdict are appended by machines under the same replay
# validation as everything else in `dispatches._fold`; inferring "LANDED"
# over the top of one is exactly the class of confident wrongness this module
# exists to end. What stays UNTRUSTED is the fold's reading of ABSENT verbs.

# THE CONSERVATIVE TERMINAL EACH STRUCTURED CLOSE RECORDS (task/744, T1).
# The keys are `dispatches._CLOSE_STATE_FIELDS`' grammar — every reason
# `dispatches._fold` will admit, each one gated there by its own full proof
# fields. What lives HERE and nowhere else is the MEANING: which of this
# module's states a recorded closure conservatively implies. That is why the
# table is written out rather than imported — the authority holds field lists,
# not states, so there is nothing to import. The BINDING is a test
# (`test_every_recorded_close_reason_has_a_terminal`) that enumerates the
# authority's keys, so a ninth reason added to the grammar turns the suite red
# instead of silently falling through to artifact inference.
#
# CONSERVATIVE means: the WEAKEST claim the closure's own proof supports.
# `subsumed` and `superseded` both say the work moved to another row, never
# that it landed — where it went is that row's question. `withdrawn` and
# `stranded` both say nobody owes anything further, which is CANCELLED.
# `resolved` and `discharged` retire a debt without authorizing a landing,
# which is REVIEWED.
_CLOSE_TERMINAL = {
    "landed": LANDED,
    "delivered-report": BUILT,
    # `discharged` AND `resolved` ARE SUCCESSOR-SHAPED, NOT VERDICT-SHAPED
    # (task/744 round 2, item [170]). Both read REVIEWED here, which claims
    # a verdict was recorded — and for `discharged` a verdict CANNOT exist:
    # `dispatches._fold` admits that reason only for an OPEN BUILD with NO
    # verdict, and `landreq._close_ladder_discharged` proves it by a
    # SUCCESSOR's landed, gate-verified APPROVE. `resolved` is the same shape
    # one step over — a confirming round retiring a contrary. In both the
    # obligation ended because somebody ELSE's row carried it, which is what
    # SUPERSEDED says. LANDED would be the tempting word and it is wrong:
    # the landing proven is the SUCCESSOR's, and LANDED is a claim about
    # THIS row's work at HEAD.
    "discharged": SUPERSEDED,
    "resolved": SUPERSEDED,
    # `carried` IS THE ONE CLOSURE WHOSE PROOF IS THIS MODULE'S OWN
    # (task/756). Every other reason records something the ledger knows and
    # leaves carriage to be inferred; this one is admitted ONLY on an
    # affirmative `rowworld` carriage measurement, which is strictly stronger
    # than what `landed` demands. LANDED is therefore not a generous reading
    # of it — it is the literal content of the proof that let it be written.
    # Note the mandatory gate still re-measures: a carried row whose work is
    # LATER taken back reads UNKNOWN like any other, because the closure is a
    # record of a past measurement and `_enforce` asks about now.
    "carried": LANDED,
    # `chain-proof` IS SUPERSEDED, AND LANDED WOULD BE THE EXACT ERROR THE
    # REASON EXISTS TO PREVENT. It sits with discharged and resolved, not with
    # carried: the obligation ended because a DESCENDANT carried it, and the
    # landing proven is that descendant's. This row's own carriage is not
    # merely unproven — the question died with the pruned object, which is the
    # whole precondition. LANDED is a claim about THIS row's work at HEAD, so
    # writing it here would let the terminal state say what the door refuses to
    # say: the reason is named chain-proof and never carried, and its arms
    # assert that rendering as hard as they assert the close.
    "chain-proof": SUPERSEDED,
    "superseded": SUPERSEDED,
    "subsumed": SUPERSEDED,
    "withdrawn": CANCELLED,
    "stranded": CANCELLED,
    # `expired` IS CANCELLED BY THIS TABLE'S OWN RULE FOR ITS NEIGHBOURS: it
    # says nobody owes anything further, and it proves no landing. The other
    # three words would each claim something the door refuses to claim.
    # SUPERSEDED would assert a successor carried the work and there is none —
    # the defining fact of this population is that nothing carried it.
    # REVIEWED would assert a debt-retiring verdict, and the reason is
    # admitted ONLY for a verdict that authorizes nothing. LANDED is a claim
    # about this row's work at HEAD, and the door refuses any row whose tip
    # reached trunk. What is left is the honest one: the review happened, it
    # authorized nothing, the work never arrived, and the obligation is over.
    "expired": CANCELLED,
    # `endorsement-moot` IS SUPERSEDED, AND THIS TABLE'S OWN RULE FOR ITS
    # NEIGHBOURS PICKS THE WORD. LANDED is the tempting one — the tip really
    # IS on trunk and the door measured it — and it is wrong for the sentence
    # this table states twice above: "LANDED is a claim about THIS row's work
    # at HEAD" under THIS row's authority, and the whole premise of the door
    # is that the authority was somebody else's. Writing LANDED here would let
    # the rendered state say what the reason refuses to say, which is the exact
    # error `chain-proof` sits in this table to avoid; every burn-down, board
    # and counter reading terminals would then read an endorsement as an
    # approved landing.
    #
    # SUPERSEDED is what is left and it is honest: the obligation ended because
    # the work reached trunk under an authority that is not this row's, which
    # is the same sentence `discharged`, `resolved` and `chain-proof` carry.
    # CANCELLED — `expired`'s word — would be the opposite error: it asserts
    # the work never arrived, and here it demonstrably did.
    "endorsement-moot": SUPERSEDED,
}

# TERMINALITY IS NOT ALWAYS A STATUS, and `_lifecycle` only read statuses and
# close reasons (task/744, L2). `dispatches` records an
# abandonment as `status="verdict"` plus an `abandoned=True` FLAG — its own
# comment says two of the three status spellings it once used were
# UNREACHABLE for exactly this reason — so an abandoned row reached the
# artifact arms and derived a stage for an obligation nobody holds.
#
# `withdrawn=True` is deliberately NOT here. The flag ANNOTATES a verdict
# without rewriting it, so the row stays at its verdict's word; only
# `close_reason="withdrawn"`, which is a structured close with its own proof
# fields, ends a row. Two spellings, two meanings, and collapsing them would
# cancel rows whose reviewer merely retracted an objection.
_TERMINAL_FLAGS = {"abandoned": CANCELLED}

# What `_lifecycle`'s third element says about the LEDGER, which is a
# different question from what its first element says about the ROW.
L_OPEN = "open"                  # no terminal recorded; the artifacts may speak
L_RECORDED = "recorded"          # a terminal was recorded and is returned
L_UNREADABLE = "unrecognized"    # a terminal was recorded and CANNOT be read


def _lifecycle(row):
    """The authoritative terminal the ledger RECORDED.
    -> (state-or-None, evidence-or-None, one of L_OPEN/L_RECORDED/L_UNREADABLE)

    Only states the replay validated get a word here: `status == "cancelled"`
    is a cancel event, and each `close_reason` is admitted by
    `dispatches._fold` only with its full proof fields.

    THE THIRD ELEMENT IS THE CURE (task/744, T1). The old shape returned
    `(None, None)` for BOTH "this row was never closed" and "this row was
    closed for a reason I cannot read", so `derive` could not tell them apart
    and ran the artifact arms on both. On the second one that is a false
    certainty by construction: the ledger asserted a terminal, we failed to
    name it, and git then answered a question the ledger had already closed —
    a row closed `superseded` whose successor's work sits on trunk read
    LANDED, which is the ledger's own record overruled by an inference about
    somebody else's commit. Now `L_UNREADABLE` reaches `derive` as its own
    fact and ends the row at UNKNOWN, per store law: a skipped case yields
    UNKNOWN, never a confident terminal."""
    if row.get("status") == "cancelled":
        return CANCELLED, Evidence(
            "status", ("cancelled", row.get("cancel_reason"))), L_RECORDED
    # `closed_by_landing` is a monotonic historical receipt written without a
    # close_reason, so it is tested before the table rather than through it.
    if row.get("closed_by_landing"):
        return LANDED, Evidence(
            "closure", ("landed", row.get("landing_trunk_sha")
                        or row.get("close_evidence"))), L_RECORDED
    for flag, terminal in _TERMINAL_FLAGS.items():
        if row.get(flag):
            return terminal, Evidence("flag", (flag, row.get("status"))), \
                L_RECORDED
    reason = row.get("close_reason")
    if reason in _CLOSE_TERMINAL:
        return _CLOSE_TERMINAL[reason], Evidence(
            "closure", (reason, row.get("landing_trunk_sha")
                        if reason == "landed" else row.get("close_evidence"))
        ), L_RECORDED
    # A CLOSURE WE CANNOT NAME IS STILL A CLOSURE. Either spelling of it — an
    # unrecognized reason, or the `closed` status with no reason at all — says
    # the ledger ended this row, and neither says how.
    if reason or row.get("status") == "closed":
        return None, Evidence("closure-unrecognized", (reason,)), L_UNREADABLE
    return None, None, L_OPEN


def _verdict(row):
    """-> (a verdict ended this row, its polarity or None for UNDECLARED).

    Replay only ever stamps `polarity` beside an accepted verdict event, and
    `_replay_polarity` already reduced unknown spellings to None. A `closed`
    row that CARRIES a polarity closed after its verdict and keeps it; a
    `closed` row without one never had a verdict, and calling it reviewed
    would invent a review nobody recorded."""
    if row.get("status") == "verdict":
        return True, row.get("polarity")
    if row.get("status") == "closed" and row.get("polarity"):
        return True, row.get("polarity")
    return False, None


# ------------------------------------------------------------- subject tip

def subject_tip(row, world):
    """The commit this row's WORK IS, or None. -> (sha, evidence, why-not)

    THE TRAP THAT MAKES EVERY BUILD ROW READ LANDED. A build row's `tip` is the
    BASE it was cut from, not the artifact it produced (`landreq.py:2236-2238`
    records the base and leaves `review_sha` None). The base is by construction
    an ancestor of trunk, so a derivation that asked "is row['tip'] on trunk?"
    would answer YES for every build row ever dispatched. Verified 2026-08-05:
    row `b2bc5e4a411a` (lane bigfile-split-seat-py) has tip
    `d225f4c3b798`, which IS an ancestor of origin/main, while its lane branch
    sits 19 commits ahead and unlanded. So a build row's subject is its LANE
    BRANCH HEAD and never its recorded tip.

    A review row is the opposite: it names the exact object it reviewed, and
    that object is the subject."""
    if not isinstance(row, dict):
        return None, None, "row is not a record"
    if row.get("kind") == "build":
        ref = _lane_ref(row)
        head = _hex((world.branches or {}).get(ref)) if ref else None
        if head:
            return head, Evidence("branch", (ref, head)), None
        retipped = _retipped_tip(row)
        if retipped:
            return retipped, Evidence("retip", retipped), None
        if not ref:
            return None, None, "build row names no lane, so it names no branch"
        return None, None, ("no branch %s and no verified retip — a reaped "
                            "branch and one that never existed are the same "
                            "absence" % ref)
    tip = _hex(row.get("reviewed_tip")) or _hex(row.get("tip"))
    if not tip:
        return None, None, "review row records no reviewed tip"
    return tip, Evidence("reviewed_tip", tip), None


# ------------------------------------------------------------------ landed

def _row_tokens(row):
    """The id prefixes an integrator writes into a fold subject, longest first.
    Only prefixes of the row's OWN id — a lane NAME is a substring match and
    matches sibling rounds, which is how a chain-parent gets called landed."""
    rid = _hex((row or {}).get("id"))
    if not rid:
        return ()
    return tuple(rid[:n] for n in (32, 16, 12, 8) if len(rid) >= n)


# THE COMMITTER-DATE DATING HELPERS ARE DELETED, NOT LEFT UNCALLED (task/744, T10).
# `_dispatch_epoch` and `_proven_after` existed to prove a commit postdates a
# dispatch by comparing committer instants. A rebase rewrites that instant, so
# they proved authorship of the last branch move and nothing about landing.
# Leaving them present-but-unused would be an invitation: the next author finds
# a helper whose docstring argues for exactly the inference this reshape removed,
# and re-wires it. Dead code that asserts a rejected rule is worse than dead.
# `world.trunk_cts` stays — other readers use it; only this INFERENCE is gone.




def _carriage_of(row, world):
    """Does trunk HEAD affirm it carries THIS row's work? True/False/None.

    A lookup, not a computation — `rowworld` measures it once per IMMUTABLE
    (base, tip) pair and keys the answer by ROW ID. Keyed by row rather than
    by lane ref because the pair comes from the LEDGER: the chain's recorded
    base and the reviewed/carrier tip, neither of which a live branch can
    move (meld e:1786207689).

    A row ABSENT from the map has no such pair — no carrier, no verified
    retip, or an unresolvable object — and reads None, which `_enforce` turns
    into UNKNOWN. That is a deliberate ruling, not an oversight: strict
    current-carry has NO reaped-branch exception, and id-binding alone is
    historical."""
    rid = _hex(row.get("id"))
    carriage = world.carriage
    if carriage is None or not rid:
        return None
    return carriage.get(rid)


def _reverted(row, world, tip):
    """Has trunk TAKEN THIS WORK BACK? -> (undo sha or None, why-not or None)

    HISTORICALLY-APPEARED IS NOT CURRENTLY-CARRIED, AND THAT IS A FACT ABOUT
    THE WORK, NOT ABOUT ONE PROOF OF IT (task/744, T3). Blocker 6 put this
    question inside the content-identity rung, where it guarded one of the
    four proofs and none of the others. Every other rung then answered a
    reverted lane with LANDED, and none of them had to be wrong to do it:

      - ANCESTRY is still true after a revert. `git revert` adds a commit; it
        does not unreach the original, so a landed-then-reverted lane head
        remains an ancestor of trunk forever.
      - TREE IDENTITY was true at the matching commit and says nothing about
        HEAD — which is precisely what a later revert changes.
      - ID BINDING is a fold subject naming this row, and the fold that
        landed the work is not rewritten when the work is taken back.

    So the question is asked ONCE, about the work, before any rung speaks —
    the same ordering that cures `derive` itself. A clean revert of C carries
    C's diff REVERSED, so hashing trunk with `-R` makes it collide with C's
    FORWARD id; `world.trunk_reverts` is `{forward pid: newest undo}`.

    THREE ANSWERS, and the third is why this returns a pair. An undo NEWER
    than the newest forward match is decisive: the work is provably absent at
    HEAD. An undo that cannot be ORDERED against its match decides nothing,
    and must not be collapsed into either — it blocks a positive answer while
    asserting no negative one. No undo at all leaves every rung free.

    THE CAP EDGE (task/744). A visible undo with NO forward match is decisive
    too, and used to degrade to "the scan hit its cap". Both scans read the
    same newest-first `-n <cap>` window, so anything newer than a visible undo
    is also inside it: if the undo is visible and no landing is, there is no
    re-land to have missed. The cap can hide an OLD landing; it cannot hide a
    NEW one behind an undo it did show us."""
    # AN UNREAD REVERT INDEX IS NOT AN EMPTY ONE (task/744, T4). This rung
    # gates every landing proof, so `trunk_reverts == {}` asserts "trunk took
    # nothing back" for the whole population — a clean bill of health that a
    # failed `-R` scan would hand out for free. rowworld now carries the
    # error, and the honest answer is the inconclusive one.
    if world.reverts_unavailable:
        return None, ("the trunk revert index could not be built (%s), so "
                      "whether this work is still carried at HEAD cannot be "
                      "established" % world.reverts_unavailable)
    index = world.trunk_index or {}
    reverts = world.trunk_reverts or {}
    forward = world.trunk_patch_ids or {}
    by_commit = world.commit_patch_ids or {}
    ref = _lane_ref(row)
    subjects = list((world.branch_commits or {}).get(ref) or ())
    if tip and tip not in subjects:
        subjects.append(tip)
    unordered = None
    for sha in subjects:
        pid = by_commit.get(sha)
        undo = reverts.get(pid) if pid else None
        if not undo:
            continue
        match = forward.get(pid)
        if match is None:
            return undo, ("trunk carries a revert of this work (%s) and no "
                          "landing of it; a revert the scan CAN see cannot "
                          "hide a re-land newer than itself" % undo[:12])
        fwd, rev = index.get(match), index.get(undo)
        if fwd is None or rev is None:
            unordered = unordered or (
                "a trunk revert of this work (%s) cannot be ordered against "
                "its landing (%s), so whether the work is carried at HEAD is "
                "not established" % (undo[:12], match[:12]))
        elif rev < fwd:                      # newest-first: smaller is newer
            return undo, ("this work landed on trunk as %s and was reverted "
                          "by %s afterwards, so it is not carried at HEAD"
                          % (match[:12], undo[:12]))
    return None, unordered


def _landed(row, world, tip):
    """Is this row's work on trunk? -> (evidence, None) or (None, why-not).

    FOUR INDEPENDENT PROOFS, strongest first. Each is a positive artifact.

    1. ANCESTRY — the exact object is reachable from trunk.
    2. TREE IDENTITY — a trunk commit newer than the lane's merge-base points
       at the EXACT tree the lane tip points at. This is the proof a SQUASH
       land leaves (blocker 7): the per-commit patch-ids truthfully vanish
       into one folded commit, but the folded commit's tree is the lane's
       final state byte for byte. Position rules out the degenerate match —
       rev-list order lists a child before every parent, so a match indexed
       BEFORE the merge-base cannot be an ancestor of the branch, while a
       net-zero branch can only ever match at or behind its own base.
    3. CONTENT IDENTITY — every commit the branch carries past the merge-base
       has its patch-id on trunk. Ancestry is the WRONG question for rebased
       work (`vcs.py:565`): the integrator rebases, so the landed commit is
       patch-identical and object-different, and ancestry truthfully answers
       "no". A single unmatched commit is decisive AGAINST landing — landing
       part of a stack is exactly the state that must keep its branch.
    4. ID BINDING — a trunk commit message names this row's dispatch id. This
       is the only proof that survives the branch being reaped, and it is
       matched on the ROW ID, never the lane name.

    ANCESTRY IS A BUILD-ROW PROOF ONLY (task/744, T10). A build row's subject
    is its lane head, which trunk carries only by landing it. A review row
    names the object it LOOKED AT, and that object may have been on trunk
    BEFORE the row existed — a design round, a doc, a whole-repo question.
    Measured on the live estate: reading its presence as proof reported LANDED
    for three superseded rows whose obligation was still open (`b73f16ca721a`,
    `7e2219140e90`, `669393cb4d8a` — each reviewing the base `d225f4c3b798`).

    That is why this paragraph no longer says a non-build row lands by
    ancestry once the object is DATED after the dispatch. The dating came from
    the COMMITTER INSTANT, which a rebase rewrites, so the "proof" belonged to
    whoever last moved the branch; and it defended those three rows only by
    accident, since a base commit is old enough to fail the date test for
    reasons unrelated to whose work it is. A review row's presence on trunk is
    INCONCLUSIVE full stop: it neither lands nor vetoes the rungs below, and a
    review row reaches LANDED only through the non-forgeable rungs — content
    identity and id-binding. There used to be a `strict` flag confining this
    question to `_chain`, which left the ordinary path deriving LANDED for
    every base-reviewing verdict row (blockers 2/5)."""
    # A COMMITTER DATE IS NOT PROOF AND THIS RUNG NO LONGER ASKS FOR ONE
    # (task/744, T10). `_proven_after` compared the commit's committer instant
    # against the dispatch instant — and a rebase REWRITES that instant, so
    # the "proof" is authored by whoever last moved the branch. Worse, it
    # failed asymmetrically: an UNPARSEABLE stamp declined to land (correct)
    # while a PARSEABLE one minted LANDED, so the same forgeable field
    # answered honestly when unreadable and confidently when readable.
    #
    # ANCESTRY NOW COUNTS ONLY FOR A BUILD ROW, whose subject IS its lane head
    # — trunk carries that only by landing it. A REVIEW row names the object
    # it LOOKED AT, which may have been on trunk before the row existed (a
    # design round, a doc, a whole-repo question), so its presence there is
    # not movement of this row's work. Review rows still land through the
    # rungs below on NON-FORGEABLE proof: content identity and id-binding.
    # THE REVERT QUESTION IS ASKED ONCE, ABOUT THE WORK, BEFORE ANY RUNG
    # SPEAKS (task/744, T3). A decisive undo ends the function: no proof of
    # having appeared on trunk can outrank proof of having been taken back
    # off it. An UNORDERABLE undo is not a negative — it blocks the positive
    # rungs and states why, which is the difference between "absent" and "we
    # could not tell", and collapsing the two is the false certainty this
    # whole reshape is about.
    undone, why_undone = _reverted(row, world, tip)
    if undone or why_undone:
        return None, why_undone

    # THE CARRIAGE RUNG THAT USED TO LIVE HERE IS GONE, NOT MOVED BESIDE
    # ITSELF (task/744 round 2). The NEW ROOT put affirmative carriage
    # on `_enforce`, where EVERY door to LANDED passes it — this rung guarded
    # only the doors that came through `_landed`, which is exactly how the
    # ledger's `closed_by_landing` and a chain hop's closure went around it.
    # A second copy here would be an unreachable claim about a rule the gate
    # already owns, and this module has now taught me that lesson three
    # times: a duplicated invariant is the shape that lets one copy rot.
    undecided = None
    if tip and tip in (world.trunk_shas or frozenset()):
        if row.get("kind") == "build":
            return Evidence("ancestry", tip), None
        undecided = ("the reviewed tip is on trunk, but a review row names "
                     "the object it looked at rather than its own work, and "
                     "no non-forgeable artifact ties that presence to this "
                     "row — ancestry alone proves nothing here")

    # THE CONTENT RUNG RUNS BEFORE ID BINDING, and the reason is the VETO.
    # A review caught the original order (ancestry -> id-binding -> content)
    # contradicting the docstring above, and the contradiction was not
    # cosmetic: content identity is the only rung that can say NO. Ordered
    # last, a trunk commit that merely NAMES this row returned LANDED before
    # the veto could fire, so a PARTIAL fold with tidy bookkeeping derived
    # LANDED with the branch-keeping protection dead — the exact state the
    # veto exists to refuse.
    #
    # But the veto fires ONLY when it is DECISIVE. Unreadable patch-ids and a
    # capped trunk scan are INCONCLUSIVE, not negative, so they fall through
    # to id binding carrying their reason. Id binding is the only proof that
    # survives the branch being reaped, and collapsing inconclusive into no
    # is how a fail-safe turns into a false negative.
    ref = _lane_ref(row)
    commits = (world.branch_commits or {}).get(ref) if ref else None

    # RUNG 2: TREE IDENTITY, build rows only — a review row's subject can be
    # a trunk commit outright, whose tree trivially matches itself. All four
    # clauses are load-bearing: `commits` proves the branch carries work at
    # all, the match and the merge-base must both sit in trunk's order, and
    # the STRICT `<` is what refuses a net-zero branch matching its own base.
    if row.get("kind") == "build" and tip and commits:
        tree = (world.commit_trees or {}).get(tip)
        match = (world.trunk_trees or {}).get(tree) if tree else None
        base = (world.merge_bases or {}).get(ref)
        index = world.trunk_index or {}
        if match is not None and base in index and match in index \
                and index[match] < index[base]:
            return Evidence("tree-identity", (match, tree)), None

    if tip and commits:
        pids, missing = [], []
        for sha in commits:
            pid = (world.commit_patch_ids or {}).get(sha)
            (pids if pid else missing).append(pid or sha)
        if missing:
            # WHOSE FAULT THE GAP IS, STATED HONESTLY (task/744, T4). When the
            # lane patch-id scan itself failed, every commit looks unreadable
            # and this used to blame the commits — sending a reader to
            # inspect a branch whose only problem was that nobody could run
            # `git log -p` over it.
            undecided = undecided or (
                ("the lane patch-id scan failed (%s), so the %d commits on "
                 "%s have no readable patch-id through no fault of their own"
                 % (world.lane_scan_unavailable, len(commits), ref))
                if world.lane_scan_unavailable else
                ("%d of %d commits on %s have no readable patch-id"
                 % (len(missing), len(commits), ref)))
        else:
            # THE REVERT CHECK THAT USED TO LIVE HERE IS GONE, NOT MOVED
            # BESIDE ITSELF (task/744, T3). Blocker 6 wrote it inline, and
            # `_reverted` above now asks the same question about the same
            # patch-ids for the whole function — so by the time this rung
            # runs, no commit on this lane has an undo, decisive or
            # unorderable, or the function already returned. Keeping a second
            # copy would be an unreachable branch stating a rule the rung
            # above owns, which is how a reader learns the wrong precedence.
            landed_on = [(world.trunk_patch_ids or {}).get(pid)
                         for pid in pids]
            if all(landed_on):
                return Evidence("content-identity",
                                (ref, len(pids), landed_on[0])), None
            if world.patch_scan == "capped":
                undecided = undecided or (
                    "trunk patch-id scan hit its cap, so an absent patch-id "
                    "does not prove the work is absent")
            elif world.patch_scan != "complete":
                # Shallow history, a failed enumeration, an unreadable scan:
                # NO answer, never a negative one (blocker 9).
                undecided = undecided or (
                    "the trunk patch scan is unavailable, so an absent "
                    "patch-id is not evidence of absence")
            else:
                # DECISIVE NO: every commit had a readable patch-id, the scan
                # covered all of trunk, and at least one is absent. Say so
                # rather than returning a bare None — an unstated negative is
                # indistinguishable from "nothing was measured".
                return None, ("%d of %d commits on %s are absent from trunk "
                              "by patch-id; landing part of a stack is "
                              "exactly the state that must keep its branch"
                              % (sum(1 for x in landed_on if not x),
                                 len(pids), ref))

    for token in _row_tokens(row):
        sha = (world.trunk_tokens or {}).get(token)
        if sha:
            return Evidence("id-binding", (token, sha)), None
    return None, undecided


# -------------------------------------------------------------- supersession

def _chain(row, world):
    """Follow the CARRIER topology until a successor is provably on trunk.
    -> (hops, evidence-or-None)

    A SUPERSEDED PARENT STAYS `open` (premise `--supersedes leaves the parent
    open`), so the fold reaches `delivery == "observed"` and prints
    AWAITING_BUILD — a row nobody owes anything on, billed to a builder.

    THE FROZEN POINTER IS NOT THE TOPOLOGY (blocker 3). `superseded_by` names
    the FIRST successor forever; when that successor is cancelled and a
    SIBLING takes the work, the pointer names a corpse while the obligation
    lands elsewhere — a live repro found a parent pointing at a
    cancelled review while carrier resolution reached the approved, landed
    sibling. So the walk reads `world.carriers` — computed per population by
    `dispatches.carrier`, which honors siblings, chain identity,
    cancellation/withdrawal and pass-throughs — and a row whose carrier is
    nobody stays VISIBLE at its own state rather than hiding behind a dead
    link.

    EACH HOP IS JUDGED BY ITS OWN LIFECYCLE, not by git alone (task/744, T2;
    it was blocker 4's `_landed` call, which was already an improvement on
    the bare token lookup it replaced but still asked only half the question).
    See `_hop_lands`.

    NO NUMERIC CAP (blocker 13): `dispatches.py` already removed hop caps
    after a legitimate 66-link chain, because a cap turns depth into a
    positive SUPERSEDED. The visited set is what terminates a cycle."""
    hops, seen = [], {str((row or {}).get("id") or "")}
    rid = (world.carriers or {}).get(str((row or {}).get("id") or ""))
    while rid and rid not in seen:
        seen.add(rid)
        hops.append(rid)
        landed = _hop_lands(rid, world)
        if landed:
            return tuple(hops), landed
        rid = (world.carriers or {}).get(rid)
    return tuple(hops), None


def _hop_lands(rid, world):
    """Does THIS successor's own state discharge the parent? -> evidence|None

    THE PARENT'S OBLIGATION IS DISCHARGED BY A LANDING, NOT BY A COMMIT
    (task/744, T2). The old walk asked `_landed` of each hop and nothing
    else, so a successor that had been CANCELLED, closed `withdrawn`, or
    given a FIX verdict still landed its parent the moment its tip appeared
    on trunk — the parent read LANDED on the strength of work its own
    successor's reviewer had rejected. Git cannot see a verdict, so a
    git-only hop test cannot help but do this.

    So the hop is judged by the ladder in miniature, in the ladder's own
    order: the LEDGER'S recorded terminal first (only `landed` discharges,
    and its own closure is the evidence — any other terminal, or one we
    cannot name, means the work did not land here), then the VERDICT (a
    polarity that demanded further work never discharges), then the OPEN
    REVIEW (a row still owed a verdict has not landed anything, and its
    subject tip on trunk is its author's landing, not its own), and only then
    the artifacts.

    A SUCCESSOR THE SNAPSHOT CANNOT READ IS STILL JUDGED, as it always was:
    with no row to read there is no lifecycle to consult, and its only
    reachable proof is the id binding `_landed` consults last. That is why
    absence-from-the-snapshot is tested explicitly rather than by reading an
    empty dict — an empty dict has no `kind`, so the open-review rung would
    otherwise refuse every unreadable successor and turn a missing row into a
    negative claim."""
    successor = (world.rows or {}).get(rid)
    known = isinstance(successor, dict)
    judged = successor if known else {"id": rid}
    if known:
        ended, ended_evidence, ledger = _lifecycle(judged)
        if ended == LANDED:
            # THE HOP MEETS THE SAME BAR AS THE ROW (NEW ROOT): a
            # successor closed `landed` whose work trunk no longer carries
            # cannot discharge its parent, and returning its closure here
            # short-circuited the carriage question entirely.
            return ended_evidence if _carriage_of(judged, world) is True \
                else None
        if ledger != L_OPEN:
            return None
        concluded, polarity = _verdict(judged)
        if concluded and polarity != "approve":
            return None
        if judged.get("kind") != "build" and not concluded:
            return None
    tip, _tip_evidence, _why = subject_tip(judged, world)
    landed, _why_not = _landed(judged, world, tip)
    return landed


# --------------------------------------------------------------------- gate

def _judge_receipt(receipt, world, tip):
    """Does this ONE receipt bind `tip`? -> (evidence, why-not).

    THE TRAP: a gate receipt snapshots the WORKING TREE, which includes
    untracked files, so a receipt can bind a tree object no commit has. Trusting
    `receipt.head == tip` alone would let a run over uncommitted work vouch for
    a commit that never contained it. So the receipt's tree is compared to the
    tree the commit actually points at, and a disagreement REFUSES rather than
    assuming either side.

    `dirty` must be false and `head_after`/`tree_after` must equal the before
    values: a suite that ran while the tree moved measured neither state."""
    commit_tree = _hex((world.commit_trees or {}).get(tip))
    receipt_tree = _hex(receipt.get("tree"))
    if not commit_tree:
        return None, ("receipt %s binds head %s, whose tree is unreadable"
                      % (receipt.get("id"), tip[:12]))
    if not receipt_tree or receipt_tree != commit_tree:
        return None, ("receipt %s binds tree %s but commit %s points at %s — "
                      "refusing rather than assuming"
                      % (receipt.get("id"), str(receipt_tree)[:12], tip[:12],
                         commit_tree[:12]))
    if receipt.get("dirty") is not False:
        return None, "receipt %s was minted on a dirty tree" % receipt.get("id")
    if _hex(receipt.get("head_after")) != tip \
            or _hex(receipt.get("tree_after")) != receipt_tree:
        return None, ("receipt %s moved under the suite (head/tree changed)"
                      % receipt.get("id"))
    if receipt.get("status") != "OK":
        return None, ("receipt %s recorded status %r"
                      % (receipt.get("id"), receipt.get("status")))
    return Evidence("gate-receipt", (receipt.get("id"), commit_tree)), None


def _receipt_by_id(world, tip, gate):
    """The receipt recorded for `tip` under id `gate`, or None.

    A TIP LEGITIMATELY CARRIES SEVERAL RECEIPTS — a re-run after a flake, a
    second host, a re-gate on the same tree — and `_gated` returns whichever
    binding one it reached first. An approval cites the receipt its reviewer
    actually saw, which need not be that one, so the approval's claim is
    checked against the receipt it NAMES (task/744, T6)."""
    for receipt in (world.receipts_by_head or {}).get(tip) or ():
        if _hex(receipt.get("id")) == gate:
            return receipt
    return None


def _gated(world, tip):
    """The first receipt that BINDS `tip`, or why none of them do.

    Every candidate is judged rather than only the newest: a head can carry a
    dirty or failed run alongside a clean one, and picking by recency alone
    would let the wrong receipt speak for the commit. The refusal reported is
    the first one, so a caller sees a reason rather than a bare no."""
    why = None
    for receipt in (world.receipts_by_head or {}).get(tip) or ():
        evidence, reason = _judge_receipt(receipt, world, tip)
        if evidence:
            return evidence, None
        why = why or reason
    return None, why


# ----------------------------------------------------------------- approval

def _approved(row, world, tip, receipt_id):
    """A CROSS-FAMILY approve bound to this EXACT tip. -> (evidence, why-not).

    THREE RULES, each of which was once broken in production.

    FAMILY COMES FROM THE MODEL, NEVER THE LABEL. `world.families` is built by
    `dispatches._approval_identity_family_evidence`, which reads a verified
    native runtime or a measured proxy route and gives seat names, labels and
    harness types exactly zero weight. Two seats called `codex` and `codex-2`
    are ONE family; `codex` and `ds4pro` are two (measured 2026-08-05).

    `concur` AUTHORIZES NOTHING. It is absent from `WORK_POLARITIES` by
    construction (`verdicts.py:39`) so that endorsement has a word that promises
    no landing. Testing membership in `WORK_POLARITIES` and then requiring
    `approve` keeps this module honest without restating the vocabulary.

    AN APPROVE IS BOUND TO ONE OBJECT. A verdict on tip A says nothing about
    tip B, so an approval whose `reviewed_tip` is not the current head is not
    this head's approval — work continued past it."""
    lane = str((row or {}).get("lane") or "").strip()
    approvals = (world.approvals or {}).get(lane) or ()
    authors = (world.authors or {}).get(lane) or frozenset()
    author_families, unknown_author = set(), False
    for seat in authors:
        fams = (world.families or {}).get(seat)
        if fams:
            author_families |= set(fams)
        else:
            unknown_author = True
    # ORDER MUST NOT DECIDE (task/744, T14). Every refusal
    # below used to RETURN, so the FIRST unusable approve ended the search and
    # a perfectly good one later in the tuple was never reached. The
    # exact probe: `(missing-gate ds4pro, valid helm-claude-2)` derived GATED
    # and the SAME PAIR REVERSED derived APPROVED — one row, two answers,
    # decided by nothing but iteration order. The refusals are collected and
    # the search continues; only an EXHAUSTED candidate set reports them.
    refusals, valid = [], []
    for approval in approvals:
        if approval.get("polarity") != "approve":
            continue
        if approval.get("polarity") not in WORK_POLARITIES:
            continue
        if _hex(approval.get("reviewed_tip")) != tip:
            continue
        seat = approval.get("recipient")
        fams = (world.families or {}).get(seat)
        if not fams:
            refusals.append("the approve by @%s cannot be counted — no "
                            "verified family evidence for that seat" % seat)
            continue
        if not author_families:
            refusals.append("no verified family evidence for the author of "
                            "lane %s, so cross-family cannot be established"
                            % lane)
            continue
        if set(fams) & author_families:
            continue
        if unknown_author:
            refusals.append("an author of lane %s has no family evidence, so "
                            "a cross-family claim would be a guess" % lane)
            continue
        gate = _hex(approval.get("gate"))
        if not gate:
            refusals.append("the approve by @%s carries no gate token" % seat)
            continue
        # THE APPROVAL'S OWN RECEIPT IS JUDGED, NOT COMPARED TO A PICK
        # (task/744, T6). This used to refuse whenever the cited gate differed
        # from `receipt_id` — and `receipt_id` is whichever binding receipt
        # `_gated` reached FIRST. One tip legitimately carries several: a
        # re-run after a flake, a second host, a re-gate on the same tree. So
        # a reviewer who approved against a perfectly good receipt was told
        # their approve "binds receipt X, but the tip is gated by Y", and the
        # row read GATED with an approve sitting on it — an owner-visible
        # false negative produced entirely by iteration order.
        #
        # The question the word APPROVED actually asks is whether the receipt
        # THIS APPROVAL CITES binds THIS tip, so that is what is asked, using
        # the same `_judge_receipt` every other receipt faces. Nothing is
        # loosened: a cited receipt that is absent, dirty, failed, or bound to
        # another tree is refused exactly as before, and now says which.
        if gate != _hex(receipt_id):
            cited = _receipt_by_id(world, tip, gate)
            if cited is None:
                refusals.append("the approve by @%s cites gate %s, which is "
                                "not among the receipts recorded for this tip"
                                % (seat, gate[:12]))
                continue
            bound, why_unbound = _judge_receipt(cited, world, tip)
            if not bound:
                refusals.append("the approve by @%s cites gate %s, which does "
                                "not bind this tip — %s"
                                % (seat, gate[:12], why_unbound))
                continue
        valid.append(Evidence("approve", (approval.get("id"), seat,
                                          sorted(fams), gate)))
    if valid:
        # THE CREDITED WITNESS IS CANONICAL, NOT THE FIRST ONE FOUND (see
        # supplement 2). Returning early made the STATE order-independent and
        # left the EVIDENCE order-dependent: two valid cross-family approvals
        # with distinct valid receipts both reach APPROVED while crediting
        # different rows, so the same population explains itself differently
        # depending on tuple order. A reader checking the derivation gets a
        # different answer than the one who checked it yesterday. Sorting by
        # the approval's own id is arbitrary but STABLE, which is the whole
        # requirement.
        return min(valid, key=lambda e: str(e.detail[0] or "")), None
    # EXHAUSTED, not short-circuited. Reporting the FIRST refusal keeps the
    # message a caller can act on while the SEARCH stays order-independent —
    # which of several refusals is quoted may vary; whether the row is
    # APPROVED may not.
    return None, (refusals[0] if refusals else None)


# ------------------------------------------------------------------ derive
#
# THE MANDATORY GATE (task/744 round 2, the ROOT of T15 + L1 + L3 + L5 + L6).
#
# Round 1 cured the per-case shape INSIDE the ladder and left the ladder's own
# doors uncured. Several rungs still returned a confident state before the
# invariants that could refute it had run: `_lifecycle`'s terminal answered
# LANDED at the very top, three lines after a docstring saying no rung may
# answer before a higher-precedence fact has been consulted, so a
# `closed_by_landing` row whose work was later REVERTED still derived a
# current LANDED. The open-review rung answered before carrier resolution, so
# a review whose obligation had moved to a successor stayed billed. The
# terminal mapping answered before the contrary invariant, so a `withdrawn`
# close whose work landed anyway read a bland CANCELLED.
#
# One shape, several doors — so the cure is one DOOR, not several patches.
# `derive` is now a thin wrapper that nothing can route around: the ladder
# produces a CANDIDATE, and `_enforce` is what turns a candidate into an
# answer. A rung that wants to say LANDED states its candidate; whether that
# word survives is decided in exactly one place, against facts gathered once,
# for every rung alike. Adding a rung later cannot reintroduce the class,
# because a rung has no way to return past the door.

_CONFIDENT = (LANDED, REVIEWED, BUILT, SUPERSEDED, CANCELLED, APPROVED,
              GATED, REVIEWING, BUILDING, UNSTARTED)


class _Mandatory(object):
    """The facts EVERY confident state is judged against, measured once.

    Held here rather than recomputed per rung because the defect was never
    that a rung computed them wrongly — it was that a rung answered without
    computing them at all."""

    __slots__ = ("contrary", "carried", "why_uncarried", "held", "carrier",
                 "reverted", "why_reverted", "carriage")

    def __init__(self, row, world, tip):
        concluded, polarity = _verdict(row)
        landed, why_uncarried = _landed(row, world, tip)
        # REFUSED FOR EVIDENCE AGAINST, NOT FOR EVIDENCE MISSING. The first
        # cut of this gate refused LANDED whenever nothing affirmed carriage,
        # and that is the OPPOSITE error to L5: a row closed `landed` whose
        # branch was long since reaped has no artifact left to affirm
        # anything, and calling it UNKNOWN discards a machine-written
        # terminal in favour of a measurement nobody can take. Six chain arms
        # and one ledger arm went red saying so. What L5 actually names is a
        # POSITIVE contradiction — trunk took the work back — so that is what
        # is measured here and what `_enforce` refuses on.
        self.reverted, self.why_reverted = _reverted(row, world, tip)
        # A CONTRARY IS ANY RECORDED "THIS IS NOT COMING" MET BY WORK ON
        # TRUNK. It outranks the ledger's own terminal word (L1).
        #
        # THE FIRST CUT ASKED ONLY ABOUT A VERDICT'S POLARITY, and the
        # refinement is that the CLOSE ITSELF can be the change-demanding
        # statement: a row closed `withdrawn` with NO verdict at all still
        # says nobody owes anything and nothing is coming. Measured on this
        # tip — withdrawn-close + fix verdict + work on trunk read UNKNOWN
        # (right), while withdrawn-close + NO verdict + work on trunk read
        # CANCELLED, indistinguishable from the same close with the work
        # ABSENT. Close-over-polarity is valid; close-over-LATER-ARTIFACT is
        # not, and only the second case had no witness.
        #
        # The non-delivery set is DERIVED from `_CLOSE_TERMINAL` rather than
        # retyped: a reason means non-delivery exactly when its conservative
        # terminal is CANCELLED, so a ninth reason added there cannot arrive
        # here unclassified.
        ending = _non_delivery(row)
        self.contrary = None
        if landed:
            if concluded and polarity != "approve":
                self.contrary = polarity or "UNDECLARED"
            elif ending:
                self.contrary = ending
        self.carried = landed
        self.why_uncarried = why_uncarried
        # AFFIRMATIVE CARRIAGE IS A FACT ABOUT THE ROW, NOT A RUNG INSIDE
        # ONE PROOF (task/744, NEW ROOT). It lived only in
        # `_landed`, so every path that reached LANDED WITHOUT `_landed` — the
        # ledger's own `closed_by_landing`, and `_hop_lands` short-circuiting
        # on a successor's closure — sailed past it. A probe: one
        # `closed_by_landing` row derives LANDED for carriage True, False AND
        # None alike, and 33 of my arms passed while missing it, because they
        # all entered through the rung rather than around it.
        self.carriage = _carriage_of(row, world)
        self.held = row.get("status") == "held"
        self.carrier = (world.carriers or {}).get(str(row.get("id") or ""))


def _non_delivery(row):
    """The recorded ending that says NOTHING IS COMING, or None.

    A structured close whose conservative terminal is CANCELLED — and the
    `abandoned` flag, which is the same statement in the other spelling.
    Derived from `_CLOSE_TERMINAL` so the two tables cannot drift."""
    reason = row.get("close_reason")
    if reason and _CLOSE_TERMINAL.get(reason) == CANCELLED:
        return "close:" + str(reason)
    for flag, terminal in _TERMINAL_FLAGS.items():
        if row.get(flag) and terminal == CANCELLED:
            return "flag:" + str(flag)
    return None


def _enforce(candidate, row, facts):
    """The one door. A candidate state, judged against the mandatory facts.

    Only DOWNGRADES: a rung may lose its word here, never gain a stronger
    one. That direction is what makes the door safe to add to — a new
    invariant can refuse an existing answer but cannot invent one."""
    state = candidate.state
    if state == LANDED and facts.reverted:
        return Derivation(UNKNOWN, candidate.evidence,
                          facts.why_reverted)
    # EVERY LANDED CANDIDATE, WHATEVER DOOR IT CAME THROUGH. True affirms;
    # False is reserved for a POSITIVE anti-carriage artifact and the
    # relation does not currently mint one; None means NO WITNESS AFFIRMED,
    # which covers both "the question could not be asked" and "no witness
    # could see it". Only True authorizes the word — `why_uncarried` was
    # diagnostic here, and diagnostics do not refuse anything.
    # A RECORDED LANDING IS NOT AN INFERENCE, AND MUST NOT BE RE-MEASURED
    # (a revised ruling, task/756). A structured `landed` close and
    # `closed_by_landing` are affirmative, non-forgeable proof that LANDING
    # OCCURRED — written under the fold's own proof gates at a moment when
    # the artifacts supported them. Present-content carriage answers a
    # DIFFERENT question, and for this estate's history it usually cannot be
    # answered at all: measured over the 196 rows the ledger closed as
    # landed, 98 of 105 askable came back non-affirming and 91 had no
    # askable pair. Requiring a live re-measurement there makes one scalar
    # state pretend a recorded event never happened.
    #
    # So carriage gates the INFERRED candidates — ancestry, tree identity,
    # content identity, id binding — and the explicitly-current `carried`
    # verb, which is where a stale claim can actually be minted. The
    # supersession chain is exempt for the older reason: its hop was already
    # carriage-judged by `_hop_lands`.
    # ...AND `carried` IS NOT ONE OF THEM (a direct pure-derive probe on
    # this tip). A `carried` closure's ENTIRE CONTENT is a current-carriage
    # claim, so exempting it from current-carriage re-measurement is
    # circular: the row would assert LANDED on a record of a measurement
    # nobody is allowed to re-take. A `landed` close is different in kind —
    # it records that LANDING OCCURRED, which no later measurement can
    # un-happen. The exemption is for proofs INDEPENDENT of the question, and
    # my first cut spelled it "any closure", which let the one dependent
    # closure through.
    recorded = any(
        e.kind == "flag"
        or (e.kind == "closure"
            and str((e.detail or ("",))[0]) != "carried")
        for e in candidate.evidence)
    if state == LANDED and facts.carriage is not True and not recorded \
            and not any(e.kind == "supersession-chain"
                        for e in candidate.evidence):
        return Derivation(UNKNOWN, candidate.evidence,
                          "trunk HEAD does not affirm that it carries this "
                          "row's work (carriage %r), and a record of its "
                          "having landed once is not that affirmation"
                          % (facts.carriage,))
    # A CHAIN LANDING IS EXEMPT HERE BECAUSE IT WAS ALREADY JUDGED, not
    # because it is trusted: a parent that lands through a SUCCESSOR has no
    # carriage of its own by construction — its lane is not where the work
    # went — and `_hop_lands` now requires affirmative carriage of the hop
    # that produced this evidence. Requiring the parent's own carriage on top
    # would refuse every legitimate supersession, which is the same
    # missing-evidence-versus-evidence-against error I already made once in
    # this gate and had seven arms tell me about.
    if facts.contrary and state in (REVIEWED, CANCELLED, BUILT, LANDED):
        return Derivation(UNKNOWN, candidate.evidence + (
            Evidence("contrary", (facts.contrary, _hex(row.get("reviewed_tip")))),),
            "a %s verdict concluded this row and its work is on trunk anyway "
            "— that disagreement is unresolved here, and %s states it as "
            "settled" % (facts.contrary, state))
    if state == REVIEWING and facts.held:
        return Derivation(UNKNOWN, candidate.evidence,
                          "this row is HELD, and a hold is not a review in "
                          "progress — `helm dispatch release` is the "
                          "transition that reopens it")
    # THE CARRIER PROMOTION IS GONE FROM HERE (supplement
    # 1). It turned REVIEWING into SUPERSEDED — a NEW confident word minted by
    # the gate, which is exactly what "downgrade-only" forbids, and I asserted
    # downgrade-only in the commit message while the code did this three
    # screens below. Carrier resolution belongs in CANDIDATE PRODUCTION, so
    # the ladder consults it before saying REVIEWING at all.
    return candidate


def derive(row, world):
    """The state of one land-request row, derived from artifacts. -> Derivation

    PURE: no I/O, no writes, no clock, no subprocess. Same `World`, same answer.
    Every read is a dict or set lookup, which is why a full population costs
    the world build and essentially nothing per row.

    THIS FUNCTION IS A DOOR, NOT A LADDER. `_ladder` decides what the row
    LOOKS like; `_enforce` decides whether that word survives the mandatory
    invariants. Keeping them apart is the point — see the block above.

    `row` is a folded dispatch record (`dispatches.snapshot_with_verdicts()`).
    `world` is a `helm.rowworld.World`."""
    if not isinstance(row, dict) or not _hex(row.get("id")):
        return Derivation(UNKNOWN, (), "not a land-request row")
    if world is None:
        return Derivation(UNKNOWN, (), "no artifact snapshot was supplied")
    # A ROW THAT NAMES NO REPOSITORY CANNOT BE JUDGED BY THIS ONE'S
    # ARTIFACTS (task/744, addendum 4). `rowworld._rows_for`
    # includes repo-less legacy rows in EVERY repository's snapshot, because
    # excluding them would invent "this row is not ours" — but including them
    # silently invented "this row IS ours", and that is the direction that
    # mints confidence: a probe had repo A's own fold token landing a
    # foreign legacy row. The row stays VISIBLE, which is why it is still in
    # the snapshot; what it cannot do is borrow A's evidence.
    if _hex(row.get("id")) in (world.unscoped or frozenset()):
        return Derivation(UNKNOWN, (Evidence("unscoped", row.get("lane")),),
                          "this row names no repository, so no artifact in "
                          "THIS one can speak for it — visible, unplaced, "
                          "and not to be judged by somebody else's trunk")
    tip, _tip_evidence, _why = subject_tip(row, world)
    facts = _Mandatory(row, world, tip)
    candidate = _ladder(row, world, facts)
    if candidate.state not in _CONFIDENT:
        return candidate
    return _enforce(candidate, row, facts)


def _ladder(row, world, facts):
    """The precedence lattice. Returns a CANDIDATE — see `derive`."""
    # THE LEDGER'S OWN TERMINALS OUTRANK INFERRED PROGRESS (blocker 2). A
    # cancelled row is nobody's obligation no matter what its branch did, and
    # a structured close carries its own proof. It no longer outranks the
    # MANDATORY facts, though: this returns a candidate like every other rung
    # and `_enforce` still gets to refuse it (L1, L5).
    ended, ended_evidence, ledger = _lifecycle(row)
    if ended:
        return Derivation(ended, (ended_evidence,))
    if ledger == L_UNREADABLE:
        return Derivation(UNKNOWN, (ended_evidence,),
                          "the ledger closed this row for a reason this "
                          "module cannot name (%r) — what git shows about "
                          "the work cannot re-open a recorded ending"
                          % (row.get("close_reason"),))

    tip, tip_evidence, why_no_tip = subject_tip(row, world)
    found = [tip_evidence] if tip_evidence else []

    # EVIDENCE IS MEASURED ONCE, IN `_Mandatory`, BEFORE THIS LADDER RUNS —
    # and since round 2 the ladder does not get to skip it: `_enforce` sees
    # the same facts whatever rung answered. Computing is not concluding.
    concluded, polarity = _verdict(row)
    landed, why_unlanded = facts.carried, facts.why_uncarried

    # THE CONTRARY-LAND INVARIANT USED TO BE A RUNG HERE, AND THAT WAS THE
    # BUG (round 2, L1). As a rung it could only refuse the states BELOW it —
    # so `_lifecycle`'s terminal, which answers above every rung, sailed past
    # it and a row closed `withdrawn` whose work landed anyway read a bland
    # CANCELLED. An invariant that only some answers pass is not an
    # invariant. It lives in `_enforce` now, where every candidate meets it.

    # A RECORDED VERDICT THAT AUTHORIZED NO LANDING ENDS THE ROW AT REVIEWED.
    # fix and supersede DEMAND further work, concur endorses without
    # authorizing, and UNDECLARED refuses to guess — for every one of them the
    # tip sitting on trunk is somebody else's landing, and deriving LANDED
    # here is how 128 change-demanding verdicts read as delivered work.
    if concluded and polarity != "approve":
        return Derivation(REVIEWED, tuple(found) + (
            Evidence("verdict", (polarity or "UNDECLARED",
                                 _hex(row.get("reviewed_tip")))),))

    # RUNG 3 — AN OPEN REVIEW OUTRANKS THE LANDED SHORTCUT. A review row with
    # no concluded verdict is OWED one, and its subject tip sitting on trunk
    # is somebody else's landing: the reviewer's obligation did not evaporate
    # because the author's work merged. This ran AFTER `_landed` and so a
    # still-owed review reported LANDED — the reviewer vanished from every
    # surface that reads this state.
    if row.get("kind") != "build" and not concluded:
        # CARRIER FIRST (supplement 1). A review whose obligation has
        # moved to a successor is not REVIEWING, and asking here — in
        # candidate production — is what lets `_enforce` stay downgrade-only.
        if facts.carrier:
            return Derivation(SUPERSEDED, tuple(found) + (
                Evidence("superseded-by", (facts.carrier,)),),
                "a successor carries this row's obligation, so the review it "
                "is still open for is not owed here")
        return Derivation(REVIEWING, tuple(found) + (
            Evidence("open-review", row.get("status")),),
            why_unlanded or why_no_tip)

    if landed:
        return Derivation(LANDED, tuple(found) + (landed,))

    hops, chain_evidence = _chain(row, world)
    if chain_evidence:
        return Derivation(LANDED, tuple(found) + (
            Evidence("supersession-chain", (hops, chain_evidence)),))
    if hops:
        return Derivation(SUPERSEDED, tuple(found) + (
            Evidence("superseded-by", hops),),
            "the successor is not bound to trunk, so where the work went is "
            "not established here")

    # AN APPROVE THAT HAS NOT LANDED IS STILL A CONCLUDED REVIEW. Its landing,
    # when it happens, is proven by the arms above; until then the truthful
    # stage is the verdict, not the receipt that happened to bind its tip.
    if concluded:
        return Derivation(REVIEWED, tuple(found) + (
            Evidence("verdict", ("approve", _hex(row.get("reviewed_tip")))),),
            why_unlanded)

    # THE OPEN-REVIEW RUNG MOVED UP (task/744) rather than being duplicated:
    # it now runs at rung 3, ABOVE the landed shortcut, because a review row
    # owed a verdict is REVIEWING whether or not its subject tip reached
    # trunk. Nothing reaches here that it would have caught — a review row
    # without a concluded verdict already returned, and one WITH a verdict
    # answered at REVIEWED or the contrary invariant above. Leaving a second
    # copy here would be an unreachable branch asserting a rule the ladder
    # already owns, which is how a reader learns the wrong precedence.
    if not tip:
        return Derivation(UNKNOWN, tuple(found), why_no_tip)

    gated, why_ungated = _gated(world, tip)
    if not gated:
        # "NO RECEIPT BINDS THIS TIP" IS A CLAIM ABOUT THE WHOLE LEDGER, and
        # a ledger with holes cannot back it (blocker 11): a skipped row is
        # PRESENT AND UNJUDGEABLE — it may be this tip's receipt — and an
        # unreadable ledger says even less. Either way the stage states that
        # lean on receipt ABSENCE (UNSTARTED, BUILDING) are unknowable, while
        # a receipt that DID bind stays positive evidence above.
        if world.receipts_unavailable:
            return Derivation(UNKNOWN, tuple(found),
                              "the gate receipt ledger could not be read "
                              "(%s) — a binding receipt may exist"
                              % world.receipts_unavailable)
        if world.receipts_skipped:
            return Derivation(UNKNOWN, tuple(found),
                              "%d receipt rows could not be judged — the "
                              "absence of a receipt for this tip is not "
                              "evidence" % world.receipts_skipped)
        ref = _lane_ref(row)
        # A BRANCH WHOSE WALK FAILED IS NOT A BRANCH WITH NO COMMITS
        # (task/744, T4). Both leave the ref absent from `branch_commits`,
        # and every stage word below — UNSTARTED, BUILDING — is a positive
        # claim about what the lane contains. rowworld now names the refs it
        # could not walk, so the one case that knows nothing says so.
        if ref and ref in (world.branch_walk_failed or frozenset()):
            return Derivation(UNKNOWN, tuple(found),
                              "the commit walk for %s failed, so how much "
                              "work this lane carries is unknown — an "
                              "unwalkable branch is not an empty one" % ref)
        commits = (world.branch_commits or {}).get(ref) if ref else None
        if commits is not None and not commits:
            return Derivation(UNSTARTED, tuple(found) + (
                Evidence("no-commits-past-merge-base", ref),))
        # BUILDING asserts "not landed yet", and that claim leans on the
        # trunk patch scan. An unavailable scan cannot back it (blocker 9):
        # an unread scan is not an empty one, so the state is UNKNOWN.
        if world.patch_scan == "unavailable":
            return Derivation(UNKNOWN, tuple(found),
                              "the trunk patch scan could not be read, so "
                              "BUILDING cannot be asserted — an unread scan "
                              "is not an empty one")
        return Derivation(BUILDING, tuple(found) + (
            Evidence("commits", len(commits) if commits else None),),
            why_ungated or why_unlanded)

    receipt_id = gated.detail[0]
    approved, why_unapproved = _approved(row, world, tip, receipt_id)
    if approved:
        return Derivation(APPROVED, tuple(found) + (gated, approved))
    return Derivation(GATED, tuple(found) + (gated,), why_unapproved)
