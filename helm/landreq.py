#!/usr/bin/env python3
"""helm lr — the LAND REQUEST lifecycle VIEW over the dispatch ledger plus
Git observation of trunk. list/show/stalls/project are read-only; `land` emits
a candidate receipt, `discharge` proof-gates contrary-debt reconciliation, and
`abandon` records a review-evidence write-off only after four refusal interlocks.

An LR is a single dispatch seen as a land loop. It binds the author, the
lane/branch, the exact review commit (the dispatch tip), the reviewer verdict
(the dispatch verdict), and the landed evidence (the reviewed change observed
reaching trunk). The dispatch ledger is the durable workflow truth, and Git is
the landing authority; the LR observes the integrator's merge or cherry-pick.

LAND RECEIPTS (coordination-substrate slice B): `helm lr land <id>` emits a
signed, chain-ordered `helm.land` coordination turn on the room node — reusing
chat's exact topic-parameterized signed-emit path, fee-free — and write-throughs
the returned {turn, receipt, chain} into a durable local index.

HONEST AUTHORITY BOUND: dregg does not yet disclose the payload carried by a
turn. Replay can recompute the stored digest and validate schema/field/transport
shapes, but cannot prove that the named turn actually signed that digest. A
hand-written row with arbitrary hex turn/receipt/chain and a recomputed payload
is therefore locally indistinguishable from the write-through. Such a row is
`local-unverified`: useful diagnostic/correlation evidence, NEVER authority that
can strengthen Git's lifecycle observation. The positive receipt can kill
`(landing unobservable)` only after payload disclosure supplies a distinct
verified state.

The local index still fails closed within its honest scope. A wrong topic,
field that no longer rebinds, or junk transport shape is REJECTED; competing
bindings are CONFLICT. Strict physical replay refuses the entire receipt index
when any complete malformed/oversize/id-less row exists, while an unterminated
final tail remains outside the durability boundary. Contamination cannot select
a convenient sibling, but it also cannot veto independently readable Git.

Git landedness remains tri-state end to end. Ancestry rc 0 is positive, rc 1 is
genuinely non-ancestral, and rc 128/timeout is UNDETERMINED; a non-positive
ancestry answer falls through to live per-commit patch identity. Only exact
`git cherry` +/- output becomes a fact, so two unanswerable probes stay unknown
rather than manufacturing a READY stall. `trunk_sha` remains the candidate's
historical land-event anchor. Its stored `patch_id` is correlation evidence;
lifecycle landedness derives independently from live Git.

Receipting is FAIL-OPEN: with no signer/node the receipt is simply not recorded
and landing stays exactly Git-observed. A locally consistent receipt likewise
only annotates that observation. The read views (list/show/stalls/project) stay
read-only. `land` writes a diagnostic receipt; `discharge` is deliberately
FAIL-CLOSED because it retires an owed obligation.

Because every wait is now a named, queryable state with a DWELL time, a loop
sitting in a non-terminal state past its per-stage threshold IS the stall signal
— the owner's workflow-gap-finder ("where are we missing delays and
opportunities to optimize workflows"):

  OPEN             dispatched, delivery to the reviewer not yet confirmed
  AWAITING_REVIEW  delivered to the reviewer, no verdict yet (review loop)
  READY            APPROVED verdict, reviewed tip not yet on trunk (land loop)
  CHANGES_REQUESTED  a FIX verdict — the ball is back with the AUTHOR, and the
                   dwell is owed by the author, not the reviewer or the lander
  REVIEWED         a verdict of UNDECLARED polarity: a review happened but the
                   ledger never recorded whether it said yes. NOT a land loop —
                   see the honest-refusal note below
  MERGED_LOCAL     tip on local trunk but not the upstream trunk (push loop)
  LANDED           tip on the upstream trunk — terminal; local trunk is the
                   landing target when no upstream is configured
  SUPERSEDED       the reviewer declared this tip replaced — terminal
  DELIVERED_REPORT a BUILD artifact was handed off under explicit artifact and
                   chat report references — terminal, with no Git land claim
  ABANDONED        reviewed commit missing and every surviving-work interlock
                   clear — terminal, with land state permanently UNKNOWN

VERDICT POLARITY, AND WHY `REVIEWED` REFUSES RATHER THAN GUESSES. This module
used to derive READY from `status == "verdict"`, i.e. from a review having
HAPPENED, never from its having said YES. A live incident put five loops in
READY for over a day carrying two SUPERSEDEDs and three FIX verdicts — not
one an approval — and `stalls` billed
all five as land-side workflow gaps. The limitation was DOCUMENTED here in
prose the whole time and the behaviour lied anyway: documented is not done.

The polarity now comes from the dispatch verdict event (`approve`/`fix`/
`supersede`) and is never inferred from the evidence prose, because keyword-
sniffing free text is the per-case-handler spiral — the next reviewer writes
"needs work", then "REJECT", then "blocked" — and that spiral's only cure is
whole-object validation plus an honest refusal. So a row written before polarity
existed reads REVIEWED and carries NO land threshold: a land delay cannot be
billed against a verdict that may have been a rejection. `stalls` reports those
rows in their own line rather than dropping them silently — an unmeasurable
loop is surfaced as unmeasurable, never as healthy.

Git is observed for every closed row because physical history is a fact, not a
reading of intent. An undeclared row demonstrably on trunk is honestly
MERGED_LOCAL/LANDED. FIX and SUPERSEDE retain their verdict-derived lifecycle
state, while the separate landed/merged_local fields expose contrary inclusion
without suppressing the fact or laundering it into a normal approved land. A
contrary row stays operationally non-terminal, is owed by the integrator, and is
surfaced in list/show/board until the contradiction is reconciled.

Landing arms on an APPROVED (or undeclared-but-observed) verdict, matching the
dispatch ledger's own grammar. A CANCELLED dispatch is abandoned, never a land
loop — excluded upstream unless an explicit delivered-report correction event
binds its artifact and handoff references; cancellation prose is never inferred.
Stdlib-only and import-safe;
list/show/stalls stay read-only while each terminal mutation is explicit and
proof- or refusal-gated.
"""
import calendar
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import unicodedata

from . import dispatches, foldcheck, home, pk, projscope, vcs
from .store import load as store_load

# Land-side stall thresholds (seconds), measured — like every LR dwell — from
# the VERDICT stamp, because git records no moment for the local merge or the
# push: MERGED_LOCAL/LANDED are OBSERVED, so their timeline ts is None and the
# only clock a land-side loop has is its verdict. The thresholds are therefore
# CUMULATIVE from the verdict and MONOTONIC in stage order — land within the
# land budget of the verdict, reach upstream within a further push budget of
# it. Because MERGED_LOCAL >= READY, forward progress (READY -> MERGED_LOCAL)
# can never manufacture a stall on a loop that was healthy in READY: the merge
# CLEARS the land-side wait and re-arms the push budget rather than flipping a
# one-second-old push loop to STALLED. (A future revision could date the merge
# from the trunk commit's committer date; v1 is observe-only, never gating.)
# The review-side stages (OPEN, AWAITING_REVIEW) instead honor the dispatch
# row's own advisory deadline_s. Terminal LANDED never stalls.
_LAND_BUDGET_S = 3600      # land within 1h of the verdict
_PUSH_BUDGET_S = 1800      # reach upstream within a further 30m
LAND_STALL_S = {"READY": _LAND_BUDGET_S,
                "MERGED_LOCAL": _LAND_BUDGET_S + _PUSH_BUDGET_S}
# REVIEWED is deliberately ABSENT from every threshold table: it is either an
# undeclared historical verdict OR a declared approval held by tier/gate policy.
# Neither creates a billable land/author clock until authorization is resolved.
# CHANGES_REQUESTED honours the row's advisory deadline_s like the other
# review-side waits, because the obligation it names is a re-dispatch by the AUTHOR.
_AUTHOR_STAGES = ("CHANGES_REQUESTED",)
# polarity -> the state a verdict ALONE puts a loop in, before git observation.
# ONE table drives both the live state and the timeline, so a polarity added
# later cannot be honoured in one place and silently forgotten in the other.
#
# UNDECLARED IS DELIBERATELY ABSENT AS A KEY, so `.get(polarity, "REVIEWED")`
# is the LIVE path for it rather than an unreachable fallback. An earlier draft
# mapped None -> REVIEWED explicitly and kept the same default; a mutation that
# flipped the default to READY — restoring the exact bug this module exists to
# fix — left the whole suite green, because undeclared hit the key and the
# default was dead code. A guarantee routed through a line that can never
# execute is not a guarantee, and a test cannot pin it.
VERDICT_STATE = {"approve": "READY", "fix": "CHANGES_REQUESTED",
                 "supersede": "SUPERSEDED"}
def gate_requirement(row, index=None, epoch=None):
    """What this verdict's WRITER could do. -> "none" | "required" | "unknown".

    A verdict may only be REQUIRED to carry a receipt if the code that WROTE it
    could mint one, and that is a property of the WRITER — stamped by NAME on
    the event as `gate_caps` — never of the clock and never of a version int.

    Both earlier designs failed on the same axis, and both were measured. A
    wall-time boundary cannot say whether a receipt was OBTAINABLE: the
    boundary instant passed while the feature was still unlanded, so the real
    ledger already held verdicts stamped after it that no gate verb served. An
    integer policy then invited `2` to mean "newer" and "stricter" at once, so
    a `>= 1` check would silently accept a row that never met the stricter
    rule.

    THE THIRD STATE IS THE ONE THAT MATTERS. A field PRESENT but unreadable is
    UNKNOWN, not "old writer" — demoting it would let a corrupted byte launder
    an ungated approve into READY, which is this lane's own defect wearing a
    data-corruption costume. Un-readying real work is visible and recoverable;
    silently authorizing a land is neither."""
    # PRESENCE, NOT TRUTHINESS — `.get()` collapses the very distinction this
    # function exists to make. A cross-family review found the shape by grammar-fuzzing the
    # field: a row carrying `gate_caps: null` is PRESENT and unreadable, and
    # `.get()` handed back None exactly as an ABSENT key does, so it read as
    # "old writer" and SILENCED the requirement. One null is all it would have
    # taken to launder an ungated approve into READY — the third state
    # collapsing back into the first, in the one line that defines them.
    if not isinstance(row, dict) or "gate_caps" not in row:
        # ABSENT. Legacy ONLY if this verdict was appended BEFORE the fleet
        # became gate-capable. At or after the epoch, absence is UNKNOWN — the
        # fleet demonstrably had the gate by then, so something else explains
        # the missing stamp, and the measured something-else is a reviewer
        # running ./bin/helm from a worktree that had not rebased. That
        # bypassed the requirement twice within ten minutes of it landing,
        # silently, on 9 of 12 live worktrees.
        if epoch == dispatches.EPOCH_LOST:
            # The boundary itself is unknown, so no row's position can be
            # judged against it. Loud and total: nothing reaches READY until
            # the marker is repaired. The alternative — re-deriving a cutover
            # from the current ledger — is the retroactive authorization this
            # whole mechanism exists to prevent.
            return "unknown"
        if epoch is None or not isinstance(epoch, int) or index is None \
                or not isinstance(index, int) or index < epoch:
            return "none"                   # genuinely an old writer
        return "unknown"
    caps = row["gate_caps"]
    if caps == dispatches.GATE_CAPS_UNKNOWN:
        return "unknown"
    # CLEAN HERE TOO, rather than trusting that replay already did. A guard
    # that assumes its input was sanitized upstream is a guard with a second
    # entrance, and `[1]` — a list whose MEMBERS are junk — walked straight
    # through the first version of this by passing the isinstance check.
    caps = dispatches.clean_gate_caps(caps)
    if caps == dispatches.GATE_CAPS_UNKNOWN:
        return "unknown"
    return "required" if dispatches.GATE_CAP_RECEIPT in caps else "none"


def _unknown_gate_caps_why(row):
    """The refusal sentence for requirement=="unknown", BY CAUSE.

    gate_requirement collapses two different worlds into one word, and the
    one sentence they shared named the wrong one. ABSENT means the WRITER
    stamped nothing — measured live: nine live worktrees predated the
    gate_caps writer, five of them SEAT HOMES, and `./bin/helm` resolves its
    package from its own realpath, so a reviewer inside a stale home ran the
    stale writer without knowing. 34 post-epoch verdicts carry no stamp and
    every one came from the four seats with stale homes. UNREADABLE means the
    field is there and corrupt — a genuine ledger repair.

    Telling an integrator to "repair the ledger row" when the row is intact
    and the defect is three days upstream costs a whole diagnosis; it cost
    exactly one, which is why this split exists."""
    absent = not isinstance(row, dict) or "gate_caps" not in row
    if absent:
        return ("approved by a writer that stamped no gate_caps — the ledger "
                "row is INTACT; the reviewer's helm could not gate. Almost "
                "always a stale checkout: `./bin/helm` runs the package beside "
                "it, so a seat home that has not rebased writes ungated "
                "approves silently. Check `grep -c gate_caps <checkout>/helm/"
                "dispatches.py` (0 means it cannot gate), refresh that "
                "checkout, and re-verdict from it — do not repair the row")
    return ("the verdict's gate_caps field is present but unreadable, so "
            "helm cannot say which rules this approval was written under — "
            "repair the ledger row rather than landing past it")


def _approval_refusal(row, index=None, epoch=None):
    """(why, tier_state) for one APPROVE authorization decision.

    Projection and discharge consume the SAME decision. A raw polarity string
    is not authority: the reviewer must be inside the approval tier, the
    writer's gate capability must be readable, and a required receipt must be
    bound. Keeping this in one predicate prevents discharge from retiring a
    FIX with an approval that the land path itself refuses."""
    try:
        tier_state, tier_why = dispatches.approval_tier(
            row.get("recipient") or "", repo=row.get("repo_id"))
    except Exception:                       # noqa: BLE001 — fail closed
        tier_state, tier_why = "unknown", "approval-tier check raised"
    if tier_state in ("outside", "unknown"):
        return (("approved by a reviewer the approval tier does not permit — "
                 + (tier_why or "the tier could not be evaluated")
                 + "; re-review inside the tier rather than landing past it"),
                tier_state)
    requirement = gate_requirement(row, index=index, epoch=epoch)
    if requirement == "unknown":
        return (_unknown_gate_caps_why(row), tier_state)
    bound = bool(dispatches._GATE_ID.fullmatch(str(row.get("gate") or "")))
    if requirement == "required" and not bound:
        return (("approved with no minted gate receipt — run `helm gate run` on "
                 "the reviewed tip and re-verdict with the evidence line it "
                 "prints"), tier_state)
    return None, tier_state


# --------------------------------------------------------------------------
# READY says "this may be merged", and it says it for rows a land door will
# refuse. Three ways, all MEASURED on the live ledger rather than imagined:
# an unmarked SELF-REVIEW (reviewer == author, so nobody independent looked),
# and a
# STALE BASE (the receipt was minted against a trunk that has since moved).
#
# THIS LAYER ONLY TELLS THE TRUTH. It never refuses and never downgrades the
# state word other consumers key on — STAGE_ORDER, OWED_BY and LAND_STALL_S
# all index "READY", so rewriting it here would quietly drop the row out of
# stall accounting and owed-by routing. Enforcement stays in the land door,
# which owns its own rungs; this is the display saying which one will bite.
_READY_RUNG_ORDER = ("SELF-REVIEW", "STALE-BASE")

# How many trunk commits a READY row's base may lack before the base is called
# DEAD rather than merely older. The unit is "trunk commits the gate receipt
# never saw", because that is the thing staleness actually risks: the receipt
# proved the lane's tree against ITS base, and every commit trunk gained since
# is proof the compose leg must re-carry (rebase, re-gate, the CURRENT suite).
#
# 450 IS MEASURED, NOT ROUND (re-measured against the live ledger;
# the first cut said 120 off a "busiest night = 94 commits" reading that used
# AUTHOR dates — rebases keep those, so land-time velocity was undercounted
# 3.5x, and 120 sat INSIDE the live compose queue: five approve-bearing rows
# awaiting the nightly compose measured 102..133 behind, one with a lease live
# at the moment of measurement. A bar that accuses the working audience is the
# rejected reachability rung back again). By COMMITTER date — when trunk
# actually gained the commit — the busiest day yet moved 363 and
# the busiest 6pm-to-6pm night 332. The measured population splits clean:
# active/awaiting-compose cluster <= 133 behind, unambiguous zombies >= 572
# (READY zombies 725..1372). 450 sits in that gap — ~25% margin over the
# busiest measured day, and every certain zombie flagged; a 1-day ambiguous
# row (measured 207..244) reads CURRENT and crosses the bar within a day at
# current velocity if truly abandoned. POLICY KNOB in one place: if trunk
# velocity changes materially, re-measure BOTH clusters (committer dates, not
# author dates) and move it back into the gap.
STALE_BASE_BEHIND = 450
_GATE_INDEX_MEMO = {}
def _gate_receipt_index():
    """{receipt id: record} for every minted gate receipt.

    ONE READ PER PROCESS, memoised on the ledger's (size, mtime), because the
    projection is already a ~27s pass and a per-row read would multiply
    that by the row count. The memo key is the file's own identity, so an
    append invalidates it without anyone remembering to.

    Fails to {} — an unreadable ledger must make every receipt rung render
    UNVERIFIED, never "fine". Same law as the projection's own docstring: an
    absent verification field may not default to verified."""
    try:
        from . import gate
        path = gate.receipts_path()
        st = os.stat(path)
        key = (path, st.st_size, st.st_mtime_ns)
    except Exception:
        return {}
    if _GATE_INDEX_MEMO.get("key") == key:
        return _GATE_INDEX_MEMO["index"]
    index = {}
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue          # one torn line never blinds the rest
                rid = str(rec.get("id") or "")
                if rid:
                    index[rid] = rec
    except OSError:
        return {}
    _GATE_INDEX_MEMO.clear()
    _GATE_INDEX_MEMO.update(key=key, index=index)
    return index


def ready_rung(lr, index=None):
    """The SPECIFIC rung this READY row fails, or None when all pass.

    Returns one of _READY_RUNG_ORDER, or "UNVERIFIED" when a rung cannot be
    EVALUATED (receipt absent from the ledger, trunk unreadable). UNVERIFIED is
    a real answer and never a pass: the whole defect being fixed is a word that
    implied more than it had checked, and replacing it with a different
    over-claim would be the same bug wearing a new label.

    THE RUNG LAW, and the candidate history that shaped it. A rung must
    describe something the LAND PATH will actually surface — a refusal at the
    door, a conflict at the compose leg, a re-gate against a suite the receipt
    never ran — or this surface commits the very defect it exists to cure.
    Two candidates were briefed, built, tested green and ratified, and both
    were caught only by running the surface against the LIVE board:

      RECEIPT VERSION — rejected, see the note below. The door never opens
      the receipt.

      BASE STALENESS BY REACHABILITY — rejected as MEASURED, and it reached
      trunk before it was caught. "Older than trunk" is the normal state of
      every row the moment anything else lands (76 of 96 measured). Narrowing
      it to "trunk cannot REACH the receipt's head" looked right and was
      worse: a lane's receipt attests THE LANE'S OWN TIP, and a lane tip is
      not reachable from trunk until it LANDS — so it flagged every
      properly-gated IN-FLIGHT lane, which is the entire audience this board
      serves. It rendered an approved, minutes-old, host-bound row as
      defective.

    STALE-BASE is that question asked with the RIGHT measure, and
    the difference is the whole admission case. Reachability is boolean and
    true of every in-flight lane; `base_behind` is `rev-list --count
    tip..trunk` — how many trunk commits this row's history LACKS — which
    measured <= 133 for every active or awaiting-compose lane on the busiest
    week yet (trunk at 300+ commits/day) and 725..1372 for the READY zombies
    (re-measured live). It is computed from LIVE git by the projection
    (`_base_behind`), never guessed from receipt fields, and it accuses only
    when helm's own landedness ladder already said UNLANDED: a row whose work
    reached trunk by rebase reads LANDED (patch identity, `_landing_proof`)
    long before this rung is consulted, and a row helm could not observe
    carries no `base_behind` at all — UNKNOWN never accuses. It is a REPORT,
    not a refusal: the compose leg rebases and re-gates, so the door still
    works on a dead base — what died was anyone REMEMBERING the row. Two of
    five open rows sat ready-on-a-dead-base at once, receipts internally
    perfect, every instrument reporting health (a live-audit finding), because
    nothing on this board computed behind-ness for a ready row."""
    if str(lr.get("state")) != "READY":
        return None
    author = str(lr.get("author") or "").strip()
    reviewer = str(lr.get("reviewer") or "").strip()
    if author and reviewer and author == reviewer:
        return "SELF-REVIEW"
    token = str(lr.get("gate") or "").strip()
    if not token:
        return "UNVERIFIED"           # READY with no token to check at all
    index = _gate_receipt_index() if index is None else index
    rec = index.get(token)
    if not isinstance(rec, dict):
        return "UNVERIFIED"           # the ledger cannot speak for this token
    # RECEIPT VERSION IS DELIBERATELY NOT A RUNG HERE. It reads like one -- a
    # hostless early-version receipt cannot bind to the tree it names -- and 81 of the
    # 131 live READY rows carry one. But the land door's landability check is
    # `_GATE_ID.fullmatch(row["gate"])`, a regex on the TOKEN STRING: it never
    # opens the receipt record, so it cannot refuse on version, host or
    # binding. That is BY DESIGN, and the design is sound -- binding is
    # verified at `dispatch verdict` WRITE time (the verb resolves the token
    # and REFUSES divergent ancestry, dirty trees and non-OK status), so an
    # APPROVE cannot be appended without an already-verified token and the
    # door reads a durable ledger fact rather than a caller's claim.
    #
    # So the door WOULD land all 81, which means READY does not overstate for
    # them, which means flagging them would assert a consequence that does not
    # exist -- the same misleading-agents defect this surface exists to cure,
    # pointed the other way. Receipt quality is real and belongs on a
    # receipt-quality surface, never inside a word whose whole meaning is
    # "may this be merged".
    #
    # LAST IN SEVERITY ORDER ON PURPOSE: a dead base never masks a missing
    # reviewer or an unverifiable receipt — those bite at the door, this one
    # bites at the compose leg. `base_behind` is only ever stamped by `_lr` on
    # a row the ladder already measured UNLANDED, and a row that carries none
    # (unmeasured, unobservable, or projected by older code) stays silent
    # here: UNKNOWN never accuses. bool is excluded because it IS an int and
    # a stray flag must not read as "1 commit behind".
    behind = lr.get("base_behind")
    if isinstance(behind, int) and not isinstance(behind, bool) \
            and behind >= STALE_BASE_BEHIND:
        return "STALE-BASE"
    return None


def ready_word(lr, index=None):
    """The state word a reader should SEE: plain READY only when every rung
    passes, else READY-<RUNG>. Every other state is returned untouched."""
    rung = ready_rung(lr, index=index)
    return "READY-%s" % rung if rung else str(lr.get("state"))


def base_state(lr, stale_at=None):
    """LANDED / UNLANDED-CURRENT / UNLANDED-STALE / UNKNOWN — where this row's
    WORK stands relative to the landing target, on helm's own evidence ladder
    (ancestry > patch identity; a NO from the strongest instrument is not a NO
    from the claim).

    The ladder is not re-run here — it already ran. `landed`/`merged_local`
    are `_landing_proof`'s verdict (ancestry fast path, `git cherry` patch
    identity behind it), so a row whose sha was rewritten by the integrator's
    rebase reads LANDED, never stale: a live census measured 131 of 153
    resolvable non-ancestor approves as landed-by-rebase, and a predicate that
    called those stale would send someone to re-land work already on trunk.

    UNKNOWN IS A REAL ANSWER AND EVERY BLIND PATH LANDS ON IT. A row helm
    could not observe (no repo binding, unresolvable tip, ancestry rc 128), a
    row whose behind-count could not be measured, and a row the projection
    deliberately did not measure (`base_behind` is stamped on READY rows only
    — the one state whose whole meaning is "waiting to be merged") all answer
    UNKNOWN, never a confident CURRENT and never an accusing STALE.

    `stale_at` parameterises the policy knob for callers that want their own
    bar; the default is the measured STALE_BASE_BEHIND."""
    stale_at = STALE_BASE_BEHIND if stale_at is None else stale_at
    if lr.get("landed") or lr.get("merged_local"):
        return "LANDED"
    if not lr.get("observable"):
        return "UNKNOWN"
    behind = lr.get("base_behind")
    if not isinstance(behind, int) or isinstance(behind, bool):
        return "UNKNOWN"
    return "UNLANDED-STALE" if behind >= stale_at else "UNLANDED-CURRENT"


TERMINAL = ("LANDED", "SUPERSEDED", "ABANDONED", "DELIVERED_REPORT")
_TERMINAL_ANNOTATIONS = (
    ("abandoned", "abandon_ts", "ABANDONED"),
    ("discharged", "discharge_ts", "DISCHARGED"),
    ("withdrawn", "withdraw_ts", "WITHDRAWN"),
    ("closed_by_landing", "landing_ts", "CLOSED_BY_LANDING"),
)
STAGE_ORDER = {"OPEN": 0, "AWAITING_REVIEW": 1, "AWAITING_BUILD": 1,
               "REVIEWED": 2, "CHANGES_REQUESTED": 2, "READY": 2,
               "MERGED_LOCAL": 3, "LANDED": 4, "SUPERSEDED": 4,
               "ABANDONED": 4, "DELIVERED_REPORT": 4}
# Who owes the next move. This is the field a poke/escalation actuator needs
# (an open owner ask): "stalled" alone never says whose turn it is, and
# nagging the reviewer for a fix the AUTHOR owes is how a watchdog earns its
# reputation for noise.
OWED_BY = {"OPEN": "integrator", "AWAITING_REVIEW": "reviewer",
           "AWAITING_BUILD": "builder",
           "CHANGES_REQUESTED": "author", "READY": "lander",
           "MERGED_LOCAL": "lander", "REVIEWED": "nobody (undeclared)",
           "LANDED": "nobody", "SUPERSEDED": "nobody",
           "ABANDONED": "nobody", "DELIVERED_REPORT": "nobody"}

# Trunk is main-or-master; the landing target is the upstream trunk when a
# remote publishes one, else the local trunk (the owner's local-first estate never
# pushes, so local trunk IS the landing target there).
LOCAL_TRUNK = ("refs/heads/main", "refs/heads/master")
UPSTREAM_TRUNK = ("refs/remotes/origin/main", "refs/remotes/origin/master",
                  "refs/remotes/origin/HEAD")

# The signed coordination turn a candidate land record emits, and the local
# diagnostic write-through index folded from the signer response.
LAND_TOPIC = "helm.land"
LAND_SCHEMA = "helm.land/2"        # strict replay schema (v1 had no binding)
LAND_TAG = "helm.land:b2b:"        # algorithm-tagged digest, chat/premise pattern
_RS = "\x1e"                       # ASCII record separator: fields never slide
RECEIPTS = "land-receipts.jsonl"   # durable, per-estate, survives worktree prune
_FIELD_CAP = 256                   # a lane/branch as it enters the signed record

# What the recorded evidence for one reviewed tip amounts to. REJECTED/CONFLICT
# are UNKNOWN (never landed); CONTRADICTED means a receipt claim lost even its
# local consistency with history, so live git governs.
# R_LOCAL was called R_VALID, and the NAME was doing the laundering.
# What this state actually means is "the row typechecks and its payload
# recomputes" — LOCAL SELF-CONSISTENCY. It is NOT dregg attestation: replay cannot
# ask the node what payload a turn carried. Arbitrary hex turn/receipt/chain plus
# a recomputed payload therefore passes. R_LOCAL is diagnostic only and may NEVER
# strengthen Git's observation; payload disclosure/verification is the missing
# authority boundary.
#
# R_UNREADABLE is the state that says "I COULD NOT LOOK", and it is a different
# fact from every other value here — all of which report something the index
# ACTUALLY SAID. Its absence was a defect a review reproduced with one chmod: a
# PermissionError on the receipt ledger was suppressed into an empty index, so
# `_receipt_for` found no row, answered R_NONE, and the card printed "land
# receipt none". "There is no receipt" and "helm cannot read the receipt
# ledger" rendered identically, which is the one confusion this surface exists
# to prevent. It stays diagnostic-only like the rest: Git still governs.
R_NONE, R_LOCAL, R_REJECTED, R_CONFLICT, R_CONTRADICTED, R_UNREADABLE = (
    "none", "local-unverified", "rejected", "conflict", "contradicted",
    "unreadable")
# Internal keys; reviewed tips and patch-ids are hex strings, so neither can
# collide. TWO markers because the two failures are not the same claim: a
# CORRUPT complete row is a receipt that exists and is bad (REJECTED, and it
# poisons the whole strict read); an I/O failure is no reading at all.
_RECEIPT_LEDGER_ERROR = object()
_RECEIPT_LEDGER_UNREADABLE = object()
_LEDGER_MARKERS = ((_RECEIPT_LEDGER_ERROR, R_REJECTED),
                   (_RECEIPT_LEDGER_UNREADABLE, R_UNREADABLE))


def _ledger_failure(index):
    """(state, verbatim reason) when a receipt index is a FAILURE MARKER rather
    than a lookup table, else None. One owner for both markers so a caller
    cannot handle the corruption case and quietly forget the unreadable one —
    which is precisely how the unreadable case came to render as absence."""
    for marker, state in _LEDGER_MARKERS:
        if index and marker in index:
            return state, index[marker]
    return None

# Tri-state ancestry: rc 128 / a timeout is UNDETERMINED, never a negative.
ANCESTOR, NOT_ANCESTOR, UNDETERMINED = "ancestor", "not-ancestor", "undetermined"

# EXACTLY 40 (sha1) or 64 (sha256). The old {40,64} accepted 41..63, which no git
# object name can ever be — a receipt field that admits impossible values is a
# validator that has stopped validating.
_SHA = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")      # a turn/receipt hash
_HEXID = re.compile(r"[0-9a-f]{8,64}\Z")    # a patch-id


def receipts_path():
    return home.global_dir() + "/" + RECEIPTS


def _sha(value):
    return bool(isinstance(value, str) and _SHA.fullmatch(value))


def _hex64(value):
    return bool(isinstance(value, str) and _HEX64.fullmatch(value))


def _field(value):
    """One lane/branch exactly as it enters the SIGNED record: a bounded
    printable line, or None when it is not one. Control characters (the RS
    itself included) could slide one field into another inside the digest, so
    they are never a valid receipt field. None/absent normalizes to ""."""
    if value is None:
        return ""
    if not isinstance(value, str) or len(value) > _FIELD_CAP:
        return None
    # "Cs" — LONE SURROGATES — belong here for a different reason than the rest.
    # The others could slide one field into another inside the digest; a surrogate
    # instead survives this check and then makes the payload's UTF-8 encode RAISE,
    # upstream of every fail-open, so `record_land` crashes rather than refusing.
    # A validator whose rejection path is an exception in someone else's frame is
    # not a validator.
    if any(unicodedata.category(c) in ("Cc", "Cf", "Cs", "Zl", "Zp")
           for c in value):
        return None
    return value


def land_record(lane, branch, reviewed_tip, patch_id, trunk_sha):
    """The canonical coordination record: a schema-tagged RS-join of the design's
    [lane, branch, reviewed_tip, patch_id, trunk_sha]. Schema-tagged so a future
    revision occupies a DISJOINT payload space (chat's reply/react-tag law).

    The join is injective ONLY because a field may never CONTAIN the separator:
    `_field` refuses control characters at BOTH the write and the replay
    boundary, so no receipt can ever carry an RS and no pair of fields can be
    slid into one another. The guarantee lives in that validator, not here.

    VERBATIM FIELDS, NEVER RE-CANONICALIZED: replay
    recomputes every STORED payload through this join, so a strip here judges
    historical rows under TODAY'S spelling — measured live, all 29
    lane/-prefixed signed receipts stopped rebinding. Writers canonicalize
    BEFORE signing/storing (`record_land`); replay validates each payload
    under the spelling that was actually SIGNED; read-side family identity is
    `_lane_stem`'s separate job."""
    return _RS.join([LAND_SCHEMA, str(lane or ""), str(branch or ""),
                     str(reviewed_tip or ""), str(patch_id or ""),
                     str(trunk_sha or "")])


def land_payload(lane, branch, reviewed_tip, patch_id, trunk_sha):
    """THE exact payload string a helm.land turn signs: an algorithm-tagged
    blake2b-256 digest over the canonical record (chat.digest_payload's pattern),
    78 B — inside the same frame budget chat's 73 B digest rides in.

    Signing the DIGEST is what makes the receipt a CRYPTOGRAPHIC BINDING of all
    five fields rather than a bag of trusted JSON: replay recomputes it from the
    stored fields, so a forged patch_id or trunk_sha no longer rebinds and the
    row is refused."""
    return LAND_TAG + hashlib.blake2b(
        land_record(lane, branch, reviewed_tip, patch_id, trunk_sha)
        .encode("utf-8"), digest_size=32).hexdigest()


# 600 because the live receipts sit at trunk depth 505-579 (measured
# live); a 400 cap reported every one of them UNKNOWN.
STORED_PID_SCAN_CAP = 600


def _rescue_cache_path():
    # Beside the receipts it rescues, in the same durable estate that
    # "survives worktree prune" — a cache keyed by an immutable
    # patch-id is valid forever, so it must outlive any worktree.
    return home.global_dir() + "/land-rescue.json"


def _stored_patch_index(gitdir, trunk, cap=STORED_PID_SCAN_CAP):
    """{patch-id: trunk sha} for the newest `cap` trunk commits, plus whether
    the cap was BREACHED. Built ONCE per call — the naive shape asks this
    question per receipt and pays the whole walk each time.

    MEASURED on this repo: 7.9 ms per commit, so 600 commits is
    ~4.7s. That is why the answer is cached (below) rather than recomputed:
    receipts are immutable and a landed patch stays landed, so a hit is true
    forever. Cost is paid once, by whoever renders first."""
    rv = _git(gitdir, "rev-list", trunk, "-%d" % int(cap))
    if rv is None or rv.returncode != 0:
        return None, False
    shas = [ln.strip() for ln in (rv.stdout or "").splitlines() if ln.strip()]
    index = {}
    for sha in shas:
        pid = _patch_id(gitdir, sha)
        if pid:
            index.setdefault(pid, sha)
    return index, len(shas) >= int(cap)


def _stored_patch_on_trunk(gitdir, patch_id, trunk, index=None, capped=False):
    """Did the change a receipt RECORDED land on `trunk`? -> (sha, why).

    THE CASE THIS EXISTS FOR: `_landing_proof` answers about a TIP, and both
    its rungs need that tip readable — ancestry resolves it, the patch
    comparison diffs it. Land receipts routinely outlive their tips: the
    integrator lands a rebased or cherry-picked commit, the gated object
    becomes reachable from nothing, git prunes it. Measured on this repo's own
    receipts: ALL SIX had BOTH anchors dead (reviewed_tip AND trunk_sha — 12
    of 12 objects unreadable), every rung returned unknown, and the owner's
    LANDED card read "? UNKNOWN" six times. The owner spotted it.

    The receipt carries `patch_id`, computed at WRITE time while the object
    was alive — 6 of 6 have one. It survives the pruning that killed the shas
    and is the only identity left that can answer. Measured: all six resolve,
    at trunk depths 505-579, which is why the scan bound is not small.

    A breached bound is UNKNOWN and never absent: a cap that skips commits
    cannot prove a patch is not among them."""
    pid = str(patch_id or "").strip().lower()
    if not pid or not _HEXID.fullmatch(pid):
        return None, None
    # KEYED BY REPO AND PATCH-ID, never patch-id alone: the estate
    # is global and one patch-id can exist in more than one repository, so a
    # bare-pid cache let repo B inherit repo A's foreign sha.
    ckey = "%s\t%s" % (str(gitdir or ""), pid)
    cached = (pk.read_json(_rescue_cache_path(), {}) or {}).get(ckey)
    if isinstance(cached, str) and cached:
        # A CACHED CORRELATION IS STILL RE-CHECKED against live trunk —
        # after a trunk reset a cached hit would otherwise stay "true". A patch-id is
        # immutable but TRUNK IS NOT — a reset or force-move can take the
        # correlated commit off it, and a cache that never re-asks would keep
        # naming a sha nobody can reach. One ancestry call per hit; the walk
        # is still what the cache saves.
        # A CACHED HIT MUST RE-PROVE BOTH LEGS, and the second is the one a
        # first cut missed: ancestry proves the cached sha is still
        # ON trunk, and says NOTHING about whether that sha still carries the
        # patch-id it was cached UNDER. The poisoned-cache probe, one repo:
        # key=(gitdir, pid_A) -> sha_B where B is on trunk and patch_id(B) !=
        # pid_A. Ancestry passes, and the card then tells the reader "this
        # receipt's patch-id matches B" when it does not — a cache validated
        # for LIVENESS and never for CORRECTNESS, one that checks
        # whether its value is still real but not whether it still answers the
        # question asked.
        anc = _git(gitdir, "merge-base", "--is-ancestor", cached, trunk)
        if anc is None:
            return None, "cached correlation could not be re-checked — UNKNOWN"
        if anc.returncode != 0:
            return None, ("the cached correlation %s is no longer on this "
                          "trunk — re-deriving" % cached[:12])
        got = _patch_id(gitdir, cached)
        if got is None:
            return None, ("the cached correlation %s could not be re-hashed — "
                          "UNKNOWN, not a match" % cached[:12])
        if got != pid:
            return None, ("the cached correlation %s no longer carries this "
                          "receipt's patch-id — re-deriving" % cached[:12])
        return cached, "content identity (the receipt's stored patch-id)"
    if index is None:
        return None, None
    sha = index.get(pid)
    if sha:
        try:
            c = pk.read_json(_rescue_cache_path(), {}) or {}
            c[ckey] = sha
            pk.write_json(_rescue_cache_path(), c)
        except Exception:
            pass          # a cold cache is slow, never wrong
        return sha, "content identity (the receipt's stored patch-id)"
    if capped:
        return None, ("absent from the newest %d trunk commits, but the scan "
                      "was CAPPED there — UNKNOWN, not absent"
                      % int(STORED_PID_SCAN_CAP))
    return None, None


def _chain_root_index():
    """{reviewed_tip -> chain_root} for every LR row that declares one, or {}.

    READ FROM THE RAW SNAPSHOT, NEVER THE PROJECTION. chain_root is a RAW
    LEDGER FIELD that `_lr` copies through untouched, so the full projection
    is pure waste here — and it is not cheap waste. Measured on this ledger:
    `project_raw()` 26.4s vs `dispatches.snapshot()` 0.034s, and this function
    sits on the dashboard's polling path behind a 12s client deadline. The
    first cut used project_raw and turned an 0.89s card into a 24.8s one — it
    would have replaced a repetitive card with a card that never renders.

    WHY DERIVED AND NOT STORED: a land receipt is a SIGNED historical claim,
    and its payload is the thing the signature covers. Adding a field to it
    would change what gets signed, would only ever help receipts minted after
    the change, and would leave every existing receipt — the ones on the
    owner's card today — without identity. The projection already knows which
    rows share a chain, so the join answers for receipts already written and
    touches nothing signed.

    FAILS OPEN, ALWAYS, AND THAT IS THE POINT. Every failure returns {}, which
    makes each receipt its own group downstream: the card renders repetitively
    and shows every land. The alternative failure — guessing identity from a
    lane LABEL — merged two unrelated lands and labelled one "1 earlier round",
    which DELETES a landing from the owner's view (the refuted
    first cut did exactly that). Repetition is a cost; invisibility is a lie. This index is
    never allowed to be the reason the card cannot render."""
    try:
        from . import dispatches
        snap, unavailable = dispatches.snapshot()
    except Exception:
        return {}
    if unavailable or not snap:
        return {}
    seen = {}
    for row in snap.values():
        if not isinstance(row, dict):
            continue
        root = row.get("chain_root")
        if not isinstance(root, str) or not root:
            continue
        # A row that declares no chain is NOT a chain of one — it gets no
        # entry, so the receipt falls through to its own group. Only a
        # DECLARED root ever groups anything.
        # `ref` IS TWO DIFFERENT THINGS AND THE ROW DOES NOT SAY WHICH: on a
        # --kind build it is the BASE the work was dispatched FROM, on a
        # --kind review it is the reviewed TIP. Indexing it blindly made every
        # BUILD row dispatched from trunk claim that trunk sha as its chain's
        # tip — which is why one real landing's tip was claimed by FIVE
        # roots: its own review row plus four unrelated lanes that merely
        # STARTED from it. Filtering by kind takes that tip from 5 roots to 1
        # and the ledger's ambiguous tips from 19 to 8.
        fields = ("reviewed_tip", "ref") if row.get("kind") == "review" \
            else ("reviewed_tip",)
        for field in fields:
            tip = row.get(field)
            if isinstance(tip, str) and len(tip) >= 12:
                seen.setdefault(tip.strip().lower(), set()).add(root)
    # AMBIGUOUS IS ABSENT, NOT FIRST-WINS, and this stays as defence
    # in depth even after the kind filter above: 8 tips remain declared under
    # more than one root for reasons the kind filter cannot explain away. The first cut took whichever root the snapshot dict
    # happened to yield first, so flipping two rows' insertion order flipped
    # the answer: arbitrary, not correctness. A receipt at such a tip would be
    # attributed to an unrelated chain and FOLDED INTO A LAND IT DOES NOT
    # BELONG TO — the same "one landing rendered as a round of another" failure
    # already refuted in the renderer, reappearing one layer down in the
    # producer. An ambiguous root is not a DECLARED root, so it answers None
    # and the receipt renders alone.
    return {tip: roots.pop() for tip, roots in seen.items() if len(roots) == 1}


def verified_lands(limit=8):
    """(rows, unavailable) — recorded landings, each RE-DERIVED against trunk NOW.

    THE OWNER-VERIFICATION SURFACE (a design-council mandate: "give the owner
    ONE end-to-end verification he can do himself"). Every other land signal is an
    agent's claim rendered back — an agent says it landed, the row says LANDED,
    and the reader is still taking our word one layer down.

    PROOF IS CONTENT, NEVER THE MERGE COMMIT. A receipt's `trunk_sha` is the
    LOCAL trunk at land time, and local trunk is ephemeral by construction:
    local main is re-pointed at origin/main after each push, the old merge
    commit becomes unreachable, and git collects it. Measured: 0 of
    6 receipt trunk_shas and 0 of 4 ledger landing_trunk_shas still named a
    live object, with ZERO forced-updates in 133 origin/main reflog entries —
    nothing was rewritten, those commits simply never belonged to the trunk
    everyone reads. So the reviewed tip's CONTENT is the question, answered by
    the same `_landing_proof` ladder the projection uses (ancestry, else patch
    identity), which survives the rebases and squashes a land window performs.

      on_trunk True   the reviewed change is on that trunk NOW (`how` says
                      whether by ancestry or by patch identity)
      on_trunk False  proven absent — a real state, not an error
      on_trunk None   git could not answer: UNKNOWN, and it says so

    `trunk_ref` names WHICH trunk answered, because "landed" without it is the
    local-vs-origin confusion this module's own law forbids. `trunk_sha` stays
    on the row as PROVENANCE — what the writer saw — and never as the proof.
    """
    rows, unavailable = _receipt_rows()
    if unavailable:
        return [], unavailable
    cache, out, _pid_index = {}, [], {}
    # ONE projection read for the whole card, not one per receipt: project_raw
    # walks the entire ledger (486 rows live), and this function is on the
    # dashboard's polling path.
    chain_idx = _chain_root_index()
    for row in reversed(rows):
        if not isinstance(row, dict) or row.get("topic") != "helm.land":
            continue
        gitdir = row.get("repo_id")
        tip = str(row.get("reviewed_tip") or "").strip().lower()
        local, upstream = _trunk_refs(gitdir, cache)
        origin = _origin_configured(gitdir)
        trunk = upstream if origin else local
        proof = _landing_proof(gitdir, tip, trunk) if trunk else None
        # THE RESCUE. Only when the tip-based ladder could not answer — never
        # to override an `absent`, which is a real proven state, and never in
        # front of ancestry, which is cheaper and decisive.
        rescued_sha, rescue_why = (None, None)
        # NO ID, NO WALK: a row whose patch_id is absent or malformed
        # can never be answered by the index, and building 600 commits' worth
        # of patch-ids to discover that is pure waste on every read.
        _pid = str(row.get("patch_id") or "").strip().lower()
        _usable = bool(_pid) and bool(_HEXID.fullmatch(_pid))
        if proof in (None, "unknown") and trunk and _usable:
            key = (gitdir, trunk)
            if key not in _pid_index:
                _pid_index[key] = (None, False)     # built lazily, once
            rescued_sha, rescue_why = _stored_patch_on_trunk(
                gitdir, row.get("patch_id"), trunk, *_pid_index[key])
            if rescued_sha is None and rescue_why is None:
                if _pid_index[key][0] is None:
                    _pid_index[key] = _stored_patch_index(gitdir, trunk)
                    rescued_sha, rescue_why = _stored_patch_on_trunk(
                        gitdir, row.get("patch_id"), trunk, *_pid_index[key])
        out.append({
            "rescued_sha": rescued_sha, "rescue_why": rescue_why,
            "lane": row.get("lane"), "reviewed_tip": tip or None,
            "trunk_sha": str(row.get("trunk_sha") or "") or None,
            "ts": row.get("ts"), "trunk_ref": trunk,
            # on_trunk IS A GIT VERDICT AND STAYS ONE. A forged-receipt
            # repro: a hand-built accepted receipt carrying a dead tip plus ANY
            # unrelated live patch-id returned on_trunk=true — the receipt
            # asserting its OWN landing. helm/landreq.py:40 has always said
            # the stored patch_id is "correlation evidence" and that
            # "lifecycle landedness derives INDEPENDENTLY FROM LIVE GIT"; the
            # first cut of this rescue made correlation into authority, eight
            # lines below the sentence forbidding it.
            "on_trunk": True if proof in ("ancestor", "patch-equivalent")
            else (False if proof == "absent" else None),
            "how": proof if proof in ("ancestor", "patch-equivalent") else None,
            # THE THIRD STATE. Not proof, not silence: the receipt's own
            # stored id CORRELATES to a live trunk commit, named, and the
            # surface renders it distinctly from a Git-proven land. This is
            # what the owner needed — six bare "? UNKNOWN" with no reason —
            # and it is sayable without lying about who proved it.
            "correlates_to": rescued_sha, "correlation_why": rescue_why,
            # CHAIN IDENTITY, so the card can fold a lane's ROUNDS into one
            # LAND without inferring identity from a lane LABEL. None means
            # "this receipt's row declared no chain" — the surface must then
            # render it alone, never merge it with a neighbour that happens to
            # share a name and a batch trunk.
            "chain_root": chain_idx.get(tip) if tip else None,
        })
        if len(out) >= max(1, int(limit)):
            break
    return out, None


def _validate_receipt(row, tip):
    """STRICT-SCHEMA replay of one index row -> (row, None) or (None, reason).

    Nothing is trusted: the schema and the exact topic must match, reviewed_tip
    must be a full sha bound to both the row's own key and the tip being asked
    about, trunk_sha (THE authority anchor) must be a full sha, patch_id must be
    a hex object name or absent, lane/branch must be bounded printable lines,
    the transport must carry a COMMITTED signature shape (hex64 turn + hex64
    receipt + a non-negative int chain index — `type is int` so a bool is never
    a chain index), and the stored payload must REBIND all five fields."""
    if not isinstance(row, dict):
        return None, "row is not an object"
    if row.get("schema") != LAND_SCHEMA:
        return None, "schema is not %s" % LAND_SCHEMA
    if row.get("topic") != LAND_TOPIC:
        return None, "topic is not %s" % LAND_TOPIC
    reviewed = row.get("reviewed_tip")
    if not _sha(reviewed):
        return None, "reviewed_tip is not a full sha"
    if reviewed != row.get("id") or (tip is not None and reviewed != tip):
        return None, "reviewed_tip is not bound to this row's index key"
    trunk = row.get("trunk_sha")
    if not _sha(trunk):
        return None, "trunk_sha (the authority anchor) is not a full sha"
    patch = row.get("patch_id")
    if patch is not None and not (isinstance(patch, str)
                                 and _HEXID.fullmatch(patch)):
        return None, "patch_id is not a hex object name"
    lane, branch = _field(row.get("lane")), _field(row.get("branch"))
    if lane is None or branch is None:
        return None, "lane/branch is not one bounded printable line"
    if not (_hex64(row.get("turn")) and _hex64(row.get("receipt"))
            and type(row.get("chain")) is int and row["chain"] >= 0):
        return None, "no committed signing receipt (turn/receipt/chain)"
    if row.get("payload") != land_payload(lane, branch, reviewed, patch, trunk):
        return None, "payload does not rebind these fields"
    return row, None


def _receipt_rows():
    """(rows, failure marker) — ONE strict read of the durable receipt index.

    A read that failed answers a marker dict naming WHICH failure and carrying
    the reason verbatim, never an empty table. An empty table is a claim — "no
    receipts are recorded" — and only a read that SUCCEEDED may make it."""
    from . import eventledger
    rows, unavailable = eventledger.checked_events(receipts_path(), strict=True)
    if not unavailable:
        return rows, None
    corrupt = unavailable.startswith(eventledger.CORRUPT_PREFIX)
    return [], {(_RECEIPT_LEDGER_ERROR if corrupt
                 else _RECEIPT_LEDGER_UNREADABLE): unavailable}


def _receipts_by_tip():
    """{reviewed_tip: [every row for it]} from the durable index — deliberately
    NOT last-wins: two rows binding different lands to one tip are a CONFLICT,
    and a conflict must never silently resolve to whichever was appended last.

    Storage that could not be read remains the fail-open Git floor for the
    LIFECYCLE — receipts never gate landing — but it is no longer silent: it
    returns the UNREADABLE marker, so the diagnostic reads "could not look"
    rather than "nothing there". A malformed COMPLETE physical row is a third
    thing again: strict replay preserves that corruption as a poison marker, so
    a good sibling cannot be cherry-picked while the bad one silently
    disappears. An unterminated tail is not durable and is ignored by the one
    event-ledger parser.
    """
    rows, failed = _receipt_rows()
    if failed:
        return failed
    out = {}
    for row in rows:
        out.setdefault(str(row["id"]), []).append(row)
    return out


def _receipts_by_patch():
    """{patch_id: [rows]} — the SAME receipts, indexed by CONTENT identity.

    THE WHOLE POINT. A reviewed tip is content-addressed over HISTORY, so a
    rewrite (filter-repo, squash-root, rebased trunk) changes every recorded
    tip at once and every receipt dangles. A patch-id hashes the DIFF, so the
    same change recomputes to the same id on the other side of a rewrite.

    `_patch_id` has computed that stable identity since the rebase-instability
    fix, and `land_record` has stored it on every receipt — but the index was
    keyed by tip alone, so the durable identity sat beside the fragile one and
    was never the thing looked up. That is the gap: not a missing primitive, a
    primitive wired as evidence instead of as a key.

    Same not-last-wins discipline as _receipts_by_tip: two receipts sharing a
    patch-id are a CONFLICT to be surfaced, never silently collapsed."""
    rows, failed = _receipt_rows()
    if failed:
        return failed
    out = {}
    for row in rows:
        pid = str(row.get("patch_id") or "")
        if pid:
            out.setdefault(pid, []).append(row)
    return out


def receipt_for_content(gitdir, tip, trunk_ref=None, by_tip=None, by_patch=None):
    """(state, row, reason, how) — resolve a receipt by TIP, else by CONTENT.

    `how` is "tip" | "patch" | None, so a caller can report which identity
    carried the proof rather than implying the tip still resolves.

    The rewrite case in one sentence: after history is rewritten the tip
    recorded on the receipt is gone, but the CHANGE is still on trunk under a
    new sha — computing that new tip's patch-id finds the receipt recorded
    under the old one. No commit-map required, no migration required; the
    proof survives because it was keyed by what it actually attested.

    Fail-open exactly like the tip path: no patch-id (git unavailable, an
    empty range) simply yields the tip verdict unchanged. A missing stable
    identity must never turn a found receipt into a refusal."""
    by_tip = _receipts_by_tip() if by_tip is None else by_tip
    state, row, why = _receipt_for(tip, by_tip)
    if state != R_NONE:
        return state, row, why, "tip"
    pid = _patch_id(gitdir, tip, trunk_ref)
    if not pid:
        return state, row, why, None
    by_patch = _receipts_by_patch() if by_patch is None else by_patch
    failure = _ledger_failure(by_patch)
    if failure:
        return failure[0], None, failure[1], None
    rows = (by_patch or {}).get(pid) or []
    if not rows:
        return state, row, why, None
    # Validate against the tip each row RECORDED, not the one we were handed —
    # they legitimately differ across a rewrite, and that difference is the
    # entire reason this path exists.
    valid, reasons = [], []
    for r in rows:
        rec, bad = _validate_receipt(r, str(r.get("id") or ""))
        (valid if rec else reasons).append(rec or bad)
    if not valid:
        return R_REJECTED, None, reasons[0], "patch"
    if len({str(r.get("id")) for r in rows}) > 1:
        return R_CONFLICT, None, (
            "two receipts share patch-id %s under different reviewed tips" % pid
        ), "patch"
    return R_LOCAL, valid[-1], None, "patch"


def _receipt_for(tip, by_tip):
    """(state, validated row, reason) — the recorded evidence for one tip.

    A tip with no row is R_NONE (git governs, the fail-open floor) — and that
    answer is reserved for an index that was actually READ. Storage that could
    not be read is R_UNREADABLE, because "no receipt" is a finding and this is
    the absence of one. A corrupt complete physical row poisons strict replay as
    R_REJECTED. Every logical row invalid is likewise R_REJECTED. Rows that
    disagree — different bindings for one tip, or a valid row contaminated by an
    invalid sibling — are R_CONFLICT. Only a unanimous, fully-rebound set is
    R_LOCAL — locally consistent, never chain-verified and therefore never state
    authority."""
    failure = _ledger_failure(by_tip)
    if failure:
        return failure[0], None, failure[1]
    rows = ((by_tip or {}).get(tip) or []) if tip else []
    if not rows:
        return R_NONE, None, None
    valid, reasons = [], []
    for row in rows:
        rec, why = _validate_receipt(row, tip)
        (valid if rec else reasons).append(rec or why)
    if not valid:
        return R_REJECTED, None, reasons[0]
    if len({r["payload"] for r in valid}) > 1:
        return R_CONFLICT, None, (
            "%d receipts bind DIFFERENT lands to this tip" % len(valid))
    if reasons:
        return R_CONFLICT, None, (
            "the index holds both a valid and an invalid receipt for this tip "
            "(%s)" % reasons[0])
    return R_LOCAL, valid[-1], None


def _patch_id(gitdir, tip, trunk_ref=None):
    """git patch-id of the landed commits — a rebase/ff-STABLE content identity
    for the receipt (a rebased land recomputes to the same id). Best-effort: the
    range from the branch's fork point (merge-base with trunk) to the reviewed
    tip, falling back to the tip commit's own diff when that range is empty (the
    tip already sits on trunk after a fast-forward). Fail-open None — a receipt
    is keyed by the reviewed tip, so a missing patch-id never gates it."""
    if not gitdir or not tip:
        return None
    base = None
    if trunk_ref:
        mb = _git(gitdir, "merge-base", tip, trunk_ref)
        if mb is not None and mb.returncode == 0 and mb.stdout.strip():
            base = mb.stdout.strip()
    rng = base + ".." + tip if base and base != tip else tip + "^.." + tip
    # same pin as _range_patch_id, same measurement: this id is SIGNED into
    # land receipts that cross boxes, so the diff shape cannot float on the
    # minting box's diff.noprefix
    d = _git(gitdir, "-c", "diff.noprefix=false", "diff", rng)
    text = d.stdout if d is not None and d.returncode == 0 else ""
    if not text:
        return None
    p = _git(gitdir, "patch-id", "--stable", input_text=text)
    parts = p.stdout.split() if p is not None else []
    return parts[0] if p is not None and p.returncode == 0 and parts else None


_DEL_NAMED = 3     # deleted paths NAMED in the refusal; "+N more" follows
_DEL_CLIP = 56     # per-path byte clip — measured with the flag/cure line


def _tracked_deletions(gitdir, trunk, tip):
    """(paths, err) — TRACKED files that `trunk..tip` deletes.

    Only tracked-path deletions from the tip's merge range against trunk.
    The diff asks: after merging this tip onto trunk, which entries in the
    index are deleted? UNKNOWN fails safe: any git failure, no trunk, or
    unparseable output returns a non-None err, and the caller REFUSES the
    land rather than allowing an unscannable deletion.
    """
    if not gitdir or not trunk or not tip:
        return None, "cannot compute deletion set — missing repo/trunk/tip"
    tr = _trunk_sha(gitdir, trunk)
    if not tr:
        return None, "cannot compute deletion set — trunk unresolvable"
    # Compare against the fork point, not the current trunk tip. The land
    # command fires AFTER a merge, at which point trunk has already moved past
    # the reviewed tip — diff(trunk, tip) would see the REVERSE direction.
    # diff(merge_base, tip) names what the tip DELETES relative to trunk at
    # the moment it forked, which is the actual deletion set that would enter
    # trunk on merge.
    mb = _git(gitdir, "merge-base", tr, tip)
    base = mb.stdout.strip() if mb is not None and mb.returncode == 0 else tr
    d = _git(gitdir, "diff", "--diff-filter=D", "--name-only", "-z", base, tip)
    if d is None or d.returncode != 0:
        return None, ("cannot compute deletion set — git diff failed: %s"
                      % (d.stderr.strip() if d else "no output"))
    tracked = _git(gitdir, "ls-files", "-z")
    if tracked is None or tracked.returncode != 0:
        return None, ("cannot compute deletion set — git ls-files failed: %s"
                      % (tracked.stderr.strip() if tracked else "no output"))
    tset = set(f for f in tracked.stdout.split("\0") if f)
    deleted = [f for f in d.stdout.split("\0") if f and f in tset]
    return deleted, None


def _deletion_refusal(paths):
    """The refusal text — names up to N paths, then '+K more TRACKED files'.
    Names the flag and states WHY deletion is different from an ordinary land:
    deleting from a remote is hard to unwind, so it needs a deliberate ack."""
    n = len(paths)
    named = paths[:_DEL_NAMED]
    more = ""
    if n > _DEL_NAMED:
        more = " — +%d more TRACKED file%s" % (n - _DEL_NAMED,
                                                "s"[:n - _DEL_NAMED != 1])
    lines = ["helm lr land: REFUSED — this merge deletes %d TRACKED file%s%s"
             % (n, "s"[:n != 1], more)]
    for p in named:
        lines.append("  %s" % (p if len(p) <= _DEL_CLIP else p[:_DEL_CLIP] + "..."))
    lines.append("deleting from a remote is hard to undo; pass --ack-deletions "
                 "to confirm you are authorized")
    return "\n".join(lines)


def _trunk_sha(gitdir, trunk_ref=None):
    ref = trunk_ref or _resolve_ref(gitdir, LOCAL_TRUNK)
    if not ref:
        return None
    p = _git(gitdir, "rev-parse", "--verify", "--quiet", ref)
    return p.stdout.strip() if p is not None and p.returncode == 0 else None


def record_land(lane, branch, reviewed_tip, patch_id, trunk_sha,
                repo_id=None, has_upstream=False, upstream=False,
                profile=None):
    """Record a candidate land event: emit a signed, chain-ordered helm.land
    coordination turn over the payload digest (chat's topic-parameterized
    signed-emit path, fee-free), then write through the returned
    {turn, receipt, chain} into the durable local index. Returns
    (receipt-record, None) on a recorded signed land, or (None, reason) — the
    FAIL-OPEN signal.

    The ACTUAL trunk sha is mandatory because it identifies the historical land
    event the coordination turn claimed. What is written always replays: the
    finished record is put through `_validate_receipt` before it is appended.
    Replay can prove local field/payload consistency but, until dregg discloses a
    turn's payload, cannot prove that the stored payload is what the turn signed;
    the local index therefore annotates observation and never strengthens it.

    FAIL-OPEN is total: with no signer/node (the production default — signing is
    opt-in) NOTHING is written and landing stays EXACTLY today's git-observed
    behavior. This never raises and never blocks a land."""
    from . import chat, eventledger
    lane, branch = _field(lane), _field(branch)
    if lane is None or branch is None:
        return None, ("lane/branch must each be one printable line of at most "
                      "%d characters" % _FIELD_CAP)
    lane = _strip_lane_prefix(lane) or lane
    tip = str(reviewed_tip or "").strip().lower()
    trunk = str(trunk_sha or "").strip().lower()
    patch = str(patch_id).strip().lower() if patch_id else None
    if not _sha(tip):
        return None, "a land receipt needs the reviewed tip's FULL sha to key on"
    if not _sha(trunk):
        return None, ("a land receipt needs the ACTUAL trunk sha it claims to "
                      "have landed onto — the historical event identity")
    if patch is not None and not _HEXID.fullmatch(patch):
        return None, "patch_id must be a hex object name"
    payload = land_payload(lane, branch, tip, patch, trunk)
    info, failure = chat.emit_coordination_turn(LAND_TOPIC, payload, profile)
    if not info:
        return None, (failure or {}).get("reason") or "no signed land receipt"
    rec = {"id": tip, "schema": LAND_SCHEMA, "topic": LAND_TOPIC,
           "payload": payload, "reviewed_tip": tip, "lane": lane,
           "branch": branch, "patch_id": patch, "trunk_sha": trunk,
           "repo_id": repo_id, "has_upstream": bool(has_upstream),
           "upstream": bool(upstream), "turn": info.get("turn_hash"),
           "receipt": info.get("receipt_hash"), "chain": info.get("chain_index"),
           "profile": profile or "", "ts": pk.now_ts()}
    ok, why = _validate_receipt(rec, tip)
    if not ok:
        return None, "refusing to record a receipt that fails its own replay: " + why
    if not eventledger.append(receipts_path(), rec):
        return None, ("land turn signed (chain %s) but the local index write "
                      "failed" % rec["chain"])
    return rec, None


def _git(gitdir, *args, input_text=None):
    # A REPO BINDING THAT IS NOT A STRING IS NOT A PATH. `repo_id` is copied
    # verbatim out of the ledger row and never re-validated, so a junk value
    # arrives here as itself — and subprocess raises TypeError on an argv
    # member that is not a string, which is not one of the failures this
    # function's callers are told to expect. Not-a-path answers exactly the
    # way an absent path does: there is no repository here to ask.
    if not isinstance(gitdir, str) or not gitdir:
        return None
    # ONE ANSWER PER QUESTION PER PROJECTION. Inside `projscope.scope()` the
    # same argv against the same repository is spawned once and every later
    # asker gets that answer; outside a scope this is byte-identical to the
    # bare spawn below and nothing is remembered. Measured over the
    # live ledger: 4140 spawns in one `helm lr list --all`, 650 of them exact
    # repeats (one `merge-base --is-ancestor` asked 48 times). The saving is
    # the smaller half of why this is here — the larger half is that a
    # projection which asks git the SAME question twice can be told two
    # different things by a trunk that moved between them, and then publishes
    # both under one read stamp.
    return projscope.memo(("landreq._git", gitdir, args, input_text),
                          lambda: _git_spawn(gitdir, args, input_text))


def _git_spawn(gitdir, args, input_text):
    """The bare spawn. Split out ONLY so the memo above has something to call
    — every existing test that patches `landreq._git` still replaces the whole
    function, memo included, and sees no change."""
    try:
        return subprocess.run(["git", "--git-dir", gitdir, *args],
                              input=input_text, capture_output=True, text=True,
                              timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None


def _resolve_ref(gitdir, names):
    for name in names:
        p = _git(gitdir, "rev-parse", "--verify", "--quiet", name)
        if p is not None and p.returncode == 0 and p.stdout.strip():
            return name
    return None


def _ancestry(gitdir, commit, ref):
    """TRI-STATE ancestry — the fix for a two-valued answer to a three-valued
    question. Now ALSO shared on the seam as vcs `ancestry` (ANCESTOR /
    NOT_ANCESTOR / vcs.UNKNOWN, `git -C <checkout>` substrate); this private
    copy stays only for the declared `--git-dir` exception (vcs.py header) —
    unify by teaching the seam a gitdir target.
    `git merge-base --is-ancestor` exits 0 for an ancestor, 1 for a
    genuine non-ancestor, and 128 (bad/missing object, corrupt repo) for a
    question it could not answer; a timeout/OSError is equally UNDETERMINED.
    Folding 128/timeout into "not an ancestor" asserts a negative on an unknown
    and manufactures false READY/stalled rows; unknown must stay unobservable."""
    if not gitdir or not commit or not ref:
        return UNDETERMINED
    p = _git(gitdir, "merge-base", "--is-ancestor", commit, ref)
    if p is None:
        return UNDETERMINED
    if p.returncode == 0:
        return ANCESTOR
    return NOT_ANCESTOR if p.returncode == 1 else UNDETERMINED


def _is_ancestor(gitdir, tip, ref):
    """Boolean fast-path projection for patch-identity landedness."""
    return _ancestry(gitdir, tip, ref) == ANCESTOR


def _vanished_proof(gitdir, tip):
    """`absent` when NO REACHABLE SOURCE HOLDS this object; else `unknown`.

    THE LOGIC ERROR THIS RETIRES RAN BACKWARDS. A git object that does not
    exist used to read `unknown`, and the subsumed door requires `absent` — so
    the STRONGEST evidence of absence was scored WEAKER than an ordinary
    negative. helm's two oldest rows (4fdd32fcf664 at 7.9d, b970911edbe6 at
    7.8d, audited 2026-08-03) passed every other clause — chain matches,
    cross-family holds, and their cure is a proven ancestor of
    main — and were permanently unclosable on that one technicality.

    *** AND THE OBVIOUS FIX IS THE SAME BUG POINTING THE OTHER WAY. *** A
    LOCAL MISS IS NOT ABSENCE. The object can live on a remote, in another
    clone, or in a reflog nobody consulted; flipping on a local miss
    manufactures a confident false `absent` and starts closing rows whose work
    never landed, which is strictly worse than refusing. So the precondition
    IS the fix: every reachable source is consulted, and only unanimous
    silence converts.

    A FAILED PROBE IS NOT A NEGATIVE RESULT. If any lookup ERRORS — network
    down, remote unreachable, reflog unreadable — this returns `unknown`, not
    `absent`. That is the rule the fleet re-derived on five separate surfaces:
    a check that could not look must never answer as if it had.

    WHAT THIS PROVES, AND WHAT IT DOES NOT. It proves no source we can reach
    holds the object. It does NOT prove the change never landed: without the
    object there is no patch-id to compare, so a landed-under-a-rewrite tip
    is indistinguishable from one that never landed. That residual is bounded
    by the door this feeds, not by this function — `subsumed` independently
    requires a cross-family APPROVE whose reviewed tip IS proven on trunk, so
    the work being answered is established by the confirmation rather than by
    this absence. A future caller that wants `absent` WITHOUT that corroborating
    clause must not reuse this result as if it carried one."""
    if not _sha(tip):
        return "unknown"      # not a full sha: nothing to look an object up by
    sha = tip.lower()
    # REFUTATION PASS. Each probe can only ever DISPROVE absence — finding the
    # object somewhere. None of them can confirm it, which is why unanimous
    # silence is the bar rather than any single positive.
    remote = _git(gitdir, "ls-remote", "origin")
    if remote is None or remote.returncode != 0:
        return "unknown"      # could not ask the remote at all
    if sha in remote.stdout.lower():
        return "unknown"      # the remote publishes it: it is not vanished
    reflog = _git(gitdir, "reflog", "--all", "--no-abbrev",
                  "--format=%H %gd %gs")
    if reflog is None or reflog.returncode != 0:
        return "unknown"      # could not read the reflogs
    if sha in reflog.stdout.lower():
        return "unknown"      # it existed here once; the object DB lost it,
                              # which is a repair job, not an absence
    return "absent"


def _landing_proof(gitdir, tip, ref):
    """How the CHANGE in `tip` relates to `ref`.

    Returns exactly one of ``ancestor``, ``patch-equivalent``, ``absent``, or
    ``unknown``. Compatibility helper `_landed` projects that richer result back
    to the historical True / False / None contract.

    Ancestry answers "is this exact object reachable", which is a different
    question from "did this work land". Lands here are routinely
    CHERRY-PICKED: the integrator's stated rule is to land the single gated
    commit onto current trunk rather than merge the branch, because landing
    less than was gated is safe and landing more never is. That gives the
    landed change a NEW sha, and the gated one stays reachable from nothing.

    So an ancestry-only test reports a landed loop as still awaiting its
    land — forever, because nothing later makes the old sha reachable — and
    the stall surface fills with zombies. That is the same failure as
    deriving READY from "a verdict exists", one state further along: a
    terminal fact inferred from a proxy that does not carry it.

    Measured on this repo's own history: one land (a capture-grammar delim
    fix, on trunk as a cherry-picked sha) was gated at a pre-land lane tip
    that no later rebase
    kept. Ancestry on the gated sha said NOT LANDED while the patch was
    demonstrably on trunk, and `helm lr` showed the loop READY.

    Ancestry stays the FAST path — when it is true it is decisive and costs
    one call. A negative OR unknown falls through to the independent patch-id
    comparison: an exact '-' proves present and '+' proves absent. If that
    fallback is also unreadable, the result stays unknown.

    MEASURED, min of 5 warm runs, 1281 commits on main. Ancestry
    costs 1.2ms. The cherry fallback costs 3.4ms for a tip 10 commits from
    trunk, and 44.1ms for one 577 commits out.

    THE METHOD IS STATED BECAUSE THE NUMBERS DEPEND ON IT. A cold or averaged
    run reads several times higher. The previous wording gave figures with no
    method, so nobody could reproduce them or tell when they aged.
    Treat every absolute ms here as an order of magnitude.

    MEASURED, same repo and same run: 3.4ms at distance 10 against 44.1ms at
    577 is a 13x spread. That spread is the cost model, and it survives any
    method. Cost scales with the tip's DISTANCE from trunk, not with repo size,
    because cherry computes patch ids from the merge-base forward. Distance is
    the axis to watch if this ever gets slow. The earlier prose predicted that
    axis but never showed it.

    MEASURED at the same time: 69 of 521 rows were non-terminal, and only those
    reach the fallback. That is well under a second across a full projection
    which already shells out per row. No cache is warranted.

    The read is PER COMMIT, not per range: `git cherry` lists every commit in
    the range and its first line is the OLDEST, so asking it about a stack
    and believing line one answers about a DIFFERENT commit. This scans for
    the line whose sha matches the one being asked about. An absent line, a
    changed format or an empty output are UNKNOWN — unrecognised output must
    never become either a silent land or a false READY/stall.

    Patch-id equality is the claim: Git compares normalized patch content, not
    author metadata or integration method. Rebase and cherry-pick can therefore
    prove the same change under a different object id. A commit that landed and
    was later REVERTED still reads landed, correctly: the work
    landed and that obligation was discharged; the undo is a different loop
    with a different reviewed tip.
    """
    ancestry = _ancestry(gitdir, tip, ref)
    if ancestry == ANCESTOR:
        return "ancestor"
    if ancestry == NOT_ANCESTOR and _sha(tip):
        sha = tip
    else:
        full = _git(gitdir, "rev-parse", "--verify", "--quiet",
                    tip + "^{commit}")
        if full is None:
            return "unknown"      # git itself did not run: no reading at all
        if full.returncode != 0 or not full.stdout.strip():
            return _vanished_proof(gitdir, tip)
        sha = full.stdout.strip()
    # `git cherry <upstream> <head>`: '-' marks a commit whose patch already
    # exists upstream under another sha, '+' one that does not.
    p = _git(gitdir, "cherry", ref, sha)
    if p is None or p.returncode != 0:
        return "unknown"
    for line in p.stdout.splitlines():
        parts = line.split()
        if len(parts) != 2 or parts[1] != sha:
            continue
        if parts[0] == "-":
            return "patch-equivalent"
        if parts[0] == "+":
            return "absent"
        return "unknown"
    return "unknown"


def _landed(gitdir, tip, ref):
    proof = _landing_proof(gitdir, tip, ref)
    if proof in ("ancestor", "patch-equivalent"):
        return True
    return False if proof == "absent" else None


def _object_exists(gitdir, sha):
    """True / False / None — does `sha` still name an object in this repo?

    BARE full-sha form PINNED, never the `^{commit}` peel: the peel form exits
    128 on a missing object, indistinguishable from a corrupt repo, while the
    bare form answers rc 0 present / rc 1 missing / anything else UNKNOWN.
    UNKNOWN (spawn failure, timeout, rc ∉ {0,1}) is None and every caller of
    this in the close ladders FAILS CLOSED on it."""
    if not gitdir or not sha:
        return None
    p = _git(gitdir, "cat-file", "-e", sha)
    if p is None:
        return None
    if p.returncode == 0:
        return True
    return False if p.returncode == 1 else None


# Stems for the LANE FAMILY probe (interlock law: lane-names-are-a-family —
# probe the stem). A lane is renamed across rounds (`-r2`), rescued
# (`rescue/`), re-cut (`prerebase/`), or carried in a worktree dir (`wt-…`),
# and a probe that matches only the exact name UNDER-matches — the dangerous
# direction for a block-only check.
_STEM_PREFIXES = ("lane/", "rescue/", "prerebase/", "wt/")
_STEM_SUFFIX = re.compile(r"(?:-r[0-9]+|-d[0-9a-f]{7,})\Z")
_STEM_FLOOR = 6     # a floor prevents a universal match; above it stay generous


def _stem(name):
    """One ref/lane/path name reduced to its family stem.

    THE CANONICAL SUFFIX VOCABULARY, not a private one (a cross-family
    review finding): this probe once knew only -rN/-dHEX, so feature-review and
    feature-build read as strangers to the refusal-only liveness joins while
    `_lane_stem` called them family — and stranded is irreversible, so the
    probe must never under-match. It now composes `_LANE_FAMILY_SUFFIX_RE`
    (the one role/round vocabulary) plus the -dHEX worktree spelling only
    this probe meets. Matching is casefolded; the sliced spelling keeps its
    case, because refs are case-sensitive on disk."""
    s = str(name or "")
    if s.startswith("refs/heads/"):
        s = s[len("refs/heads/"):]
    elif s.startswith("refs/remotes/"):
        s = s[len("refs/remotes/"):]
        s = s.split("/", 1)[1] if "/" in s else s
    low = s.casefold()
    for prefix in _STEM_PREFIXES:
        if low.startswith(prefix):
            s = s[len(prefix):]
            break
    while True:
        low = s.casefold()
        m = _STEM_SUFFIX.search(low) or _LANE_FAMILY_SUFFIX_RE.search(low)
        if not m:
            return s
        s = s[:m.start()]


def _stems_match(a, b):
    """GENEROUS family match: exact equality always; containment either way
    above the floor. Over-matching costs an operator one read (the refusal
    names the ref); under-matching would cost a false terminal.

    CASEFOLDED on both sides (a cross-family review finding): `_stem` slices the
    ORIGINAL case so emitted evidence names the ref as it is spelled — but
    case is spelling, not identity, and comparing the preserved spellings
    made canonical identity (`_lane_stem`, casefolded) call two names ONE
    family while this liveness match called them strangers. The under-match
    reaches the irreversible closure doors (stranded/out-of-scope)."""
    a = str(a or "").casefold()
    b = str(b or "").casefold()
    if not a or not b:
        return False
    if a == b:
        return True
    return min(len(a), len(b)) >= _STEM_FLOOR and (a in b or b in a)


def _family_stems(lane, branch):
    """The needle stems derived from a row's own lane and branch fields."""
    return [s for s in {_stem(lane), _stem(branch)} if s]


def _lane_family_refs(gitdir, lane, branch):
    """(matches, err) — every live ref in the row's OWN repo whose stem is in
    the row's lane family, as (refname, objectname) pairs. `err` non-None
    means the ref table could not be read: UNKNOWN, and every caller REFUSES
    on it (tri-state law — a probe that could not look never clears)."""
    needles = _family_stems(lane, branch)
    p = _git(gitdir, "for-each-ref",
             "--format=%(refname) %(objectname)", "refs/heads", "refs/remotes")
    if p is None or p.returncode != 0:
        return None, ("Git could not enumerate the repository's refs%s"
                      % ((": " + p.stderr.strip()) if p is not None
                         and p.stderr.strip() else ""))
    matches = []
    for line in p.stdout.splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        refname, obj = parts
        stem = _stem(refname)
        if any(_stems_match(stem, needle) for needle in needles):
            matches.append((refname, obj))
    return matches, None


def _lane_family_worktrees(gitdir, lane, branch):
    """(matches, err) — every worktree of the row's OWN repo whose branch is
    in the lane family OR whose path basename stem-matches, as row dicts from
    `vcs.parse_worktree_records` (which already refuses a truncated porcelain
    stream). Same tri-state contract as the ref probe."""
    from . import vcs
    needles = _family_stems(lane, branch)
    if not isinstance(gitdir, str) or not gitdir:
        return None, "no repository to enumerate worktrees in"
    try:
        p = subprocess.run(["git", "--git-dir", gitdir, "worktree", "list",
                            "--porcelain", "-z"], capture_output=True,
                           timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None, "git worktree list could not be run"
    rows, err = vcs.parse_worktree_records(p.returncode, p.stdout, p.stderr)
    if err:
        return None, err
    matches = []
    for row in rows:
        branch_stem = _stem(row.get("branch"))
        base_stem = _stem(os.path.basename(str(row.get("path") or "")))
        if any(_stems_match(branch_stem, n) or _stems_match(base_stem, n)
               for n in needles):
            matches.append(row)
    return matches, None


def _worktree_dirty(path):
    """True / False / None — does this worktree hold uncommitted changes?"""
    if not isinstance(path, str) or not path:
        return None
    try:
        p = subprocess.run(["git", "-C", path, "status", "--porcelain"],
                           capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if p.returncode != 0:
        return None
    return bool(p.stdout.strip())


def _trunk_refs(gitdir, cache):
    """(local_trunk_ref, upstream_trunk_ref) for one git dir, resolved once.

    A gitdir that is not a string is refused BEFORE the cache lookup, and the
    ordering is the whole point: `gitdir not in cache` raises TypeError on an
    unhashable value, so a row carrying `repo_id: ["not", "a", "path"]` used
    to crash the projection of that row here, one statement before `_git`
    would have declined it. It is not a path; it has no trunk to name."""
    if not isinstance(gitdir, str):
        return None, None
    if gitdir not in cache:
        cache[gitdir] = (_resolve_ref(gitdir, LOCAL_TRUNK),
                         _resolve_ref(gitdir, UPSTREAM_TRUNK))
    return cache[gitdir]


def _origin_configured(gitdir):
    """True / False / None for whether this repo declares an origin.

    Read-only projection historically treats a missing tracking ref as no
    upstream. A mutation that retires debt must be stricter: configured origin
    plus an absent/unreadable authoritative ref is UNKNOWN, never local-only.
    """
    p = _git(gitdir, "config", "--get", "remote.origin.url")
    if p is None:
        return None
    if p.returncode == 0:
        return bool(p.stdout.strip())
    return False if p.returncode == 1 else None


def _canonical_trunk(gitdir, value):
    """Resolve one explicit branch/remote-tracking name to (ref, sha, err)."""
    raw = str(value or "").strip()
    if not raw:
        return None, None, "--trunk is required"
    if raw.startswith("-") or re.fullmatch(r"[0-9a-fA-F]{7,64}", raw):
        return None, None, "--trunk must name a branch or remote-tracking ref, not an object id"
    if raw.startswith("refs/"):
        candidates = [raw] if dispatches._valid_trunk_ref(raw) else []
    else:
        candidates = [name for name in ("refs/heads/" + raw,
                                        "refs/remotes/" + raw)
                      if dispatches._valid_trunk_ref(name)]
    found = []
    for name in candidates:
        p = _git(gitdir, "show-ref", "--verify", "--hash", name)
        if p is not None and p.returncode == 0 and p.stdout.strip():
            found.append(name)
    if len(found) != 1:
        return None, None, ("--trunk %s resolved to %d named refs; pass one exact "
                            "branch or remote-tracking ref" % (raw, len(found)))
    ref = found[0]
    p = _git(gitdir, "rev-parse", "--verify", "--end-of-options",
             ref + "^{commit}")
    sha = p.stdout.strip().lower() if p is not None and p.returncode == 0 else ""
    if not _sha(sha):
        return None, None, "--trunk %s does not resolve to one commit" % raw
    return ref, sha, None


def _close_repo(lr, path):
    """Select the explicit proof repository without guessing across clones."""
    stored = lr.get("repo_id")
    if path:
        info = dispatches._repo_info(path)
        if not info:
            return None, "--repo %s is not a readable Git working tree" % path
        selected = info["repo_id"]
        if stored and selected != stored:
            return None, ("%s is bound to repository %s; --repo resolves to %s — "
                          "refusing cross-repository proof"
                          % (lr["id"], stored, selected))
        return selected, None
    if stored:
        return stored, None
    return None, ("%s has no repository binding; pass --repo PATH (helm will not "
                  "search for a repository by commit id)" % lr["id"])


def _git_observe(gitdir, tip, cache):
    """Observe whether the reviewed CHANGE reached local/upstream trunk.

    Patch identity, not object reachability alone, is the fact: normal lands may
    cherry-pick the gated commit and therefore mint a new sha. ``_landed`` keeps
    ancestry as its fast path and falls through to the per-commit patch-id answer.
    A missing repo/tip/object is unobservable, and every relevant trunk leg must
    have a real True/False answer before the combined lifecycle is observable.
    """
    blind = {"observable": False, "local": False, "upstream": False,
             "has_upstream": False}
    if not gitdir or not tip:
        return blind
    local_ref, up_ref = _trunk_refs(gitdir, cache)
    blind["has_upstream"] = bool(up_ref)
    p = _git(gitdir, "cat-file", "-e", tip + "^{commit}")
    if not (local_ref or up_ref) or p is None or p.returncode != 0:
        return blind
    local = _landed(gitdir, tip, local_ref) if local_ref else False
    upstream = _landed(gitdir, tip, up_ref) if up_ref else False
    if local is None or upstream is None:
        return blind
    return {"observable": True, "local": local, "upstream": upstream,
            "has_upstream": bool(up_ref)}


# What a row helm did not read Git for knows about trunk: nothing, and it says
# so in every field. ONE definition, because `_lr` used to carry a second copy
# inline — and a hand-built "blind" dict that omits the receipt keys is exactly
# how those rows came to answer "receipt: none" by DEFAULT rather than by
# lookup.
_UNOBSERVED = {"observable": False, "local": False, "upstream": False,
               "has_upstream": False}


def _base_behind(gitdir, tip, cache):
    """How many landing-target commits `tip`'s history LACKS, or None.

    `rev-list --count tip..trunk` is the commits reachable from trunk and not
    from the tip — every one of them landed after this lane's base, which is
    exactly what a gate receipt minted at that base never saw. It is 0 for a
    lane cut from trunk's tip, small for a same-night lane however unreachable
    its own head is (the trap the rejected reachability rung fell into), and
    measured 725..1372 for the live zombies the stale-base rung is about.

    The target mirrors `_lr`'s landed leg — upstream trunk when one exists,
    else local — so the drift is counted against the same ref the row's
    landedness was judged on; disagreeing legs would let a row read LANDED on
    one trunk and stale against another.

    None is the only failure value and it means UNMEASURED, never zero:
    a spawn failure, a non-zero rc (unrelated histories included — rev-list
    refuses nothing, but an unresolvable tip does), or unparsable output all
    decline to answer, and `base_state` renders that UNKNOWN. Memoised in the
    projection's per-pass cache because duplicate dispatch rows for one tip
    are common (the census hit several) and the count cannot change inside a
    single projection."""
    local_ref, up_ref = _trunk_refs(gitdir, cache)
    ref = up_ref or local_ref
    if not ref or not tip:
        return None
    key = ("base_behind", gitdir, tip, ref)
    if key not in cache:
        p = _git(gitdir, "rev-list", "--count", "%s..%s" % (tip, ref))
        out = p.stdout.strip() if p is not None and p.returncode == 0 else ""
        cache[key] = int(out) if out.isdigit() else None
    return cache[key]


def _observe(gitdir, tip, cache, receipts=None, git=True):
    """Polarity-blind evidence reduction: Git facts + receipt diagnostics.

    Receipt replay can validate local structure but cannot verify which payload a
    dregg turn carried. Every receipt state is therefore orthogonal diagnostics:
    it may NEVER promote, veto, or erase a Git observation. A future independently
    verified state can earn authority; R_LOCAL/R_REJECTED/R_CONFLICT cannot.

    `git=False` suppresses the LIVE TRUNK READING and nothing else. Two row
    kinds do not pay for it — an OPEN loop (helm reads no trunk before a
    verdict exists) and a CLOSED-BY-LANDING row (its closure is already an
    immutable recorded fact that a fresh reading can neither strengthen nor
    reopen) — and both used to skip this function ENTIRELY and default their
    receipt state to R_NONE. So a receipt ledger helm could not open rendered
    as "land receipt: none" on exactly those rows: the second time on this card
    that "I could not look" was printed as "there is nothing there", after the
    first was fixed one call site downstream.

    The receipt answer is a dict lookup in an index `project()` already read
    once — no Git, no cost — so it is never the thing skipped."""
    git_facts = _git_observe(gitdir, tip, cache) if git else dict(_UNOBSERVED)
    state, rec, why = _receipt_for(tip, receipts)
    if state == R_NONE:
        return dict(git_facts, receipt=False, receipt_state=R_NONE,
                    receipt_reason=None)
    # R_UNREADABLE rides the same non-promoting path as the two bad states — it
    # can no more veto Git than they can — but it is carried through as its own
    # value so the reader is told helm never got to look, instead of being told
    # there is nothing to see.
    if state in (R_REJECTED, R_CONFLICT, R_UNREADABLE):
        return dict(git_facts, receipt=False, receipt_state=state,
                    receipt_reason=why)
    if not git:
        # The anchor check below IS a live Git read, and it is the one thing a
        # `git=False` row must not silently be told helm performed. R_LOCAL is
        # still exactly true — the row typechecks and its payload recomputes —
        # and the reason says which half was not asked, rather than borrowing
        # the checked branch's wording.
        return dict(git_facts, receipt=False, receipt_state=R_LOCAL,
                    receipt_reason="local payload self-consistency only; the "
                                   "receipt's trunk anchor was NOT checked "
                                   "against live Git for this row")
    local_ref, _up_ref = _trunk_refs(gitdir, cache)
    anchor = _ancestry(gitdir, rec["trunk_sha"], local_ref)
    if anchor == NOT_ANCESTOR:
        return dict(git_facts, receipt=False, receipt_state=R_CONTRADICTED,
                    receipt_reason="the locally claimed trunk_sha %s left trunk's "
                                   "history" % rec["trunk_sha"][:12])
    return dict(git_facts, receipt=False, receipt_state=R_LOCAL,
                receipt_reason="local payload self-consistency only; dregg turn "
                               "payload binding unavailable — live Git governs")


class _Unstamped:
    """THE EVENT HAPPENED AND THE LEDGER CARRIES NO STAMP FOR IT.

    A class rather than a bare `object()` so a leak past the wire normalizer is
    legible (`UNSTAMPED`) in a traceback or a diff instead of an address.

    FALSY ON PURPOSE, and this is the load-bearing half. As a STAMP it is
    empty — there is no instant in it — so every `if ts:` and `bool(ts)` in
    this module gets the honest answer about the instant, and PRESENCE is asked
    the only way it can now be asked: `is not None`. A truthy marker would have
    made `verdict_ts or delivered_ts` accidentally correct, which is worse than
    wrong: the fallthrough that caused the defect would keep passing, so
    nothing would measure whether the fix was still there."""
    __slots__ = ()

    def __bool__(self):
        return False

    def __repr__(self):
        return "UNSTAMPED"


# The third answer beside a real stamp and None ("no such event on the record"),
# and the one this module kept collapsing into the other two. ONE missing `ts`
# on a verdict event produced both of this card's transition lies at once:
#
#   * `post_verdict` fell through `or` to the DELIVERY instant, so an APPROVE
#     recorded at an unknown moment rendered a confident 2h50 dwell and could
#     go STALLED on a number nothing measured;
#   * and `_timeline` tested the STAMP to decide whether the STEP existed, so
#     that same verdict vanished off the timeline entirely.
#
# A row whose verdict helm cannot date is not a row without a verdict. Presence
# and instant are two facts, so they are two values from here down.
UNSTAMPED = _Unstamped()


def _first(*stamps):
    """The first transition that HAPPENED — unstamped or not.

    Deliberately not `a or b`: UNSTAMPED must beat an earlier event's real
    stamp, and an empty-string ts must not slide past either. Truthiness cannot
    express "this happened at an unknown time"; identity with None can."""
    for stamp in stamps:
        if stamp is not None:
            return stamp
    return None


def _wire_ts(stamp):
    """One transition stamp as it may cross the wire: a string, or None.

    UNSTAMPED is an internal presence marker and is not JSON — `/api/lr` would
    answer 500 rather than a board if one reached the encoder. Every field that
    carries a transition instant out of `_lr` goes through here."""
    return stamp if isinstance(stamp, str) else None


# The event kinds this module takes a STAMP from, in one place because two
# readers need the same list: `_transitions`, which dates them, and `_refused`,
# which names the ones the fold would not take. An event kind absent here is
# never dated and never called refused (a notify-failed marker is a real fact
# and not a transition).
_OPEN_KINDS = ("dispatch", "add", "posting", None)
_DATED_KINDS = ("delivered", "verdict")


def _transitions(events):
    """(open, delivered, verdict) read from a row's raw ledger events — the
    existing per-event timestamps, no new clock. Each is a stamp, UNSTAMPED
    (the event is on the record and carries no ts at all), or None (no such
    event at all). A ts that is PRESENT and junk stays a stamp and is typed as
    unreadable by `_step`, which is a third fact again. Legacy snapshot rows
    that carry a verdict without a discrete event are the None case and still
    fall back to the opening stamp.

    Events are the row's already-grouped slice, so project() reads the ledger
    once, not once per row (that per-row history() reparse was O(N^2))."""
    open_ts = delivered_ts = verdict_ts = None
    for ev in events:
        event, ts = ev.get("event"), ev.get("ts")
        stamp = ts if ts else UNSTAMPED       # "" is no stamp, not a stamp
        if event in _OPEN_KINDS and open_ts is None:
            open_ts = stamp
        elif event == "delivered" and delivered_ts is None:
            delivered_ts = stamp
        elif event == "verdict" and verdict_ts is None:
            verdict_ts = stamp
    return open_ts, delivered_ts, verdict_ts


# How far ahead of the reading clock a stamp may sit and still be treated as a
# real instant. Stamps are written locally and truncated to the second, so an
# honest one is never ahead of a later read; this is slack for a clock that
# stepped between the write and the read, not a licence for a record dated next
# century. Beyond it the instant is UNUSABLE — not recent, not old, not zero.
# ONE definition: `helm.web`'s closed-window reads it from here rather than
# keeping a second copy that could drift away from this one.
FUTURE_SKEW_S = 60


def _notify_failed(events):
    """The row's durable notification-failure marker, from the SAME event slice
    every other fact about the row is derived from.

    `dispatches._notify_failed_for` re-reads the WHOLE ledger for one row, and
    inside this walk that is three separate faults at once. It is a second
    append boundary — a marker appended after the board's read is a fact from a
    different instant than the row printed beside it, which is the straddle
    `snapshot_and_events` exists to close. It is an O(N) reparse per
    undelivered row inside a projection built to avoid exactly that. And
    `events()` drops checked_events' reason, so a ledger that could not be read
    answers "no failure marker" — which on this card renders as a mention that
    went out fine."""
    return next((ev for ev in events if isinstance(ev, dict)
                 and ev.get("event") == "notify-failed"), None)


def _epoch(ts):
    """Epoch for one ledger stamp, or None when it is not a readable stamp.

    Deliberately NOT dispatches._age_s: that answers 0 for a stamp it cannot
    parse, i.e. "just now", so it can never be asked whether the stamp was
    readable at all. Every question of the form "do we actually know when" goes
    through this."""
    try:
        return calendar.timegm(time.strptime(str(ts), "%Y-%m-%dT%H:%M:%SZ"))
    except (TypeError, ValueError):
        return None


def _threshold(state, deadline_s):
    if state in TERMINAL:
        return None
    if state == "REVIEWED":
        return None            # undeclared polarity — nothing is billable
    if state == "OPEN":
        return None            # unbilled — the integrator owes the notification
    if state in ("AWAITING_REVIEW", "AWAITING_BUILD") + _AUTHOR_STAGES:
        try:
            return int(deadline_s)
        except (TypeError, ValueError):
            return None
    return LAND_STALL_S.get(state)


def _step(state, ts, now):
    """One timeline step, carrying WHY its stamp is missing when it is.

    Found while fixing the closure stamp, and it is the same defect twice over
    in a rendering nobody had looked at:

    * a stamp that is not a stamp went through verbatim, so a verdict event
      with a corrupt ts printed `READY not-a-stamp` — junk in the position
      that answers "when did this happen";
    * and every absent stamp was rendered by the readers as "(git-observed,
      no ledger stamp)", which is TRUE of exactly one step — the MERGED_LOCAL
      / LANDED row `_timeline` appends itself, because git records no moment
      for a merge helm noticed. Applied to a ledger event that simply carries
      no ts it is a confident explanation of something nobody looked at, and
      an OPEN step is never git-observed: helm does not read git at all until
      a verdict exists.

    So the missing-ness is typed here, once, where the facts are known, rather
    than guessed at by each reader from the shape of a None."""
    # UNSTAMPED is the ledger event carrying no ts AT ALL, which is the first
    # case and not the second: `_epoch` cannot parse the marker either, and
    # letting it fall through would report "the stamp is not a timestamp" over
    # a row that has no stamp to be wrong about.
    if ts is None or ts is UNSTAMPED:
        return {"state": state, "ts": None}
    at = _epoch(ts)
    if at is None:
        return {"state": state, "ts": None, "ts_unreadable": True}
    # A FOURTH kind, and the one that nearly shipped: suppressing the
    # impossible CLOSURE stamp left the same value printing one line lower, on
    # the step. The retirement annotation IS a timeline step, so a stamp ruled
    # unusable for the closure instant was still rendered verbatim as when the
    # step happened — `WITHDRAWN 2099-01-01T00:00:00Z`, straight off the wire.
    # A stamp is refused in every position it can be read from, or it is not
    # refused.
    if at > now + FUTURE_SKEW_S:
        return {"state": state, "ts": None, "ts_impossible": True}
    return {"state": state, "ts": ts}


def _timeline(state, open_ts, delivered_ts, verdict_ts, verdict_state, now,
              annotation=None):
    """Preserve verdict intent and the mutually exclusive terminal annotation.

    A STEP IS DRAWN BY THE EVENT'S EXISTENCE, NEVER BY ITS STAMP. Both tests
    here used to be truthiness on the timestamp, so a delivery or a verdict the
    ledger recorded WITHOUT a ts was not rendered late or vaguely — it was not
    rendered at all, and the row's history read as though the review had never
    been delivered and the verdict had never been reached. `_step` already
    types the three kinds of missing stamp; the step's own presence is a
    different question and is answered from `_transitions`."""
    tl = [_step("OPEN", open_ts, now)]
    if delivered_ts is not None:
        stage_name = "AWAITING_BUILD" if (state == "AWAITING_BUILD" or verdict_state == "AWAITING_BUILD") else "AWAITING_REVIEW"
        tl.append(_step(stage_name, delivered_ts, now))
    if verdict_ts is not None:
        tl.append(_step(verdict_state, verdict_ts, now))
    if state in ("MERGED_LOCAL", "LANDED") and state != verdict_state:
        # THE one step that is genuinely git-observed, and it says so itself
        # instead of leaving every reader to infer it from an absent stamp.
        tl.append({"state": state, "ts": None, "observed": True})
    if annotation:
        tl.append(_step(annotation[0], annotation[1], now))
    return tl


def _refused(events, taken):
    """The transition kinds the record HOLDS for this row and the fold did NOT
    take — in the order this module reads them, deduplicated, empty on every
    healthy row.

    ONE READ WAS NOT YET ONE RULE, and then one rule was not yet the RIGHT
    question. Folding both projections from a single `checked_events` closed the
    straddle; deferring to the canonical state closed the case where a refused
    event was the ONLY one of its kind. Neither reaches two same-kind events
    with one accepted: the state says a verdict happened — true — and cannot
    say WHICH event was it, so reading the first verdict in the raw slice took
    the REFUSED one's stamp. A review probe put 2001-01-01 on it and the card
    printed a verdict from 2001 with a 25-year dwell and nothing amiss beside it.

    So stamps come from the fold's OWN accepted slice (see
    `dispatches._fold`), and this function reports the difference. Identity, not
    equality: two events can be byte-identical and only one of them was taken.

    The kinds are THIS module's, from `_transitions` — the events whose stamps
    a reader here would otherwise have used. It is deliberately not a mirror of
    `_apply`'s branch set: naming a refused `notify-failed` would be noise, and
    re-deriving that set is the second acceptance rule this whole line of
    defects came from."""
    taken_ids = {id(ev) for ev in taken}
    out = []
    for ev in events:
        kind = ev.get("event")
        if id(ev) not in taken_ids and kind in _DATED_KINDS and kind not in out:
            out.append(kind)
    return out


def _consumed_confirmations(current):
    """{confirmation row id -> the id of the close that consumed it}.

    ONE PASS over the SAME snapshot the projection already holds — never a
    second read, per project_raw's one-read law.

    THE `close_reason` CHECK IS DEFENSIVE AND CURRENTLY UNREACHABLE, and saying
    so is the honest form. `confirmation_id` reaches a snapshot ONLY through a
    `close` event (it lives in dispatches._CLOSE_STATE_FIELDS), and every close
    event carries a reason — MEASURED on the live ledger: 19 rows hold both
    fields, ZERO hold confirmation_id alone. A test that planted the state
    directly could not construct the fixture; the
    must-hit control caught that, which is the only reason this note is not a
    claim that it was proved. Mutating the check out is therefore INERT on
    every input reachable today, NOT a survivor. It stays because it fails
    CLOSED: if a future writer ever records the link without completing the
    close, an unfinished close must retire no debt."""
    out = {}
    for row in (current or {}).values():
        if not isinstance(row, dict) or not row.get("close_reason"):
            continue
        cid = str(row.get("confirmation_id") or "").strip()
        if cid:
            out.setdefault(cid, row.get("id"))
    return out


def _lr(row, events, taken, cache, now, attest, receipts=None, index=None,
        epoch=None, consumed=None):
    rid = row["id"]
    tip = row.get("tip")
    # STAMPS FROM WHAT THE FOLD TOOK; the raw slice is still what the record
    # HOLDS, and the difference between them is a finding.
    open_ts, delivered_ts, verdict_ts = _transitions(taken)
    refused = _refused(events, taken)
    closed = row.get("status") in ("verdict", "closed")
    polarity = dispatches._replay_polarity(row.get("polarity"))
    closed_by_landing = bool(row.get("closed_by_landing"))
    abandoned = bool(row.get("abandoned"))
    close_reason = row.get("close_reason")
    delivered_report = row.get("status") == "closed" \
        and close_reason == "delivered-report"
    build_landed = row.get("status") == "closed" \
        and close_reason == "landed" and bool(row.get("landing_review_id"))
    # Intent never decides whether facts are read. Observe every unresolved
    # closed row so a FIX/SUPERSEDE tip physically present on trunk stays visible
    # as a contrary fact. A CLOSED-BY-LANDING receipt is already the immutable
    # historical fact, so repeating live Git work can neither strengthen nor
    # reopen it — and the same monotonic skip covers close --reason landed /
    # superseded / stranded, whose write-time ladders proved their terminal
    # once and never re-observe. close --reason withdrawn is deliberately NOT
    # in that set: its premise (the change stayed off trunk) is FALSIFIABLE,
    # so git keeps being read and a later land re-exposes the row.
    obs = _observe(row.get("repo_id"), tip, cache, receipts,
                   git=closed and not closed_by_landing and not abandoned
                   and close_reason not in ("landed", "superseded",
                                            "stranded", "subsumed",
                                            "delivered-report"))
    observable = obs["observable"]
    landed = (obs["upstream"] if obs["has_upstream"] else obs["local"]) \
        if observable else False
    merged_local = observable and obs["local"] and not landed
    live_contrary = closed and polarity in ("fix", "supersede") \
        and (landed or merged_local)
    discharged = bool(row.get("discharged"))
    withdrawn = bool(row.get("withdrawn"))
    annotation = next(((label, row.get(ts_field))
                       for flag, ts_field, label in _TERMINAL_ANNOTATIONS
                       if row.get(flag)), None)
    if annotation is None and close_reason:
        # The _TERMINAL_ANNOTATIONS generalization: one timeline step per
        # terminal, CLOSED_<REASON>, dated by the close stamp. Exclusivity at
        # the write boundary guarantees at most one annotation exists.
        annotation = ("CLOSED_" + close_reason.upper().replace("-", "_"),
                      row.get("close_ts"))
    # Discharge retires the debt, never the fact. If Git later becomes unreadable,
    # the replayed event still preserves that this row once entered contrary.
    # Withdraw retires a do-not-land debt the same operational way (terminal,
    # owed-by-nobody, never stalled) but is by definition NOT a contrary — the
    # withdrawn change is correctly absent from trunk, so there is no
    # landed-despite-verdict contradiction to expose.
    #
    # LIFECYCLE: a withdraw is truthful WHEN RECORDED (Git proved absence then),
    # but its premise is falsifiable — if the withdrawn change LATER lands, the
    # row must not stay silently terminal. The withdraw event stays as history,
    # but the live landed-despite-FIX fact re-exposes the row as CONTRARY (the
    # same contradiction an unwithdrawn FIX-despite-land shows), owed by the
    # integrator again. A withdraw is a resolution, not a veto on the future.
    withdraw_contradicted = withdrawn and live_contrary
    # The SAME falsifiability clause generalizes to close --reason withdrawn:
    # its git observation stays live (see the _observe gate above), so a later
    # land re-exposes the row as CONTRARY, non-terminal, owed by the
    # integrator, flagged close_contradicted. The other three close reasons
    # are monotonic — their write-time ladders proved the terminal once.
    close_contradicted = close_reason == "withdrawn" and live_contrary
    # CONSUMED AS EVIDENCE — the door's own exhaust, and the reason the board
    # could not reach zero. A confirmation round is dispatched --supersedes
    # <original>; the reviewer files SUPERSEDE/FIX on it; that verdict closes
    # the ORIGINAL through subsumed/resolved — and the CONFIRMATION ROW itself
    # is never closed by anything. Its reviewed tip is on trunk (it was written
    # ON the landed successor, which is the whole point of it) and its polarity
    # is non-approve, which is the contrary signature exactly. So EVERY USE OF
    # THE DOOR MINTED ONE PERMANENT DEBT. Measured on the live ledger:
    # 13 contrary rows, all 13 carrying `supersedes`, ZERO originals — and
    # CONTRARY had become ~85% false positives generated by the correct path,
    # which is how a real unauthorized land arrives wearing the same badge as
    # seven legitimate closes.
    #
    # The ledger already knew: the close that consumed this row recorded its id
    # as `confirmation_id`. Nothing read it. `consumed` is that link, folded
    # ONCE per projection from the SAME snapshot (project_raw's one-read law).
    #
    # NOTE WHICH HALF IS SUPPRESSED, because the distinction is this file's own:
    # the FACT stays (`contrary` stays True — the row IS on trunk under a
    # non-approve verdict, and that is true). Only the DEBT is retired, exactly
    # as `discharged` does. The banner keeps rendering and gains a reason.
    evidence_consumed = bool(consumed) and rid in consumed and live_contrary
    retired = abandoned or closed_by_landing \
        or close_reason is not None and not close_contradicted \
        or (discharged or withdrawn) and not withdraw_contradicted \
        or evidence_consumed
    contrary = live_contrary or discharged \
        or row.get("close_contrary_state") == "landed"
    active_contrary = live_contrary and not (
        discharged or (withdrawn and not withdraw_contradicted)
        or close_reason is not None and not close_contradicted
        or evidence_consumed)
    # WHEN THE ROW ENTERED ITS POST-VERDICT STATE — and `or` answered the wrong
    # question. A verdict event carrying no readable ts is a verdict at an
    # UNKNOWN instant, and the fallthrough dated it from the DELIVERY instead:
    # an APPROVE nobody can date rendered a confident 2h50 dwell, crossed a
    # stall threshold on that number, and reported STALLED — the card asserting
    # a measurement over a stamp it never had. The fallback belongs to the
    # LEGACY case alone (a snapshot row with no discrete verdict event at all),
    # which is the None case and the only one `_first` falls through.
    post_verdict = _first(verdict_ts, delivered_ts, open_ts)
    # The state the VERDICT ALONE puts the loop in, before git is consulted.
    # UNDECLARED and any unrecognised value both fall to REVIEWED — never READY.
    # Two independent lines enforce that and BOTH are mutation-covered:
    # `_replay_polarity` normalises an unknown ledger value to None, and this
    # default catches None itself.
    verdict_state = VERDICT_STATE.get(polarity, "REVIEWED")
    # MECHANICAL ENFORCEMENT AT THE LAND PATH — a design-council mandate, and
    # the half a verdict-side UNVERIFIED did not deliver. A review reproduced it:
    # a brand-new untokened APPROVE came back gate=UNVERIFIED and land_state=
    # READY, so recording the unchecked state changed nothing about whether the
    # work could land. Marking a claim unverified is not enforcing anything.
    #
    # READY is the ONLY state this touches, because READY is the one that says
    # "this may be merged". An approve without a bound receipt stays REVIEWED —
    # honestly reviewed, and not clear to land.
    #
    # GRANDFATHERING IS PER ROW AND KEYED ON THE WRITER. A verdict written by
    # code that had no gate to run keeps the old derivation forever; one
    # written by a gate-capable writer must bind. The alternative — permitting
    # untokened approves indefinitely so nothing in flight breaks — preserves
    # the unchecked path as a permanent option, which is what the council
    # called shipping on unverifiable gates.
    ungated, tier_state = _approval_refusal(
        row, index=index, epoch=epoch) \
        if verdict_state == "READY" else (None, "none")
    if ungated:
        verdict_state = "REVIEWED"
    if delivered_report:
        state, entered = "DELIVERED_REPORT", row.get("close_ts")
    elif build_landed:
        state, entered = "LANDED", row.get("close_ts")
    elif abandoned:
        # Evidence disappearance is the terminal fact. It says nothing about
        # where equivalent content landed, so no later Git observation may
        # relabel this row as landed or withdrawn.
        state, entered = "ABANDONED", post_verdict
    elif closed_by_landing:
        # Closure records a physical fact, not a positive opinion. Keep the
        # verdict-derived state and its immutable UNDECLARED polarity forever.
        state, entered = verdict_state, post_verdict
    elif closed and polarity in ("fix", "supersede"):
        # Verdict intent remains the lifecycle state even when Git reports a
        # contrary physical fact; landed/merged_local expose the contradiction
        # instead of suppressing it or laundering it into an approved land.
        state, entered = verdict_state, post_verdict
    elif closed and landed:
        state, entered = "LANDED", post_verdict
    elif closed and merged_local:
        state, entered = "MERGED_LOCAL", post_verdict
    elif closed:
        state, entered = verdict_state, post_verdict
    elif row.get("delivery") == "observed":
        # Same law one stage earlier: a delivery event with no stamp means this
        # row has been AWAITING_REVIEW / AWAITING_BUILD since an unknown moment,
        # not since it opened. Dating it from the opening would bill the
        # recipient for the whole pre-delivery wait as well.
        stage_name = "AWAITING_BUILD" if row.get("kind") == "build" else "AWAITING_REVIEW"
        state, entered = stage_name, _first(delivered_ts, open_ts)
    else:
        state, entered = "OPEN", open_ts
    # BASE DRIFT, measured for READY rows only — the one state whose whole
    # meaning is "waiting to be merged", and the one the live READY zombies wore
    # while nothing computed their behind-ness. Every other state answers
    # base_state() as LANDED or UNKNOWN, honestly: an unmeasured dimension is
    # never rendered current. Gated on `observable` so a blind row costs no
    # spawn and cannot be accused on a half-reading; one cached rev-list per
    # (tip, trunk) pair otherwise.
    behind = _base_behind(row.get("repo_id"), tip, cache) \
        if state == "READY" and observable else None
    retire_ts = annotation[1] if annotation else None
    terminal = retired or state in TERMINAL and not active_contrary
    # WHEN this loop CLOSED — a different question from `entered_ts`, which is
    # when it entered its current state. An approve on Tuesday whose change
    # reaches trunk on Friday entered READY on Tuesday and CLOSED on Friday, and
    # dating the closure from the verdict dropped it out of every "closed
    # recently" window while it was still warm (reproduced exactly that way).
    # A retirement writes a real closure stamp; a SUPERSEDE verdict IS the
    # closure. A git-OBSERVED landing has no stamp anywhere — git records no
    # moment for the merge helm noticed — so this stays None and the reader is
    # told the instant is unknown rather than handed the verdict's.
    #
    # AND WHETHER THAT STAMP IS A STAMP. The instant was parsed for the dwell
    # and then the RAW value was copied here, so a retirement written with a
    # corrupt ts rendered as "closed not-a-stamp" — junk presented as the
    # answer to the very question this field exists to answer — while every
    # counter of undateable closures stayed at zero (reproduced on a
    # withdrawn row). An unreadable stamp is NOT the same fact as an absent
    # one, and collapsing it into None would be this card's other recurring
    # defect: a row helm could not read reported as a row with nothing to
    # read. So the value is dropped and the reason is carried.
    #
    # AND WHETHER A STAMP THAT PARSES IS A POSSIBLE ONE. The window learned to
    # rule a future instant UNUSABLE and this read did not, so the card went on
    # being SENT `closed_ts: "2099-01-01T00:00:00Z"` and printing it as the
    # closure instant — while a recent entry stamp proved, correctly, that the
    # row really did close inside the window. So the surface both showed the
    # impossible date and acted on it. The first cut left this, reasoning that
    # 2099 is absurd rather than plausible; the review's point is that the row
    # is still
    # ON the card as recently closed, and a reader who trusts the date beside
    # it is reading a value nothing measured. It is a THIRD state, not the
    # unreadable one — the ledger holds a well-formed timestamp that cannot be
    # a closure — so it gets its own term rather than borrowing that wording.
    closed_ts, closed_ts_unreadable, closed_ts_impossible = None, False, False
    closed_at = None
    if terminal:
        stamp = retire_ts if retired else \
            verdict_ts if state == "SUPERSEDED" else None
        # A SUPERSEDE verdict the ledger recorded with no ts has no closure
        # stamp to be wrong about — that is absence, and calling it unreadable
        # would print "the ledger's stamp is not a timestamp" over a row that
        # carries none.
        stamp = None if stamp is UNSTAMPED else stamp
        at = _epoch(stamp) if stamp is not None else None
        if stamp is not None and at is None:
            closed_ts_unreadable = True
        elif at is not None and at > now + FUTURE_SKEW_S:
            closed_ts_impossible = True
        else:
            closed_ts, closed_at = stamp, at
    # THE INTERVAL'S END, and `now` is only ever the end of a LIVE one. A
    # terminal row stopped dwelling when it CLOSED, so its measurement runs to
    # the closure instant — and when the record carries no usable one, the
    # interval has no end and the dwell is UNKNOWN, not a number.
    #
    # The first fix covered a RETIRED row (an absent, unreadable or impossible
    # retirement stamp) and left the larger case standing, reasoning that a
    # terminal row never stalls so a growing dwell misleads without alarming.
    # A cross-family review ruled it load-bearing under this card's own bar: a
    # git-OBSERVED landing is terminal with no closure stamp ANYWHERE, so it
    # read dwell_known TRUE and grew 3600 -> 7200 across two projections of a
    # row that had already closed. Quiet is exactly the failure mode this card
    # exists to remove. Git proves the interval ENDED; it supplies no END
    # INSTANT, and a confident number over that is the same shape as the other
    # four. One owner for every terminal row now, rather than the retired
    # subset alone.
    # `closed_at` is only ever set inside the terminal branch above, so it
    # carries the terminal test with it — a second `terminal and` here read
    # as a guard and was dead logic (a mutation of it could not fail).
    dwell_now = closed_at if closed_at is not None else now
    dwell = dispatches._age_s({"ts": entered}, dwell_now)
    # WHETHER THAT NUMBER IS A MEASUREMENT. `_age_s` answers 0 — "just now" —
    # for a stamp it cannot read, so a row whose entry event carries no ts, or a
    # corrupt one, renders as the freshest thing on the board and can never
    # stall. The number stays (every consumer sorts and formats it), and this
    # flag is what stops it being read as an observation.
    #
    # A STAMP FROM THE FUTURE IS NOT A MEASUREMENT EITHER, and it is the same
    # collapse again: `_age_s` clamps its subtraction at 0, so a row that
    # entered its state in 2099 — or one closed BEFORE it entered, which is the
    # same broken record — reads a permanent, confident "0m" and can never
    # stall. Unreadable, impossible and MISSING-AN-END all mean the age was
    # never measured, so all of them answer UNKNOWN.
    entered_at = _epoch(entered)
    dwell_known = entered_at is not None \
        and entered_at <= dwell_now + FUTURE_SKEW_S \
        and not (terminal and closed_at is None)
    threshold = None if retired else _threshold(
        "OPEN" if active_contrary else state, row.get("deadline_s"))
    # A land-side stall is only assertable when landing is OBSERVABLE; the
    # review-side states — and CHANGES_REQUESTED, which is the ledger's own
    # record of a FIX verdict — are pure ledger facts and always assertable.
    assertable = observable or state in ("OPEN", "AWAITING_REVIEW", "AWAITING_BUILD") \
        + _AUTHOR_STAGES
    contrary_state = row.get("discharge_contrary") if discharged \
        else "landed" if row.get("close_contrary_state") == "landed" \
        else "landed" if landed else "merged-local" if merged_local else None
    owed_by = "nobody" if retired else "integrator" if active_contrary \
        else "nobody (undeclared)" if state == "REVIEWED" and polarity is None \
        else "unknown (declared verdict held)" if state == "REVIEWED" \
        else OWED_BY.get(state, "unknown")
    out = {"id": rid, "state": state, "observable": observable,
            "polarity": polarity, "owed_by": owed_by,
            "author": row.get("sender"), "reviewer": row.get("recipient"),
            "kind": row.get("kind"),
            "lane": row.get("lane"), "branch": row.get("ref"),
            "chain_root": row.get("chain_root"),
            "supersedes": row.get("supersedes"),
            "base_sha": tip if row.get("kind") == "build" else None,
            "review_sha": row.get("landing_review_tip") if build_landed else
            None if row.get("kind") == "build" else tip,
            "repo_id": row.get("repo_id"),
            "verdict_ref": row.get("verdict_ref"),
            "reviewed_tip": row.get("landing_review_tip") if build_landed else
            row.get("reviewed_tip"),
            # UNSTAMPED never crosses the wire: it is a presence marker, not an
            # instant, and `/api/lr` would answer 500 rather than a board if
            # one reached the JSON encoder. `dwell_known` above is the field
            # that carries "this row's entry instant is unknown".
            "entered_ts": _wire_ts(entered), "dwell_s": dwell,
            "dwell_known": dwell_known, "closed_ts": closed_ts,
            "closed_ts_unreadable": closed_ts_unreadable,
            "closed_ts_impossible": closed_ts_impossible,
            # The transitions the record holds and this projection REFUSED.
            # Empty on every healthy row; naming them is what keeps a refusal
            # from being a silent disagreement between the state and the
            # timeline drawn beneath it.
            "ledger_refused": refused,
            "deadline_s": row.get("deadline_s"),
            "stall_threshold_s": threshold,
            "stalled": False if retired else bool(
                threshold and dwell >= threshold and assertable),
            "terminal": terminal,
            "land_state": "NOT_CLAIMED" if delivered_report else
            "UNKNOWN" if abandoned or not observable else
            "LANDED" if landed else "MERGED_LOCAL" if merged_local else "ABSENT",
            "landed": landed, "merged_local": merged_local,
            # None on every non-READY row and on any READY row whose drift
            # could not be measured — UNMEASURED, never zero.
            "base_behind": behind,
            "contrary": contrary, "contrary_state": contrary_state,
            # WHY this landed-despite-verdict row is not a debt: it was the
            # confirming round, and the named close consumed it as evidence.
            # Carried so the banner states a reason instead of vanishing —
            # a badge that silently disappears teaches nobody.
            "consumed_by": (consumed or {}).get(rid) if evidence_consumed
            else None,
            "contrary_target": row.get("discharge_target") \
            if discharged else row.get("close_contrary_target") \
            if row.get("close_contrary_state") == "landed" \
            else "upstream" if landed and obs["has_upstream"] \
            else "local" if contrary else None,
            "close_reason": close_reason,
            "close_ts": row.get("close_ts"),
            "close_evidence": row.get("close_evidence"),
            "artifact_ref": row.get("artifact_ref"),
            "report_ref": row.get("report_ref"),
            "delivered_report_correction":
            bool(row.get("delivered_report_correction")),
            "cancel_reason": row.get("cancel_reason"),
            "close_contradicted": close_contradicted,
            "close_proof_mode": row.get("close_proof_mode"),
            # the LIVE step — whether this land reached the RUNNING FLEET, a
            # different fact from reaching trunk (`_delivery_phrase`)
            "close_delivery_class": row.get("close_delivery_class"),
            "close_delivery_restart": row.get("close_delivery_restart"),
            "translated_tip": row.get("translated_tip"),
            "closing_repo_id": row.get("closing_repo_id"),
            "closing_trunk_ref": row.get("closing_trunk_ref"),
            "closing_trunk_sha": row.get("closing_trunk_sha"),
            "landing_review_id": row.get("landing_review_id"),
            "landing_review_tip": row.get("landing_review_tip"),
            "landing_review_verdict_anchor":
            row.get("landing_review_verdict_anchor"),
            "landing_review_tier_state": row.get("landing_review_tier_state"),
            "landing_review_gate_requirement":
            row.get("landing_review_gate_requirement"),
            "landing_review_gate": row.get("landing_review_gate"),
            "landing_review_approval_anchor":
            row.get("landing_review_approval_anchor"),
            "control_sha": row.get("control_sha"),
            "confirmation_id": row.get("confirmation_id"),
            "confirmation_tip": row.get("confirmation_tip"),
            "confirmation_ref": row.get("confirmation_ref"),
            "original_author": row.get("original_author"),
            "confirmation_recipient": row.get("confirmation_recipient"),
            "original_author_family": row.get("original_author_family"),
            "confirmation_recipient_family":
            row.get("confirmation_recipient_family"),
            "original_verdict_anchor": row.get("original_verdict_anchor"),
            "confirmation_verdict_anchor":
            row.get("confirmation_verdict_anchor"),
            "original_family_evidence": row.get("original_family_evidence"),
            "confirmation_family_evidence":
            row.get("confirmation_family_evidence"),
            "original_family_anchor": row.get("original_family_anchor"),
            "confirmation_family_anchor":
            row.get("confirmation_family_anchor"),
            "confirmation_tier_state": row.get("confirmation_tier_state"),
            "confirmation_gate_requirement":
            row.get("confirmation_gate_requirement"),
            "confirmation_gate": row.get("confirmation_gate"),
            "confirmation_approval_anchor":
            row.get("confirmation_approval_anchor"),
            "original_proof_mode": row.get("original_proof_mode"),
            "absence_trunk_ref": row.get("absence_trunk_ref"),
            "absence_trunk_sha": row.get("absence_trunk_sha"),
            "discharged": discharged,
            "superseding_tip": row.get("superseding_tip"),
            "superseding_id": row.get("superseding_id"),
            "discharge_ref": row.get("discharge_ref"),
            "discharge_ts": row.get("discharge_ts"),
            "withdrawn": withdrawn,
            "withdraw_ref": row.get("withdraw_ref"),
            "withdraw_ts": row.get("withdraw_ts"),
            "withdraw_contradicted": withdraw_contradicted,
            "abandoned": abandoned,
            "abandon_reason": row.get("abandon_reason"),
            "abandon_ts": row.get("abandon_ts"),
            "abandon_repo_id": row.get("abandon_repo_id"),
            "abandon_object_state": row.get("abandon_object_state"),
            "abandon_proof_mode": row.get("abandon_proof_mode"),
            "abandon_proof_version": row.get("abandon_proof_version"),
            "abandon_trunk_mention_state":
            row.get("abandon_trunk_mention_state"),
            "abandon_trunk_mention_proof_mode":
            row.get("abandon_trunk_mention_proof_mode"),
            "abandon_trunk_mention_proof_version":
            row.get("abandon_trunk_mention_proof_version"),
            "abandon_branch_state": row.get("abandon_branch_state"),
            "abandon_branch_proof_mode": row.get("abandon_branch_proof_mode"),
            "abandon_branch_proof_version": row.get("abandon_branch_proof_version"),
            "abandon_worktree_state": row.get("abandon_worktree_state"),
            "abandon_worktree_proof_mode": row.get("abandon_worktree_proof_mode"),
            "abandon_worktree_proof_version":
            row.get("abandon_worktree_proof_version"),
            "abandon_land_state": row.get("abandon_land_state"),
            "closed_by_landing": closed_by_landing,
            "landing_repo_id": row.get("landing_repo_id"),
            "landing_trunk_ref": row.get("landing_trunk_ref"),
            "landing_trunk_sha": row.get("landing_trunk_sha"),
            "landing_proof_mode": row.get("landing_proof_mode"),
            "landing_proof_version": row.get("landing_proof_version"),
            "landing_ts": row.get("landing_ts"),
            "has_upstream": obs["has_upstream"],
            # NO DEFAULTS ON THIS AXIS. `.get(..., R_NONE)` is what turned a
            # row helm never asked about into a row with no receipt; now every
            # row goes through `_observe`, so a KeyError here would mean a new
            # bypass exists — and `project()` turns that into a LOUD
            # unavailable board, which is the right answer to "helm cannot say".
            "receipt": obs["receipt"],
            "receipt_state": obs["receipt_state"],
            "receipt_reason": obs["receipt_reason"],
            "gate": row.get("gate") or "", "ungated": ungated,
            "tier": tier_state,
            "timeline": _timeline(state, open_ts, delivered_ts, verdict_ts,
                                  verdict_state, now, annotation),
            # DELIVERY IS AN EVENT, NOT A TIMESTAMP. Both of these used to be
            # truthiness on the stamp, so a delivery the ledger recorded
            # without a ts reported `notified: false` — the record's own
            # delivery event denied because helm could not date it.
            "notify_failed": _notify_failed(events)
            if delivered_ts is None else None,
            "notified": delivered_ts is not None}
    out.update(attest)
    # Stamped AFTER attest so the predicate judges the fields a consumer will
    # actually read; it is a pure function of the row, so JSON consumers get
    # the same answer `base_state()` would give them live.
    out["base_state"] = base_state(out)
    return out


def project(now=None, selector=None):
    """Projected land loops, optionally narrowed to one work chain.

    Ref-less legacy rows (needs-redispatch, no exact tip) are not land loops
    and are skipped. The selector resolves against the complete eligible set
    before hydration so a globally ambiguous prefix never becomes unique by
    filtering."""
    lrs, _raw, unavailable = project_raw(now, selector=selector)
    return lrs, unavailable


ANCESTRY_PAIRS = "ancestry-pairs.jsonl"   # append-only; see _ancestry_ledger
ANCESTRY_BUDGET = 120                     # unseen pairs computed per pass
_ANCESTRY_MEMO = {}


def ancestry_pairs_path():
    return home.global_dir() + "/" + ANCESTRY_PAIRS


def _ancestry_ledger():
    """{(older, newer): bool} — is `older` an ancestor of `newer`.

    A LEDGER, NOT A CACHE, and the distinction is the whole design. Ancestry
    between two FIXED shas is IMMUTABLE: git history is append-only, so once
    both tips exist the answer can never change. There is therefore no
    invalidation problem and no staleness risk — a pair computed tonight is
    still true next year. That is what makes a bounded backfill legitimate
    against the projection's per-row-spawn law: the law forbids an
    UNBOUNDED walk every pass, not a converging one that never repeats work.

    Memoised on the file's own (size, mtime) so an append is picked up without
    anyone remembering to invalidate. Unreadable reads as EMPTY, which renders
    UNVERIFIED rather than guessing."""
    path = ancestry_pairs_path()
    try:
        st = os.stat(path)
        key = (path, st.st_size, st.st_mtime_ns)
    except OSError:
        return {}
    if _ANCESTRY_MEMO.get("key") == key:
        return _ANCESTRY_MEMO["pairs"]
    pairs = {}
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue          # one torn line never blinds the rest
                a, b = rec.get("older"), rec.get("newer")
                if a and b:
                    pairs[(a, b)] = bool(rec.get("anc"))
    except OSError:
        return {}
    _ANCESTRY_MEMO.clear()
    _ANCESTRY_MEMO.update(key=key, pairs=pairs)
    return pairs


def _ancestry_append(gitdir, unseen):
    """Compute up to ANCESTRY_BUDGET unseen pairs and append them. Returns what
    it learned. A pair that cannot be decided is NOT written — an unreadable
    answer must stay UNVERIFIED and be retried, never frozen into the ledger."""
    learned = {}
    if not unseen:
        return learned
    rows = []
    for older, newer in list(unseen)[:ANCESTRY_BUDGET]:
        try:
            rc, _out, _err = vcs.backend(gitdir).text(
                gitdir, "merge-base", "--is-ancestor", older, newer, timeout=15)
        except Exception:
            continue
        if rc not in (0, 1):
            continue                  # unreadable: leave it UNVERIFIED
        learned[(older, newer)] = (rc == 0)
        rows.append({"older": older, "newer": newer, "anc": rc == 0})
    if rows:
        try:
            with open(ancestry_pairs_path(), "a", encoding="utf-8") as fh:
                for r in rows:
                    fh.write(json.dumps(r, sort_keys=True) + "\n")
        except OSError:
            pass                      # in-memory answers still serve this pass
    return learned


CONFIRMATION_EVIDENCE = dispatches._RESOLUTION   # the door's OWN sentence

# THE RESOLVED DOOR'S ADMITTED CONFIRMATION POLARITIES (amendment A): approve
# or supersede, NEVER fix — a FIX verdict's meaning is that the work is NOT
# resolved. ONE constant, two readers (the `resolved` close rung and
# `confirmation_row`), so the door and the display classifier cannot drift
# apart. Minted for codex-2's finding (verdict on 1710265fd9a7): the first
# cut of confirmation_row never read polarity, so a FIX-polarity but
# otherwise confirmation-shaped row passed the predicate and minted a quiet
# self "c" + parent "b" out of the exact verdict that says "not resolved".
CONFIRMATION_POLARITIES = ("approve", "supersede")


def confirmation_row(lr):
    """Is this row the door's own CONFIRMATION ROUND — a value-space
    class, a row KIND the contrary classifier's vocabulary lacked?

    kind=review, dispatched --supersedes a contrary parent, verdict polarity
    in the door's OWN admitted set (CONFIRMATION_POLARITIES — approve or
    supersede, never fix), and its OWN verdict evidence OPENS the
    resolved-door sentence with a concrete resolution, read by the door's
    OWN parser (`dispatches.resolution_statement`, head-anchored; the
    sentence is CONFIRMATION_EVIDENCE, the exact contract `lr close --reason
    resolved` enforces; see VERBS.md). Such a row's reviewed tip is the CURE
    CARRIER, already on trunk BY DESIGN before the verdict was filed — that
    is the whole point of the round — so supersede-verdict + landed-tip is
    its healthy shape, not a defiance. Without this recognition every use of
    the resolved door minted one permanent CONTRARY row and every cure round
    ADDED one: the owner's card sat at 11 for hours while rows churned
    underneath (measured 2026-08-04/05: d0c72ad9, 62c5a2cc, 68e1a449,
    87a923eb — all ladder confirmations, all rendering contrary).

    EVERY RUNG IS THE DOOR'S OWN, SHARED NOT PARAPHRASED: the polarity gate
    reads the same constant the close rung reads, and the phrase gate calls
    the same parser the close rung calls — a paraphrase here is exactly how
    the polarity hole happened (a FIX-polarity row wearing the
    sentence passed the first cut; it stays LOUD now).

    THE PHRASE IS LOAD-BEARING. A supersede verdict WITHOUT it is exactly the
    contrary signature and stays loud — this predicate must never widen into
    "any supersedes review", because that is the laundering the classifier's
    keyword-inference ban (module header) exists to prevent: here the phrase
    is a WRITTEN CONTRACT of the resolved door, not sniffed sentiment. An
    alternate shape ("supersede honored through independent landing:")
    was checked against the live ledger: ZERO occurrences, so it
    is deliberately NOT accepted."""
    return lr.get("kind") == "review" and bool(lr.get("supersedes")) \
        and lr.get("polarity") in CONFIRMATION_POLARITIES \
        and dispatches.resolution_statement(lr.get("verdict_ref")) is not None


def _supersedes_reaches(candidate, target, current):
    """True | False | None — does CANDIDATE cite TARGET through its chain?

    The supersedes edge is the writer's explicit statement that one round
    continues another. Missing/cyclic ancestry is UNKNOWN, never a negative:
    a broken proof cannot authorize retirement, but it also cannot honestly
    say that no citation exists beyond the unreadable edge."""
    seen = set()
    node = candidate
    while True:
        parent = str(node.get("supersedes") or "")
        if not parent:
            return False
        if parent == target:
            return True
        if parent == dispatches.CHAIN_UNKNOWN or parent in seen:
            return None
        seen.add(parent)
        node = current.get(parent)
        if node is None:
            return None


def _carrier_evidence(row, relation):
    return {
        "id": str(row.get("id") or ""),
        "reviewed_tip": str(row.get("reviewed_tip") or ""),
        "relation": relation,
    }


def contrary_discharge(lr, chain, reach, pairs, unseen, current=None):
    """"a" | "b" | "c" | None | "unverified" — did succession DISCHARGE this
    row, so the contrary banner is a lie?

    All consumers share `_succession`: citation through the supersedes graph,
    or measured Git lineage from this row's tip to an approved on-trunk tip.
    Chain membership and timestamp order prove neither. The row itself remains
    the separate confirmation-instrument arm (`c`)."""
    if confirmation_row(lr):
        return "c"
    state, carrier = _succession(lr, chain, reach, pairs, unseen, current)
    if state == SUCCESSION_MOVED:
        return "b" if carrier["relation"] == "supersedes-ancestry" else "a"
    root = str(lr.get("chain_root") or "")
    if state == SUCCESSION_UNKNOWN and root \
            and root != dispatches.CHAIN_UNKNOWN:
        return "unverified"
    return None


SUCCESSION_MOVED = "moved"        # citation or measured lineage carried the row
SUCCESSION_HELD = "held"          # readable proof says no candidate carried it
SUCCESSION_UNKNOWN = "unknown"    # missing identity or unreadable proof, never "no"


def succession_facts(lr, chain, reach, pairs=None, unseen=None, current=None):
    """(state, carrier evidence) from ONE citation/lineage walk.

    `carrier` is the immutable evidence payload consumers need: id, full tip,
    and the relation that proved carriage. HELD and UNKNOWN name none. `pairs`
    is the ancestry ledger; an absent pair stays UNKNOWN and is added to
    `unseen` for bounded measurement rather than guessed."""
    return _succession(lr, chain, reach, pairs, unseen, current)


def succession_state(lr, chain, reach, pairs=None, unseen=None, current=None):
    """Did provable succession carry this row? moved | held | unknown.

    DISPLAY ONLY: `stalled` and `owed_by` remain untouched. UNKNOWN covers both
    missing chain identity and a carrier relation whose citation/ancestry
    cannot be read; neither may collapse into a confident HELD."""
    return _succession(lr, chain, reach, pairs, unseen, current)[0]


def _succession(lr, chain, reach, pairs=None, unseen=None, current=None):
    """The one walk. (state, carrier evidence)."""
    root = str(lr.get("chain_root") or "")
    if not root or root == dispatches.CHAIN_UNKNOWN:
        return SUCCESSION_UNKNOWN, None
    pairs = pairs or {}
    unseen = unseen if unseen is not None else set()
    if current is None:
        current = {str(row.get("id") or ""): row
                   for rows in chain.values() for row in rows if row.get("id")}
    target = str(lr.get("id") or "")
    target_tip = str(lr.get("reviewed_tip") or "")
    unknown = False
    for sib in chain.get(root, ()):
        if str(sib.get("id") or "") == target:
            continue
        tip = str(sib.get("reviewed_tip") or "")
        if not tip or tip not in reach:
            continue
        admitted = sib.get("polarity") == "approve" or confirmation_row(sib)
        if not admitted:
            continue
        cited = _supersedes_reaches(sib, target, current)
        if cited is True:
            return SUCCESSION_MOVED, _carrier_evidence(
                sib, "supersedes-ancestry")
        if cited is None:
            unknown = True
        if sib.get("polarity") != "approve" or not target_tip:
            continue
        if tip == target_tip:
            return SUCCESSION_MOVED, _carrier_evidence(sib, "tip-equal")
        known = pairs.get((target_tip, tip))
        if known is None:
            unseen.add((target_tip, tip))
            unknown = True
        elif known:
            return SUCCESSION_MOVED, _carrier_evidence(sib, "tip-descendant")
    return (SUCCESSION_UNKNOWN if unknown else SUCCESSION_HELD), None


def _annotate_succession(out, current, reach, gitdir=None):
    """Stamp succession facts on every row, measuring ancestry by budget."""
    gitdir = gitdir or os.getcwd()
    chain = {}
    for row in current.values():
        root = str(row.get("chain_root") or "")
        if root:
            chain.setdefault(root, []).append(row)
    pairs = _ancestry_ledger()
    unseen = set()
    for lr in out.values():
        if not lr.get("terminal"):
            succession_facts(lr, chain, reach, pairs, unseen, current)
    if unseen:
        learned = _ancestry_append(gitdir, unseen)
        if learned:
            pairs = dict(pairs); pairs.update(learned)

    def stamp(lr, known, missing):
        state, carrier = succession_facts(
            lr, chain, reach, known, missing, current)
        lr["succession_state"] = state
        lr["succession_carrier"] = carrier

    unseen = set()
    for lr in out.values():
        stamp(lr, pairs, unseen)
    if unseen:
        learned = _ancestry_append(gitdir, unseen)
        if learned:
            pairs = dict(pairs); pairs.update(learned)
            for lr in out.values():
                if lr.get("succession_state") == SUCCESSION_UNKNOWN:
                    stamp(lr, pairs, set())


def _trunk_reach(gitdir=None):
    """Commits reachable from origin/main. Empty on any trouble, which makes
    every ancestry answer fall back to its unknown/held arm rather than to a
    confident yes."""
    try:
        rc, listing, _e = vcs.backend(gitdir or os.getcwd()).text(
            gitdir or os.getcwd(), "rev-list", "--max-count=20000",
            "origin/main", timeout=30)
        return set(listing.split()) if rc == 0 else set()
    except Exception:
        return set()


def _annotate_contrary_discharge(out, current=None, gitdir=None, reach=None):
    """Stamp contrary discharge from the shared succession proof."""
    rows = [l for l in out.values() if l.get("contrary")]
    if not rows:
        return
    current = current or out
    gitdir = gitdir or os.getcwd()
    reach = _trunk_reach(gitdir) if reach is None else reach
    chain = {}
    for row in current.values():
        chain.setdefault(str(row.get("chain_root") or ""), []).append(row)
    # THE BUDGET GOES TO THE ROWS A READER SEES, FIRST. Measured:
    # all 231 contrary rows need 4796 unseen pairs, while the 52 IN-FLIGHT ones
    # need 24. Spending a shared budget in dict order starved the in-flight set
    # behind terminal history — three passes ran, the ledger grew by a full
    # budget each time, and the in-flight UNVERIFIED count never moved. A
    # backfill that converges only in principle is not converging.
    pairs = _ancestry_ledger()
    live = [l for l in rows if not l.get("terminal")]
    unseen = set()
    for l in live:
        contrary_discharge(l, chain, reach, pairs, unseen, current)
    if unseen:
        learned = _ancestry_append(gitdir, unseen)
        if learned:
            pairs = dict(pairs)
            pairs.update(learned)
    unseen = set()
    for l in rows:
        l["contrary_discharge"] = contrary_discharge(l, chain, reach, pairs, unseen, current)
    if unseen:
        learned = _ancestry_append(gitdir, unseen)
        if learned:
            pairs = dict(pairs); pairs.update(learned)
            for l in rows:
                if l.get("contrary_discharge") == "unverified":
                    l["contrary_discharge"] = contrary_discharge(
                        l, chain, reach, pairs, set(), current)


def _selected_chain_ids(current, eligible, selector):
    """(raw ids, err) for SELECTOR's complete declared work chain.

    Hydration is narrow; identity and topology are not. Resolve against every
    eligible row first, seed every row with the same effective root, then walk
    supersedes in both directions through the full raw snapshot. Cancelled rows
    remain traversable transit, forks stay visible, and unrelated legacy roots
    or CHAIN_UNKNOWN rows never collapse into one bucket."""
    row, err = dispatches._resolve_row(
        eligible, selector, noun="land request", list_hint="helm lr list")
    if err:
        return None, err
    selected = str(row.get("id") or "")
    root = _effective_root(row)
    seeds = {selected}
    if root is not None:
        seeds.update(rid for rid, raw in current.items()
                     if _effective_root(raw) == root)
    children = {}
    for rid, raw in current.items():
        parent = raw.get("supersedes")
        if parent:
            children.setdefault(parent, []).append(rid)
    seen, stack = set(), list(seeds)
    while stack:
        rid = stack.pop()
        if rid in seen:
            continue
        seen.add(rid)
        parent = (current.get(rid) or {}).get("supersedes")
        if parent in current:
            stack.append(parent)
        stack.extend(children.get(rid, ()))
    return seen, None


def project_raw(now=None, selector=None):
    """({id: lr}, {id: RAW ledger row}, unavailable) — ONE read, both views.

    A selector narrows only expensive row hydration to its work-chain closure.
    The canonical raw snapshot remains complete for integrity, transit, epoch,
    succession, and frontier annotations.

    The chain topology needs rows this projection deliberately drops
    (status=cancelled is transit, not a land loop), and reading the ledger a
    SECOND time to get them would reintroduce the two-instants bug this
    function's own docstring exists to warn about. So the raw snapshot rides
    out with the projection instead."""
    now = time.time() if now is None else now
    # ONE READ, EVERY DERIVATION. The state fold, the per-row events, the
    # events the fold ACTUALLY TOOK and the POSITION AND IDENTITY of each
    # accepted verdict are four views of one instant, and the third is why the
    # second cannot be trusted alone: with two same-kind events and one
    # accepted, only the fold knows which. The fourth is why the gate epoch
    # rides this read too: a boundary expressed as an append position may only
    # be compared with positions counted in the same pass of the same prefix.
    #
    # The state fold and the per-row events are two
    # projections of the same ledger and they must come from the same instant.
    # Read separately they were two instants, and @codex reproduced what fits
    # between them: an append that lands after the snapshot and before the
    # grouped read puts a row's DELIVERY in the timeline while the headline
    # still says OPEN — a card contradicting itself with `unavailable: None`,
    # because both reads succeeded and neither was missing anything. See
    # `dispatches.snapshot_and_events`.
    #
    # The failure half is unchanged and still load-bearing: a read that fails
    # refuses the whole projection rather than keeping the state and losing
    # every transition stamp, which is how an hour-old delivered row once
    # rendered AWAITING_REVIEW at 0m with nothing stalled and nothing said.
    current, by_id, taken_by_id, verdicts, unavailable = \
        dispatches.snapshot_and_events()
    if unavailable:
        return {}, {}, unavailable
    # AND A ROW ID THE RECORD HOLDS THAT THE FOLD COULD NOT OPEN, which is the
    # same hole one layer earlier than every other check on this projection.
    #
    # `strict=True` validates the PHYSICAL shape of each line — complete JSON,
    # an object, a non-empty id — and says nothing about whether those events
    # amount to an obligation. A ledger whose ONLY row is a well-formed
    # `delivered` event with no dispatch before it therefore READ cleanly:
    # `_new_state` refused the genesis, `_fold` skipped the id, and the board
    # went out with `loops: []` and `unavailable: null`. The card rendered "the
    # dispatch ledger READ cleanly" over a record it had wholly failed to
    # project (@codex-3).
    #
    # IT CANNOT BE REPORTED PER-ROW EITHER, and that is what makes it belong
    # here. `ledger_refused` hangs off a canonical row; at genesis there is no
    # row to hang it on, so the per-row display path is structurally blind to
    # it. The claim this function makes is about the WHOLE record, so an id it
    # could not open is a hole in that claim — exactly like the projection
    # failure below, and answered the same way rather than two ways.
    unopenable = sorted(rid for rid in by_id if rid not in current)
    if unopenable:
        return {}, {}, ("the dispatch ledger holds %d row id(s) with events but no "
                    "openable dispatch (%s%s) — helm read every line and cannot "
                    "say what obligations they are, so the board is INCOMPLETE "
                    "and will not render part of it as the whole; repair those "
                    "ledger rows"
                    % (len(unopenable), ", ".join(r[:12] for r in unopenable[:3]),
                       ", …" if len(unopenable) > 3 else ""))
    # ONE epoch for the whole projection, so every row is judged against the
    # same cutover rather than against whatever its own reader happened to see.
    #
    # AND A THROW HERE DEGRADES TO EPOCH_LOST rather than killing the whole
    # board. @codex: this call sat OUTSIDE the per-row try, so a marker holding
    # `founder: []` raised TypeError out of gate_epoch and took every land loop
    # with it — a projection that refuses one row is the designed worst case; a
    # projection that raises is an outage. gate_epoch validates its own marker
    # now, so this is the second wall, and the only reading of an unreachable
    # boundary that is safe is the one that authorizes nothing.
    try:
        epoch = dispatches.gate_epoch(current, verdicts)
    except Exception:                   # noqa: BLE001 — refuse, never crash
        epoch = dispatches.EPOCH_LOST
    receipts = _receipts_by_tip()       # one write-through index read, folded in
    eligible = {rid: row for rid, row in current.items()
                if not row.get("migration") and row.get("tip")
                and row.get("status") != "cancelled"}
    if selector is not None:
        wanted, err = _selected_chain_ids(current, eligible, selector)
        if err:
            return {}, current, err
        eligible = {rid: row for rid, row in eligible.items() if rid in wanted}
    # ONE ATTEST SIDECAR READ TOO, only for rows this projection will consume.
    # An unreadable sidecar makes that axis UNKNOWN per row; it never erases a
    # dispatch or turns the workflow board into an empty one.
    attests = dispatches.attest_projections(eligible.values())
    # ONE FOLD FOR THE CONSUMED-EVIDENCE LINK, from the snapshot already read.
    # Built over `current` rather than `eligible`: a close that consumed a
    # confirmation still vouches for it even when the CLOSING row is itself
    # filtered out of the board (cancelled transit, migration), and dropping
    # that link would re-mint the debt this exists to retire.
    consumed = _consumed_confirmations(current)
    cache, out = {}, {}
    # ONE STORE READ FOR THE WHOLE PROJECTION. `_lr` -> `approval_tier` ->
    # `load_certain_policy` -> `load_all` re-walked and re-parsed the ENTIRE
    # typed store PER ROW. Profiled 2026-07-31 over 312 land loops: 30.7 of 41
    # seconds inside that one chain, 716 root loads and 244,335 frontmatter
    # parses of the same 1,308 files. The owner's console card budgets 12s for
    # /api/lr, so the land pipeline was UNREADABLE on the surface built to show
    # it — it rendered "DISPATCH LEDGER UNREADABLE" while the ledger was fine.
    #
    # The scope is bounded to THIS CALL and drops on exit, so no policy read can
    # outlive the projection that asked for it. That matters here specifically:
    # a stale-policy cache would be the same failure class as the 40h-old web
    # server and the 8-day-old console render this fix exists because of.
    #
    # AND ONE ANSWER PER QUESTION FOR THE DERIVED READS TOO — `projscope`, same
    # law, same shape, one scope wide enough to cover the annotations below
    # because they shell out to git exactly as the row loop does. That commit
    # ("one store read per projection") named what it was leaving:
    # "the remaining 10.1s is git (865 subprocess spawns through
    # _landing_proof) ... left alone deliberately and filed separately." The
    # ledger has since grown from 312 land loops to 1222 and the deferred half
    # is now the whole cost: MEASURED 2026-08-06, 4140 spawns and 627
    # `approval_tier` resolutions over SIXTEEN distinct (recipient, repo) keys,
    # each one walking every fd in /proc and firing a network canary. See
    # helm/projscope.py for why asking one question once is a correctness fix
    # here and not only a faster one.
    with store_load.read_scope(), projscope.scope():
        for rid, row in eligible.items():
            try:
                out[rid] = _lr(row, by_id.get(rid, ()), taken_by_id.get(rid, ()),
                               cache, now, attests[rid], receipts,
                               index=dispatches.verdict_index(verdicts, rid),
                               epoch=epoch, consumed=consumed)
            except Exception as e:
                # A ROW THAT CANNOT BE PROJECTED IS NOT A ROW THAT IS NOT THERE.
                # This used to `continue`, which is a SILENT PARTIAL: the snapshot
                # held the row, the board went out without it, and `unavailable`
                # stayed None — so the card printed one fewer lane and said it had
                # read cleanly. With one row on the ledger that is a board of "0
                # in flight" over a record that holds a land loop, which is the
                # single sentence this surface exists to never say. @codex
                # reproduced it with `repo_id: ["not", "a", "path"]`: unhashable
                # as a cache key, TypeError out of `_trunk_refs`, row gone.
                #
                # The rows that DID project are not certifiable either, because
                # helm does not know whether the fault belongs to this row or to
                # the read that produced all of them. So a partial projection
                # reports itself as a failed one, exactly as the half-read ledger
                # above does, and names the row that has to be repaired.
                return {}, {}, ("land loop %s could not be projected (%s: %s) — the "
                            "board is INCOMPLETE and helm will not render part of "
                            "it as the whole; repair that ledger row"
                            % (rid, type(e).__name__, str(e)[:120]))
        reach = _trunk_reach()
        _annotate_succession(out, current, reach)
        _annotate_contrary_discharge(out, current=current, reach=reach)
        _annotate_frontier_debt(out, current)
    return out, current, None


# The EIGHT proof values §2.7 binds, recorded PER ANCESTOR. Two of them relieve
# and six do not, and collapsing the six into one word was the shortfall the
# integrator refused to waive: `cross-repo` and `no-pin` are SEMANTIC states,
# not shades of "unknown". repo_id already carries FOUR distinct values in the
# live ledger (spec §2.6), so a reader who cannot tell "this ancestor is in
# ANOTHER REPOSITORY" from "git could not read it" has lost the one fact that
# decides whether the debt is even ours to bill.
PROOF_ANCESTOR = "ancestor"
PROOF_PATCH_EQUIVALENT = "patch-equivalent"
PROOF_ABSENT = "absent"
PROOF_UNKNOWN = "unknown"
PROOF_NO_TIP = "no-tip"
PROOF_NO_REPO = "no-repo"
PROOF_CROSS_REPO = "cross-repo"
PROOF_NO_PIN = "no-pin"

# `patch-equivalent` RELIEVES and is never flattened into `ancestor`: this repo
# cherry-picks its lands, so relief that cannot say WHICH proof carried it is
# not falsifiable, and a later reader would have to re-run git to find out.
_PROOF_RELIEVES = (PROOF_ANCESTOR, PROOF_PATCH_EQUIVALENT)
# carrier_discharge is a TRI-STATE over those eight. False means PROVEN not on
# trunk; None means the question could not be answered, which is never a "no".
_PROOF_DISCHARGE = {
    PROOF_ANCESTOR: True, PROOF_PATCH_EQUIVALENT: True, PROOF_ABSENT: False,
}

FRONTIER_DEBT_KNOWN = "known"
FRONTIER_DEBT_UNKNOWN = "unknown"

# A carrier RELIEVES only on one of these. `absent` and `unknown` both KEEP the
# debt, and that collapse is load-bearing: landreq's own `_landing_proof` and
# `vcs.landed_state` disagree about which of those two a row is (measured
# 2026-08-05 — _landing_proof answers `absent` for a sha whose object is not in
# the repo at all, and for a range whose merges leave `git cherry` short), so a
# rule that distinguished them would inherit that disagreement. This one cannot.


def _descend(rid, kids, node, take):
    """Every descendant of `rid` where `take(cid, row)` holds, NOT descending
    past one that does.

    THE THREE QUESTIONS THIS FILE ASKS ABOUT A CHAIN DIFFER ONLY IN THEIR STOP
    CONDITION, and writing them as three walks meant three chances to get the
    cycle guard, the transit-node handling or the fork semantics subtly
    different — a second census inherits none of the first one's scars. So the
    traversal is stated once and the QUESTION is the parameter:

      who carries this row      take = it carries
      is the debt discharged    take = it carries AND its work is on trunk
                                (so the walk continues PAST an unlanded
                                carrier, which is the whole correction)
      which visible rows stand  take = it survives to the board
        for this chain

    STOPPING AT A HIT bounds the work and matches `_relieved`, which returns
    at its first carrier. HONESTY ABOUT ITS FORCE: I first wrote that this was
    "the semantic, not an optimisation", and a mutation refuted me — removing
    the stop broke no test, because no CURRENT caller can observe it (a board
    row cannot have a carrying descendant: such a descendant would relieve it,
    and a relieved row is not on the board — measured 0 of 16 live). It is
    pinned below as a CONTRACT of this primitive rather than of its callers,
    so a fourth question added later inherits it deliberately.

    `node.get(cid)` may be None — a cancelled row is TRANSIT, never billable
    and always traversable — so every predicate must tolerate it."""
    out, seen, stack = [], set(), list(kids.get(rid, ()))
    while stack:
        cid = stack.pop()
        if cid in seen:
            continue
        seen.add(cid)
        if take(cid, node.get(cid)):
            out.append(cid)
            continue
        stack.extend(kids.get(cid, ()))
    return out


def _annotate_frontier_debt(out, current, gitdir=None):
    """Bill the FRONTIER for the ancestors it is suppressing while having
    landed nothing.

    THE RULING THIS IMPLEMENTS, and it is not the obvious one. Mechanism A's
    first draft narrowed `_absorbs_debt` so a live carrier stops absorbing
    unless its own tip reached trunk. That is the right OBSERVATION — a
    successor that exists but has landed nothing has not discharged its
    parent — and the wrong OUTPUT: measured 2026-08-05 over the live board,
    5 of 5 parents it un-suppressed had their live carrier ALSO on the board,
    so every single one rendered the same chain twice. `_loop_rows` states the
    invariant that forbids exactly that: "SHOWING BOTH AS IN-FLIGHT
    DOUBLE-BILLS ONE CHAIN."

    So BOARD MEMBERSHIP IS UNTOUCHED HERE. `_absorbs_debt` and `_relieved` are
    not narrowed, the parent stays suppressed, and the fact moves to the row
    someone can actually act on. A parent nobody can move is not a debt
    reminder, it is a second name for the frontier's own work.

    WHY vcs.landed_state RATHER THAN THIS MODULE'S `_landing_proof`: the two
    disagree, and vcs is the conservative one. `_landing_proof` answers
    `absent` — a claim of PROOF — for a sha whose object is not in the repo at
    all, and for a range where merges leave `git cherry` unable to account for
    every commit. vcs returns UNKNOWN in both, which is the honest reading, and
    a vanished object is not evidence that nothing landed.

    RECOMPUTED EVERY PASS, MEMOISED NEVER. The answer is a function of CURRENT
    trunk, so a trunk rewrite, force-push or reset flips it on the next
    projection with no operator action (spec A-REOPEN). The immutable ancestry
    ledger stays a fast path for a FIXED pin and is never consulted as "was
    this row relieved" — a new pin is a new question.

    UNREADABLE TOPOLOGY IS UNKNOWN, NEVER EMPTY. If the chain forest cannot be
    trusted, every row reads FRONTIER_DEBT_UNKNOWN rather than an empty list,
    because "I checked and this frontier owes nothing" and "I could not check"
    are different answers and a caller must not be handed the reassuring one by
    default."""
    for lr in out.values():
        lr["frontier_debt"] = []
        lr["frontier_debt_mark"] = ""
        lr["frontier_debt_state"] = FRONTIER_DEBT_UNKNOWN
        lr["carrier_discharge"] = None
        lr["carrier_discharge_proof"] = PROOF_UNKNOWN
    kids, node, err = _chain_forest(current, out)
    if err:
        return                              # every row already reads UNKNOWN
    from . import vcs
    cache, roots = {}, {}

    def _pin(repo_id, root):
        """(trunk ref, err) for one repository, resolved ONCE per pass.

        A repository that cannot prove its own trunk object relieves NOTHING —
        the positive control `_close_ladder_resolved` already applies at its
        rung 0, reused here rather than restated: "refusing to adjudicate a
        resolution over an unreadable substrate"."""
        if root not in roots:
            try:
                local, upstream = _trunk_refs(repo_id or root, roots)
                trunk = upstream if _origin_configured(repo_id or root) \
                    else local
            except Exception:
                trunk = None
            if trunk and _object_exists(repo_id or root, trunk) is not True:
                trunk = None                # cannot prove its own trunk object
            roots[root] = trunk
        return roots.get(root)

    def proof_for(owner, carrier):
        """The EIGHT-value proof for whether `carrier` discharged `owner`.

        ORDER IS FAIL-CLOSED: every question that can be answered WITHOUT git
        is asked first, so a cross-repository pair is named as such rather than
        being handed to a backend that would answer about the wrong trunk."""
        crepo = str(carrier.get("repo_id") or "")
        orepo = str(owner.get("repo_id") or "")
        if not crepo:
            return PROOF_NO_REPO
        if orepo and crepo != orepo:
            # One repository's trunk proves nothing about another's, and a debt
            # is never suppressed across that line.
            return PROOF_CROSS_REPO
        tip = str(carrier.get("reviewed_tip") or "")
        if not tip:
            return PROOF_NO_TIP
        root = crepo[:-5] if crepo.endswith("/.git") else (gitdir or "")
        if not root:
            return PROOF_NO_REPO
        trunk = _pin(crepo, root)
        if not trunk:
            return PROOF_NO_PIN
        key = (root, tip, trunk)
        if key not in cache:
            try:
                got = vcs.backend(root).landed_state(root, tip, trunk)
            except Exception:
                got = None                  # a broken probe is UNKNOWN
            cache[key] = {
                "ancestor": PROOF_ANCESTOR,
                "patch-equivalent": PROOF_PATCH_EQUIVALENT,
                "not-ancestor": PROOF_ABSENT,
                "absent": PROOF_ABSENT,
            }.get(got, PROOF_UNKNOWN)
        return cache[key]

    def landed(lr, owner=None):
        """Kept as the boolean the walks ask for; the PROOF is the record."""
        return proof_for(owner if owner is not None else lr, lr) \
            in _PROOF_RELIEVES

    def discharged(rid):
        """Does ANY descendant that carries `rid` have work on trunk?

        WALKS PAST AN UNLANDED CARRIER, and that is the whole correctness of
        this function. A suppression walk stops at today's carrier because
        `_relieved` returns there; asking whether the DEBT is discharged is a
        different question, and a carrier that landed nothing may itself have a
        successor that did. Measured 2026-08-05: stopping at the first carrier
        billed 20 ancestors where only 5 were genuinely owed — 15 of them were
        discharged deeper in their own chain, and billing a frontier for those
        would have re-invented the over-count from the other end."""
        owner = out.get(rid) or {}
        return bool(_descend(
            rid, kids, node,
            lambda _c, row: row is not None and _carries(row)
            and landed(row, owner)))

    def board_heirs(rid):
        """The rows a reader will actually SEE that stand for `rid`'s chain.

        Billing the IMMEDIATE carrier is not enough and the live board proves
        it: on a chain A -> B -> C where nothing landed, B carries A and C
        carries B, but B is itself suppressed, so the only visible row (C)
        reported ONE owed round out of three. Measured 2026-08-05: 5 billed
        frontiers, only 2 of them on the board. A debt parked on an invisible
        row is not a debt reminder.

        So the walk continues THROUGH suppressed descendants and attributes the
        ancestor to every descendant that survives to the board — the same
        `not terminal and not _relieved` predicate `_loop_rows` renders by."""
        return _descend(rid, kids, node,
                        lambda cid, _row: cid in visible)

    # THE BOARD SET, COMPUTED ONCE. `board_heirs` used to ask
    # `_relieved(cid, ...)` per candidate, and `_relieved` walks the subtree —
    # so the annotation was quadratic and a full projection took 64s, of which
    # only 14s was git. Measured, not guessed: the profile said 158 landed_state
    # calls with a 100% cache hit rate, which is what sent me looking here
    # instead of at the backend.
    visible = {lr["id"] for lr in out.values()
               if not lr.get("terminal") and not _relieved(lr["id"], kids, node)}

    def best_proof(rid):
        """This row's OWN relief proof, over its carriers, deterministically.

        A relieving carrier wins outright — one proven land discharges the row
        however many other carriers went nowhere. Otherwise the carriers are
        read in sorted id order so two runs over one ledger never disagree."""
        cands = sorted(_descend(rid, kids, node,
                                lambda _c, row: _carries(row)))
        owner = out.get(rid) or {}
        seen = []
        for cid in cands:
            carrier = node.get(cid)
            if carrier is None:
                continue
            p = proof_for(owner, carrier)
            if p in _PROOF_RELIEVES:
                return p                    # proven land: nothing else matters
            seen.append(p)
        # NO CARRIER AT ALL is not one of the eight. The eight answer "did the
        # carrier discharge this row"; a row nobody carries has no such
        # question, and fabricating `no-tip` for it would be the same collapse
        # the eight exist to prevent, one case further out.
        return seen[0] if seen else None

    owed = {}
    for lr in out.values():
        # LIVE ROWS ONLY. A terminal row is not awaiting anyone's carrier, and
        # asking git about all 1050 rows instead of the 183 live ones turned a
        # sub-second pass into a timeout — measured, not predicted.
        if lr.get("terminal"):
            continue
        proof = best_proof(lr["id"])
        lr["carrier_discharge"] = _PROOF_DISCHARGE.get(proof) if proof else None
        lr["carrier_discharge_proof"] = proof
        if discharged(lr["id"]):
            continue
        for cid in board_heirs(lr["id"]):
            owed.setdefault(cid, []).append((lr["id"], proof))
    for lr in out.values():
        rounds = sorted(owed.get(lr["id"], ()))
        lr["frontier_debt"] = [{"id": rid, "carrier_discharge_proof": p,
                                "carrier_discharge": _PROOF_DISCHARGE.get(p)}
                               for rid, p in rounds]
        # ONE mark per frontier, never one per ancestor: a wall of per-ancestor
        # marks on a single row is the attention-budget failure in a different
        # costume. The §2.7 texts are re-phrased from "%s carries this row" to
        # "this row carries %s", which is the whole of ruling (a) in a sentence.
        lr["frontier_debt_mark"] = (
            "THIS ROW CARRIES %d UNDISCHARGED ROUND(S): %s — still live debt"
            % (len(rounds), ", ".join("%s (%s)" % (rid[:12], p)
                                      for rid, p in rounds))) if rounds else ""
        lr["frontier_debt_state"] = FRONTIER_DEBT_KNOWN


def _order(lr):
    return (STAGE_ORDER.get(lr["state"], 9), -lr["dwell_s"])


def _loop_rows(lrs, raw, include_landed=False):
    """Rows for the primary board: carrying chain frontiers by default.

    A superseded predecessor may remain canonically open because no close event
    was minted for it, while an absorbing descendant already owns its debt.
    Showing both as in-flight double-bills one chain. `--all` stays historical
    and preserves every row; the default view folds only validated topology.
    """
    if include_landed:
        return sorted(lrs.values(), key=_order)
    kids, node, err = _chain_forest(raw, lrs)
    if err:
        raise _ChainUntrustworthy(err)
    vals = [lr for lr in lrs.values()
            if not lr["terminal"] and not _relieved(lr["id"], kids, node)]
    return sorted(vals, key=_order)


def loops(include_landed=False, now=None):
    """(sorted land loops, unavailable). Carrying frontiers by default."""
    lrs, raw, unavailable = project_raw(now)
    if unavailable:
        return None, unavailable
    try:
        return _loop_rows(lrs, raw, include_landed), None
    except _ChainUntrustworthy as e:
        return None, str(e)


def filed_split(lrs, raw):
    """The all-time FILED population behind the board, partitioned. ONE owner
    for the derivation: /api/lr's card strip and `helm lr list`'s headers both
    render this split, and two walks would let the two surfaces disagree about
    one record (the owner's 2026-08-04 ask, and codex-2's finding that the
    first cut gave the CLI only the total).

    THE BUCKET IS NAMED `open`, NOT `in_flight`, AND THAT RENAME IS THE POINT
    (task/324). This docstring already said the honest thing — "a board list
    that folds chain frontiers can read lower, and both are true" — while the
    surface printed BOTH numbers under the one word "in flight": this split's
    raw count and `_loop_rows`' chain-folded one. The owner read 32 and 277
    on ONE PAGE under one noun, 8.6x apart, and no reader could tell which was
    lying. Neither was. They answer DIFFERENT QUESTIONS:

      open (here)      every non-terminal FILED row except REVIEWED. Ledger
                       accounting: how much is on the books.
      in flight        `_loop_rows` — non-terminal AND not relieved by a
                       debt-absorbing descendant, i.e. CHAIN-FOLDED, minus
                       honored. Work actually moving: a superseded parent
                       whose child carries the debt is not a second live loop.

    So "in flight" now belongs to exactly ONE of them, and this one says
    `open`. A knowledge that lives only in a docstring is not a surface — the
    fix is the word a reader sees, not a better comment.

    Buckets partition len(raw) EXACTLY — sum the strip and you get the total,
    so no bucket can silently leak rows. `held` is the REVIEWED parking state — undeclared polarity, or a
    declared approval held by tier/gate policy ("declared verdict held").
    `landed` is a retirement whose change is on trunk (git-observed or
    record-declared); `closed` a retirement of any other kind; `non_loop`
    every row that is not a land loop (cancelled transit, migration copies,
    ref-less legacy rows)."""
    filed = {"total": len(raw), "open": 0, "held": 0, "landed": 0,
             "closed": 0, "non_loop": len(raw) - len(lrs)}
    for lr in lrs.values():
        if not lr["terminal"]:
            filed["held" if lr["state"] == "REVIEWED" else "open"] += 1
        elif lr["landed"] or lr["state"] == "LANDED" \
                or lr.get("close_reason") == "landed":
            filed["landed"] += 1
        else:
            filed["closed"] += 1
    return filed


def inflight_rows(rows):
    """The rows that are IN FLIGHT — work actually moving. ONE owner for the
    derivation, for the same reason `filed_split` has one: two front-ends
    computing it separately is not a hypothetical, it is what happened.

    `_loop_rows` already folds chains; this drops the two kinds that are not
    work — a retirement (carried only by `--all`) and an HONORED row, whose
    verdict was discharged through succession or which IS the confirmation
    instrument. The browser has partitioned honored out since 2026-08-04
    (`00-core.js` lrHonored, `live.length + " in flight"`), on the owner's
    ruling that "superseded, if verified, should just be like another type of
    closed" — while `helm lr list` counted them IN, so one word named two
    numbers ACROSS the two surfaces even after the filed bucket was renamed
    to fix it WITHIN one (@codex, round 2). The predicate is `honored_display`,
    the same one every python surface asks and the twin of the browser's
    `lrHonored`, so the two cannot drift apart again without a parity test
    failing.

    THE ROWS ARE STILL SHOWN. This is the accounting, not the listing —
    an honored row keeps its SUPERSEDED-CLOSED mark on every surface."""
    return [lr for lr in rows
            if not lr["terminal"] and not honored_display(lr)]


def filed_line(filed):
    """The strip, one shape on every surface: `helm lr list` prints this
    string and the card's lrCardHTML mirrors it term for term (pinned against
    each other in tests), so a number the owner pastes from either surface
    reads identically on the other."""
    return ("filed %(total)d all-time · %(open)d open · %(held)d "
            "held · %(landed)d landed · %(closed)d closed · %(non_loop)d "
            "non-loop" % filed)


# THE SUPERSESSION EDGE, READ IN THE DIRECTION THE LEDGER NEVER WRITES.
# `supersedes` is stamped on the CHILD when the round is dispatched;
# `superseding_id` is stamped on the PARENT only by a close ladder — so it is
# empty on every chain that is still moving (measured 0 of 21 live predecessors
# on 2026-08-01). The primary board and both debt classifiers used to ask each
# row about itself, and a row cannot answer a question about its own future.
# They derive the edge ONCE, here, and consume it.
#
# A round that a later round supersedes is a BILLING artifact, not a debt: the
# projection accounts per ROW while the unit of work is the CHAIN.
_DEBT_ABSORBING = ("LANDED", "SUPERSEDED")
# THE CLOSE REASONS THAT CARRY WORK FORWARD. `landed` put the change on trunk,
# `superseded`/`subsumed` handed it to a resolved later round, and
# `delivered-report` handed off the build artifact under explicit references;
# each absorbs the parent's debt. `stranded`, `abandoned`, `withdrawn` and
# `out-of-scope` did NOT — they end the work, so whatever the parent owed is
# still owed.
_DEBT_ABSORBING_CLOSE = ("landed", "superseded", "subsumed",
                         "delivered-report")


def _absorbs_debt(kid):
    """Does this successor take on its parent's debt?

    codex-3, refuting the first cut by topology rather than by reading: the first
    version asked only for the PROJECTED STATE to be LANDED/SUPERSEDED, and a
    row's state does not track its closure. A child closed close_reason=landed
    can still project REVIEWED, and close_reason=superseded can still project
    CHANGES_REQUESTED — both TERMINAL, both absorbing, both invisible to a
    state tuple. His probes returned [p] where [] was correct, so the parent
    stayed billed for a debt its successor had already discharged.

    THAT IS THE SAME MISTAKE THIS LANE EXISTS TO FIX, one layer over: I keyed
    on a PROJECTION of the fact instead of the fact. The close reason is what
    the ledger actually records; the state is a rendering of it."""
    if not kid["terminal"]:
        return True                       # a live successor carries the debt
    if kid.get("close_reason") in _DEBT_ABSORBING_CLOSE:
        return True
    if kid.get("closed_by_landing") or kid.get("landed"):
        return True
    # Rows that reached a terminal STATE without a recorded close reason —
    # legacy shapes the ledger never retro-fits — still read honestly here.
    return kid["state"] in _DEBT_ABSORBING


def _chain_children(lrs):
    """{parent id -> [child rows]} for every supersedes edge on the board."""
    kids = {}
    for lr in lrs.values():
        parent = lr.get("supersedes")
        if parent:
            kids.setdefault(parent, []).append(lr)
    return kids


class _ChainUntrustworthy(Exception):
    """The supersedes graph cannot be trusted, so NO classifier answer is
    honest. Carried as an exception rather than a per-row flag because the
    surface is all-or-nothing: handing a caller some rows AND an unavailable
    notice is two answers to one question."""


def _effective_root(row):
    """This endpoint's PROVEN chain identity on an edge, or None.

    THE TWO ENDS ARE NOT SYMMETRIC, and treating them alike is the regression
    codex-3 caught in review: I refused any edge whose parent lacked a root,
    which DARKENED THE WHOLE BOARD for an ordinary legacy-parent -> v3-child
    continuation. That is worse than the hole it closed — refusing valid work
    beats folding an unprovable edge only in the direction nobody wanted.

      known root            -> that root.
      no root, names nobody -> its own id: it ROOTS ITS OWN CHAIN, which is
                               what _resolve_chain roots a child at.
      no root, names someone-> None: a continuation with no recorded identity
                               has no self-rooting excuse.
      CHAIN_UNKNOWN         -> None, on either end, always.

    ONE RULE, NO ROLE ARGUMENT — and that is the point rather than a tidy-up.
    The asymmetry codex-3 named is REAL, but it is not a property of the
    endpoints, it is a property of the TOPOLOGY: a child always names a
    parent, so it can never take the self-rooting branch. I first wrote an
    explicit is_child parameter and a mutation proved it decides NOTHING —
    deleting the branch changed no answer, because the supersedes test that
    follows already covers every child. A guard a mutation shows to be inert
    is decoration, and this repo removes those rather than keeping them.

    KEYED ON TOPOLOGY, NOT SCHEMA VERSION. My first cut branched on v in
    (1,2), and the census refuted it: 392 of the 398 root-like rows are v3,
    not legacy. Whether a row roots its own chain is answered by whether it
    NAMES A PARENT, and that answer is the same in every schema — so the rule
    needs no version table and cannot rot as versions advance.

    CHAIN_UNKNOWN IS NEVER IDENTITY ON EITHER SIDE. It is the replayed form of
    a MALFORMED field, deliberately never demoted to ABSENT because ABSENT is
    the permissive branch; two rows both reading UNKNOWN are two independently
    corrupted rows, and their equality is agreement that they are broken, not
    proof they share a chain.

    NO STRING/TYPE/SHAPE RE-VALIDATION HERE: dispatches._replay_chain is the
    single owner and its docstring forbids consumers re-checking, because two
    guards rejecting one value means mutating either leaves the other
    rejecting and NEITHER is measured. Post-replay the value is exactly
    id | None | CHAIN_UNKNOWN.

    SCOPED TO EDGES, NEVER TO ROWS: 392 of 628 live rows carry root=None with
    no supersedes and are perfectly healthy — they root their own chain and
    the writer seals the id later. A row-level predicate would call all of
    them corrupt."""
    root = row.get("chain_root")
    if root == dispatches.CHAIN_UNKNOWN:
        return None
    if root is not None:
        return root
    # Roots its own chain iff it names no parent — true for a parent that is
    # a chain root, false for every child by construction.
    return row.get("id") if not row.get("supersedes") else None


def _chain_forest(raw, lrs):
    """({parent id -> [child ids]}, {id -> row-or-None}, err) over the RAW rows.

    BUILT FROM THE LEDGER, NOT THE PROJECTION. project() drops
    status=cancelled at the top of its loop, while the chain grammar legally
    permits a successor of a cancelled parent — so a projection-only graph
    SILENTLY DISCONNECTS `p -> cancelled -> live`, and the live carrier stops
    absorbing p's debt (codex-3, immutable-tip audit). A cancelled node is
    TRANSIT: never billable, always traversable.

    INTEGRITY IS FAIL-CLOSED AND TOTAL. A cycle, or a child whose chain_root
    disagrees with its parent's, makes the WHOLE surface unavailable rather
    than yielding a partial row list — because "this row still bills" and
    "the board is unavailable" are two different external answers and a
    caller cannot be handed both. A topology we cannot trust must never be
    able to SUPPRESS a debt."""
    kids, node = {}, {}
    for rid, row in (raw or {}).items():
        node[rid] = lrs.get(rid)          # None => transit-only (cancelled)
        parent = row.get("supersedes")
        if parent:
            kids.setdefault(parent, []).append(rid)
            prow = raw.get(parent) or {}
            proot, croot = _effective_root(prow), _effective_root(row)
            if parent in raw and (proot is None or croot is None):
                # AN ABSENT ROOT IS UNKNOWN IDENTITY, NOT A PASS (codex-3,
                # in refutation). The first version required BOTH roots to
                # be truthy before comparing them, so a v3 child that names a
                # parent and omits chain_root skipped validation entirely and
                # the fold went on to SUPPRESS its parent with unavailable
                # None — the exact "cannot trust the graph, still suppressed a
                # debt" outcome the rest of this function exists to prevent.
                #
                # LEGACY IS NOT THIS SHAPE and stays unaffected: a pre-chain
                # row carries NO supersedes at all, so it forms no edge and is
                # never validated. A row that DOES name a parent while
                # carrying no root is a v3 anomaly, and the ledger cannot
                # testify that the two belong to one chain.
                return None, None, (
                    "chain topology is UNTRUSTWORTHY: the edge %s -> %s "
                    "carries no chain root on %s — a supersedes link whose "
                    "own identity is unrecorded cannot be proved to join one "
                    "chain, and folding across it would suppress a debt on an "
                    "unprovable relationship"
                    % (parent[:12], rid[:12],
                       "the child" if not croot else "the parent"))
            if parent in raw and proot != croot:
                return None, None, (
                    "chain topology is UNTRUSTWORTHY: row %s claims root %s "
                    "but its parent %s carries %s — a fold over a graph whose "
                    "own identity disagrees could suppress any debt"
                    % (rid[:12], croot[:12], parent[:12], proot[:12]))
    # Iterative, colour-marked, bounded by the board: no recursion depth to
    # exhaust and no arbitrary cap, so no case is ever skipped unexamined.
    WHITE, GREY, BLACK = 0, 1, 2
    colour = {}
    for start in list(kids) + list(node):
        if colour.get(start, WHITE) != WHITE:
            continue
        stack = [(start, iter(kids.get(start, ())))]
        colour[start] = GREY
        while stack:
            nid, it = stack[-1]
            nxt = next(it, None)
            if nxt is None:
                colour[nid] = BLACK
                stack.pop()
                continue
            c = colour.get(nxt, WHITE)
            if c == GREY:
                return None, None, (
                    "chain topology is UNTRUSTWORTHY: %s is its own ancestor "
                    "— the supersedes graph holds a cycle" % nxt[:12])
            if c == WHITE:
                colour[nxt] = GREY
                stack.append((nxt, iter(kids.get(nxt, ()))))
    return kids, node, None


def _carries(lr):
    """Does this PROJECTED row carry a parent's debt on its own account?"""
    return lr is not None and _absorbs_debt(lr)


def _relieved(rid, kids, node):
    """Is any descendant of `rid` carrying its debt?

    Traverses THROUGH every non-carrying terminal — abandoned, withdrawn,
    stranded, cancelled — because those end a NODE, never a BRANCH: an
    abandoned round may validly receive a later live or landed successor.
    A fork bills each carrying leaf once and suppresses every ancestor of
    one; only when EVERY reachable branch dies non-absorbing does the
    nearest unresolved ancestor keep its debt."""
    seen, stack = set(), list(kids.get(rid, ()))
    while stack:
        cid = stack.pop()
        if cid in seen:
            continue
        seen.add(cid)
        if _carries(node.get(cid)):
            return True
        stack.extend(kids.get(cid, ()))
    return False


def _successor_owning(lr, kids):
    """The successor round that OWNS this row's debt, or None.

    A round superseded by a LIVE sibling is owed by nobody: reworking round 1
    is meaningless once round 6 exists, so billing round 1's dwell to its
    author names an obligation no action can discharge. Neither can the close
    ladder retire it — its `--reason superseded` rung requires THIS row's
    reviewed change to have physically reached trunk, which is false by
    construction for a round that was rewritten (measured on 5aa24f98, whose
    chain head had already LANDED and which the ladder still refused).

    ABANDONED is deliberately NOT debt-absorbing. A successor that was written
    off carried nothing forward, so the parent keeps whatever it owed — the
    one direction where staying stalled is the honest answer."""
    for kid in kids.get(lr["id"], ()):
        if _absorbs_debt(kid):
            return kid
    return None


_OWED_SEAT_FIELD = {"author": "author", "reviewer": "reviewer", "builder": "reviewer"}


def _owed_by_whom(lr):
    """"author (kimi)" — the ROLE plus the SEAT that actually holds it.

    `owed_by` is an ENFORCEMENT role. `ball_holder` is the server-side display
    projection that can also answer "nobody" when succession has already
    honored the row without rewriting that audit fact. Both the CLI and the
    wire consume it; a browser may not independently reinterpret discharge.

    MEASURED 2026-08-04, on myself, twice: I told the fleet I had ZERO authored
    CHANGES_REQUESTED stalls during a burndown that had asked every seat for
    exactly that number. I had one, 13h50m old. A control on my own claim found
    the only seat-looking strings in the whole listing were a LANE NAME
    ("codex-refusal-ends-turn-reroute") whose author was somebody else — so the
    surface could not answer the question it was being read for, and answering
    it wrong looked exactly like answering it right.

    The ROLE STAYS. "owed by author" carries which LEG is owed, which the seat
    name does not; a listing that printed only "kimi" would trade one missing
    half for the other. Roles with no single holder — integrator, nobody,
    unknown — render unchanged, because inventing a name for them would be the
    same defect pointing the other way."""
    role, who = ball_holder(lr)
    return "%s (%s)" % (role, who) if who else role


def _moved_tip_marker(lr, store_row, repo_dir):
    """"  (tip MOVED +N since review)" when the lane tip advanced under the
    reviewer, else "".

    A verdict binds a BASE as well as a TIP: when the lane's head moves after
    the review, the verdict may be answering a question that no longer
    exists. Measured 2026-08-04: trunk moved 45+ times in one night and the
    integrator hand-computed this exact fact before every land with
    merge-base three-dot, because the surface would not say it. N is the
    three-dot count (reviewed...head), never two-dot — two-dot
    phantom-inflates a stale lane with the trunk side of the merge-base
    (helm task #91, landed, same trap).

    TRI-STATE SILENCE, deliberately: no ref_branch, no such ref, an
    unreadable repo, or a moved-tip count that cannot be computed all render
    NOTHING — the marker only ever speaks when it measured. An absent marker
    must never read as "the tip did not move"."""
    reviewed = str(lr.get("reviewed_tip") or "")
    lane = str(lr.get("lane") or "")
    if not reviewed or not lane or not repo_dir:
        return ""
    # refs/heads/lane/ + the PREFIX-STRIPPED lane (#142 round 2, codex
    # finding 5): the raw stored lane of a historical row already carries
    # lane/, and lane/lane/foo never verifies, so the marker silently never
    # spoke for exactly the rows most likely to have moved.
    rb = (store_row or {}).get("ref_branch") \
        or ("refs/heads/lane/" + _strip_lane_prefix(lane))
    from . import vcs
    be = vcs.backend(repo_dir)
    # Every spawn goes through the seam — the DirectSpawnAudit counts direct
    # git invocations outside it, and this marker already cost one gate red
    # for exactly that.
    rc, head, _e = be.text(repo_dir, "rev-parse", "--verify",
                           "--end-of-options", rb, timeout=5)
    if rc != 0 or not head:
        return ""
    if head == reviewed:
        return ""
    # reviewed..head, NEVER reviewed...head. Two-dot counts the commits
    # the reviewer has not seen; three-dot counts from the merge-base,
    # and a REBASED lane (old tip dropped, work replayed) shares no
    # history with its old tip — the symmetric form then counts the
    # whole fork, which is the #91 phantom-inflation trap in its exact
    # form. The direction matters too: trunk..lane counts what the lane
    # LACKS, which is the stale reading wearing the moved reading's
    # clothes.
    rc2, n, _e2 = be.text(repo_dir, "rev-list", "--count",
                          reviewed + ".." + head, timeout=5)
    if rc2 != 0:
        return ""
    return "  (tip MOVED +%s since review)" % n if n and n != "0" else ""


def _landed_marker(store_row, state=None):
    """"  (ALREADY ON TRUNK at <sha> — needs CLOSING, not building)" when this
    row's own reviewed work is already reachable from trunk, else "".

    THE MISFIRE THIS EXISTS FOR, from the integrator's transcript — a DISPATCH
    ROW id (deliberately not spelled here: a bare 12-hex token in source reads
    as a commit to every fresh clone, and this file's own docref rung refuses
    it) on lane parked-dispatch-rebind, listed as:

        AWAITING_BUILD  11h42m  (STALLED)
          (>= 4h00m in AWAITING_BUILD, owed by builder (a named seat))

    AWAITING_BUILD and a dwell clock are both claims about the LEDGER. Neither
    asks whether the work exists, so the surface billed a named seat for
    11h42m of delay on a row whose content had already moved under a different
    chain root. A stall nag that cannot see trunk does not merely go stale — it
    routes a real person at a job that is done.

    A MARKER, NOT A FILTER, and that distinction cost a gate to learn. The
    first cure dropped landed rows inside `dispatches.owed()`, the shared
    chokepoint — 18 failures, because the WORK-OFFER rung wants that same row
    to SURFACE precisely so the seat CLOSES it. Two consumers, opposite acts,
    one fact. Speaking here changes what the reader is TOLD and removes
    nothing, so the row stays visible to every other consumer.

    IT REPLACES THE OWED-BY CLAUSE RATHER THAN APPENDING TO IT. The first cut
    printed "owed by builder (seat)" AND "(ALREADY ON TRUNK ...)" in one
    sentence, which left the authoritative routing debt intact and merely
    argued with it — @codex-3 called that contradictory and was right. When
    the work is on trunk the row is owed by NOBODY, and the line now says so
    in the same clause that used to name a debtor.

    TRI-STATE SILENCE, the same law `_moved_tip_marker` states above: absent,
    unknown, an unreadable repo and every failure render NOTHING. An absent
    marker must never read as "this did not land".

    A BUILD ROW'S REF IS A BASE, NEVER ITS CONTENT — measured 2026-08-06, 48 of
    the 52 resolvable BUILD refs on the live ledger are ALREADY ANCESTORS OF
    TRUNK, so a raw landing probe would stamp this marker on nearly every
    AWAITING_BUILD row ever written, which is the loudest possible version of
    the defect installed as its cure. `_offer_landing_state` already owns that
    rule (a REVIEW row's ref is its reviewed tip; a BUILD without an explicit
    reviewed_tip stays UNKNOWN) and is REUSED rather than restated. It is also
    what keeps this cheap: that UNKNOWN returns before any git call.
    """
    if not isinstance(store_row, dict):
        return ""
    try:
        from . import seats_work_offer
        landed, trunk = seats_work_offer._offer_landing_state(store_row)
    except Exception:                    # noqa: BLE001 — a marker that cannot
        return ""                        # measure says nothing, by law
    if landed is not True or not trunk:
        return ""
    # THE VERB COMES FROM THE STATE, exactly as the ROLE does. OWED_BY already
    # keys builder/reviewer/integrator off `state`; saying "not building" at an
    # AWAITING_REVIEW row names an activity nobody was doing, which is a second
    # false clause bolted onto the correction for the first. @codex-3 caught it.
    verb = {"AWAITING_BUILD": ", not building",
            "AWAITING_REVIEW": ", not reviewing"}.get(str(state or ""), "")
    return ("ALREADY ON TRUNK at %s — needs CLOSING%s; owed by NOBODY"
            % (str(trunk)[:12], verb))


def _stalled_rows(lrs, raw=None):
    kids, node, err = _chain_forest(raw if raw is not None else {}, lrs)
    if err:
        raise _ChainUntrustworthy(err)
    out = [lr for lr in lrs.values()
           if lr["stalled"] and not lr["terminal"]
           and not _relieved(lr["id"], kids, node)]
    return sorted(out, key=lambda lr: -lr["dwell_s"])


def stalls(now=None):
    """(non-terminal loops past their per-stage threshold, unavailable) —
    the workflow-gap-finder, longest-stalled first."""
    lrs, raw, unavailable = project_raw(now)
    if unavailable:
        return None, unavailable
    try:
        return _stalled_rows(lrs, raw), None
    except _ChainUntrustworthy as e:
        return None, str(e)


def _measurable_through(lr, kids, node):
    """The same-chain successor whose VERIFIED gate answers this row's hold.

    THE IDENTICAL REVIEWED TIP IS THE WHOLE PROOF, and the reason this walk is
    narrow rather than a general "some child looks fine". A gate binds an
    interpreter, a HEAD and a TREE; a successor gated at a DIFFERENT tip is a
    measurement of different code and says nothing about this row. Same tip,
    declared polarity, a token present, and no approval refusal outstanding:
    then the verification this row is held for exists, for this exact code,
    and calling the row unmeasurable is a claim about our walk rather than
    about the board.

    Live fixture (2026-08-01): c3b19436 REVIEWED approve, gate '', held "no
    minted gate receipt" — child f5436c21 carries gate 3caeb50c at the SAME
    pre-rebase reviewed tip. That change landed as "gate: the
    epoch anchors the founding EVENT, because a work-item id comes back".

    POLARITY LAUNDERING, codex-3 at the gate: the first version tested
    `kid.get("polarity")` for TRUTH, so a same-tip child carrying a FIX and a
    valid gate rescued its parent — and a FIX AUTHORIZES NO LAND. The parent is
    held for want of an approval; a fix verdict is the opposite of one, and
    "somebody gated this tip" is not "somebody approved this tip". mark_verdict
    already refuses an ungated APPROVE for exactly this reason, and reading
    polarity truthily walked around that refusal one layer up.

    That my 190 tests missed it is the same failure as this lane's M4: every
    fixture I wrote gave the child polarity="approve", so the clause under test
    was never the clause that decided the result."""
    seen, stack = set(), list(kids.get(lr["id"], ()))
    while stack:                      # ANY validated descendant, not just a
        cid = stack.pop()             # direct child: a gated APPROVE
        if cid in seen:               # grandchild behind a FIX middle still
            continue                  # answers this row's hold, and the FIX
        seen.add(cid)                 # is traversed THROUGH rather than into
        kid = node.get(cid)           # a dead end (codex-3, blocker 1).
        if (kid is not None
                and kid.get("polarity") == "approve" and kid.get("gate")
                and not kid.get("ungated")
                and kid.get("reviewed_tip")
                and kid["reviewed_tip"] == lr.get("reviewed_tip")):
            return kid
        stack.extend(kids.get(cid, ()))
    return None


def _held_reason(lr):
    return str(lr.get("ungated") or "no reason recorded").split(" — ")[0]


def _unmeasurable_rows(lrs, raw=None):
    """(state, reason) for every loop whose dwell cannot be billed.

    REVIEWED is either undeclared OR a declared approval held by policy. Both
    are unbillable, but the reason must preserve which state the dispatch store
    actually owns.
    """
    out = []
    kids, node, err = _chain_forest(raw if raw is not None else {}, lrs)
    if err:
        raise _ChainUntrustworthy(err)
    for lr in lrs.values():
        if lr["terminal"]:
            continue
        if lr["state"] == "REVIEWED":
            # Second site of the same defect as the `lr show` polarity line:
            # "UNDECLARED" is a claim ABOUT THE LEDGER and may only be printed
            # when the ledger actually lacks a polarity. Asserting it for every
            # REVIEWED row put a false reason on the OWNER CONSOLE — measured
            # 14 of 37 live REVIEWED rows carry polarity `approve` and were all
            # labelled UNDECLARED. Those rows are held for a stated reason
            # (approval tier, or a missing receipt), which is what this now says.
            if lr.get("polarity"):
                # THE HOLD MAY ALREADY BE ANSWERED ON THE CHAIN. This row is
                # held for want of a verified gate; a successor round can carry
                # one at the SAME reviewed tip, and then the verification this
                # row lacks demonstrably exists for exactly this code. Reading
                # only the head reported "unmeasurable" about a board that
                # already held the measurement.
                if _measurable_through(lr, kids, node) is not None:
                    continue
                # FRONTIER OWNERSHIP IS INDEPENDENT OF THE GATE:
                # a descendant gate CLEARS a hold, and an
                # ungated descendant still MOVES it. A held
                # parent behind a held child is the child's
                # obligation (codex-3, blocker 2).
                if _relieved(lr["id"], kids, node):
                    continue
                out.append((lr, "%s, held — %s" % (
                    lr["polarity"].upper(), _held_reason(lr))))
            else:
                out.append((lr, "polarity UNDECLARED"))
        elif lr["state"] == "OPEN" and lr["owed_by"] == "integrator":
            out.append((lr, "unbilled (delivery never confirmed)"))
    return sorted(out, key=lambda t: -t[0]["dwell_s"])


def unmeasurable(now=None):
    """(loops whose dwell cannot be billed, unavailable) — the HONEST REFUSAL
    half of `stalls`.

    Two classes:
    - REVIEWED: polarity was undeclared, or a declared approval is held by
      authorization policy — no stall is billable until that state resolves.
    - OPEN (integrator-owed): the dispatch was persisted but delivery was
      never confirmed. The reviewer was never told. This is NOT a stall
      (you cannot be late for work you don't know exists), but it IS a live
      obligation the integrator should surface.

    Both are excluded from `stalls` and MUST be reported alongside it:
    an unmeasurable loop surfaced as unmeasurable is honest, while the
    same loop silently absent reads as healthy."""
    lrs, raw, unavailable = project_raw(now)
    if unavailable:
        return None, unavailable
    try:
        return _unmeasurable_rows(lrs, raw), None
    except _ChainUntrustworthy as e:
        return None, str(e)


def get(rid, now=None):
    lrs, unavailable = project(now, selector=rid)
    if unavailable:
        return None, unavailable
    return dispatches._resolve_row(
        lrs, rid, noun="land request", list_hint="helm lr list")


_LEGACY_COMPLETION_HINT = re.compile(r"\bdelivered\b", re.I)
_LEGACY_COMPLETION_WHY = (
    "cancel_reason contains the historical standalone word 'delivered'. This "
    "reason-text hint is not authority and may describe a negative such as "
    "'delivered neither', or code delivered/landed under a successor row. "
    "Delivered-report eligibility remains UNKNOWN until a human separately "
    "supplies a typed artifact ref, full chat row id, and evidence to "
    "annotate-delivered-report.")


def legacy_completion_hints():
    """(rows, unavailable) — reason-text audit hints, never promotions.

    The historical cohort is reproduced from ``cancel_reason`` alone. No lane,
    note, ref, or serialized whole-row text may make a row match: those fields
    often contain the word while the cancellation says nothing of completion.
    Explicit BUILD kind and current CANCELLED status are structural filters;
    every result remains UNVERIFIED and ineligible-by-unknown until an explicit
    correction event is written through the separate annotation command."""
    current, unavailable = dispatches.snapshot()
    if unavailable:
        return None, unavailable
    out = []
    for rid, row in current.items():
        reason = row.get("cancel_reason")
        if row.get("status") != "cancelled" or row.get("kind") != "build" \
                or not isinstance(reason, str) \
                or not _LEGACY_COMPLETION_HINT.search(reason):
            continue
        out.append({"id": rid, "cancel_reason": reason,
                    "classification": "UNVERIFIED",
                    "delivered_report_eligible": "UNKNOWN"})
    return sorted(out, key=lambda row: row["id"]), None


# A LANE NAME IS A FAMILY, NOT A KEY (store prior
# lane-names-are-a-family-probe-the-stem): one work family travels under round
# and role suffixes (-review, -r3, -xrev), so a probe keyed on the exact lane
# string reads a renamed lane as a different one.
#
# THE ONE FAMILY VOCABULARY. Exactly two consumers derive from this tuple and
# no other suffix set may exist in this module: `_lane_stem` (feeds the
# discharge ADMIT) and `_lane_family_names` (feeds refusal-only abandonment
# evidence). They differ in what they DO with a family match — an admit stays
# behind the full predicate stack plus a handoff proof, a broad refusal hit
# merely abstains from a write-off — but never in what a family IS: two
# independently-authored suffix sets once sat in this one file, and a lane
# renamed with a suffix only one of them knew was FAMILY to the admit side
# and a STRANGER to the refusal side, or the reverse. Longest token first, so
# -re-review strips whole before -review can split it; keep the set narrow —
# every added token widens BOTH consumers at once, and the admit side is the
# one that must never loosen casually.
_LANE_FAMILY_SUFFIXES = ("re-review", "rereview", "review", "build", "fix",
                         "xrev", "retry", "redo", r"r\d+", r"v\d+")
_LANE_ROUND_SUFFIX = re.compile(r"-(?:%s)$" % "|".join(_LANE_FAMILY_SUFFIXES))


def _strip_lane_prefix(lane):
    """Strip ONE leading family prefix (lane/, rescue/, prerebase/, wt/) from
    a lane name, preserving its case. The WRITE-TIME canonicalizer (#142): the
    ledger held the lane field two ways (798 bare rows / 82 lane/-prefixed),
    so writers strip here and the stored field is always the bare spelling —
    historical rows are never rewritten; `_lane_stem` composes this same strip
    into every read-side family join so both spellings keep matching."""
    s = str(lane or "").strip()
    low = s.casefold()
    for prefix in _STEM_PREFIXES:
        if low.startswith(prefix):
            return s[len(prefix):]
    return s


def _lane_stem(lane):
    """THE ONE lane-family normalisation — the lane with its family
    prefixes AND its trailing round/role suffixes stripped, case-folded.
    Empty when the row has no lane to family-match.

    #156: for its whole life this stripped the suffix but not the `lane/`
    prefix, so a family recorded BOTH ways (#142: 798 bare rows / 82
    lane/-prefixed) silently failed every family match — the legacy
    handoff admit refused 'lane/foo-r2' against the contrary's bare 'foo'
    even though they are ONE work family. The prefix vocabulary already
    existed for the ref/worktree probe (`_STEM_PREFIXES`); the durable fix
    is composing BOTH strips in this ONE function so no reader or writer
    ever normalises differently again. Casefold FIRST, so an upper-cased
    prefix spelling is still family; the write-time `_strip_lane_prefix`
    stays case-preserving because it feeds the stored display field."""
    lane = _strip_lane_prefix(str(lane or "").casefold())
    while True:
        m = _LANE_ROUND_SUFFIX.search(lane)
        if not m:
            return lane
        lane = lane[:m.start()]


def _author_on_walk(candidate, author, current, stem=None):
    """(True|False|None, why) — does the candidate's supersedes ancestry
    contain a row authored by `author` (and, when `stem` is given, in that
    lane family)?

    A chained candidate STATES which row it continues, so walking that record
    is the strongest handoff proof there is: the successor declared, at
    dispatch time, that its work continues a row the original author sent.
    On a CHAINED contrary the caller has already bound the candidate to the
    contrary's own chain_root, so any author row on the walk is linkage to
    THIS work and `stem` stays None. On a LEGACY contrary no chain binds the
    walk to the debt, so the author row it reaches must itself sit in the
    contrary's lane family — otherwise a candidate chained to ANY unrelated
    row of the author's would pay ANY legacy debt of that author, and the bar
    would drop from "the author's work" to "the author's name".
    TRI-STATE, fail-closed: True is proof; False means the walk COMPLETED and
    the record positively shows no such row; None means the walk is unreadable
    (missing or corrupt parent) — an unreadable proof never admits."""
    linked = ("a row authored by %s" % author if stem is None else
              "a row authored by %s in lane family %r" % (author, stem))
    seen = set()
    node = candidate
    while True:
        if node.get("sender") == author and (
                stem is None or _lane_stem(node.get("lane")) == stem):
            return True, None
        parent = node.get("supersedes")
        if parent is None:
            return False, ("its supersedes walk reaches its root without %s"
                           % linked)
        if parent == dispatches.CHAIN_UNKNOWN or parent in seen:
            return None, ("its supersedes walk is UNPROVABLE at %s — a "
                          "corrupt or self-referential parent link"
                          % node["id"][:12])
        seen.add(parent)
        node = current.get(parent)
        if node is None:
            return None, ("its supersedes walk is UNPROVABLE — parent %s is "
                          "absent from the ledger" % parent[:12])


def _legacy_handoff(candidate, lr, current):
    """(True|False|None, why) — for a LEGACY (chainless) contrary row only:
    did the lane VISIBLY change hands from the contrary's author to the
    candidate's sender?

    No chain exists to walk, so the proof is the record of the lane family
    itself: the candidate works the SAME STEM FAMILY as the contrary, and the
    candidate's sender appears as a dispatch sender elsewhere in that family —
    i.e. the successor demonstrably took over the lane, rather than dropping
    one drive-by approve into it. Tri-state, same contract as the walk."""
    stem = _lane_stem(lr.get("lane"))
    if not stem:
        return None, ("the contrary row has no lane, so its lane family is "
                      "UNPROVABLE")
    sender = candidate.get("sender")
    if not sender:
        return None, ("the candidate has no sender identity, so a handoff to "
                      "it is UNPROVABLE")
    if _lane_stem(candidate.get("lane")) != stem:
        return False, ("its lane %r is outside the contrary's lane family %r"
                       % (str(candidate.get("lane") or ""), stem))
    for row in current.values():
        if row["id"] != candidate["id"] and row.get("sender") == sender \
                and _lane_stem(row.get("lane")) == stem:
            return True, None
    return False, ("the ledger records no other dispatch from %s in lane "
                   "family %r — the lane never visibly changed hands"
                   % (sender, stem))


def _handoff_proven(candidate, lr, current, contrary_chain):
    """(True|False|None, why) — may a candidate whose sender is NOT the
    contrary's author still pay this debt? Only on a PROVABLE handoff, never a
    loosening:

      (a) the candidate's supersedes ancestry contains a row authored by the
          original author — the successor declared it continues their work.
          On a CHAINED contrary the candidate already shares the contrary's
          chain_root (the caller filtered on it), so the walk alone is
          linkage; on a LEGACY contrary no chain binds the walk to THIS debt,
          so the author row the walk reaches must itself sit in the
          contrary's lane family — otherwise chaining to ANY unrelated row
          of the author's would pay ANY legacy debt of that author; OR
      (b) the contrary is LEGACY (chainless) and the lane family visibly
          changed hands in the record (`_legacy_handoff`).

    Anything unreadable or ambiguous answers None, and every non-True answer
    refuses — the caller surfaces `why` so the next integrator does not
    re-diagnose the refusal from scratch."""
    if contrary_chain is not None:
        return _author_on_walk(candidate, lr.get("author"), current)
    stem = _lane_stem(lr.get("lane"))
    if not stem:
        return None, ("the contrary row has no lane, so its lane family is "
                      "UNPROVABLE")
    walked, why = _author_on_walk(candidate, lr.get("author"), current,
                                  stem=stem)
    if walked is True:
        return walked, why
    return _legacy_handoff(candidate, lr, current)


def discharge(rid, superseding_tip, evidence):
    """DEPRECATED for exactly one release: `helm lr close --reason
    superseded` is the one terminal verb and this alias only maps arguments
    onto it — no second code path. The full discharge ladder (chain-walked
    candidate, author/repo binding, containment, trunk proofs) lives whole in
    `_close_ladder_superseded`; new writes emit a `close` event, and every
    historical `discharge` event replays forever."""
    return close(rid, "superseded", evidence=evidence, tip=superseding_tip)


def withdraw(rid, evidence):
    """DEPRECATED for exactly one release: `helm lr close --reason withdrawn`
    is the one terminal verb and this alias only maps arguments onto it — no
    second code path. The absence gate (Git proves the reviewed change OFF
    the pinned trunk; True is a lie, None fails closed) lives whole in
    `_close_ladder_withdrawn`, falsifiability clause included: a later land
    re-exposes the row as CONTRARY."""
    return close(rid, "withdrawn", evidence=evidence)


def _abandon_trunk(gitdir):
    cache = {}
    local_ref, upstream_ref = _trunk_refs(gitdir, cache)
    origin = _origin_configured(gitdir)
    if origin is None:
        return None, "Git could not identify authoritative trunk"
    trunk = upstream_ref if origin else local_ref
    return (trunk, None) if trunk else (None, "authoritative trunk is unreadable")


# Derived from _LANE_FAMILY_SUFFIXES — THE one family vocabulary, declared
# beside `_lane_stem` above. This side is refusal-only evidence and its hits
# abstain rather than admit, but the VOCABULARY may not fork: it once did,
# and the two sets disagreed about -build, -re-review, -xrev, -v2 in the same
# file. Case-insensitive here because raw lane labels arrive unfolded;
# `_lane_stem` casefolds its input instead.
_LANE_FAMILY_SUFFIX_RE = re.compile(
    r"(?:%s)\Z" % "|".join("-" + s for s in _LANE_FAMILY_SUFFIXES),
    re.IGNORECASE)


def _lane_family_names(name):
    """Exact lane plus conservative workflow-family prefixes.

    Lane labels are free text: one work family has appeared as ``foo-review``,
    ``foo-contract`` and ``foo-r6``. Exact-name lookup mistakes those renames for
    silence. Every useful hyphen prefix is therefore refusal-only evidence; a
    known round/role suffix may additionally expose a short legacy stem. Broad
    hits can abstain from abandonment but can never claim land.
    """
    name = str(name or "").strip()
    names = [name] if name else []
    parts = name.split("-")
    for end in range(len(parts) - 1, 0, -1):
        prefix = "-".join(parts[:end]).rstrip("-")
        if prefix and (end >= 2 or len(prefix) >= 8) and prefix not in names:
            names.append(prefix)
    current = name
    while current:
        broader = _LANE_FAMILY_SUFFIX_RE.sub("", current).rstrip("-")
        if broader == current:
            break
        if broader and broader not in names:
            names.append(broader)
        current = broader
    return tuple(names)


def _tag_matches_lane_family(ref, names):
    """Does a tag name belong to this lane family? BLOCK-ONLY evidence, so
    under-matching is the dangerous direction: every namespace gets the same
    suffix tolerance, not only gate/.

    The first version granted startswith() tolerance ONLY through a special
    gate/ branch; every other namespace fell to a boundary regex whose
    trailing lookahead rejects a FOLLOWING HYPHEN — so exact tag-shaped fixtures
    (archive/roster-read-cache-v2, rescue/ds4pro-landreq-staged-2026-07-27)
    MISSED their own lanes while gate/argv-body-guard-r4 matched. helm-claude
    reproduced the asymmetry against live refs post-land. The cure: strip ANY
    leading <namespace>/ prefix and give the remainder the same
    startswith-with-boundary tolerance — a suffix beginning with a separator
    is revision decoration, while a run-on word (roster-read-cachex) still
    fails the boundary."""
    short = ref[10:] if ref.startswith("refs/tags/") else ""
    if not short:
        return False
    low = short.lower()
    tail = low.rsplit("/", 1)[-1]        # gate/x, archive/x, rescue/x, x — one rule
    for name in names:
        needle = name.lower()
        if tail.startswith(needle):
            rest = tail[len(needle):]
            if rest == "" or not re.match(r"[A-Za-z0-9_]", rest):
                return True
        if re.search(r"(?<![A-Za-z0-9_-])" + re.escape(needle)
                     + r"(?![A-Za-z0-9_-])", low):
            return True
    return False


def _lane_trunk_tag(gitdir, names, trunk):
    """A family tag whose commit reached trunk, or none/unknown.

    Tags are structured refs, not prose. A matching tag is still correlation —
    it never proves that the reviewed tip landed — but a trunk-ancestor target
    is sufficient refusal-only evidence against an irreversible write-off.
    """
    p = _git(gitdir, "for-each-ref", "--format=%(refname)", "refs/tags")
    if p is None or p.returncode != 0:
        return {"state": "unknown", "reason": "Git could not scan tag refs"}
    for ref in (p.stdout or "").splitlines():
        ref = ref.strip()
        if not ref.startswith("refs/tags/"):
            return {"state": "unknown", "reason": "tag ref list is unreadable"}
        if not _tag_matches_lane_family(ref, names):
            continue
        q = _git(gitdir, "rev-parse", "--verify", "--quiet", ref + "^{commit}")
        if q is None or q.returncode != 0:
            return {"state": "unknown",
                    "reason": "family tag target is unreadable: %s" % ref}
        values = (q.stdout or "").split()
        if len(values) != 1 or not _FULL_SHA_RE.fullmatch(values[0].lower()):
            return {"state": "unknown",
                    "reason": "family tag target has unreadable identity: %s" % ref}
        sha = values[0].lower()
        relation = _ancestry(gitdir, sha, trunk)
        if relation == UNDETERMINED:
            return {"state": "unknown",
                    "reason": "Git could not compare family tag to trunk: %s" % ref}
        if relation == ANCESTOR:
            return {"state": "tag", "tag": ref, "sha": sha, "trunk": trunk}
    return {"state": "none", "trunk": trunk}


def _lane_trunk_mention(gitdir, lane):
    """Structured/ambiguous/tag/none/unknown trunk correlation for one lane.

    Commit-message conventions and family tags are correlation, never landing
    proof. They can only BLOCK the irreversible abandon write. Any hit refuses;
    only complete readable scans with no evidence permit the independent
    MISSING-object gate to proceed.
    """
    name = _lane_stem(lane)
    if not name:
        return {"state": "unknown", "reason": "row has no lane identity"}
    names = _lane_family_names(name)
    trunk, err = _abandon_trunk(gitdir)
    if err:
        return {"state": "unknown", "reason": err}
    tag = _lane_trunk_tag(gitdir, names, trunk)
    if tag["state"] != "none":
        return tag
    p = _git(gitdir, "log", trunk, "--format=%H%x1f%B%x1e")
    if p is None or p.returncode != 0:
        return {"state": "unknown", "reason": "Git could not scan trunk messages"}
    structured = []
    for n in names:
        q = re.escape(n)
        end = r"(?![A-Za-z0-9_-])"
        structured.extend((
            re.compile(r"^land:\s+(?:lane/)?" + q + end, re.IGNORECASE),
            re.compile(r"^Merge branch ['\"](?:lane/)?" + q + r"['\"]",
                       re.IGNORECASE),
            re.compile(r"^Merge (?:branch\s+)?lane/" + q + end,
                       re.IGNORECASE),
        ))
    ambiguous = None
    for record in (p.stdout or "").split("\x1e"):
        record = record.strip("\n\x00 ")
        if not record or "\x1f" not in record:
            continue
        sha, body = record.split("\x1f", 1)
        if not _FULL_SHA_RE.fullmatch(sha.strip().lower()):
            return {"state": "unknown", "reason": "trunk log shape is unreadable"}
        for line in body.splitlines():
            text = line.strip()
            if not text:
                continue
            if any(pattern.search(text) for pattern in structured):
                return {"state": "structured", "sha": sha.strip().lower(),
                        "line": text, "trunk": trunk}
            low = text.lower()
            for n in names:
                needle = n.lower()
                hit = needle in low if len(n) < 8 else re.search(
                    r"(?<![A-Za-z0-9_-])" + re.escape(needle)
                    + r"(?![A-Za-z0-9_-])", low)
                if hit and ambiguous is None:
                    ambiguous = {"state": "ambiguous",
                                 "sha": sha.strip().lower(), "line": text,
                                 "trunk": trunk}
    return ambiguous or {"state": "none", "trunk": trunk}


def _worktree_status(path):
    try:
        return vcs.backend(path).proc(
            path, "status", "--porcelain", "--untracked-files=all", timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None


def _lane_work_interlock(gitdir, lane):
    """Branch/worktree evidence that may only REFUSE abandonment."""
    name = _lane_stem(lane)
    if not name:
        return {"branch_state": "unknown", "worktree_state": "unknown",
                "reason": "row has no lane identity"}
    refs = tuple("refs/heads/lane/" + n for n in _lane_family_names(name))
    trunk, err = _abandon_trunk(gitdir)
    if err:
        return {"branch_state": "unknown", "worktree_state": "unknown",
                "reason": err}
    existing = {}
    for ref in refs:
        p = _git(gitdir, "rev-parse", "--verify", "--quiet", ref)
        if p is None or p.returncode not in (0, 1):
            return {"branch_state": "unknown", "worktree_state": "unknown",
                    "reason": "Git could not resolve lane branch %s" % ref}
        if p.returncode == 0:
            sha = ((p.stdout or "").split() or [""])[0].lower()
            if not _FULL_SHA_RE.fullmatch(sha):
                return {"branch_state": "unknown", "worktree_state": "unknown",
                        "reason": "lane branch %s has unreadable identity" % ref}
            existing[ref] = sha
    branch_state = "none"
    for ref, sha in existing.items():
        relation = _ancestry(gitdir, sha, trunk)
        if relation == UNDETERMINED:
            return {"branch_state": "unknown", "worktree_state": "unknown",
                    "reason": "Git could not compare lane branch to trunk"}
        if relation != ANCESTOR:
            return {"branch_state": "unlanded", "worktree_state": "unknown",
                    "ref": ref, "sha": sha,
                    "reason": "lane branch %s@%s is unlanded" % (ref, sha[:12])}
        branch_state = "merged"
    p = _git(gitdir, "worktree", "list", "--porcelain")
    if p is None or p.returncode != 0:
        return {"branch_state": branch_state, "worktree_state": "unknown",
                "reason": "Git could not list registered worktrees"}
    entries, current = [], {}
    for line in (p.stdout or "").splitlines() + [""]:
        if not line.strip():
            if current:
                entries.append(current)
                current = {}
            continue
        key, _, value = line.partition(" ")
        current[key] = value
    # Detached registrations carry no lane identity. A detached checkout at the
    # reviewed tip still retains that object, so the earlier EXISTS gate refuses;
    # if Git cannot read it, the earlier UNKNOWN gate refuses instead.
    matches = [entry for entry in entries if entry.get("branch") in refs]
    if not matches:
        return {"branch_state": branch_state, "worktree_state": "none"}
    for entry in matches:
        path = entry.get("worktree") or ""
        if not os.path.isabs(path) or not os.path.isdir(path):
            return {"branch_state": branch_state, "worktree_state": "unknown",
                    "reason": "registered worktree path is absent or unreadable: %s"
                    % (path or "-")}
        status = _worktree_status(path)
        if status is None or status.returncode != 0:
            return {"branch_state": branch_state, "worktree_state": "unknown",
                    "reason": "registered worktree status is unreadable: %s" % path}
        if (status.stdout or "").strip():
            return {"branch_state": branch_state, "worktree_state": "dirty",
                    "path": path, "reason": "dirty worktree %s" % path}
    return {"branch_state": branch_state, "worktree_state": "clean"}


def _abandon_interlocks(gitdir, lane):
    mention = _lane_trunk_mention(gitdir, lane)
    work = _lane_work_interlock(gitdir, lane)
    return {"mention": mention, "branch_state": work.get("branch_state"),
            "worktree_state": work.get("worktree_state"),
            "work_reason": work.get("reason"), "work_ref": work.get("ref"),
            "work_sha": work.get("sha"), "work_path": work.get("path")}


def _abandon_alternative(lr):
    """The truthful existing-object route for an abandon refusal."""
    rid = lr["id"]
    if lr.get("terminal"):
        return "%s is already terminal through %s" % (rid, lr.get("state"))
    polarity = lr.get("polarity")
    if polarity == "approve":
        return ("reviewed commit exists; integrate that exact commit, then use "
                "`helm lr land %s` to record the candidate receipt" % rid)
    if polarity in ("fix", "supersede"):
        if lr.get("contrary"):
            return ("reviewed commit exists in a contrary land; use `helm lr "
                    "discharge %s <approved-successor-tip> <evidence>` after "
                    "the required approved successor lands" % rid)
        if lr.get("observable"):
            return ("reviewed commit exists and is absent from authoritative "
                    "trunk; use `helm lr withdraw %s <evidence>`" % rid)
        return ("reviewed commit exists but trunk state is UNKNOWN; repair Git "
                "visibility before choosing discharge or withdraw")
    if polarity is None:
        if lr.get("landed") or lr.get("merged_local"):
            return ("reviewed commit exists and landing is observable; use "
                    "`helm lr close-landed %s --trunk REF`" % rid)
        if lr.get("observable"):
            return ("reviewed commit exists but is not proven landed; no existing "
                    "terminal is truthful yet — obtain a declared review, or "
                    "land it before using close-landed")
        return ("reviewed commit exists but trunk state is UNKNOWN; repair Git "
                "visibility before choosing an existing terminal verb")
    return "reviewed commit exists; abandonment is refused"


def abandon(rid, reason, repo=None):
    """Terminally record destroyed reviewed evidence with land state UNKNOWN.

    This is deliberately narrower than every proof-based terminal. It records
    only that Git reported the exact reviewed commit MISSING at the locked
    mutation boundary; it never infers whether equivalent content landed.
    """
    lr, err = get(rid)
    if err:
        return None, err
    reason, err = dispatches._clean(reason, "abandon reason", 256)
    if err:
        return None, err
    if lr.get("abandoned"):
        if lr.get("abandon_reason") == reason:
            return lr, None
        return None, "%s already has a different abandon" % lr["id"]
    if lr.get("terminal"):
        return None, "%s is already terminal through %s" % (lr["id"], lr["state"])
    if lr.get("kind") != "review":
        return None, ("%s is not a review row — abandon writes off reviewed work "
                      "and can never close a build dispatch" % lr["id"])
    reviewed = lr.get("reviewed_tip")
    if not lr.get("verdict_ref") or not _FULL_SHA_RE.fullmatch(str(reviewed or "")):
        return None, "%s has no verdict with an immutable full reviewed tip" % lr["id"]
    gitdir, err = _close_repo(lr, repo)
    if err:
        return None, err
    exists = _commit_exists(gitdir, reviewed)
    if exists is None:
        return None, ("reviewed commit state is UNKNOWN; Git/repository state "
                      "must be readable before abandonment can be recorded")
    if exists:
        return None, _abandon_alternative(lr)
    interlocks = _abandon_interlocks(gitdir, lr.get("lane"))
    mention = interlocks["mention"]
    if mention["state"] == "unknown":
        return None, ("trunk mention state is UNKNOWN: %s; abandonment not "
                      "recorded" % mention.get("reason", "scan unavailable"))
    if mention["state"] in ("structured", "ambiguous", "tag"):
        if mention["state"] == "tag":
            label = "landed family tag"
            evidence = "%s -> %s" % (
                mention.get("tag") or "-", (mention.get("sha") or "")[:12])
        else:
            label = "structured trunk mention" if mention["state"] == "structured" \
                else "AMBIGUOUS trunk mention"
            evidence = "%s: %s" % (
                (mention.get("sha") or "")[:12], mention.get("line") or "-")
        return None, ("%s %s — this is correlation, not proven landed, but "
                      "suspicion blocks an irreversible write" % (label, evidence))
    if interlocks.get("branch_state") == "unlanded":
        return None, interlocks.get("work_reason") or "lane branch is unlanded"
    if interlocks.get("worktree_state") == "dirty":
        return None, interlocks.get("work_reason") or "dirty worktree blocks abandon"
    if interlocks.get("branch_state") == "unknown" \
            or interlocks.get("worktree_state") == "unknown":
        return None, ("lane branch/worktree state is UNKNOWN: %s; abandonment "
                      "not recorded" % (interlocks.get("work_reason") or
                                         "work state unreadable"))
    row, err = dispatches._record_abandon_proven(
        lr["id"], reviewed, reason, _commit_exists, _abandon_interlocks)
    if err:
        if "exists at the mutation boundary" in err:
            fresh, fresh_err = get(lr["id"])
            return None, _abandon_alternative(fresh) if not fresh_err else err
        return None, err
    return get(row["id"])


def annotate_delivered_report(rid, artifact_ref, report_ref, evidence):
    """Append the explicit correction for one historical cancelled BUILD."""
    row, err = dispatches.record_delivered_report_correction(
        rid, artifact_ref, report_ref, evidence)
    if err:
        return None, err
    return get(row["id"])


def close_landed(rid, repo=None, trunk=None, live=False, needs_restart=None):
    """DEPRECATED for exactly one release: `helm lr close --reason landed` is
    the one terminal verb and this alias only maps arguments onto it — no
    second code path. Its historical UNDECLARED-only domain is a subset of
    landed's (approve rows are now admissible too); `--repo`/`--trunk` pass
    through, and legacy CLOSED-BY-LANDING rows answer retries idempotently
    under the same repo + trunk-ref-alias identity. The live-step declaration
    passes through too — a deprecated spelling is not a door around it."""
    return close(rid, "landed", repo=repo, trunk=trunk, live=live,
                 needs_restart=needs_restart)


# ------------------------------------------------------------- helm lr close
# THE ONE TERMINAL VERB. Seven reasons, one ladder each, every rung tri-state
# (unreadable is UNKNOWN and UNKNOWN refuses), every ladder ending in
# proved-or-refused, never defaulted. Interlock laws inherited verbatim:
#   1. heuristic-blocks-never-authors-irreversible-write — lane stems, commit
#      subjects, receipts and blob overlap only ever REFUSE a close; the only
#      authoring proofs are _landing_proof ancestry/patch-id (direct or via
#      the recorded translation sidecar), the discharge candidate+containment
#      conjunction, `_landed is False` for withdrawn (directly or via the
#      recorded translation, both legs adjudicated), and the object-pruned +
#      all-liveness-rungs conjunction for stranded. The chain's declared
#      polarity (`_chain_polarity`) is a GATE input, never an authoring
#      proof: it selects which Git-backed ladder's rungs apply, and those closes
#      are still proven by the git rungs above. Delivered-report is deliberately
#      outside that family: its exact artifact/report/evidence object authors a
#      handoff terminal and makes no Git assertion.
#   2. lane-names-are-a-family — every lane-keyed probe matches the STEM
#      FAMILY generously; a match only ever refuses and NAMES the ref, so
#      over-matching costs one operator read while under-matching would cost
#      a false terminal.
#   4. four places work lives — the named tip (_object_exists), the lane
#      branch (stem-family refs), an uncommitted worktree (porcelain via
#      vcs.parse_worktree_records), and trunk under a rewritten sha (patch-id
#      directly and through the translated tip). Blob containment over
#      history is deliberately advisory-only in --dry-run output this slice:
#      a pruned tip has no readable blobs to take, and a dangling-but-present
#      tip already refuses stranded at the object rung — it never gates and
#      never authors.
#
# `lr land` is NOT this verb and is NOT deprecated: it stays the standalone
# AT-INTEGRATION receipt verb — fail-open signed witness + fail-closed
# deletion guard, fired at the moment the merge exists locally, pre-push.
# `close --reason landed` is the POST-OBSERVATION terminal recorded after the
# land is observable on the named trunk. Aliasing land onto close would flip
# exit 0→1 in land's primary window (a local merge not yet pushed reads
# absent under the upstream trio) and the deletion guard is provably hollow
# post-merge, so the receipt verb keeps its moment and close keeps its proof.
# No receipt is ever written by close: receipts are diagnostic-never-authority
# and a close-time receipt would sample post-merge trunk.

CLOSE_CLI_REASONS = ("landed", "superseded", "withdrawn", "out-of-scope",
                     "stranded", "subsumed", "delivered-report", "discharged",
                     "resolved")

# ------------------------------------------------------- the LIVE step
# A land reaches TRUNK. Whether it reached the RUNNING FLEET is a SECOND fact,
# and until this existed the ledger implied the first meant the second. It does
# not, and it cost us twice in one day: a landed fix seats could not benefit
# from until relaunch, and a "closes #158" land whose own commit message says
# it takes effect on RELAUNCH.
#
# Owner rule (premise land-to-live-compression-owner-directive), two classes:
#   cli      every helm invocation is a fresh process off main, so CLI-class
#            code is LIVE AT LAND — the moment it is on trunk.
#   process  beacons, the web service, proxies, daemons, and anything a
#            long-running seat holds in memory keep executing the code they
#            loaded at start. NOT live at land. Until each re-arms, the running
#            fleet holds PRE-LAND code, and `--needs-restart WHAT` names what.
#
# The closer DECLARES; helm never derives. A changed-file heuristic could guess
# the class, and interlock law 1 (heuristic-blocks-never-authors-irreversible-
# write) forbids exactly that: a guess would author a permanent claim about the
# fleet. And UNKNOWN never resolves to the quiet answer — a landed close with
# no declaration is REFUSED at the writer, and the pre-declaration rows that
# predate this render UNDECLARED, never live.
#
# This module RECORDS the obligation; it never discharges it and never signals
# anything. `helm rearm` is the existing verb that measures live processes
# against HEAD and cycles the stale waiters their owners re-arm. So a
# process-class row prints as an OPEN LOOP for good: the ledger's job is to say
# the debt was taken on, and rearm's job is to say whether it is still owed.
DELIVERY_CLI = "cli"
DELIVERY_PROCESS = "process"
_DELIVERY_REQUIRED = (
    "close --reason landed must declare the LIVE step: --live when this is "
    "CLI-class code (every helm invocation is a fresh process off main, so it "
    "is live AT LAND), or --needs-restart WHAT when it is process-class "
    "(beacons/web/proxies/daemons/in-memory seat state still hold pre-land "
    "code until WHAT re-arms). Trunk and the running fleet are different "
    "facts and helm will not guess which one this land reached")


def _delivery(live, needs_restart):
    """((class, restart), err) for a landed close — the ONE normalisation.

    Exactly one door. Both is a contradiction, neither is the UNKNOWN this
    whole field exists to refuse, and `--needs-restart` with nothing named is
    a restart obligation nobody could ever discharge."""
    named = str(needs_restart or "").strip()
    if live and named:
        return None, ("close --reason landed declares ONE delivery class: "
                      "--live (CLI-class) or --needs-restart WHAT "
                      "(process-class), never both")
    if live:
        return (DELIVERY_CLI, None), None
    if needs_restart is not None and not named:
        return None, ("close --reason landed --needs-restart must NAME what "
                      "still holds pre-land code (a beacon, helm-web, a "
                      "proxy, the seats) — an unnamed restart is an "
                      "obligation nobody can discharge")
    if not named:
        return None, _DELIVERY_REQUIRED
    cleaned, err = dispatches._clean(named, "close delivery restart", 256)
    if err:
        return None, err
    return (DELIVERY_PROCESS, cleaned), None


# WHOSE CODE A LONG-LIVED PROCESS HOLDS. `_delivery` normalises the
# DECLARATION; this names the small set of paths that make a --live
# declaration checkable against what the land actually touched. Every entry is
# a process `helm rearm` already knows by name, and the docstring at the top of
# helm/rearm.py is the source — keep the two together when either moves.
#
#   helm/web…       the `helm-web` systemd unit (rearm.WEB_UNIT). A PREFIX, not
#                   a list, because the service is spread over ~19 sibling
#                   modules and a per-file list would go stale on the next
#                   split (two test-side copies of that list already have).
#   beacons/chat    the armed `helm chat wait` inbox beacon — the waiter rearm
#                   SIGTERMs so its owner re-arms on new code.
#   proxies/daemons the long-lived helm processes rearm reports as advisory and
#                   deliberately never signals.
#
# ONLY PYTHON, and the extension is load-bearing rather than tidy: a long-lived
# process freezes the MODULES it imported, not the data it re-reads. The web
# UI under helm/web_ui is 31 template fragments plus a manifest that
# web_ui_loader reads ON EACH REQUEST — deliberately, to preserve the old
# hot-template lifetime — so the browser page IS live at land and the prefix
# would otherwise have refused those lands wrongly (it did, on the first
# historical replay of this rung, before the suffix went in).
#
# DELIBERATELY SMALL, and additive: a path outside it is not a claim that it is
# CLI-class, only that this rung cannot speak for it. Extend by adding the path
# and the process that holds it.
_PROCESS_CLASS_PREFIXES = ("helm/web",)
_PROCESS_CLASS_PATHS = frozenset({
    "helm/beacons.py", "helm/chat.py",
    "helm/proxywatch.py", "helm/seat_proxy.py", "helm/modelrouter.py",
    "helm/watchdog.py", "helm/keepalive.py",
})


def _process_class_path(path):
    """Is `path` an imported module a long-lived helm process holds?"""
    cleaned = str(path or "").strip().lstrip("./")
    if not cleaned.endswith(".py"):
        return False
    return (cleaned in _PROCESS_CLASS_PATHS
            or cleaned.startswith(_PROCESS_CLASS_PREFIXES))


def _land_files(gitdir, tip, pinned):
    """(paths this land put on trunk, None), or (None, why) for UNKNOWN.

    THE BASE THE ROW DOES NOT RECORD, RECOVERED FROM TOPOLOGY. A closed lane's
    diff against trunk is empty — MEASURED 2026-08-05 over the coordination
    ledger, all 65 landed closes carrying a delivery class have their reviewed
    tip as an ANCESTOR of trunk, so the merge-base of tip and trunk is the tip
    and the subtraction yields nothing — and no row stores a base. But the
    MERGE that carried the tip onto trunk does store one: its FIRST parent is
    trunk immediately before the land, so `diff first-parent..merge` is exactly
    what this land added. `rev-list --merges --ancestry-path --parents
    tip..trunk` names every merge between the two and the fold is the one
    holding the tip as a NON-FIRST parent. Verified byte-identical to the
    fork-point range diff on every land it answered, and it answered 62 of
    those 65.

    NOT THE LAND RECEIPT, measured rather than assumed: land-receipts.jsonl
    holds no row at all for 59 of the same 65 (a receipt is written by `lr
    land` at integration, and most rows close without one), and of the 4 it
    does hold, every `trunk_sha` names trunk AFTER the land, whose merge-base
    with the tip is the tip — an empty diff indistinguishable from a clean
    one. Two answerable of 65 is not a mechanism. Receipts are also
    diagnostic-never-authority here by this module's own law.

    UNKNOWN NEVER REFUSES, so every leg that cannot attribute says so instead
    of guessing: a fast-forward land has no fold commit, an octopus fold mixes
    lanes this would wrongly blame on one row, a tip that reached trunk only by
    patch identity has no ancestry path, and a git failure is a hiccup."""
    if not _sha(tip) or not _sha(pinned):
        return None, "the tip or trunk sha is not a resolvable object name"
    walk = _git(gitdir, "rev-list", "--merges", "--ancestry-path", "--parents",
                "%s..%s" % (tip, pinned))
    if walk is None or walk.returncode != 0:
        return None, "git could not walk the merges between the tip and trunk"
    fold = None
    for line in walk.stdout.splitlines():
        parts = line.split()
        # EXACTLY two parents, tip SECOND. An octopus would attribute other
        # lanes' files to this row, and a tip in FIRST position is trunk being
        # merged INTO the lane, not the lane arriving. Oldest match wins:
        # rev-list prints newest-first and a re-merge is not the fold.
        if len(parts) == 3 and parts[2].lower() == tip:
            fold = parts
    if fold is None:
        return None, ("no two-parent merge on trunk carries this tip as its "
                      "merged side")
    diff = _git(gitdir, "diff", "--name-only", fold[1], fold[0])
    if diff is None or diff.returncode != 0:
        return None, "git could not diff the merge that carried this tip"
    return sorted({p for p in diff.stdout.splitlines() if p.strip()}), None


def _process_class_refusal(rid, gitdir, tip, trunk_ref, pinned, klass):
    """The refusal for a --live close whose own land touched process-class
    code, or None.

    The sibling rung above answers "is THIS CHECKOUT's guard rail armed", a
    standing property of the box. This one answers the question nothing asked
    before it: does the DECLARATION match what the land TOUCHED. `--live` says
    every line of this change is running the moment it is on trunk, and a land
    that moved the web service or a beacon has not been running anywhere since
    it merged — that is the same BUILT != WIRED class, arrived at honestly.

    Unlike the guard rung this one CAN attribute, and does: the files are the
    fold merge's own diff, so the refusal names paths THIS land put on trunk
    rather than a condition it merely coexists with. Everything else is
    inherited — UNKNOWN is silent, and the escape is a truer declaration."""
    if klass != DELIVERY_CLI:
        return None                 # process-class already records the debt
    files, _why = _land_files(gitdir, tip, pinned)
    if files is None:
        return None                 # can't tell: never a refusal
    held = [p for p in files if _process_class_path(p)]
    if not held:
        return None
    return ("%s declares --live, but this land put PROCESS-CLASS code on "
            "%s@%s (%s). CLI-class means every helm invocation is a fresh "
            "process off main; the helm-web unit, an armed `helm chat wait` "
            "beacon, and the proxies/daemons keep executing the code they "
            "loaded at start, so those files are NOT live at land and the "
            "running fleet still holds the pre-land ones. Declare the "
            "standing obligation with --needs-restart WHAT (`helm rearm` "
            "discharges the web unit and the beacons), or if a path here no "
            "longer belongs to a long-lived process drop it from "
            "_PROCESS_CLASS_PATHS. Measured from the merge that carried %s "
            "onto trunk, not from the declaration"
            % (rid, trunk_ref, pinned[:12], ", ".join(held), tip[:12]))


def _retired_by(lr):
    """What already retired this projected row, or None."""
    if lr.get("discharged"):
        return "discharge"
    if lr.get("withdrawn"):
        return "withdraw"
    if lr.get("closed_by_landing"):
        return "close-landed"
    if lr.get("abandoned"):
        return "abandon"
    if lr.get("close_reason"):
        return "close --reason %s" % lr["close_reason"]
    return None


def _retired_refusal(lr):
    return ("%s is already retired by %s; a row is retired once — refusing a "
            "different closure" % (lr["id"], _retired_by(lr)))


def _pin_ref(gitdir, ref):
    """(full sha, err) — ONE resolution of `ref` at ladder entry (D7). Every
    rung below runs against the pinned sha, so a trunk that moves mid-ladder
    cannot make two rungs answer about two different trunks; the recorded pin
    is what makes a proof that went stale-but-was-true auditable."""
    p = _git(gitdir, "rev-parse", "--verify", "--end-of-options",
             ref + "^{commit}")
    sha = p.stdout.strip().lower() if p is not None and p.returncode == 0 else ""
    if not _sha(sha):
        return None, ("Git could not pin %s to one commit — the close ladder "
                      "runs against one pinned sha or not at all" % ref)
    return sha, None


def _trio_trunk(gitdir):
    """(ref, pinned sha, target, err) — the discharge trio verbatim, then the
    D7 pin. Configured origin with an absent/unreadable tracking ref is
    UNKNOWN, never local-only: a mutation that retires debt is stricter than
    the read-only projection."""
    cache = {}
    local_ref, upstream_ref = _trunk_refs(gitdir, cache)
    origin = _origin_configured(gitdir)
    if origin is None:
        return None, None, None, ("Git could not determine whether an "
                                  "authoritative upstream exists")
    if origin:
        if not upstream_ref:
            return None, None, None, ("origin is configured but its "
                                      "authoritative tracking ref is missing "
                                      "or unreadable")
        ref, target = upstream_ref, "upstream"
    else:
        if not local_ref:
            return None, None, None, "the repository has no readable local trunk"
        ref, target = local_ref, "local"
    sha, err = _pin_ref(gitdir, ref)
    if err:
        return None, None, None, err
    return ref, sha, target, None


def _close_trunk(lr, gitdir, trunk):
    """(ref, pinned sha, target, err) for the repo-proof reasons (landed /
    stranded): an explicit --trunk is always canonical; a bound row without
    one uses the discharge trio; an unbound row REQUIRES --trunk (the
    _canonical_trunk refusal is exactly that requirement)."""
    if str(trunk or "").strip() or not lr.get("repo_id"):
        ref, sha, err = _canonical_trunk(gitdir, trunk)
        if err:
            return None, None, None, err
        target = "upstream" if ref.startswith("refs/remotes/") else "local"
        return ref, sha, target, None
    return _trio_trunk(gitdir)


def _trunk_aliases(stored):
    aliases = {stored}
    stored = str(stored or "")
    if stored.startswith("refs/heads/"):
        aliases.add(stored[len("refs/heads/"):])
    if stored.startswith("refs/remotes/"):
        aliases.add(stored[len("refs/remotes/"):])
    return aliases


def _close_ladder_delivered_report(rid, artifact_ref, report_ref, evidence,
                                   dry_run):
    """Close one OPEN BUILD with artifact identity plus a chat handoff receipt."""
    if not dry_run:
        out, err = dispatches._record_close_proven(
            rid, "delivered-report", None, evidence=evidence,
            artifact_ref=artifact_ref, report_ref=report_ref)
        if err:
            return None, err
        return get(out["id"])
    values, err = dispatches.clean_delivered_report_refs(
        artifact_ref, report_ref, evidence)
    if err:
        return None, err
    artifact_ref, report_ref, evidence = values
    current, unavailable = dispatches.snapshot()
    if unavailable:
        return None, unavailable
    row, err = dispatches._resolve_row(current, rid)
    if err:
        return None, err
    event = {"v": 3, "event": "close", "seq": row["seq"] + 1,
             "id": row["id"], "ts": dispatches.pk.now_ts(),
             "close_reason": "delivered-report", "close_proof_version": 1,
             "artifact_ref": artifact_ref, "report_ref": report_ref,
             "close_evidence": evidence}
    retired = dispatches._close_retired_by(row)
    if retired:
        if dispatches._close_idempotent("delivered-report", row, event):
            return get(row["id"])
        return None, ("dispatch %s is already retired by %s; a row is retired "
                      "once — refusing a different closure" %
                      (row["id"], retired))
    err = dispatches._close_event_error(event, row)
    if err:
        return None, "close does not bind: %s" % err
    return {"dry_run": True, "id": row["id"],
            "reason": "delivered-report", "artifact_ref": artifact_ref,
            "report_ref": report_ref, "evidence": evidence}, None


def close(rid, reason, evidence=None, tip=None, repo=None, trunk=None,
          dry_run=False, live=False, needs_restart=None,
          artifact_ref=None, report_ref=None):
    """(result, err) — the one terminal verb. On a write, `result` is the
    refreshed land request; on an idempotent retry, the standing one; under
    dry_run a would-close summary dict (nothing appended on EITHER path — a
    dry-run refusal returns the refusing rung's exact message).

    `live` / `needs_restart` are the LIVE-step declaration, required for
    --reason landed and refused on every other reason (see `_delivery`)."""
    reason = str(reason or "").strip()
    if (live or needs_restart is not None) and reason != "landed":
        return None, ("the delivery declaration (--live / --needs-restart) "
                      "belongs to --reason landed — no other terminal claims "
                      "a change reached the running fleet")
    if reason not in CLOSE_CLI_REASONS:
        return None, ("close --reason must be one of %s"
                      % "|".join(CLOSE_CLI_REASONS))
    if reason != "delivered-report" \
            and (artifact_ref is not None or report_ref is not None):
        return None, "artifact/report refs belong only to --reason delivered-report"
    if reason == "delivered-report" \
            and (tip is not None or repo is not None or trunk is not None):
        return None, ("delivered-report carries no tip override, repository, or "
                      "trunk proof")
    if reason == "delivered-report":
        return _close_ladder_delivered_report(
            rid, artifact_ref, report_ref, evidence, dry_run)
    if evidence is not None:
        evidence, err = dispatches._clean(evidence, "close evidence", 256)
        if err:
            return None, err
    if reason == "out-of-scope":
        return _close_out_of_scope(rid, evidence, dry_run)
    # ONE projection for the row AND its chain. The polarity gates below
    # consult the chain's declared polarity (`_chain_polarity`), and the chain
    # must come from the same read as the row it speaks for — a second read
    # would let an append between them show a child the row's own read never
    # saw. An unavailable projection is the UNREADABLE-CHAIN case: no door
    # opens, in words, right here. Hydrate that chain only; the projection still
    # carries the complete raw instant for topology and integrity.
    lrs, unavailable = project(selector=rid)
    if unavailable:
        return None, unavailable
    lr, err = dispatches._resolve_row(
        lrs, rid, noun="land request", list_hint="helm lr list")
    if err:
        return None, err
    if reason == "landed":
        return _close_ladder_landed(lr, evidence, repo, trunk, dry_run, lrs,
                                    live=live, needs_restart=needs_restart)
    if reason == "superseded":
        return _close_ladder_superseded(lr, evidence, tip, dry_run, lrs)
    if reason == "withdrawn":
        return _close_ladder_withdrawn(lr, evidence, dry_run, lrs)
    if reason == "stranded":
        return _close_ladder_stranded(lr, evidence, repo, trunk, dry_run)
    if reason == "discharged":
        return _close_ladder_discharged(lr, evidence, dry_run, lrs)
    if reason == "resolved":
        return _close_ladder_resolved(lr, evidence, repo, trunk, dry_run)
    return _close_ladder_subsumed(lr, evidence, repo, trunk, dry_run)


def _landed_idempotent(lr, repo, trunk):
    """A row already terminally landed answers a retry by IDENTITY (D8):
    closing repo + trunk REF ALIAS only — the sha is deliberately excluded,
    so a retry after trunk movement returns the row, exit 0, and the recorded
    pin stays the historical anchor. Legacy closed-by-landing rows answer
    under the same alias rule."""
    legacy = lr.get("closed_by_landing")
    stored_repo = lr.get("landing_repo_id") if legacy \
        else lr.get("closing_repo_id")
    stored_ref = lr.get("landing_trunk_ref") if legacy \
        else lr.get("closing_trunk_ref")
    stored_sha = lr.get("landing_trunk_sha") if legacy \
        else lr.get("closing_trunk_sha")
    gitdir, err = _close_repo(lr, repo)
    if err:
        return None, err
    raw = str(trunk or "").strip()
    if not raw:
        ref, _sha_, _target, err = _close_trunk(lr, gitdir, None)
        if err:
            return None, err
        raw = ref
    if stored_repo == gitdir and raw in _trunk_aliases(stored_ref):
        return lr, None
    return None, ("%s is already %s on %s %s@%s; refusing a different closure"
                  % (lr["id"],
                     "CLOSED BY LANDING" if legacy else "CLOSED (LANDED)",
                     stored_repo, stored_ref, (stored_sha or "")[:12]))


def _chain_polarity(lr, lrs):
    """(effective polarity, chain provenance, err) for a close-ladder
    polarity gate.

    THE BUG CLASS THIS RETIRES: a decision recorded in prose that the
    machine-readable gate cannot see. be5e82bbe0b5 sat REVIEWED for 8d08h
    with a verdict that SAID "SUPERSEDED — guard not landing" while its
    polarity field stayed UNDECLARED; every terminal door gated on the field,
    the human's ruling lived in the words, and a verdict is immutable so the
    row could never gain the field directly. The chained round b5c7a8df67df
    then declared an explicit SUPERSEDE about the same code — and the doors
    still did not look there. The classifiers already learned that THE CHAIN
    IS THE WORK IDENTITY (`_successor_owning`, `_measurable_through`); this
    is the same walk, lent to the gates.

    THE BOUNDARY, so a chain-aware door is not a door any chained row can
    open:
      1. THE CHAIN SPEAKS ONLY WHEN THE ROW IS SILENT. A declared own
         polarity is immutable and authoritative; no chained round can flip
         an APPROVE into a withdrawable row or launder a FIX into a landable
         one. Own polarity short-circuits before any chain read.
      2. ONLY A VERDICT ABOUT THE SAME CODE COUNTS: a descendant (supersedes
         edges walked transitively, cycle-guarded, via `_chain_children`)
         whose reviewed tip is THIS row's reviewed tip or its recorded
         rewrite translation. A polarity declared at a different tip is a
         ruling about different code — `_measurable_through` narrows for the
         identical reason.
      3. CONFLICT REFUSES, NAMED. An APPROVE and a FIX/SUPERSEDE among the
         counting descendants is a chain that disagrees with itself; the err
         names the disagreeing rows so the operator resolves the chain
         instead of re-diagnosing the refusal.
      4. UNKNOWN IS A REFUSAL IN WORDS. An unreadable translation sidecar
         makes the same-code set unprovable, so the chain is unreadable and
         says so; a child with no declared polarity asserts nothing (the
         original refusal stands — the gate got a second honest source, not
         a looser rule). The unreadable-LEDGER leg refuses one layer up, in
         `close`, before any door is reached."""
    own = lr.get("polarity")
    if own:
        return own, None, None
    reviewed = lr.get("reviewed_tip")
    if not reviewed or not lrs:
        return None, None, None
    kids = _chain_children(lrs)
    queue, seen, descendants = list(kids.get(lr["id"], ())), set(), []
    while queue:
        kid = queue.pop(0)
        if kid["id"] in seen:
            continue        # a cycle is a malformed chain, not a walk for us
        seen.add(kid["id"])
        queue.extend(kids.get(kid["id"], ()))
        descendants.append(kid)
    if not descendants:
        return None, None, None
    # The same-code set needs the translation sidecar only when a chain
    # actually exists to match against — a chainless row keeps its exact
    # pre-chain failure surface.
    table, terr = _ref_translations_checked()
    if terr:
        return None, None, (
            "the ref-translation sidecar is unreadable (%s) — the chain's "
            "declared polarity is UNKNOWN, and a polarity gate never consults "
            "a chain it could not fully read" % terr)
    same = {reviewed}
    new = table.get(reviewed)
    if new:
        if not _sha(new):
            full = _git(lr.get("repo_id"), "rev-parse", "--verify", "--quiet",
                        new + "^{commit}")
            if full is not None and full.returncode == 0 \
                    and full.stdout.strip():
                new = full.stdout.strip().lower()
        same.add(new)
    declares = [(kid["id"], kid["polarity"]) for kid in descendants
                if kid.get("polarity") in ("approve", "fix", "supersede")
                and kid.get("reviewed_tip") in same]
    if not declares:
        return None, None, None
    classes = {"approve" if pol == "approve" else "contrary"
               for _rid, pol in declares}
    if len(classes) > 1:
        return None, None, (
            "the chain holds CONFLICTING declared polarities about this "
            "row's reviewed code (%s) — a door never closes a row whose "
            "chain disagrees with itself; resolve the chain first"
            % ", ".join("%s=%s" % (rid[:12], pol.upper())
                        for rid, pol in sorted(declares)))
    if "approve" in classes:
        polarity = "approve"
    else:
        polarity = "supersede" if any(pol == "supersede"
                                      for _rid, pol in declares) else "fix"
    via = ", ".join(sorted(rid[:12] for rid, _pol in declares))
    return polarity, via, None


def _determinate_root(row):
    """A row's chain root ONLY when it is a readable id -> id | None.

    THREE REPLAYED STATES COLLAPSE TO TWO HERE, deliberately.
    `dispatches._replay_chain` keeps ABSENT (None — a row from before chains
    existed) and CHAIN_UNKNOWN (a row whose supersedes could not be replayed)
    distinguishable, because they are different FACTS. For the one question
    this asks — "are these two rows on DIFFERENT chains?" — they are the same
    ANSWER: unreadable. Only a well-formed id can answer it, so both fall to
    None and every caller suppresses.

    Reading it any other way is how a truthy sentinel became a confident
    verdict: a `if row.get("chain_root")` guard admits CHAIN_UNKNOWN, which
    is the string "UNKNOWN", and then compares it as though it were an id."""
    root = row.get("chain_root")
    if not root or root == dispatches.CHAIN_UNKNOWN:
        return None
    return root


def _close_landed_proof(gitdir, reviewed, trunk_ref, pinned):
    """(proof mode, translated tip, err) for one reviewed tip on pinned trunk."""
    proof = _landing_proof(gitdir, reviewed, pinned)
    if proof == "absent":
        return None, None, (
            "Git proved %s is neither an ancestor of %s@%s nor patch-equivalent "
            "to it; landing closure NOT recorded"
            % (reviewed[:12], trunk_ref, pinned[:12]))
    if proof != "unknown":
        return proof, None, None
    table, terr = _ref_translations_checked()
    if terr:
        return None, None, ("the ref-translation sidecar is unreadable (%s) — "
                            "absence cannot be adjudicated" % terr)
    new = table.get(reviewed)
    if not new:
        return None, None, ("Git could not prove whether %s landed on %s@%s; "
                            "landing closure NOT recorded"
                            % (reviewed[:12], trunk_ref, pinned[:12]))
    if not _sha(new):
        full = _git(gitdir, "rev-parse", "--verify", "--quiet", new + "^{commit}")
        if full is not None and full.returncode == 0 and full.stdout.strip():
            new = full.stdout.strip().lower()
    translated = _landing_proof(gitdir, new, pinned)
    if translated not in ("ancestor", "patch-equivalent"):
        return None, None, (
            "Git could not prove %s landed on %s@%s, directly or through its "
            "recorded translation %s; landing closure NOT recorded"
            % (reviewed[:12], trunk_ref, pinned[:12], new[:12]))
    return "translated-" + translated, new, None


def _close_ladder_build_landed(lr, repo, trunk, dry_run, live=False,
                               needs_restart=None):
    """Close one BUILD obligation through an authorized review descendant.

    Carries the SAME live-step declaration as the ordinary landed ladder: a
    BUILD row closes with `--reason landed` too, and leaving one landed door
    undeclared would reopen the exact hole the other door closes."""
    current, verdicts, unavailable = dispatches.snapshot_with_verdicts()
    if unavailable:
        return None, "dispatch ledger unavailable: %s" % unavailable
    parent = current.get(lr["id"])
    if parent is None:
        return None, "BUILD dispatch is absent from the locked evidence read"
    if parent.get("status") != "open" or parent.get("kind") != "build":
        return None, "chain-walked landed closure needs one OPEN BUILD dispatch"
    gitdir, err = _close_repo(parent, repo)
    if err:
        return None, err
    trunk_ref, pinned, _target, err = _close_trunk(parent, gitdir, trunk)
    if err:
        return None, err
    epoch = dispatches.gate_epoch(current, verdicts)
    approved, misses, unchained = [], [], []
    for row in current.values():
        if row.get("status") != "verdict" or row.get("kind") != "review" \
                or row.get("polarity") != "approve" \
                or row.get("repo_id") != parent.get("repo_id") \
                or not row.get("reviewed_tip"):
            continue
        index = dispatches.verdict_index(verdicts, row["id"])
        if index is None:
            continue
        reaches, why = dispatches._chain_reaches(row, parent["id"], current)
        if reaches is not True or row["id"] == parent["id"]:
            if reaches is None:
                misses.append("%s: %s" % (row["id"][:12], why))
            elif reaches is False and row["id"] != parent["id"] \
                    and _determinate_root(row) is not None \
                    and _determinate_root(parent) is not None \
                    and _determinate_root(row) != _determinate_root(parent):
                # the door KNOWS this approve exists and fails on chain
                # topology, not landedness — discarding that fact printed a
                # generic landedness refusal for a chain problem (measured
                # on the directive row: --new-work severed the chain and the
                # refusal carried no detail at all). SAME-ROOT siblings stay
                # on the established descent refusal: a sibling is a
                # DESCENT miss, pinned by its own test; only a DISJOINT
                # chain is the re-chain-via-confirmation class. And ONLY a
                # DETERMINATE disjoint, on BOTH sides — see
                # `_determinate_root`: absent (legacy) and CHAIN_UNKNOWN
                # (unreplayable) are both unreadable, and the first cure
                # tested TRUTHINESS, which let the CHAIN_UNKNOWN sentinel —
                # a truthy string — through to be compared as if it were an
                # id, so an unreadable chain still minted a confident
                # disjoint verdict (codex FIX r3, second pass).
                unchained.append(row["id"][:12])
            continue
        refusal, tier = _approval_refusal(row, index=index, epoch=epoch)
        if refusal:
            misses.append("%s: %s" % (row["id"][:12], refusal))
            continue
        requirement = gate_requirement(row, index=index, epoch=epoch)
        if requirement not in ("none", "required"):
            misses.append("%s: gate requirement UNKNOWN" % row["id"][:12])
            continue
        approved.append((index, row, tier, requirement))
    approved.sort(key=lambda item: item[0])
    unknown = []
    for _index, review, tier, requirement in approved:
        proof, translated, why = _close_landed_proof(
            gitdir, review["reviewed_tip"], trunk_ref, pinned)
        if proof is None:
            if "could not" in str(why).lower() or "unreadable" in str(why).lower():
                unknown.append("%s: %s" % (review["id"][:12], why))
            continue
        anchor = verdicts[review["id"]][1]
        gate_id = str(review.get("gate") or "")
        approval = dispatches._landed_review_approval_anchor(
            anchor, tier, requirement, gate_id)
        fields = {
            "dry_run": True, "id": parent["id"], "reason": "landed",
            "landing_review_id": review["id"],
            "landing_review_tip": review["reviewed_tip"],
            "landing_review_verdict_anchor": anchor,
            "landing_review_tier_state": tier,
            "landing_review_gate_requirement": requirement,
            "landing_review_gate": gate_id,
            "landing_review_approval_anchor": approval,
            "closing_repo_id": gitdir, "closing_trunk_ref": trunk_ref,
            "closing_trunk_sha": pinned, "proof_mode": proof,
            "translated_tip": translated,
        }
        # Same last-rung placement as the ordinary ladder: a BUILD row with no
        # proven review descendant hears THAT, not a lecture about --live.
        declared, derr = _delivery(live, needs_restart)
        if derr:
            return None, derr
        delivery_class, delivery_restart = declared
        # BOTH LANDED DOORS OR NEITHER. This ladder's own docstring says a
        # BUILD row closes with --reason landed too, and leaving one door
        # unchecked would reopen exactly the hole the other one closes.
        # THE TRANSLATED TIP, NOT THE ORIGINAL. @codex-2: this ladder already
        # computes `translated` above precisely because a rebased lane's
        # original object can be GONE — and then passing the original made
        # _land_files answer UNKNOWN, which fails open, and --live closed on
        # a land that moved process-class code. The hole sat in the door I
        # added specifically so there would not be one, and it opens on the
        # COMMON case: a lane that was rebased before landing.
        #
        # The ordinary ladder already resolves the landed object before
        # asking; this makes the BUILD door ask the same question of the same
        # object. `or reviewed_tip` keeps the un-translated case working
        # rather than turning a resolvable land into a can't-tell.
        refusal = _process_class_refusal(
            parent["id"], gitdir,
            str(translated or review["reviewed_tip"]).lower(),
            trunk_ref, pinned, delivery_class)
        if refusal:
            return None, refusal
        fields["close_delivery_class"] = delivery_class
        fields["close_delivery_restart"] = delivery_restart
        if dry_run:
            return fields, None
        row, err = dispatches._record_close_proven(
            parent["id"], "landed", None, close_proof_version=2,
            landing_review_id=review["id"],
            landing_review_tip=review["reviewed_tip"],
            landing_review_verdict_anchor=anchor,
            landing_review_tier_state=tier,
            landing_review_gate_requirement=requirement,
            landing_review_gate=gate_id,
            landing_review_approval_anchor=approval,
            closing_repo_id=gitdir, closing_trunk_ref=trunk_ref,
            closing_trunk_sha=pinned, proof_mode=proof,
            translated_tip=translated,
            delivery_class=delivery_class, delivery_restart=delivery_restart)
        if err:
            return None, err
        return get(row["id"])
    if not approved and not misses and not unknown and unchained:
        # every candidate fell at the SAME rung, and it is not landedness:
        # say the class and its cure, never the generic refusal. The sample
        # is sorted and labelled a sample — an unordered dict-walk slice
        # presented bare reads as 'nearest', which it never was (codex FIX)
        shown = ", ".join(sorted(unchained)[:3])
        more = len(unchained) - min(len(unchained), 3)
        return None, ("%d authorized-looking APPROVE review(s) exist in this "
                      "repo but none CHAINS to this parent (e.g. %s%s) — an "
                      "unchained approve cannot close a build row; re-chain "
                      "via a confirmation row that --supersedes this parent"
                      % (len(unchained), shown,
                         ", +%d more" % more if more else ""))
    detail = "; ".join((unknown or misses)[:3])
    return None, ("no exact review descendant carries an authorized APPROVE "
                  "whose reviewed tip is proven on pinned trunk%s" %
                  (" (%s)" % detail if detail else ""))


def _close_ladder_landed(lr, evidence, repo, trunk, dry_run, lrs=None,
                         live=False, needs_restart=None):
    reviewed = lr.get("reviewed_tip")
    if lr.get("closed_by_landing") or lr.get("close_reason") == "landed":
        return _landed_idempotent(lr, repo, trunk)
    if _retired_by(lr):
        return None, _retired_refusal(lr)
    if not lr.get("verdict_ref") or not reviewed:
        if lr.get("kind") == "build":
            return _close_ladder_build_landed(lr, repo, trunk, dry_run,
                                              live=live,
                                              needs_restart=needs_restart)
        return None, ("%s has no verdict — Git cannot prove a review "
                      "happened; deliver a verdict, or if the obligation is "
                      "moot, dispatch cancel / close --reason out-of-scope"
                      % lr["id"])
    polarity, via, perr = _chain_polarity(lr, lrs)
    if perr:
        return None, perr
    if polarity in ("fix", "supersede"):
        if lr.get("polarity"):
            return None, ("%s is a FIX/SUPERSEDE verdict — its reviewed tip "
                          "on trunk is a CONTRARY, not a resolution; that row "
                          "wants --reason superseded or stays open as "
                          "contrary debt" % lr["id"])
        return None, ("%s carries a chain-declared %s about its reviewed "
                      "code (via %s) — its reviewed tip on trunk is a "
                      "CONTRARY, not a resolution; that row wants --reason "
                      "superseded or stays open as contrary debt"
                      % (lr["id"], polarity.upper(), via))
    gitdir, err = _close_repo(lr, repo)
    if err:
        return None, err
    trunk_ref, pinned, _target, err = _close_trunk(lr, gitdir, trunk)
    if err:
        return None, err
    proof = _landing_proof(gitdir, reviewed, pinned)
    translated = None
    if proof == "absent":
        return None, ("Git proved %s is neither an ancestor of %s@%s nor "
                      "patch-equivalent to it; landing closure NOT recorded"
                      % (reviewed[:12], trunk_ref, pinned[:12]))
    if proof == "unknown":
        # TRANSLATION-MEDIATED (D2): the sidecar is a MECHANICAL recorded map
        # written by `migrate_refs --apply` — it AUTHORS; commit subjects,
        # receipts and blob overlap never do (interlock law 1).
        table, terr = _ref_translations_checked()
        if terr:
            return None, ("the ref-translation sidecar is unreadable (%s) — "
                          "absence cannot be adjudicated" % terr)
        new = table.get(reviewed)
        if not new:
            return None, ("Git could not prove whether %s landed on %s@%s; "
                          "landing closure NOT recorded"
                          % (reviewed[:12], trunk_ref, pinned[:12]))
        if not _sha(new):
            full = _git(gitdir, "rev-parse", "--verify", "--quiet",
                        new + "^{commit}")
            if full is not None and full.returncode == 0 and full.stdout.strip():
                new = full.stdout.strip().lower()
        tproof = _landing_proof(gitdir, new, pinned)
        if tproof not in ("ancestor", "patch-equivalent"):
            return None, ("Git could not prove %s landed on %s@%s, directly "
                          "or through its recorded translation %s; landing "
                          "closure NOT recorded"
                          % (reviewed[:12], trunk_ref, pinned[:12], new[:12]))
        proof, translated = "translated-" + tproof, new
    # THE LIVE STEP, last rung on purpose. Git decides whether this reached
    # TRUNK and says so in its own words first; only a change Git already
    # proved landed is ever asked whether it reached the RUNNING FLEET. Put
    # this rung earlier and an unlanded row would be told to declare its
    # delivery class instead of being told it never landed.
    declared, derr = _delivery(live, needs_restart)
    if derr:
        return None, derr
    delivery_class, delivery_restart = declared
    # AND A LANDED GUARD IS NOT AN INSTALLED GUARD. The two delivery classes
    # cover code helm RUNS (fresh per invocation, live at land) and code a
    # PROCESS holds (stale until it re-arms). A generated git hook is neither
    # and looks exactly like the first: the generator IS live at land, so the
    # honest reasoning arrives at --live and is wrong about the world, because
    # git does not invoke the generator — it invokes a SNAPSHOT in .git/hooks
    # that holds pre-land rules until `install-guard --apply` rewrites it.
    #
    # MEASURED 2026-08-04, and it is why this rung exists rather than a
    # docstring: a hardening fix closed a '..' traversal escape in peek-birth,
    # landed, and was CLOSED COMPLETED as #92 — into the generator, never into
    # the hooks. For however long, #92 was finished code protecting nothing
    # while the row said done. The class is BUILT != WIRED, and our definition
    # of done (verdict, gate, merge) does not contain the step that makes a
    # guard real.
    #
    # WHAT THIS RUNG DOES NOT CLAIM: it does NOT attribute the drift to THIS
    # land. Post-merge a lane's diff against trunk is empty and the row records
    # no base, so per-land attribution is not computable here — and asserting
    # it anyway would be the derived class interlock law 1 forbids. It says
    # something weaker and checkable: --live asserts this reached the RUNNING
    # fleet, and a measurably stale rail proves this checkout runs rules trunk
    # does not have. That picture is already wrong; another confident --live
    # deepens it. Both escapes are cheap and honest — arm the rail, or declare
    # the obligation with --needs-restart.
    if delivery_class == DELIVERY_CLI:
        try:
            from .work._guard import stale_guard_hooks
            # gitdir is a GITDIR (.../repo/.git), not a worktree root, and
            # stale_guard_hooks takes either — both forms resolve through
            # `rev-parse --git-path hooks` to the same shared hook dir
            # (verified against both spellings of this repo).
            # STALE ONLY, and the tri-state is the whole reason this is safe
            # to refuse on. STALE means installed-and-DIFFERENT: the repo HAS
            # helm's rail and it drifted, which is #92's exact shape. MISSING
            # means the rail was never installed — that repo never opted in,
            # is not running OLD rules, and blocking its lands would be a
            # stranger's opinion about someone else's hooks. UNKNOWN means the
            # comparison could not run, and a can't-tell must never author a
            # refusal. Measured: collapsing the three broke 5 existing landed
            # closes, because a fixture repo has no hooks at all.
            drift = [f for f in stale_guard_hooks(gitdir) if f[0] == "STALE"]
        except Exception:
            drift = []          # fail-OPEN: never cost a land on a read hiccup
        if drift:
            return None, (
                "%s declares --live, but this checkout's guard rail is not "
                "armed (%s). A generated hook is live only once INSTALLED — "
                "landing it puts it in the generator, and git keeps running "
                "the old snapshot (measured: #92's traversal refusal landed, "
                "closed COMPLETED, and protected nothing). Arm it with `helm "
                "work install-guard --apply` and re-run this close, or if "
                "this row genuinely is not the guard's, declare the standing "
                "obligation with --needs-restart 'the guard rail'. This does "
                "not claim YOUR land caused the drift — only that --live "
                "cannot be honest while it stands"
                % (lr["id"], "; ".join("%s is %s" % (n, s.lower())
                                       for s, n, _w in drift)))
    # AND THE DECLARATION ITSELF IS NOW CHECKABLE. The rung above asks about
    # the BOX; this one asks about the LAND, and it runs after because a stale
    # rail invalidates every --live on this checkout while this one is about
    # one row's own files. The translated tip is the on-trunk sha when the tip
    # landed under a rewrite, and only it has an ancestry path to walk.
    refusal = _process_class_refusal(
        lr["id"], gitdir, str(translated or reviewed).lower(), trunk_ref,
        pinned, delivery_class)
    if refusal:
        return None, refusal
    if dry_run:
        return {"dry_run": True, "id": lr["id"], "reason": "landed",
                "proof_mode": proof, "translated_tip": translated,
                "closing_repo_id": gitdir, "closing_trunk_ref": trunk_ref,
                "closing_trunk_sha": pinned,
                "close_delivery_class": delivery_class,
                "close_delivery_restart": delivery_restart}, None
    row, err = dispatches._record_close_proven(
        lr["id"], "landed", reviewed, evidence=evidence,
        closing_repo_id=gitdir, closing_trunk_ref=trunk_ref,
        closing_trunk_sha=pinned, proof_mode=proof, translated_tip=translated,
        delivery_class=delivery_class, delivery_restart=delivery_restart)
    if err:
        return None, err
    return get(row["id"])


def _preserved_under(gitdir, sha):
    """The archive/rescue ref `sha` is reachable from, or None. "" on an
    enumeration failure (UNKNOWN, distinct from unpreserved)."""
    refs = _git(gitdir, "for-each-ref", "--format=%(refname)",
                "refs/heads/rescue", "refs/tags/archive",
                "refs/heads/archive", "refs/tags/rescue")
    if refs is None or refs.returncode != 0:
        return ""
    for ref in refs.stdout.split():
        if _landed(gitdir, sha, ref) is True:
            return ref
    return None


def _translated_superseded_door(lr, superseding, dry_run, lrs=None):
    """(#101) The attestation ladder for a row with NO author binding.

    Returns (row_or_dryrun, None) when the door admits, (None, refusal) when
    it adjudicates and refuses, or (None, None) when the row is not this
    door's shape at all — the caller then falls back to the historical
    author-binding refusal, unchanged.

    Every rung fails CLOSED on UNKNOWN, and the first rung's positive
    control (the repo must read its own trunk object) is what keeps a
    degraded repo's "missing" from ever minting a close.
    """
    reviewed = lr.get("reviewed_tip")
    gitdir = lr.get("repo_id")
    if not gitdir:
        return None, None
    trunk_ref, pinned, target, terr = _trio_trunk(gitdir)
    if terr:
        return None, terr
    # rung 0 — POSITIVE CONTROL (stranded's mass-termination guard): a repo
    # that cannot read its own trunk object proves no absence.
    if _object_exists(gitdir, pinned) is not True:
        return None, ("the repository at %s cannot prove its own trunk "
                      "object — refusing to adjudicate a supersession over "
                      "an unreadable substrate" % gitdir)
    # rung 1 — the reviewed object's state picks the arm. UNKNOWN refuses;
    # a live object under NO archive ref is the ordinary ladder's row.
    present = _object_exists(gitdir, reviewed)
    if present is None:
        return None, ("Git could not determine whether %s still exists in "
                      "%s — FAIL-CLOSED on an unknown object state"
                      % (reviewed[:12], gitdir))
    preserved_ref = None
    translated = None
    proof_mode = None
    if present is False:
        # ARM A (the pre-seat-stamp orphan) — DESTROYED + TRANSLATED. The sidecar names the
        # rewrite's surviving identity; that object must be live and
        # preserved, and it answers the refutation rung in the original's
        # place.
        table, serr = _ref_translations_checked()
        if serr:
            return None, ("the ref-translation sidecar is unreadable (%s) — "
                          "the translated identity cannot be adjudicated"
                          % serr)
        translated = table.get(reviewed)
        if not translated:
            return None, None
        alive = _object_exists(gitdir, translated)
        if alive is None:
            return None, ("Git could not determine whether translated "
                          "object %s exists — FAIL-CLOSED on an unknown "
                          "object state" % translated[:12])
        if alive is not True:
            return None, ("the translated object %s is itself destroyed — "
                          "this is stranded's question, not superseded's"
                          % translated[:12])
        preserved_ref = _preserved_under(gitdir, translated)
        if preserved_ref == "":
            return None, ("Git could not enumerate archive/rescue refs — "
                          "preservation is UNKNOWN")
        if preserved_ref is None:
            return None, ("translated object %s is preserved under no "
                          "archive/rescue ref — stranded's question, not "
                          "superseded's" % translated[:12])
        proof_mode = "translated-superseded"
        # REFUTATION TRANSPOSED: the translated identity answers "did the
        # reviewed change itself land?". On trunk is LANDED in the wrong
        # reason's clothes.
        on_trunk = _landed(gitdir, translated, pinned)
        if on_trunk is None:
            return None, ("Git could not determine whether the translated "
                          "change reached trunk — refusing to supersede "
                          "over an unknown land state")
        if on_trunk is True:
            return None, ("the translated change itself IS on trunk — that "
                          "is a landed outcome, not a supersession; use "
                          "--reason landed")
    else:
        # ARM B (the archived spiral orphan) — ALIVE + ARCHIVED + REPLACED. The original was
        # preserved verbatim under an archive/ ref and its content landed
        # RE-DERIVED: containment by object is false by design, so the
        # proof is patch-equivalence between the superseding tip and the
        # archived original. No archive ref means the ordinary ladder's
        # refusal stands exactly as before.
        preserved_ref = _preserved_under(gitdir, reviewed)
        if preserved_ref == "":
            return None, ("Git could not enumerate archive/rescue refs — "
                          "preservation is UNKNOWN")
        if preserved_ref is None:
            return None, None
        # Patch-equivalence is the STRONG proof and wins when it holds.
        # When it does not — the archived spiral orphan, where the adopter REVISED the
        # original while re-deriving it, so no patch-id can match — the
        # archive tag names the preserved original and the attestation
        # rung below (a later, independent, cross-family APPROVE that the
        # LANDED tip answers this row's verdict) is the proof the
        # replaced content resolves the debt. That is the design's own
        # substitution: attestation for the binding a re-derived landing
        # cannot carry.
        relation = _landing_proof(gitdir, reviewed, superseding)
        if relation not in ("ancestor", "patch-equivalent", "absent"):
            return None, ("Git could not compare the archived original "
                          "against the superseding tip — refusing to "
                          "supersede over an unknown relation")
        proof_mode = ("archived-superseded"
                      if relation in ("ancestor", "patch-equivalent")
                      else "attested-superseded")
    # rung 5 — the SUPERSEDER: landed on pinned trunk, and carrying a LATER
    # APPROVE recorded on a DIFFERENT row than the original. With no author
    # binding the same-author clause has nothing to bind to, so independence
    # is structural: the superseding review must be its own ledger row,
    # later in append order, and never the original attesting for itself.
    landed = _landed(gitdir, superseding, pinned)
    if landed is None:
        return None, ("Git could not determine whether the superseding tip "
                      "reached trunk")
    if landed is not True:
        return None, "approved superseding tip has not reached the authoritative trunk"
    current, verdicts, unavailable = dispatches.snapshot_with_verdicts()
    if unavailable:
        return None, "dispatch ledger unavailable: %s" % unavailable
    epoch = dispatches.gate_epoch(current, verdicts)
    original_index = dispatches.verdict_index(verdicts, lr["id"])
    if original_index is None:
        return None, "the original verdict has no durable ordered event"
    approved = []
    for row in current.values():
        if row.get("status") != "verdict" \
                or row.get("polarity") != "approve" \
                or row.get("reviewed_tip") != superseding \
                or row.get("repo_id") != lr.get("repo_id"):
            continue
        if row["id"] == lr["id"]:
            continue                     # the original never attests for itself
        index = dispatches.verdict_index(verdicts, row["id"])
        if index is None or index <= original_index:
            continue
        refusal, _tier = _approval_refusal(row, index=index, epoch=epoch)
        if refusal:
            continue
        approved.append((index, row))
    if proof_mode != "translated-superseded":
        # THE CONTENT RULE, ARCHIVED + ATTESTED. The archive ref preserves
        # the reviewed ORIGINAL verbatim; the close names a superseder. For
        # the archived mode the relation (ancestor/patch-equivalent) was
        # already proven BETWEEN THOSE TWO OBJECTS at rung 4, so the content
        # binding is the superseder landing on pinned trunk — rung 5, below.
        # The attested mode's binding is the chain rule further down.
        pass
    else:
        # THE CONTENT RULE, TRANSLATED (codex's FIX on the r1 review; the
        # relation direction below is codex's r2 FIX — "carries" means the
        # superseder contains the translated cargo, never that the cargo
        # contains the superseder, so compare translated TO the superseder:
        # an approved land of the translated object's own ANCESTOR predates
        # the work and closes nothing).
        # The sidecar binds reviewed -> translated, NOT translated ->
        # superseder: a rewrite preserves the WORK's identity, but the close
        # claims THIS LAND resolves the debt, and the land is a different
        # object than the rewrite. Before this rung, an UNRELATED approved
        # land closed a translated row — reproduced: archived translation
        # plus a no-chain new-work APPROVE on an unrelated tip closed
        # translated-superseded. The superseder must carry the translated
        # change ITSELF — ancestor or patch-equivalent of the translated
        # object — or, when the adopter revised while re-deriving (no
        # patch-id can match), the superseding review must DECLARE the
        # resolution by chaining back to the original row (--supersedes,
        # walkable), exactly the rule the attested mode has always had.
        # An approved land that neither carries the change nor claims the
        # row closes nothing.
        relation = _landing_proof(gitdir, translated, superseding)
        if relation not in ("ancestor", "patch-equivalent", "absent"):
            return None, ("Git could not compare the translated object "
                          "against the superseding tip — refusing to "
                          "supersede over an unknown relation")
        if relation == "absent":
            chained = []
            for index, row in approved:
                walk, seen = row, set()
                while walk and walk["id"] not in seen:
                    seen.add(walk["id"])
                    if walk["id"] == lr["id"]:
                        chained.append((index, row))
                        break
                    parent = (walk.get("supersedes") or "").strip()
                    walk = current.get(parent) if parent else None
            if not chained:
                return None, ("the superseding tip neither carries the "
                              "translated change (no ancestry or "
                              "patch-equivalence with %s) nor declares the "
                              "resolution by chaining to this row — an "
                              "unrelated approved land cannot close a "
                              "translated row" % translated[:12])
            approved = chained
    if proof_mode == "attested-superseded":
        # THE CHAIN RULE, ATTESTED ONLY. Patch-equivalence could not bind
        # the land to the original (the adopter revised), so the only thing
        # stopping an UNRELATED approved land from laundering this row is
        # the superseding review DECLARING the resolution: its row must
        # chain back to the original (--supersedes, walkable). An approved
        # land that never claims this row closes nothing.
        chained = []
        for index, row in approved:
            walk, seen = row, set()
            while walk and walk["id"] not in seen:
                seen.add(walk["id"])
                if walk["id"] == lr["id"]:
                    chained.append((index, row))
                    break
                parent = (walk.get("supersedes") or "").strip()
                walk = current.get(parent) if parent else None
        approved = chained
    approved.sort(key=lambda pair: pair[0])
    if not approved:
        return None, ("no later APPROVE verdict on a different row binds "
                      "superseding tip %s — with no author binding on the "
                      "original row, an independent approval is the whole "
                      "door and cannot be waived" % superseding[:12])
    if dry_run:
        return {"dry_run": True, "id": lr["id"], "reason": "superseded",
                "superseding_tip": superseding,
                "superseding_id": approved[0][1]["id"],
                "contrary_state": "none",
                "proof_mode": proof_mode,
                "translated_tip": translated,
                "preserved_ref": preserved_ref,
                "closing_trunk_ref": trunk_ref,
                "closing_trunk_sha": pinned}, None
    row, err = dispatches._record_close_proven(
        lr["id"], "superseded", reviewed,
        evidence=("%s: reviewed object %s, preserved under %s"
                  % (proof_mode,
                     ("destroyed, translated to " + translated[:12])
                     if translated
                     else "archived verbatim, content landed re-derived",
                     preserved_ref)),
        superseding_tip=superseding, superseding_id=approved[0][1]["id"],
        contrary_state="none", contrary_target=target,
        closing_trunk_ref=trunk_ref, closing_trunk_sha=pinned,
        proof_mode=proof_mode, translated_tip=translated)
    if err:
        return None, err
    return get(row["id"])


def _close_ladder_superseded(lr, evidence, tip, dry_run, lrs=None):
    """The FULL discharge ladder, widened to every verdict polarity. The
    candidate filter keeps the `_approval_refusal` rung — the read-side half
    of the pipe whose write side is mark_verdict's ungated-approve refusal;
    weakening either end un-measures both. The old flat `sender != author`
    refusal is now a lane-handoff admit: a cross-sender candidate is admitted
    ONLY on a provable handoff (`_handoff_proven`, tri-state, fail-closed)."""
    superseding = str(tip or "").strip().lower()
    if not dispatches._FULL_TIP.fullmatch(superseding):
        return None, "close needs a full 40- or 64-character superseding tip"
    if not evidence:
        return None, ("close --reason superseded needs evidence — what the "
                      "superseding round changed")
    if lr.get("close_reason") == "superseded" \
            and lr.get("superseding_tip") == superseding \
            and lr.get("close_evidence") == evidence:
        return lr, None
    if lr.get("discharged"):
        if lr.get("superseding_tip") == superseding \
                and lr.get("discharge_ref") == evidence:
            return lr, None
        return None, "%s already has a different discharge" % lr["id"]
    if _retired_by(lr):
        return None, _retired_refusal(lr)
    reviewed = lr.get("reviewed_tip")
    if not lr.get("verdict_ref") or not reviewed:
        return None, "%s has no verdict to supersede" % lr["id"]
    if not lr.get("author"):
        # #101 — the translated-object-superseded door. A pre-seat-stamp row
        # carries NO author binding — not as damage but as history: the stamp
        # era did not record one, so no ladder rung keyed on it can ever
        # admit the row. When every other authority can vouch for the work —
        # the reviewed object is DESTROYED, the sidecar translates it to a
        # LIVE object PRESERVED under an archive/rescue ref, and the
        # superseding tip is LANDED with a later cross-family APPROVE — the
        # attestation substitutes for the binding. Anything less stays
        # refused: a live reviewed object, an untranslated one, or an
        # unpreserved translation is adjudicable or stranded, never this.
        door, derr = _translated_superseded_door(lr, superseding, dry_run,
                                                 lrs)
        if derr is not None or door is not None:
            return door, derr
        return None, ("%s has no stable author binding for a superseding "
                      "review" % lr["id"])
    if superseding == reviewed:
        return None, "a verdict tip cannot supersede itself"
    current, verdicts, unavailable = dispatches.snapshot_with_verdicts()
    if unavailable:
        return None, "dispatch ledger unavailable: %s" % unavailable
    original = current.get(lr["id"])
    if original is None:
        return None, ("dispatch %s is absent from the ledger read that "
                      "follows it — its work chain is UNKNOWN" % lr["id"])
    if original.get("close_reason") == "superseded" \
            and original.get("superseding_tip") == superseding \
            and original.get("close_evidence") == evidence:
        return get(original["id"])       # a racing identical close reconciles
    if original.get("discharged") \
            and original.get("superseding_tip") == superseding \
            and original.get("discharge_ref") == evidence:
        return get(original["id"])       # a racing identical DISCHARGE too
    already = dispatches._close_retired_by(original)
    if already:
        return None, ("dispatch %s is already retired by %s; a row is "
                      "retired once — refusing a different closure"
                      % (original["id"], already))
    original_index = dispatches.verdict_index(verdicts, lr["id"])
    if original_index is None:
        return None, "the original verdict has no durable ordered event"
    contrary_chain = original.get("chain_root")
    if contrary_chain == dispatches.CHAIN_UNKNOWN:
        return None, ("%s carries a malformed chain_root — its work identity "
                      "is UNKNOWN and no candidate can be proven to continue "
                      "it" % lr["id"])
    epoch = dispatches.gate_epoch(current, verdicts)
    approved = []
    handoff_misses = []
    for row in current.values():
        if row.get("status") != "verdict" \
                or row.get("polarity") != "approve" \
                or row.get("reviewed_tip") != superseding \
                or row.get("repo_id") != lr.get("repo_id"):
            continue
        if contrary_chain is not None \
                and row.get("chain_root") != contrary_chain:
            continue
        index = dispatches.verdict_index(verdicts, row["id"])
        if index is None or index <= original_index:
            continue
        refusal, _tier = _approval_refusal(row, index=index, epoch=epoch)
        if refusal:
            continue
        # THE SENDER CLAUSE, now a lane-handoff admit instead of a flat
        # refusal. A lane that changed hands mid-life (author -> successor
        # integrator) left its contrary debt payable by NOBODY: the
        # successor's genuine same-lane approved superseder passed every git
        # gate and this one clause refused it. A cross-sender candidate is
        # admitted ONLY on a provable handoff (`_handoff_proven`, tri-state,
        # fail-closed); every other predicate above and below is unchanged.
        if row.get("sender") != lr.get("author"):
            proven, why = _handoff_proven(row, lr, current, contrary_chain)
            if proven is not True:
                handoff_misses.append("%s: %s" % (row["id"][:12], why))
                continue
        approved.append((index, row))
    approved.sort(key=lambda pair: pair[0])
    if not approved:
        # BOTH LEGS, NAMED. A cross-sender candidate that failed only the
        # handoff proof is invisible in the same-author conjunction, and the
        # next integrator re-diagnoses the whole refusal from scratch. When
        # one existed, say so, with each near-miss's own unprovable leg.
        handoff = ""
        if handoff_misses:
            handoff = (" — and no cross-sender candidate proved a lane "
                       "handoff (%s)" % "; ".join(sorted(handoff_misses)[:3]))
        if contrary_chain is not None:
            return None, ("no later APPROVE verdict from the same author/repo "
                          "ON THE SAME WORK CHAIN (%s) binds superseding tip "
                          "%s — a later approve of unrelated work is not a "
                          "resolution of this one, however plausibly its "
                          "commit contains the change; dispatch the "
                          "superseding round with --supersedes %s%s"
                          % (contrary_chain[:12], superseding,
                             lr["id"][:12], handoff))
        return None, ("no later APPROVE verdict from the same author/repo "
                      "binds superseding tip %s%s" % (superseding, handoff))
    gitdir = lr.get("repo_id")
    proof = _landed(gitdir, reviewed, superseding)
    if proof is None:
        return None, ("Git could not prove the contrary tip is in the "
                      "superseding tip")
    if proof is not True:
        # #101, second entry: the row HAD an author binding and walked the
        # full ladder, but the superseding tip does not CONTAIN the reviewed
        # object — the archive-tag shapes (original preserved under an
        # archive/ ref, content landed re-derived or revised). The same
        # attestation ladder adjudicates: patch-equivalence or the chained
        # cross-family approve carries what containment cannot.
        door, derr = _translated_superseded_door(lr, superseding, dry_run,
                                                 lrs)
        if derr is not None or door is not None:
            return door, derr
        return None, "superseding tip does not contain the contrary reviewed change"
    trunk_ref, pinned, contrary_target, err = _trio_trunk(gitdir)
    if err:
        return None, err
    original_on_trunk = _landed(gitdir, reviewed, pinned)
    # THE POLARITY GATE READS THE CHAIN when the row itself is silent
    # (`_chain_polarity` — boundary and refusal shapes documented there): a
    # chain-declared FIX/SUPERSEDE row walks the contrary branch exactly as
    # an own-declared one does, because the chain is the work identity.
    polarity, _via, perr = _chain_polarity(lr, lrs)
    if perr:
        return None, perr
    if polarity in ("fix", "supersede"):
        # the contrary-on-trunk rung — this reason retires CONTRARY debt, and
        # only a contrary that physically entered trunk carries any
        if original_on_trunk is None:
            return None, ("Git could not prove the contrary change reached "
                          "authoritative trunk")
        if original_on_trunk is not True:
            return None, "the contrary change is absent from authoritative trunk"
        contrary_state = "landed"
    else:
        # approve/UNDECLARED rows carry no contrary — and the refutation rung
        # is MANDATORY: annotation truth over the trivially-contains trap (a
        # later tip always contains a change already on trunk).
        if original_on_trunk is None:
            return None, ("Git could not determine whether the reviewed "
                          "change itself is on trunk — refusing to supersede "
                          "over an unknown land state")
        if original_on_trunk is True:
            return None, ("the reviewed change itself IS on trunk — that is a "
                          "landed outcome, not a supersession; use --reason "
                          "landed")
        contrary_state = "none"
    superseding_landed = _landed(gitdir, superseding, pinned)
    if superseding_landed is None:
        return None, ("Git could not determine whether the superseding tip "
                      "reached trunk")
    if superseding_landed is not True:
        return None, "approved superseding tip has not reached the authoritative trunk"
    if dry_run:
        return {"dry_run": True, "id": lr["id"], "reason": "superseded",
                "superseding_tip": superseding,
                "superseding_id": approved[0][1]["id"],
                "contrary_state": contrary_state,
                "contrary_target": contrary_target,
                "closing_trunk_ref": trunk_ref,
                "closing_trunk_sha": pinned}, None
    row, err = dispatches._record_close_proven(
        lr["id"], "superseded", reviewed, evidence=evidence,
        superseding_tip=superseding, superseding_id=approved[0][1]["id"],
        contrary_state=contrary_state, contrary_target=contrary_target,
        closing_trunk_ref=trunk_ref, closing_trunk_sha=pinned)
    if err:
        return None, err
    return get(row["id"])


def _one_approval_family(identity, noun):
    families, evidence, anchor, err = \
        dispatches._approval_identity_family_evidence(identity)
    if err:
        return None, None, None, "%s family is UNKNOWN: %s" % (noun, err)
    if len(families) != 1:
        return None, None, None, (
            "%s must resolve to exactly one identity family (got %s)" %
            (noun, ",".join(sorted(families)) or "none"))
    return next(iter(families)), evidence, anchor, None


def _close_ladder_subsumed(lr, evidence, repo, trunk, dry_run):
    """Approved work reimplemented by a later cross-family confirmation."""
    if not evidence:
        return None, ("close --reason subsumed needs --evidence "
                      "<confirmation-row-id-or-prefix>")
    current, verdicts, unavailable = dispatches.snapshot_with_verdicts()
    if unavailable:
        return None, "dispatch ledger unavailable: %s" % unavailable
    original, err = dispatches._resolve_row(current, lr["id"])
    if err:
        return None, err
    confirmation, err = dispatches._resolve_row(
        current, evidence, noun="confirmation dispatch",
        list_hint="helm dispatch list --all")
    if err:
        return None, err
    if original.get("close_reason") == "subsumed":
        if confirmation["id"] != original.get("confirmation_id"):
            return None, _retired_refusal(original)
        gitdir, err = _close_repo(original, repo)
        if err:
            return None, err
        if trunk:
            trunk_ref, _sha_, err = _canonical_trunk(gitdir, trunk)
            if err:
                return None, err
            if trunk_ref not in _trunk_aliases(
                    original.get("closing_trunk_ref")):
                return None, _retired_refusal(original)
        return get(original["id"])
    if _retired_by(original):
        return None, _retired_refusal(original)
    reviewed = original.get("reviewed_tip")
    original_polarity = original.get("polarity")
    if original.get("status") != "verdict" \
            or original_polarity not in ("approve", "fix") \
            or not original.get("verdict_ref") or not reviewed \
            or not original.get("repo_id"):
        return None, ("%s is not an APPROVE/FIX reviewed-tip/repo verdict" %
                      original["id"])
    if original.get("kind") != "review":
        return None, "%s is not a review dispatch" % original["id"]
    chain = original.get("chain_root")
    if chain == dispatches.CHAIN_UNKNOWN:
        return None, "%s has an unreadable work chain" % original["id"]
    expected_chain = original["id"] if chain is None else chain
    original_index = dispatches.verdict_index(verdicts, original["id"])
    confirmation_index = dispatches.verdict_index(verdicts, confirmation["id"])
    if confirmation.get("status") != "verdict" \
            or confirmation_index is None or original_index is None \
            or confirmation_index <= original_index:
        return None, "confirmation must be one later durable verdict"
    if confirmation.get("repo_id") != original.get("repo_id") \
            or confirmation.get("chain_root") != expected_chain:
        return None, ("confirmation verdict is not linked to the same "
                      "chain/repo")
    if confirmation.get("kind") != "review":
        return None, "confirmation verdict is not a review dispatch"
    if confirmation.get("polarity") != "approve":
        return None, "confirmation verdict polarity is not approve"
    epoch = dispatches.gate_epoch(current, verdicts)
    refusal, tier = _approval_refusal(
        confirmation, index=confirmation_index, epoch=epoch)
    if refusal:
        return None, "confirmation approval refuses: %s" % refusal
    requirement = gate_requirement(
        confirmation, index=confirmation_index, epoch=epoch)
    if requirement not in ("none", "required"):
        return None, "confirmation gate requirement is UNKNOWN"
    confirmation_tip = confirmation.get("reviewed_tip")
    confirmation_ref = confirmation.get("verdict_ref")
    if not confirmation_tip or not dispatches._subsumption_ref(
            confirmation_ref, original_polarity):
        if original_polarity == "fix":
            return None, ("confirmation verdict evidence for FIX debt needs "
                          "`Subsumption verified FIX findings were answered on "
                          "trunk: ...` with a concrete resolution")
        return None, ("confirmation verdict evidence needs `Subsumption verified "
                      "... on trunk ...` with a concrete reimplementation statement")
    original_author = original.get("sender") or ""
    confirmation_recipient = confirmation.get("recipient") or ""
    original_family, original_family_evidence, original_family_anchor, err = \
        _one_approval_family(original_author, "original author")
    if err:
        return None, err
    confirmation_family, confirmation_family_evidence, \
        confirmation_family_anchor, err = _one_approval_family(
            confirmation_recipient, "confirmation recipient")
    if err:
        return None, err
    if original_family == confirmation_family:
        return None, ("subsumed needs cross-family confirmation; both rows resolve "
                      "to %s" % original_family)
    original_verdict_anchor = verdicts[original["id"]][1]
    confirmation_verdict_anchor = verdicts[confirmation["id"]][1]
    confirmation_gate = str(confirmation.get("gate") or "")
    confirmation_approval_anchor = dispatches._subsumed_approval_anchor(
        confirmation_verdict_anchor, tier, requirement, confirmation_gate)
    gitdir, err = _close_repo(original, repo)
    if err:
        return None, err
    trunk_ref, pinned, _target, err = _close_trunk(
        original, gitdir, trunk)
    if err:
        return None, err
    confirmation_proof = _landing_proof(gitdir, confirmation_tip, pinned)
    if confirmation_proof == "unknown":
        return None, "Git could not determine whether the confirmation is on trunk"
    if confirmation_proof not in ("ancestor", "patch-equivalent"):
        return None, "confirmation reviewed tip is absent from current pinned trunk"
    original_proof = _landing_proof(gitdir, reviewed, pinned)
    if original_proof == "unknown":
        return None, "Git could not prove the original reviewed tip absent from trunk"
    if original_proof != "absent":
        return None, "the original reviewed change is on trunk and is not subsumed"
    fields = {"dry_run": True, "id": original["id"], "reason": "subsumed",
              "confirmation_id": confirmation["id"],
              "confirmation_tip": confirmation_tip,
              "confirmation_ref": confirmation_ref,
              "original_author": original_author,
              "confirmation_recipient": confirmation_recipient,
              "original_author_family": original_family,
              "confirmation_recipient_family": confirmation_family,
              "original_verdict_anchor": original_verdict_anchor,
              "confirmation_verdict_anchor": confirmation_verdict_anchor,
              "original_family_evidence": original_family_evidence,
              "confirmation_family_evidence": confirmation_family_evidence,
              "original_family_anchor": original_family_anchor,
              "confirmation_family_anchor": confirmation_family_anchor,
              "confirmation_tier_state": tier,
              "confirmation_gate_requirement": requirement,
              "confirmation_gate": confirmation_gate,
              "confirmation_approval_anchor": confirmation_approval_anchor,
              "closing_repo_id": gitdir, "closing_trunk_ref": trunk_ref,
              "closing_trunk_sha": pinned,
              "proof_mode": confirmation_proof,
              "original_proof_mode": "absent"}
    if dry_run:
        return fields, None
    row, err = dispatches._record_close_proven(
        original["id"], "subsumed", reviewed, evidence=evidence,
        confirmation_id=confirmation["id"], confirmation_tip=confirmation_tip,
        confirmation_ref=confirmation_ref, original_author=original_author,
        confirmation_recipient=confirmation_recipient,
        original_author_family=original_family,
        confirmation_recipient_family=confirmation_family,
        original_verdict_anchor=original_verdict_anchor,
        confirmation_verdict_anchor=confirmation_verdict_anchor,
        original_family_evidence=original_family_evidence,
        confirmation_family_evidence=confirmation_family_evidence,
        original_family_anchor=original_family_anchor,
        confirmation_family_anchor=confirmation_family_anchor,
        confirmation_tier_state=tier,
        confirmation_gate_requirement=requirement,
        confirmation_gate=confirmation_gate,
        confirmation_approval_anchor=confirmation_approval_anchor,
        closing_repo_id=gitdir, closing_trunk_ref=trunk_ref,
        closing_trunk_sha=pinned, proof_mode=confirmation_proof,
        original_proof_mode="absent")
    if err:
        return None, err
    return get(row["id"])


def _close_ladder_withdrawn(lr, evidence, dry_run, lrs=None):
    if not evidence:
        return None, ("withdraw needs evidence — the author's attestation "
                      "that the verdict's resolution was carried out, since "
                      "no git proof can carry that half")
    if lr.get("close_reason") == "withdrawn" \
            and lr.get("close_evidence") == evidence:
        return lr, None
    if lr.get("withdrawn"):
        if lr.get("withdraw_ref") == evidence:
            return lr, None
        return None, "dispatch %s already has a different withdraw" % lr["id"]
    if _retired_by(lr):
        return None, _retired_refusal(lr)
    # THE POLARITY GATE READS THE CHAIN when the row itself is silent
    # (`_chain_polarity` — boundary and refusal shapes documented there). A
    # row a person declined in the verdict PROSE, with an UNDECLARED polarity
    # field, could reach no terminal at all until a chained round declared
    # the machine-readable half — be5e82bbe0b5 sat REVIEWED 8d08h that way.
    polarity, via, perr = _chain_polarity(lr, lrs)
    if perr:
        return None, perr
    if polarity not in ("fix", "supersede"):
        if lr.get("polarity"):
            return None, "%s is not a FIX/SUPERSEDE verdict row" % lr["id"]
        return None, ("%s is not a FIX/SUPERSEDE verdict row, and no chained "
                      "round declares one about its reviewed code — withdraw "
                      "retires a declared do-not-land debt only" % lr["id"])
    # NOTE: no author-binding requirement, and that is deliberate — unlike
    # the superseded ladder, withdrawn never cross-references a LATER
    # verdict's sender (there is no later verdict). The evidence is the
    # attestation regardless of who supplies it (the author, or the
    # integrator carrying out the verdict). The non-negotiable invariants are
    # below: a FIX/SUPERSEDE verdict with a reviewed tip, and Git proving
    # that tip absent from trunk. Requiring an author here would refuse
    # exactly the rows this reason is FOR — older verdicts recorded before
    # the author binding was stable (the acceptance case is one: evals-0723
    # has sender null).
    reviewed = lr.get("reviewed_tip")
    if not reviewed:
        return None, "%s has no reviewed tip to prove absent" % lr["id"]
    gitdir = lr.get("repo_id")
    trunk_ref, pinned, _target, err = _trio_trunk(gitdir)
    if err:
        return None, err
    # THE GATE: the reviewed change must be provably ABSENT from the pinned
    # trunk. True is a lie (the change landed — withdraw is the wrong verb);
    # None is UNKNOWN (fail closed). Only a proven False retires the debt.
    #
    # AND THE PROOF FOLLOWS REWRITE TRANSLATIONS (#79), in BOTH directions,
    # exactly as `--reason landed` does: a tip pruned by a recorded trunk
    # rewrite reads UNKNOWN directly while its recorded live identity is
    # provably absent — refusing there billed the row for a rewrite it did
    # not choose; and a tip that reads absent directly while its RECORDED
    # TRANSLATION is on trunk makes "absent" a lie, so a translation that
    # exists is always adjudicated, never only consulted on failure.
    landed = _landed(gitdir, reviewed, pinned)
    proof, translated = "absent", None
    if landed is not True:
        table, terr = _ref_translations_checked()
        if terr:
            return None, ("the ref-translation sidecar is unreadable (%s) — "
                          "absence cannot be adjudicated" % terr)
        new = table.get(reviewed)
        if new:
            if not _sha(new):
                full = _git(gitdir, "rev-parse", "--verify", "--quiet",
                            new + "^{commit}")
                if full is not None and full.returncode == 0 \
                        and full.stdout.strip():
                    new = full.stdout.strip().lower()
            tlanded = _landed(gitdir, new, pinned)
            if tlanded is None:
                return None, ("Git could not prove the reviewed change is "
                              "absent from trunk through its recorded "
                              "translation %s — withdraw is FAIL-CLOSED on an "
                              "unknown land state" % new[:12])
            if tlanded is True:
                return None, ("the reviewed change IS on trunk through its "
                              "recorded translation %s — a withdraw over "
                              "landed work is false. That row wants --reason "
                              "superseded (if superseded) or is a contrary, "
                              "not a withdraw" % new[:12])
            if landed is None:
                # the translation CARRIES the proof; recorded as such
                proof, translated = "translated-absent", new
            # else: absence was proven directly and the translation leg was a
            # GUARD that also proved absent — the direct proof is recorded
            landed = False
    if landed is None:
        return None, ("Git could not prove the reviewed change is absent from "
                      "trunk — withdraw is FAIL-CLOSED on an unknown land state")
    if landed is True:
        return None, ("the reviewed change IS on trunk — a withdraw over "
                      "landed work is false. That row wants --reason "
                      "superseded (if superseded) or is a contrary, not a "
                      "withdraw")
    if dry_run:
        return {"dry_run": True, "id": lr["id"], "reason": "withdrawn",
                "proof_mode": proof, "translated_tip": translated,
                "polarity": polarity, "polarity_via_chain": via,
                "absence_trunk_ref": trunk_ref,
                "absence_trunk_sha": pinned}, None
    row, err = dispatches._record_close_proven(
        lr["id"], "withdrawn", reviewed, evidence=evidence,
        proof_mode=proof, translated_tip=translated,
        absence_trunk_ref=trunk_ref, absence_trunk_sha=pinned)
    if err:
        return None, err
    return get(row["id"])


def _close_ladder_stranded(lr, evidence, repo, trunk, dry_run):
    """Object-pruned + ALL liveness rungs — the D1 ladder, every rung
    mandatory, every UNKNOWN a refusal. Only a row failing every liveness
    probe — tip pruned, no translation, no family ref, no family worktree,
    in a repo that PROVED it can read — closes stranded."""
    if not evidence:
        return None, ("close --reason stranded needs evidence — what "
                      "destroyed the substrate (e.g. the trunk rewrite that "
                      "pruned the tip)")
    reviewed = lr.get("reviewed_tip")
    if lr.get("close_reason") == "stranded":
        gitdir, err = _close_repo(lr, repo)
        if err:
            return None, err
        if lr.get("closing_repo_id") == gitdir \
                and lr.get("close_evidence") == evidence:
            return lr, None
        return None, _retired_refusal(lr)
    if _retired_by(lr):
        return None, _retired_refusal(lr)
    if not lr.get("verdict_ref") or not reviewed:
        return None, ("%s is an open row — its obligation is the review; "
                      "dispatch cancel / close --reason out-of-scope"
                      % lr["id"])
    gitdir, err = _close_repo(lr, repo)
    if err:
        return None, err
    trunk_ref, pinned, _target, err = _close_trunk(lr, gitdir, trunk)
    if err:
        return None, err
    # rung 1 — POSITIVE CONTROL: a repo that cannot read a known-good object
    # is not a repo whose "missing" means anything (THE mass-termination
    # guard). The trunk sha it just resolved is the known-good object.
    if _object_exists(gitdir, pinned) is not True:
        return None, ("the repository at %s cannot prove its own trunk "
                      "object — refusing to call anything pruned in a repo "
                      "that cannot read a known-good one" % gitdir)
    # rung 2 — OBJECT-MISSING, bare full-sha form pinned (the ^{commit} peel
    # exits 128 on missing, indistinguishable from corrupt).
    present = _object_exists(gitdir, reviewed)
    if present is True:
        return None, ("the reviewed tip %s is still an object in %s — "
                      "stranded is for destroyed substrate; this row is "
                      "adjudicable" % (reviewed[:12], gitdir))
    if present is None:
        return None, ("Git could not determine whether %s still exists in %s "
                      "— stranded is FAIL-CLOSED on an unknown object state"
                      % (reviewed[:12], gitdir))
    # rung 3 — TRANSLATION: a recorded rewrite is a live identity, not a
    # destroyed one.
    table, terr = _ref_translations_checked()
    if terr:
        return None, ("the ref-translation sidecar is unreadable (%s) — "
                      "absence cannot be adjudicated" % terr)
    new = table.get(reviewed)
    if new:
        alive = _object_exists(gitdir, new)
        if alive is True:
            return None, ("%s translates to live object %s (recorded rewrite) "
                          "— adjudicate through the translated identity: "
                          "--reason landed if it reached trunk, or leave the "
                          "row on the frontier" % (reviewed[:12], new[:12]))
        if alive is None:
            return None, ("Git could not determine whether translated object "
                          "%s exists — stranded is FAIL-CLOSED on an unknown "
                          "object state" % new[:12])
    # rung 4 — LANE-FAMILY REFS (interlock law 2: probe the stem, generous).
    matches, ferr = _lane_family_refs(gitdir, lr.get("lane"), lr.get("branch"))
    if ferr:
        return None, ("%s — stranded is FAIL-CLOSED on an unreadable ref "
                      "table" % ferr)
    if matches:
        ref, obj = matches[0]
        return None, ("a live lane-family ref exists (%s @ %s) — the work "
                      "this row claims is not destroyed; stranded refused"
                      % (ref, obj[:12]))
    # rung 5 — WORKTREES (place three of the four places work lives).
    # Existence of a family worktree is already disqualifying — dirtiness
    # need not be probed, which keeps this rung one read and makes
    # under-matching impossible.
    wts, werr = _lane_family_worktrees(gitdir, lr.get("lane"),
                                       lr.get("branch"))
    if werr:
        return None, ("the repository's worktree registry could not be read "
                      "(%s) — stranded is FAIL-CLOSED on an unknown worktree "
                      "state" % werr)
    if wts:
        return None, ("a lane-family worktree exists at %s — uncommitted or "
                      "unpushed work may live there; stranded refused"
                      % wts[0]["path"])
    if dry_run:
        return {"dry_run": True, "id": lr["id"], "reason": "stranded",
                "closing_repo_id": gitdir, "control_sha": pinned,
                "proof_mode": "object-pruned",
                "blob_containment": ("not computable — a pruned tip has no "
                                     "readable blobs to take; advisory only, "
                                     "never gates, never authors (scoped out "
                                     "of this slice by design)")}, None
    row, err = dispatches._record_close_proven(
        lr["id"], "stranded", reviewed, evidence=evidence,
        closing_repo_id=gitdir, control_sha=pinned,
        proof_mode="object-pruned")
    if err:
        return None, err
    return get(row["id"])


def _close_ladder_resolved(lr, evidence, repo, trunk, dry_run):
    """THE POLARITY-WRONG DOOR — the sibling of #177's polarity-LESS one.

    Admits exactly one shape: a FIX/SUPERSEDE verdict row whose OWN reviewed
    tip reached trunk, confirmed by a later cross-family verdict that states
    the resolution at the head of its evidence. Five rows sat doorless on
    2026-08-04 (74144aca, 87cf5f81, 28dd2907, d94af579, ef0abe53) because
    every existing reason keys on a POLARITY or TIMING artifact and none keys
    on the resolution itself.

    WHY NOT withdrawn, which would open today: withdrawn proves ABSENCE. This
    population's work is demonstrably PRESENT on trunk, so withdrawn is not a
    shortcut here, it is a false statement recorded in a binding ledger. The
    discriminator is land_state and it is the reason this door is narrow.

    WHY THE CONFIRMATION MAY BE SUPERSEDE (@opus-integrator, amendment A):
    subsumption spends POLARITY as its check and can demand an APPROVE. This
    door cannot — the TEMPORAL deadlock it exists to end is a reviewer who
    content-verified the work and could bind only SUPERSEDE because every
    receipt in existence PREDATED the dispatch (ds4pro on ef0abe53, measured:
    the token predated by 22 minutes). Requiring approve re-creates the
    deadlock. FIX stays excluded because its semantics IS not-resolved, so
    admitting it would be incoherent whatever the prose says.

    Having spent polarity, the head-anchored phrase IS the check — see
    dispatches.resolution_statement for why the ANCHOR is load-bearing.

    RESIDUAL, NAMED RATHER THAN LAUNDERED (amendment B): this door lets a
    later cross-family confirmation stand over an earlier reviewer's FIX
    without that reviewer withdrawing — REVIEWER-SHOPPING. Full independence
    is the same trust any review rests on and no ladder can manufacture it.
    What is mechanizable is done: the overridden reviewer is RECORDED on the
    close event, so the contest path exists and is auditable."""
    if not evidence:
        return None, ("close --reason resolved needs --evidence "
                      "<confirmation-row-id-or-prefix>")
    if lr.get("close_reason") == "resolved":
        if lr.get("close_evidence") == evidence:
            return lr, None
        return None, _retired_refusal(lr)
    if _retired_by(lr):
        return None, _retired_refusal(lr)
    # `lr` is the PROJECTION row, which carries `state`/`verdict_ref` — never
    # the dispatch store's `status`. Gating on `status` here silently refused
    # every real row at the first rung (caught by the integration walk, not by
    # reading). The polarity half is already enforced upstream by
    # _CLOSE_POLARITY["resolved"], so what is left to prove is that a verdict
    # was actually recorded.
    if not lr.get("verdict_ref"):
        return None, ("%s carries no verdict — resolved retires REVIEWED "
                      "debt, and an unverdicted row has discharged's door"
                      % lr["id"])
    reviewed = lr.get("reviewed_tip")
    if not reviewed:
        return None, "%s has no reviewed tip to prove on trunk" % lr["id"]
    gitdir, err = _close_repo(lr, repo)
    if err:
        return None, err
    trunk_ref, pinned, _target, terr = _trio_trunk(gitdir)
    if terr:
        return None, terr
    # rung 0 — POSITIVE CONTROL. A repo that cannot read its own trunk object
    # proves nothing about what reached it (stranded's guard, reused).
    if _object_exists(gitdir, pinned) is not True:
        return None, ("the repository at %s cannot prove its own trunk object "
                      "— refusing to adjudicate a resolution over an "
                      "unreadable substrate" % gitdir)
    if trunk:
        named_ref, _named_sha, cerr = _canonical_trunk(gitdir, trunk)
        if cerr:
            return None, cerr
        if named_ref not in _trunk_aliases(trunk_ref):
            return None, ("%s is not this row's landing trunk (%s)"
                          % (trunk, trunk_ref))
    # rung 1 — THE RESOLUTION IS MEASURED. Ancestry only: containment by a
    # later commit is exactly what the 87cf5f81 refusal rejects ("a later
    # approve of unrelated work is not a resolution of this one, however
    # plausibly its commit contains the change").
    # NOT `_landed`, and the difference is the whole ruling: _landed admits
    # "patch-equivalent" as well as "ancestor", so it would have let a row in
    # whose delta merely REAPPEARED on trunk under some other commit.
    # @opus-integrator predicted this exact hole in the refutation pass and it
    # then reproduced live — b71f8dab (whose reviewed tip is NOT an ancestor of
    # trunk; its 3-line delta landed under a different commit) sailed past this
    # rung
    # to the phrase check while the comment above it claimed ancestry-only.
    # Ruling (i): the door stays NARROW and that row goes through #177 instead.
    # A patch-id match cannot distinguish "this work landed" from "someone
    # else wrote the same three lines", and a binding ledger may not guess.
    proof = _landing_proof(gitdir, reviewed, pinned)
    if proof not in ("ancestor", "patch-equivalent", "absent"):
        return None, ("Git could not determine whether %s reached trunk — "
                      "refusing to resolve over an unknown land state"
                      % reviewed[:12])
    if proof == "absent":
        return None, ("%s is NOT on trunk — this row's work is absent, which "
                      "is withdrawn's question, not resolved's"
                      % reviewed[:12])
    if proof != "ancestor":
        return None, ("%s reaches trunk only by PATCH IDENTITY, not ancestry "
                      "— that proves an identical delta exists, never that "
                      "THIS reviewed work landed; re-review the successor "
                      "round and close it through subsumed instead"
                      % reviewed[:12])
    # rung 2 — AN INDEPENDENT CONFIRMATION, re-derived from the ledger here
    # and again at replay; never taken from the event.
    current, verdicts, unavailable = dispatches.snapshot_with_verdicts()
    if unavailable:
        return None, "dispatch ledger unavailable: %s" % unavailable
    confirmation, err = dispatches._resolve_row(
        current, evidence, noun="confirmation dispatch",
        list_hint="helm dispatch list --all")
    if err:
        return None, err
    if confirmation["id"] == lr["id"]:
        return None, "a row can never confirm its own resolution"
    original_index = dispatches.verdict_index(verdicts, lr["id"])
    confirmation_index = dispatches.verdict_index(verdicts, confirmation["id"])
    if original_index is None or confirmation_index is None \
            or confirmation_index <= original_index:
        return None, "confirmation must be one later durable verdict"
    if confirmation.get("status") != "verdict" \
            or confirmation.get("kind") != "review":
        return None, "confirmation is not a review verdict"
    if confirmation.get("repo_id") != lr.get("repo_id"):
        return None, "confirmation verdict is not on the same repository"
    # rung 3 — POLARITY SET (amendment A). approve or supersede; never fix.
    # The set is the SHARED constant so this door and the display
    # classifier's confirmation_row cannot drift apart (codex-2's finding:
    # the classifier's first cut paraphrased this rung and dropped it).
    if confirmation.get("polarity") not in CONFIRMATION_POLARITIES:
        return None, ("confirmation polarity is %s — resolved admits approve "
                      "or supersede, and never fix, whose meaning is that the "
                      "work is NOT resolved"
                      % (confirmation.get("polarity") or "undeclared"))
    # rung 4 — THE CONTENT RULE. Head-anchored, because polarity was spent.
    statement = dispatches.resolution_statement(confirmation.get("verdict_ref"))
    if not statement:
        return None, ("confirmation evidence must OPEN with `Resolution "
                      "verified on trunk: <concrete resolution>` — the phrase "
                      "is the check here, so it is read at the head and "
                      "nowhere else")
    # rung 5 — CROSS-FAMILY. The confirming reviewer's family must differ from
    # the original author's; UNKNOWN on either side refuses.
    original_family, _oev, _oanchor, err = _one_approval_family(
        lr.get("author") or "", "original author")
    if err:
        return None, err
    confirmation_family, _cev, _canchor, err = _one_approval_family(
        confirmation.get("recipient") or "", "confirmation recipient")
    if err:
        return None, err
    if original_family == confirmation_family:
        return None, ("confirmation recipient is the author's own family (%s) "
                      "— resolved needs cross-family eyes" % original_family)
    if dry_run:
        return {"dry_run": True, "id": lr["id"], "reason": "resolved",
                "confirmation_id": confirmation["id"],
                "overridden_reviewer": lr.get("reviewer"),
                "closing_trunk_ref": trunk_ref,
                "closing_trunk_sha": pinned,
                "resolution": statement}, None
    # amendment D — the PIN travels on the event, so replay validates ancestry
    # against the sha this close was decided on and a later trunk rewrite can
    # never silently invalidate it.
    row, err = dispatches._record_close_proven(
        lr["id"], "resolved", reviewed, evidence=evidence,
        closing_repo_id=gitdir, closing_trunk_ref=trunk_ref,
        closing_trunk_sha=pinned,
        confirmation_id=confirmation["id"],
        confirmation_tip=confirmation.get("reviewed_tip"),
        confirmation_ref=confirmation.get("verdict_ref"),
        original_author=lr.get("author"),
        proof_mode="resolved-on-pinned-trunk")
    if err:
        return None, err
    return get(row["id"])


def _close_ladder_discharged(lr, evidence, dry_run, lrs):
    """The #177 door: a never-verdicted BUILD row whose work is on trunk under
    a SUCCESSOR's landed, gate-verified APPROVE.

    The ladder owns no proof of its own — the derivation lives in
    dispatches.discharging_row (ONE walk, reused by the writer's lock-side
    re-check and the replay arm). The two landed legs are NOT the same
    strength and the docstring must not claim they are: the writer injects
    _writer_landed (the ledger's terminal AND a live git ancestry probe);
    replay injects _replay_landed (the ledger's terminal alone), which is
    strictly weaker — so on a contested ledger the two walks can select
    different first-match candidates, and the door fails CLOSED in that
    direction (a legitimate discharge refused, never a forged one admitted).
    What the ladder adds is the live landed leg and the idempotency shape
    every close door shares."""
    if not evidence:
        return None, ("close --reason discharged needs evidence — name the "
                      "landed round this row's work rode in on")
    if lr.get("close_reason") == "discharged":
        if lr.get("close_evidence") == evidence:
            return lr, None
        return None, _retired_refusal(lr)
    if _retired_by(lr):
        return None, _retired_refusal(lr)
    if lr.get("verdict_ref"):
        return None, ("%s is a verdict row — its own polarity owns a door; "
                      "discharged is for a row that never got one" % lr["id"])
    # The projection rows and the dispatch snapshot carry the same ids; the
    # walk needs the store's rows (chain edges live there), and the landed
    # leg probes the discharging row's own recorded landing.
    current, unavailable = dispatches.snapshot()
    if unavailable:
        return None, "dispatch ledger unavailable: %s" % unavailable
    tier, by, why = dispatches.discharging_row(
        lr["id"], current,
        is_landed=lambda tip: dispatches._writer_landed(current, tip))
    if tier is None:
        return None, "close --reason discharged refused: %s" % why
    dis_row = current.get(by) or {}
    if dry_run:
        return {"dry_run": True, "id": lr["id"], "reason": "discharged",
                "discharging_id": by,
                "discharging_tip": dis_row.get("reviewed_tip"),
                "discharge_tier": tier}, None
    row, err = dispatches._record_close_proven(
        lr["id"], "discharged", None, evidence=evidence,
        discharging_id=by,
        discharging_tip=dis_row.get("reviewed_tip"),
        discharge_tier=tier)
    if err:
        return None, err
    return get(row["id"])


def _close_out_of_scope(rid, evidence, dry_run):
    """OPEN rows only (D3) — attestation never terminates reviewed work. The
    write goes THROUGH mark_cancel, so cancel's own lock, idempotency and
    open-only re-validation are the boundary (zero unlocked window) and NO
    new event kind exists; rebind and existing replay untouched. Resolution
    reads the DISPATCH snapshot, not the land-request projection, because a
    cancelled row leaves the projection and its idempotent retry must still
    answer."""
    current, unavailable = dispatches.snapshot()
    if unavailable:
        return None, "dispatch ledger unavailable: %s" % unavailable
    row, err = dispatches._resolve_row(current, rid)
    if err:
        return None, err
    if not evidence:
        return None, ("close --reason out-of-scope needs evidence — why the "
                      "review obligation is moot")
    if row.get("status") == "verdict":
        if row.get("polarity") in ("fix", "supersede"):
            return None, ("%s carries a declared resolution debt — "
                          "attestation alone never retires it; that debt "
                          "wants --reason superseded or --reason withdrawn"
                          % row["id"])
        return None, ("%s is a verdict row — attestation never terminates "
                      "reviewed work; use the proof-bearing post-verdict close "
                      "reason that matches its recorded state" % row["id"])
    if row.get("status") == "cancelled":
        return dispatches.mark_cancel(row["id"], evidence)
    # DID IT SHIP? THIS DOOR'S OTHER GUARDS ANSWER AN ADJACENT QUESTION.
    # Everything below tests whether the obligation is MOOT and whether the
    # dispatched work is still LIVE — and a ghost BUILD row passes both
    # honestly: its review is never coming, so the obligation IS moot. Nothing
    # asked whether the work SHIPPED, so the door opened by its own terms and
    # recorded a falsehood by ours: out-of-scope on a change that is on trunk
    # says the fleet did not want work it is running (#187).
    #
    # Measured 2026-08-04: dry-running the five plausible reasons against a
    # ghost build row, four refused correctly and this one WOULD CLOSE. It is
    # the fourth guard that night found asking an adjacent question, and the
    # only one that WRITES rather than merely reporting.
    #
    # The proof is delegated, never re-derived: discharging_row already
    # computes "a successor whose land discharges this row", and a second
    # on-trunk computation here would drift from the door that must agree
    # with it. FAIL-CLOSED like the liveness block below — an unreadable
    # trunk refuses rather than assuming the work never landed.
    gitdir_ship = row.get("repo_id")
    trunk_ref, pinned, _t, terr = _trio_trunk(gitdir_ship)
    if terr:
        return None, ("%s — whether this work SHIPPED is UNKNOWN and "
                      "out-of-scope is FAIL-CLOSED on it" % terr)
    tier, by, _why = dispatches.discharging_row(
        row["id"], current,
        is_landed=lambda tip: _landed(gitdir_ship, tip, pinned) is True)
    if tier:
        # A REFUSAL THAT NAMES NO DOOR IS A DEAD END, and this one has a door:
        # the `discharged` ladder is the other half of this route. This half
        # refuses the wrong terminal; that half supplies the right one. Read
        # the LIVE reason list rather than hard-coding the name — that door
        # landed in a separate lane, and naming a reason this build does not
        # have would trade a dead end for a wrong turn.
        route = ("close it `--reason discharged`"
                 if "discharged" in CLOSE_CLI_REASONS
                 else "it wants the reason that proves the land")
        return None, ("%s is not out of scope — its work is ON TRUNK, "
                      "discharged by %s (%s). Closing it here would record "
                      "shipped work as unwanted; %s"
                      % (row["id"], str(by)[:12], tier, route))
    # LIVENESS BLOCK (D1's law applied here too): a live claim is never
    # orphaned through this door. Tri-state — unreadable refuses.
    gitdir, tiprow = row.get("repo_id"), row.get("tip")
    matches, ferr = _lane_family_refs(gitdir, row.get("lane"), row.get("ref"))
    if ferr:
        return None, ("%s — the dispatched work's liveness is UNKNOWN and "
                      "out-of-scope is FAIL-CLOSED on it" % ferr)
    for ref, obj in matches:
        live = obj == tiprow
        if not live and tiprow:
            ancestry = _ancestry(gitdir, tiprow, obj)
            if ancestry == UNDETERMINED:
                return None, ("Git could not relate the dispatched tip to "
                              "lane-family ref %s — liveness is UNKNOWN and "
                              "out-of-scope is FAIL-CLOSED on it" % ref)
            live = ancestry == ANCESTOR
        if live:
            return None, ("the dispatched work is live at %s — closing the "
                          "row orphans a live claim; rebind the obligation "
                          "(helm dispatch rebind) or finish the lane" % ref)
    wts, werr = _lane_family_worktrees(gitdir, row.get("lane"),
                                       row.get("ref"))
    if werr:
        return None, ("the repository's worktree registry could not be read "
                      "(%s) — the dispatched work's liveness is UNKNOWN and "
                      "out-of-scope is FAIL-CLOSED on it" % werr)
    for wt in wts:
        dirty = _worktree_dirty(wt.get("path"))
        if dirty is None:
            return None, ("worktree %s could not be read — the dispatched "
                          "work's liveness is UNKNOWN and out-of-scope is "
                          "FAIL-CLOSED on it" % wt.get("path"))
        if dirty:
            return None, ("the dispatched work is live at %s — closing the "
                          "row orphans a live claim; rebind the obligation "
                          "(helm dispatch rebind) or finish the lane"
                          % wt["path"])
    # LANDED ANNOTATION — never gates in either direction: a landed tip on an
    # open row is tolerated (the review became moot; the obligation was the
    # review). The check annotates, it never authors or blocks.
    landed_check = "unknown"
    cache = {}
    local_ref, up_ref = _trunk_refs(gitdir, cache)
    target = up_ref or local_ref
    if tiprow and target:
        state = _landed(gitdir, tiprow, target)
        landed_check = "true" if state is True \
            else "absent" if state is False else "unknown"
    if dry_run:
        return {"dry_run": True, "id": row["id"], "reason": "out-of-scope",
                "landed_check": landed_check}, None
    out, err = dispatches.mark_cancel(row["id"], evidence)
    if err:
        return None, err
    out = dict(out)
    out["landed_check"] = landed_check
    return out, None


def card(lr):
    """One land loop projected for a CONSOLE row — the shape `board_section`
    and `GET /api/lr` both render. PROJECTION ONLY: every field is copied from
    the row `_lr` already built; nothing here recomputes, joins or judges.

    Four of these fields exist because a panel that omits them cannot help
    reading as healthier than the record is:

    - `polarity` / `polarity_source` — REVIEWED with polarity None is "not
      stall-checked", not a quiet nearly-approved row. A declared held APPROVE
      stays distinct, and the source field names the dispatch store as owner.
    - `attest_state` / `attest_detail` / `attest_source` — the independent
      sidecar axis. A binding mismatch is durable evidence to repair, never a
      reason to rewrite polarity or retroactively re-sign a verdict.
    - `contrary_state` — WHICH physical fact contradicts the verdict (landed
      vs merged-local). Without it a console can say "contrary" and not say
      what happened; `_line` prints exactly this distinction already.
    - `review_sha_full` / `entered_ts` — the 12-char clip is for the row, but
      a reader who wants to check the claim needs the whole sha, and the
      terminal window ("closed in the last 24h") needs a real stamp rather
      than a dwell that keeps counting.
    - `closed_ts` / `closed_ts_unreadable` / `dwell_known` — the "do we
      actually know WHEN" bits. `closed_ts` is the CLOSURE instant and is
      None for a git-observed landing, which carries no stamp; a window that
      dates closure from `entered_ts` instead drops a lane that merged a
      minute ago after an old verdict. `closed_ts_unreadable` keeps the third
      state distinct: the record HAS a closure stamp and it is not a
      timestamp, which is a repair to make, not an absence to shrug at.
      `closed_ts_impossible` keeps the FOURTH: a closure stamp that parses
      perfectly and is dated in the future, which is not an instant this row
      can have closed at.
    - `ledger_refused` — the transitions the record holds that the canonical
      fold REFUSED (an out-of-sequence verdict, a delivery on a row that never
      opened). Empty on every healthy row. It travels because the alternative
      is dropping the event silently, and a timeline quietly missing a verdict
      the ledger contains is the same disagreement in the other direction.
      `dwell_known` is False when the entry stamp could not be read, i.e.
      when `dwell_s` is 0 because nothing was measured rather than because
      the row is new.
    - `gate` / `ungated` — the VERIFICATION axis, read tolerantly with
      `.get`: they do not exist until the gate lane lands, and a row without
      them must render UNVERIFIED. An absent verification field may never
      default to verified, which is why neither has a truthy fallback.
    """
    holder_role, holder_seat = ball_holder(lr)
    out = {"id": lr["id"], "state": lr["state"], "lane": lr["lane"],
            "branch": lr["branch"], "review_sha": (lr["review_sha"] or "")[:12],
            "review_sha_full": lr["review_sha"] or "",
            "base_sha": lr.get("base_sha") or "",
            "author": lr["author"], "reviewer": lr["reviewer"],
            "kind": lr.get("kind"), "polarity": lr.get("polarity"),
            "polarity_source": lr.get("polarity_source"),
            "attest_source": lr.get("attest_source"),
            "attest_state": lr.get("attest_state"),
            "attest_detail": lr.get("attest_detail"),
            "dwell_s": lr["dwell_s"], "entered_ts": lr.get("entered_ts"),
            "dwell_known": lr.get("dwell_known", False),
            "closed_ts": lr.get("closed_ts"),
            "closed_ts_unreadable": lr.get("closed_ts_unreadable", False),
            "closed_ts_impossible": lr.get("closed_ts_impossible", False),
            "ledger_refused": lr.get("ledger_refused") or [],
            "stalled": lr["stalled"],
            "observable": lr["observable"],
            "land_state": lr.get("land_state"),
            "landed": lr["landed"], "merged_local": lr["merged_local"],
            "has_upstream": lr.get("has_upstream", False),
            "contrary": lr.get("contrary", False),
            "contrary_state": lr.get("contrary_state"),
            # DISPLAY TRUTH RIDES THE WIRE TOO. `_annotate_contrary_discharge`
            # stamps whether an on-trunk APPROVE in this row's chain HONORED
            # the verdict through succession ("a"/"b") or the row IS the
            # resolved door's confirmation round ("c" — the discharge
            # instrument, rendered as a quiet "confirmation"), and `lr list`
            # renders it — but this card is the exact shape `/api/lr` serves,
            # and omitting the stamp made the owner's console count five
            # honored rows as live contrary debt (measured 2026-08-04: 11
            # shown, 6 real). Copied, never recomputed; None means "not
            # stamped" and every renderer stays LOUD on it — fail-closed,
            # like `gate`.
            "contrary_discharge": lr.get("contrary_discharge"),
            # THE CLASSIFICATION ITSELF, not just its inputs. The browser used
            # to re-derive this from the two fields above through a JS twin of
            # honored_display, and parity tests pinned the twin equal row for
            # row. Measured 2026-08-05 they still agreed — 276/276 over the
            # whole population — so this is DEDUPLICATION, not a bug fix, and
            # it changes no rendered row. Two implementations of one predicate
            # that agree today are a drift waiting to happen, and only one of
            # them feeds the owner's screen. The server already knows; send it.
            "honored": honored_display(lr),
            "discharged": lr.get("discharged", False),
            "superseding_tip": lr.get("superseding_tip"),
            "withdrawn": lr.get("withdrawn", False),
            "withdraw_contradicted": lr.get("withdraw_contradicted", False),
            "abandoned": lr.get("abandoned", False),
            "abandon_reason": lr.get("abandon_reason"),
            "abandon_ts": lr.get("abandon_ts"),
            "abandon_object_state": lr.get("abandon_object_state"),
            "abandon_proof_mode": lr.get("abandon_proof_mode"),
            "abandon_proof_version": lr.get("abandon_proof_version"),
            "abandon_trunk_mention_state":
            lr.get("abandon_trunk_mention_state"),
            "abandon_trunk_mention_proof_mode":
            lr.get("abandon_trunk_mention_proof_mode"),
            "abandon_trunk_mention_proof_version":
            lr.get("abandon_trunk_mention_proof_version"),
            "abandon_branch_state": lr.get("abandon_branch_state"),
            "abandon_branch_proof_mode": lr.get("abandon_branch_proof_mode"),
            "abandon_branch_proof_version": lr.get("abandon_branch_proof_version"),
            "abandon_worktree_state": lr.get("abandon_worktree_state"),
            "abandon_worktree_proof_mode": lr.get("abandon_worktree_proof_mode"),
            "abandon_worktree_proof_version":
            lr.get("abandon_worktree_proof_version"),
            "abandon_land_state": lr.get("abandon_land_state"),
            "close_reason": lr.get("close_reason"),
            # the VALIDATED closure instant only — the raw close stamp goes
            # through the same absent/unreadable/impossible typing as every
            # other closure stamp, and an unvalidated instant never reaches
            # the wire (the 2099 lesson, once per stamp position)
            "close_ts": lr.get("closed_ts") if lr.get("close_reason")
            else None,
            "close_contradicted": lr.get("close_contradicted", False),
            "close_proof_mode": lr.get("close_proof_mode"),
            # the LIVE step rides the wire too: a console that renders landed
            # rows must be able to say which of them the fleet actually has
            "close_delivery_class": lr.get("close_delivery_class"),
            "close_delivery_restart": lr.get("close_delivery_restart"),
            "close_evidence": lr.get("close_evidence"),
            "confirmation_id": lr.get("confirmation_id"),
            "confirmation_tip": lr.get("confirmation_tip"),
            "confirmation_ref": lr.get("confirmation_ref"),
            "original_author": lr.get("original_author"),
            "confirmation_recipient": lr.get("confirmation_recipient"),
            "original_author_family": lr.get("original_author_family"),
            "confirmation_recipient_family":
            lr.get("confirmation_recipient_family"),
            "original_verdict_anchor": lr.get("original_verdict_anchor"),
            "confirmation_verdict_anchor":
            lr.get("confirmation_verdict_anchor"),
            "original_family_evidence": lr.get("original_family_evidence"),
            "confirmation_family_evidence":
            lr.get("confirmation_family_evidence"),
            "original_family_anchor": lr.get("original_family_anchor"),
            "confirmation_family_anchor":
            lr.get("confirmation_family_anchor"),
            "confirmation_tier_state": lr.get("confirmation_tier_state"),
            "confirmation_gate_requirement":
            lr.get("confirmation_gate_requirement"),
            "confirmation_gate": lr.get("confirmation_gate"),
            "confirmation_approval_anchor":
            lr.get("confirmation_approval_anchor"),
            "original_proof_mode": lr.get("original_proof_mode"),
            "closing_repo_id": lr.get("closing_repo_id"),
            "closing_trunk_ref": lr.get("closing_trunk_ref"),
            "closing_trunk_sha": lr.get("closing_trunk_sha"),
            "landing_review_id": lr.get("landing_review_id"),
            "landing_review_tip": lr.get("landing_review_tip"),
            "landing_review_verdict_anchor":
            lr.get("landing_review_verdict_anchor"),
            "landing_review_tier_state": lr.get("landing_review_tier_state"),
            "landing_review_gate_requirement":
            lr.get("landing_review_gate_requirement"),
            "landing_review_gate": lr.get("landing_review_gate"),
            "landing_review_approval_anchor":
            lr.get("landing_review_approval_anchor"),
            "closed_by_landing": lr.get("closed_by_landing", False),
            "landing_trunk_sha": lr.get("landing_trunk_sha"),
            # TWO ANSWERS, because they answer two different questions:
            # owed_by is enforcement/audit truth and stays untouched; the
            # holder pair is the server-resolved display answer after
            # succession annotation. HOME formats this pair, never discharge.
            "owed_by": lr["owed_by"],
            "holder_role": holder_role,
            "holder_seat": holder_seat,
            # COPIED, NEVER INVENTED. This defaulted to R_NONE, which is the
            # third place on this one card where a receipt state helm does not
            # have was rendered as a receipt helm looked for and did not find —
            # a projection is not entitled to answer a question its input never
            # answered. A row that reaches here without the field sends None,
            # and the renderers say the state was not sent.
            "receipt_state": lr.get("receipt_state"),
            "timeline": lr.get("timeline") or [],
            "gate": lr.get("gate") or "",
            "ungated": lr.get("ungated")}
    if lr.get("close_reason") == "delivered-report":
        out.update(artifact_ref=lr.get("artifact_ref"),
                   report_ref=lr.get("report_ref"),
                   delivered_report_correction=lr.get(
                       "delivered_report_correction", False),
                   cancel_reason=lr.get("cancel_reason"))
    return out


def board_section(now=None):
    """The data a console renders as a 'land loops + where they're stalling'
    panel. console-design owns the actual anchor; this exposes the rows in a
    stable, brief-shaped section dict (title + loops + the stalled subset)."""
    lrs, raw, unavailable = project_raw(now)
    if unavailable:
        return {"title": "LAND LOOPS", "unavailable": unavailable,
                "loops": [], "stalled": []}
    try:
        loops_, stalled_ = _loop_rows(lrs, raw), _stalled_rows(lrs, raw)
    except _ChainUntrustworthy as e:
        # the OWNER CONSOLE is exactly where a silently-partial fold does the
        # most damage: it reads as a clean board.
        return {"title": "LAND LOOPS", "unavailable": str(e),
                "loops": [], "stalled": []}
    return {"title": "LAND LOOPS", "unavailable": None,
            "loops": [card(lr) for lr in loops_],
            "stalled": [card(lr) for lr in (stalled_ or [])]}


# ------------------------------------------------------------------------ CLI

USAGE = ("usage: helm lr list [--all] [--json] | show <id> [--json] | "
         "stalls [--json] | "
         "foldcheck <tip> [--gate gate:TOKEN] [--repo PATH] [--no-fetch] | "
         "legacy-completion-hints [--json] | "
         "land <id> [--json] | compose <id> [<id>...] [--trunk REF] "
         "[--repo PATH] [--dry-run] [--json] | close <id> --reason "
         "landed|superseded|withdrawn|out-of-scope|stranded|subsumed|"
         "delivered-report|discharged|resolved "
         "[--evidence LINE] [--artifact-ref REF] "
         "[--report-ref CHAT_REF] [--tip FULL_SHA] [--repo PATH] [--trunk REF] "
         "[--live | --needs-restart WHAT] [--dry-run] [--json] | "
         "annotate-delivered-report <id> --artifact-ref REF "
         "--report-ref CHAT_REF --evidence LINE [--json] | discharge <id> "
         "<full-superseding-tip> <evidence...> [--json] | withdraw <id> "
         "<evidence...> [--json] | abandon <id> --reason TEXT "
         "[--repo PATH] [--json] | close-landed <id> --trunk REF "
         "[--repo PATH] [--json] | refs [--repo PATH] [--json] | "
         "migrate --commit-map PATH [--repo PATH] [--apply] [--json]")


def _fmt_dwell(s):
    s = int(s or 0)
    if s < 3600:
        return "%dm" % (s // 60)
    if s < 86400:
        return "%dh%02dm" % (s // 3600, (s % 3600) // 60)
    return "%dd%02dh" % (s // 86400, (s % 86400) // 3600)


def _delivery_text(klass, restart):
    """THREE answers and no fourth, because the fourth would be a guess:
      * LIVE AT LAND — declared CLI-class. Fresh process off main; nothing to
        re-arm. This is what makes the live count non-zero, and it is printed
        rather than left blank so a reader can tell a DECLARED live row from a
        row nobody ever asked.
      * LIVE OPEN LOOP — declared process-class, naming what still holds
        pre-land code. Nothing here ever marks it discharged: this ledger
        records that the debt was taken on, and `helm rearm` is the verb that
        measures whether live processes still carry it. So it prints for good,
        and printing it IS the point.
      * DELIVERY UNDECLARED — written before the declaration existed. Not
        live, not stale, UNKNOWN and named as such."""
    if klass == DELIVERY_CLI:
        return ("LIVE AT LAND — CLI-class: every helm invocation is a fresh "
                "process off main")
    if klass == DELIVERY_PROCESS:
        return ("LIVE OPEN LOOP — process-class: %s still holds pre-land code "
                "until it re-arms (helm rearm)"
                % (restart or "an unnamed process"))
    return ("DELIVERY UNDECLARED — closed before the live step was recorded; "
            "whether the running fleet has this is UNKNOWN")


def _delivery_phrase(lr):
    """How this landed row's LIVE step reads, or None when the row is not a
    landed close at all."""
    if lr.get("close_reason") != "landed" and not lr.get("closed_by_landing"):
        return None
    return _delivery_text(lr.get("close_delivery_class"),
                          lr.get("close_delivery_restart"))


def _close_label(lr):
    """The display label of one close terminal: the reason, upper-cased, and
    LANDED_REWRITTEN when the landing proof went through the recorded
    translation — the sha changed, the change landed."""
    if lr.get("close_reason") == "landed" and str(
            lr.get("close_proof_mode") or "").startswith("translated-"):
        return "LANDED_REWRITTEN"
    return str(lr.get("close_reason") or "").upper().replace("-", "_")


def _attest_text(lr):
    return "%s from %s — %s" % (
        str(lr.get("attest_state") or "unknown").upper(),
        dispatches._source_label(lr.get("attest_source")),
        lr.get("attest_detail") or "detail unavailable")


def _retired_label(lr):
    """The terminal a row reached through the per-verb annotations
    (`helm lr withdraw` / `discharge`, whose ledger events set `withdrawn` /
    `discharged` and NO `close_reason`), or None.

    THE LYING HEADLINE, measured on the live ledger 2026-08-03: 61 of 718 rows
    were `terminal`, `owed_by: nobody`, carrying a real retirement stamp and a
    reason line — and `helm lr show` headlined them a bare CHANGES_REQUESTED,
    because every retirement headline keyed on `close_reason`, which these rows
    do not carry. Specimen 251d3c1a89bb (lane triage-committer-signal,
    withdrawn 2026-07-31T21:07:42Z with a ref naming the round that superseded
    it) read as an author's open debt at the top of its own page, forever. An
    operator who cannot read our history has no way to tell that row from a
    live one, and a tool that lies to its operator is exactly what makes a
    substrate unusable by someone outside the room.

    RETIRED, never CLOSED: these rows hold no close event, so borrowing the
    `CLOSED (...)` spelling would assert a ledger transition that is not there.
    And never LANDED: a withdraw asserts the reviewed change is correctly
    ABSENT from trunk, the opposite fact. Collapsing either distinction would
    trade one lie for another, so the label names the retirement and the body's
    `withdrawn` / `discharge` line carries the reason verbatim.

    A CONTRADICTED withdraw is NOT retired — a later land re-exposed it as live
    debt owed by the integrator — so it returns None and falls through to its
    verdict state, which is the honest headline for a row that is owed again."""
    if lr.get("withdrawn") and not lr.get("withdraw_contradicted"):
        return "WITHDRAWN"
    if lr.get("discharged"):
        return "DISCHARGED"
    return None


def honored_display(lr):
    """THE one display predicate for a contrary whose verdict was HONORED
    through succession (`contrary_discharge` "a"/"b") or which IS the
    succession instrument itself ("c", a confirmation round — rendered as a
    quiet "confirmation", never an alarm) — every python surface that quiets
    an alarm for these rows asks THIS, and web_ui.html's `lrHonored` is its
    JS twin (the parity tests pin them equal row for row).

    It exists because the first cut answered the question inline per surface
    and the surfaces immediately disagreed (codex-2, dispatch 97d8899a): an
    honored row with `stalled` True rendered honored-quiet on the home band
    and STALLED-loud on list/show/ledger-card/badge — the exact
    two-surfaces defect the lane was fixing, one field over. Honored wins
    over the stalled ALARM everywhere, because the successor already landed:
    a "stall" nobody can unstall is not a debt reminder, it is noise. The
    "c" stamp rides the same predicate for the same reason: a confirmation
    round's landed tip is its healthy shape by design, so there is nothing
    to unstall there either.

    DISPLAY ONLY: `stalled`, `owed_by` and every enforcement reader
    (`_stalled_rows`, stall billing, the wire fields) are untouched —
    "unverified" and an absent stamp both answer False, fail-closed."""
    return bool(lr.get("contrary")) \
        and lr.get("contrary_discharge") in ("a", "b", "c")


def ball_holder(lr):
    """The authoritative DISPLAY holder as ``(role, seat)``.

    `owed_by` remains the enforcement/audit role. A contrary row that has been
    honored through succession has no action left for a human to take, while a
    raw contrary remains integrator debt. For ordinary rows the role comes
    from `owed_by` and the seat comes from the same role-to-field vocabulary as
    the CLI. Missing authority stays UNKNOWN rather than becoming nobody.

    This is deliberately server-side: `card()` carries the resolved pair to
    `/api/lr`, `_owed_by_whom()` prints the same pair, and the browser only
    formats it. Confirmation discharge "c" therefore cannot make HOME and the
    CLI answer different questions by accident again."""
    if honored_display(lr):
        return "nobody", None
    if lr.get("contrary"):
        return "integrator", None
    role = lr.get("owed_by") or "unknown"
    return role, lr.get(_OWED_SEAT_FIELD.get(role, ""))


def _line(lr):
    marks = []
    if lr.get("abandoned"):
        marks.append("ABANDONED — LAND STATE UNKNOWN: %s" %
                     (lr.get("abandon_reason") or "reason unavailable"))
    if lr.get("closed_by_landing"):
        marks.append("UNDECLARED — CLOSED BY LANDING via %s" %
                     (lr.get("landing_proof_mode") or "proof"))
    if lr.get("close_reason"):
        mark = "CLOSED (%s)" % _close_label(lr)
        if lr["close_reason"] == "superseded":
            mark += " by %s" % (lr.get("superseding_tip") or "-")[:12]
        marks.append(mark)
    # The LIVE step rides beside the terminal, on the compact row too: a
    # process-class land that nothing has re-armed is an OPEN LOOP, and a loop
    # nobody prints is a loop nobody closes.
    phrase = _delivery_phrase(lr)
    if phrase:
        marks.append(phrase)
    if lr.get("close_contradicted"):
        marks.append("close CONTRADICTED — the withdrawn change later "
                     "landed; owed by integrator")
    if lr.get("withdrawn"):
        # The verdict stays visible; WITHDRAWN names the resolution, not a
        # rewrite. A withdrawn row is terminal, so no STALLED mark follows it.
        marks.append("%s — WITHDRAWN, lane abandoned in favour of the verdict"
                     % (lr.get("polarity") or "fix").upper())
    # honored-first: the stalled ALARM yields to the honored banner (one
    # predicate, every surface) — the successor already landed, so there is
    # nothing to unstall. `stalled` itself is untouched.
    if lr["stalled"] and not honored_display(lr):
        # SUCCESSION OUTRANKS THE STALL ALARM, for the same reason honored does
        # one line up: a stall nobody can unstall is noise, not a debt
        # reminder. honored_display only ever answers for CONTRARY rows, so six
        # in-flight rows whose work was cured on a successor and APPROVED by a
        # second reviewer alarmed for up to five and a half days with nothing
        # able to notice. `stalled` itself is untouched — display only.
        state = lr.get("succession_state")
        if state == SUCCESSION_MOVED:
            pass                       # the chain carried it; nothing to unstall
        elif state == SUCCESSION_UNKNOWN:
            root = str(lr.get("chain_root") or "")
            why = ("no chain root; this row predates or escaped chain sealing"
                   if not root or root == dispatches.CHAIN_UNKNOWN else
                   "carrier citation or tip ancestry is unreadable")
            marks.append("STALLED — succession UNKNOWN (%s; whether a "
                         "successor carried it cannot be read)" % why)
        else:
            marks.append("STALLED")
    if lr.get("contrary"):
        # A CONTRARY BANNER IS A CLAIM THAT A VERDICT WAS DEFIED, and it was
        # shouting on rows whose verdict was HONORED THROUGH SUCCESSION — the
        # successor landed and the door closed the row, which is the process
        # WORKING. `contrary_discharge` names which arm discharged it; a
        # discharged row states the succession instead of the alarm, and an
        # UNVERIFIED pair says so rather than guessing in either direction.
        # The `contrary` FIELD is untouched — display only.
        discharge = lr.get("contrary_discharge")
        fact = "LANDED" if lr.get("contrary_state") == "landed" \
            else "MERGED_LOCAL"
        if honored_display(lr):
            # "c" is its OWN kind, not a variant of honored wording: the row
            # is the resolved door's confirmation round, whose reviewed tip
            # is the landed cure BY DESIGN — the exact words lrMarks prints,
            # or the card and the terminal describe one row differently.
            mark = ("CONFIRMATION: %s by design — the resolution verified "
                    "on trunk; the discharge instrument, never a debt"
                    % fact) if discharge == "c" else \
                   ("SUPERSEDED-CLOSED: %s, and the %s verdict was HONORED "
                    "through succession (%s)" % (
                        fact, (lr.get("polarity") or "unknown").upper(),
                        "continuation" if discharge == "a" else "ladder discharge"))
            marks.append(mark)
            mark = None
        elif discharge == "unverified":
            mark = ("CONTRARY? %s despite %s verdict — succession UNVERIFIED "
                    "(ancestry pair not yet computed; it converges and never "
                    "returns)" % (fact, (lr.get("polarity") or "unknown").upper()))
        else:
            mark = "CONTRARY: %s despite %s verdict" % (
                fact, (lr.get("polarity") or "unknown").upper())
    if lr.get("contrary") and mark:
        if lr.get("discharged"):
            mark += "; DISCHARGED by %s" % (
                (lr.get("superseding_tip") or "-")[:12])
        if lr.get("consumed_by"):
            mark += "; CONSUMED as confirmation by %s" % (
                str(lr.get("consumed_by"))[:12])
        marks.append(mark)
    if lr["state"] == "REVIEWED" and not lr.get("closed_by_landing") \
            and not lr.get("close_reason"):
        # REVIEWED can be an undeclared historical verdict OR a declared
        # approval held by tier/gate policy. State alone cannot distinguish
        # them; polarity comes from the dispatch store and the hold reason is
        # already present on the projected row.
        source = dispatches._source_label(lr.get("polarity_source"))
        if lr.get("polarity"):
            marks.append("%s from %s — held: %s" % (
                lr["polarity"].upper(), source, _held_reason(lr)))
        else:
            marks.append("polarity UNDECLARED in %s — not stall-checked" % source)
    if lr.get("attest_state"):
        marks.append("attest " + _attest_text(lr))
    if lr.get("closed_ts_impossible"):
        marks.append("closure stamp IMPOSSIBLE — dated in the future")
    if lr.get("ledger_refused"):
        marks.append("dispatch fold REFUSED historical %s event; current fields "
                     "come from accepted dispatch state"
                     % "/".join(lr["ledger_refused"]))
    if lr.get("receipt_state") == R_UNREADABLE:
        marks.append("land receipt ledger UNREADABLE — not 'no receipt'")
    elif lr.get("receipt_state") in (R_REJECTED, R_CONFLICT):
        marks.append("land receipt %s — diagnostic only" % lr["receipt_state"])
    if not lr.get("dwell_known"):
        marks.append("dwell UNKNOWN — no usable instant to measure from or to")
    if lr["state"] in ("READY", "MERGED_LOCAL") and not lr["observable"]:
        marks.append("landing unobservable")
    if lr.get("base_state") == "UNLANDED-STALE":
        # The NUMBER beside the READY-STALE-BASE word: the word says a rung
        # bit, this says how hard — a 148 and a 1333 are different emergencies
        # and the integrator orders the compose queue by exactly this.
        marks.append("base %d commits behind trunk" % lr["base_behind"])
    mark = "  " + "  ".join("(%s)" % m for m in marks) if marks else ""
    # 17 = len("CHANGES_REQUESTED"), the longest current state.
    # An unreadable entry stamp prints "?" rather than the 0m `_age_s` invents
    # for it: the column is a measurement everywhere else on the board.
    # THE STATE WORD A READER SEES, not the one consumers key on. `ready_word`
    # composes READY-<RUNG> when a rung fails and returns every other state
    # untouched; `lr["state"]` itself is deliberately unchanged so STAGE_ORDER,
    # OWED_BY and LAND_STALL_S keep indexing "READY". Display truth only --
    # enforcement stays in the land door.
    return "  %-12s %-17s %-20s %7s  %s%s" % (
        lr["id"][:12], ready_word(lr), (lr["lane"] or "-")[:20],
        _fmt_dwell(lr["dwell_s"]) if lr.get("dwell_known") else "?",
        (lr["review_sha"] or "-")[:12], mark)


def _render_show(lr):
    state = ("REVIEWED (UNDECLARED) — CLOSED BY LANDING"
             if lr.get("closed_by_landing") else lr["state"])
    if lr.get("close_reason") == "delivered-report":
        state = "BUILD — CLOSED (%s)" % _close_label(lr)
    elif lr.get("landing_review_id") and not lr.get("close_contradicted"):
        state = "BUILD — CLOSED (LANDED via APPROVED REVIEW)"
    elif lr.get("close_reason") and not lr.get("close_contradicted"):
        # Exact per-polarity headline: the verdict-derived state (or the
        # UNDECLARED spelling) plus the terminal — never an approval the
        # ledger does not carry.
        base = "REVIEWED (UNDECLARED)" if lr.get("polarity") is None \
            else lr["state"]
        state = "%s — CLOSED (%s)" % (base, _close_label(lr))
    elif _retired_label(lr):
        # The verdict stays in the headline and the retirement is appended to
        # it — the same law `_line`'s WITHDRAWN mark states: the resolution
        # names how the debt ended, it does not rewrite what the reviewer said.
        state = "%s — RETIRED (%s)" % (lr["state"], _retired_label(lr))
    out = ["LAND REQUEST %s   %s%s" % (
        lr["id"], state,
        # honored-first, same predicate as every other surface: the stalled
        # ALARM yields to the honored banner printed on the contrary line
        "  STALLED" if lr["stalled"] and not honored_display(lr) else "")]
    recipient_role = "builder " if lr.get("kind") == "build" else "reviewer"
    out.append("  author    %s  ->  %s %s" % (
        lr["author"] or "-", recipient_role, lr["reviewer"] or "-"))
    out.append("  lane      %s" % (lr["lane"] or "-"))
    # THE CHAIN, where someone is already asking "what work is this".
    # `discharge` now REFUSES on chain mismatch and its refusal names a
    # `--supersedes <id>` to use; a reader sent here by that message must be
    # able to see the chain this row actually belongs to. Legacy rows print
    # LEGACY rather than a dash, because "written before chains existed" and
    # "value missing" are different facts.
    out.append("  chain     %s%s" % (
        lr.get("chain_root") or "LEGACY (pre-chain row; never retro-fitted)",
        "   continues %s" % lr["supersedes"] if lr.get("supersedes") else ""))
    out.append("  branch    %s" % (lr["branch"] or "-"))
    if lr.get("kind") == "build":
        out.append("  base      %s" % (lr.get("base_sha") or "-"))
    if lr.get("close_reason") == "delivered-report":
        out.append("  verdict   none — delivered-report claims no reviewed tip, "
                   "verdict polarity, Git landing, or trunk proof")
    else:
        out.append("  review    %s" % (lr["review_sha"] or "-"))
        out.append("  verdict   %s   reviewed %s" % (
            lr["verdict_ref"] or "(none)", lr["reviewed_tip"] or "-"))
        out.append("  gate      %s" % (lr.get("gate") or "UNVERIFIED"))
    if lr.get("state") == "READY" and not lr.get("terminal"):
        # Base drift, on the row detail the integrator reads before a land.
        # Three-way honest: a number (with the STALE verdict when it crosses
        # the bar), or UNMEASURED — a READY row whose drift helm could not
        # count must say so rather than render like a current one (task/266:
        # the zombies' every OTHER instrument reported health).
        #
        # NOT on a terminal row, even though a closed-as-landed row FREEZES
        # the verdict word READY: staleness is a property of WAITING work,
        # and a terminal row stops re-reading git by design — so it would
        # print "drift UNMEASURED" forever, an eternal shrug on a discharged
        # row (dogfood find: the receipt-reader-learns-v4 close in the live
        # ledger — CLOSED (LANDED), headline READY, shrugging).
        behind = lr.get("base_behind")
        if lr.get("base_state") == "UNLANDED-STALE":
            out.append("  drift     base is %d commits behind trunk — STALE "
                       "(>= %d): the compose leg re-proves this row against "
                       "all of them" % (behind, STALE_BASE_BEHIND))
        elif isinstance(behind, int):
            out.append("  drift     base is %d commit(s) behind trunk"
                       % behind)
        else:
            out.append("  drift     UNMEASURED — staleness unknown, never "
                       "assumed current")
    if lr.get("ungated") and lr.get("close_reason") != "delivered-report":
        # The whole reason this row is not READY, said where the integrator is
        # already looking for the reason.
        out.append("  NOT READY %s" % lr["ungated"])
    if lr.get("abandoned"):
        out.append("  terminal  ABANDONED — reviewed artifact unresolvable")
        out.append("  land      LAND STATE UNKNOWN — never inferred from object loss")
        out.append("  reason    %s" % (lr.get("abandon_reason") or "-"))
        out.append("  proof     %s v%s (%s)" % (
            lr.get("abandon_proof_mode") or "-",
            lr.get("abandon_proof_version") or "-",
            lr.get("abandon_object_state") or "-"))
        out.append("  interlock %s v%s (%s trunk mention)" % (
            lr.get("abandon_trunk_mention_proof_mode") or "-",
            lr.get("abandon_trunk_mention_proof_version") or "-",
            lr.get("abandon_trunk_mention_state") or "-"))
        out.append("  branch    %s via %s v%s" % (
            lr.get("abandon_branch_state") or "-",
            lr.get("abandon_branch_proof_mode") or "-",
            lr.get("abandon_branch_proof_version") or "-"))
        out.append("  worktree  %s via %s v%s" % (
            lr.get("abandon_worktree_state") or "-",
            lr.get("abandon_worktree_proof_mode") or "-",
            lr.get("abandon_worktree_proof_version") or "-"))
    if lr.get("closed_by_landing"):
        out.append("  polarity  UNDECLARED — immutable; closure records landing, "
                   "not approval")
        out.append("  closure   CLOSED BY LANDING via %s" %
                   (lr.get("landing_proof_mode") or "-"))
        out.append("  repository %s" % (lr.get("landing_repo_id") or "-"))
        out.append("  trunk     %s" % (lr.get("landing_trunk_ref") or "-"))
        out.append("  anchor    %s" % (lr.get("landing_trunk_sha") or "-"))
    if lr.get("close_reason"):
        reason = lr["close_reason"]
        if reason != "delivered-report" and lr.get("polarity") is None \
                and not lr.get("landing_review_id"):
            out.append("  polarity  UNDECLARED — immutable; closure records "
                       "%s, not approval" % reason)
        out.append("  closure   CLOSED (%s)%s%s" % (
            _close_label(lr),
            "  at %s" % lr["close_ts"] if lr.get("close_ts") else "",
            "  — CONTRADICTED: the withdrawn change later landed"
            if lr.get("close_contradicted") else ""))
        if reason == "delivered-report":
            out.append("  artifact  %s" % (lr.get("artifact_ref") or "-"))
            out.append("  report    %s" % (lr.get("report_ref") or "-"))
            out.append("  evidence  %s" % (lr.get("close_evidence") or "-"))
            out.append("  identity  reference syntax validated; artifact/chat existence "
                       "not checked")
            out.append("  land      NOT CLAIMED — this terminal records handoff evidence, "
                       "not Git")
            if lr.get("delivered_report_correction"):
                out.append("  correction explicit annotation of preserved cancel: %s"
                           % (lr.get("cancel_reason") or "-"))
        elif reason == "landed":
            out.append("  live      %s" % _delivery_phrase(lr))
            out.append("  repository %s" % (lr.get("closing_repo_id") or "-"))
            out.append("  trunk     %s" % (lr.get("closing_trunk_ref") or "-"))
            out.append("  anchor    %s  proof %s%s" % (
                lr.get("closing_trunk_sha") or "-",
                lr.get("close_proof_mode") or "-",
                "  translated tip %s" % lr["translated_tip"]
                if lr.get("translated_tip") else ""))
            if lr.get("landing_review_id"):
                out.append("  via       APPROVED REVIEW %s  tip %s" % (
                    lr["landing_review_id"],
                    lr.get("landing_review_tip") or "-"))
                out.append("  authorization tier %s; gate %s (%s); proof %s" % (
                    lr.get("landing_review_tier_state") or "-",
                    lr.get("landing_review_gate") or "none",
                    lr.get("landing_review_gate_requirement") or "-",
                    lr.get("landing_review_approval_anchor") or "-"))
        elif reason == "superseded":
            out.append("  superseded %s via dispatch %s  %s" % (
                lr.get("superseding_tip") or "-",
                lr.get("superseding_id") or "-",
                lr.get("close_evidence") or "-"))
        elif reason == "subsumed":
            out.append("  confirmation %s  tip %s" % (
                lr.get("confirmation_id") or "-",
                lr.get("confirmation_tip") or "-"))
            out.append("  reimplementation %s" % (
                lr.get("confirmation_ref") or "-"))
            out.append("  families  %s/%s -> %s/%s" % (
                lr.get("original_author") or "-",
                lr.get("original_author_family") or "-",
                lr.get("confirmation_recipient") or "-",
                lr.get("confirmation_recipient_family") or "-"))
            out.append("  authorization tier %s; gate %s (%s); proof %s" % (
                lr.get("confirmation_tier_state") or "-",
                lr.get("confirmation_gate") or "none",
                lr.get("confirmation_gate_requirement") or "-",
                lr.get("confirmation_approval_anchor") or "-"))
            out.append("  verdicts  original %s; confirmation %s" % (
                lr.get("original_verdict_anchor") or "-",
                lr.get("confirmation_verdict_anchor") or "-"))
            out.append("  trunk     %s@%s  confirmation %s; original %s" % (
                lr.get("closing_trunk_ref") or "-",
                (lr.get("closing_trunk_sha") or "-")[:12],
                lr.get("close_proof_mode") or "-",
                lr.get("original_proof_mode") or "-"))
        elif reason == "discharged":
            out.append("  discharged by %s  tip %s  tier %s" % (
                lr.get("discharging_id") or "-",
                (lr.get("discharging_tip") or "-")[:12],
                lr.get("discharge_tier") or "-"))
            out.append("  evidence  %s" % (lr.get("close_evidence") or "-"))
        elif reason == "withdrawn":
            out.append("  withdrawn absent at %s@%s  %s" % (
                lr.get("absence_trunk_ref") or "-",
                (lr.get("absence_trunk_sha") or "-")[:12],
                lr.get("close_evidence") or "-"))
        elif reason == "stranded":
            out.append("  stranded  substrate destroyed; control %s in %s  %s"
                       % ((lr.get("control_sha") or "-")[:12],
                          lr.get("closing_repo_id") or "-",
                          lr.get("close_evidence") or "-"))
    if lr.get("contrary"):
        target = "%s trunk" % (lr.get("contrary_target") or "local")
        fact = "LANDED" if lr.get("contrary_state") == "landed" \
            else "MERGED_LOCAL"
        # THE SAME WORDS `lr list` PRINTS, or the two terminals describe one
        # row differently. A stamped discharge means the verdict was HONORED
        # through succession, so "owed by integrator" would bill a debt the
        # chain already paid; UNVERIFIED stays billed AND says why; an absent
        # stamp stays loud — fail-closed. Display only: `owed_by` untouched.
        discharge = lr.get("contrary_discharge")
        suffix = " — DISCHARGED" if lr.get("discharged") \
            else " — CONFIRMATION round: the reviewed tip is the landed " \
                 "resolution by design (the discharge instrument, never a " \
                 "debt)" \
            if discharge == "c" \
            else " — SUPERSEDED-CLOSED; HONORED through succession (%s)" % (
                "continuation" if discharge == "a" else "ladder discharge") \
            if honored_display(lr) \
            else " — CLOSED (SUPERSEDED)" \
            if lr.get("close_reason") == "superseded" \
            else " — owed by integrator (succession UNVERIFIED — ancestry " \
                 "pair not yet computed; it converges and never returns)" \
            if discharge == "unverified" \
            else " — owed by integrator"
        out.append("  contrary  %s on %s despite %s verdict%s" % (
            fact, target, (lr.get("polarity") or "unknown").upper(), suffix))
        if lr.get("discharged"):
            out.append("  discharge %s via dispatch %s  %s" % (
                lr.get("superseding_tip") or "-",
                lr.get("superseding_id") or "-",
                lr.get("discharge_ref") or "-"))
    elif lr.get("withdrawn"):
        out.append("  withdrawn %s — lane abandoned in favour of the verdict  %s" % (
            (lr.get("polarity") or "fix").upper(),
            lr.get("withdraw_ref") or "-"))
    elif lr["state"] in ("MERGED_LOCAL", "LANDED"):
        target = "upstream trunk" if lr["has_upstream"] else "local trunk"
        source = " [verified receipt]" if lr["receipt"] else ""
        out.append("  landed    %s (%s)%s" % (
            "yes" if lr["landed"] else "local only, NOT pushed", target, source))
    elif lr["state"] == "REVIEWED" and not lr.get("closed_by_landing") \
            and not lr.get("close_reason"):
        # UNDECLARED is a claim about the LEDGER, so it may only be printed
        # when the ledger actually lacks a polarity. Printing it unconditionally
        # made this line contradict the row's own `verdict` line two lines up,
        # and a reader who trusted the projection over the record announced in
        # the room that an APPROVE carried no polarity.
        source = dispatches._source_label(lr.get("polarity_source"))
        if lr.get("polarity"):
            out.append("  polarity  %s — current value owned by %s; LR is a "
                       "projection. This row is REVIEWED rather than READY for "
                       "the reason on the NOT READY line, not for want of a "
                       "verdict" % (lr["polarity"].upper(), source))
        else:
            out.append("  polarity  UNDECLARED in %s — LR is a projection; no "
                       "stall is billable in either direction (a verdict is "
                       "immutable, so this row can never gain one)" % source)
    elif lr["state"] == "READY" and not lr["observable"]:
        # TWO CAUSES REACH HERE AND THEY NEED OPPOSITE READER ACTIONS. The old
        # single message blamed a missing repo binding unconditionally, while
        # this same output prints `repository` and `trunk` two lines above —
        # it named a cause its own report refutes, and sent readers hunting a
        # binding that was present and correct (audit 2026-08-05).
        # The common case by far is DELIBERATE: _observe() is called with
        # git=False for a row closed as landed/superseded/stranded/subsumed/
        # delivered-report, so a terminal row stops re-reading git. That makes
        # observable False, which makes landed False, so the state ladder can
        # never reach its `closed and landed` rung and falls through to the
        # VERDICT word — which is why a closed row still reads READY forever.
        # Measured: 121 of 135 raw-READY rows are close_reason='landed', and
        # NONE of them is observable. Nothing here is stale; it is frozen.
        why = lr.get("close_reason")
        if why:
            out.append("  landed    NOT RE-OBSERVED BY DESIGN — closed as "
                       "%s, and a terminally-closed row stops re-reading git "
                       "(proved once, never re-proved). The repo binding "
                       "above is fine. READY here is the VERDICT word frozen "
                       "at closure, NOT a live lifecycle state — this row is "
                       "already terminal; do not try to land it." % why)
        else:
            out.append("  landed    UNOBSERVABLE — no repo binding or trunk to "
                       "watch (never inferred landed)")
    receipt_state = lr.get("receipt_state")
    if receipt_state not in (None, R_NONE):
        diagnostic = " — diagnostic only; live Git governs" \
            if receipt_state in (R_REJECTED, R_CONFLICT, R_UNREADABLE) else ""
        reason = "  (%s)" % lr["receipt_reason"] \
            if lr.get("receipt_reason") else ""
        label = "%s — the land-receipt ledger could not be READ, which is NOT " \
            "the same as no receipt" % receipt_state \
            if receipt_state == R_UNREADABLE else receipt_state
        out.append("  receipt   %s%s%s" % (label, diagnostic, reason))
    if lr.get("closed_ts_impossible"):
        out.append("  closed    UNKNOWN — the ledger's closure stamp for this "
                   "row is dated in the FUTURE, so it is not an instant this "
                   "row can have closed at; repair that ledger row")
    if lr.get("attest_state"):
        out.append("  attest    " + _attest_text(lr))
    if lr.get("ledger_refused"):
        # Historical refusal is a separate axis from current polarity. The
        # dispatch fold owns both facts: accepted events supply current state;
        # refused events remain visible as durable repair evidence.
        out.append("  refused   the dispatch fold refused historical %s event; "
                   "it is absent from the state and the timeline below, whose "
                   "current fields come only from accepted dispatch events"
                   % "/".join(lr["ledger_refused"]))
    out.append("  timeline:")
    for step in lr["timeline"]:
        # "(observed)" belongs to the git-observed step ALONE. It used to be
        # printed for every absent stamp, which explained a ledger event with
        # no ts as something git had seen — and an OPEN step is never that.
        out.append("    %-16s %s" % (step["state"], step["ts"] or (
            "(the ledger's stamp here is NOT A TIMESTAMP)"
            if step.get("ts_unreadable")
            else "(the ledger's stamp here is dated in the FUTURE)"
            if step.get("ts_impossible") else "(git-observed, no ledger stamp)"
            if step.get("observed") else "(no stamp on the ledger event)")))
    threshold = lr["stall_threshold_s"]
    tail = ""
    if threshold is not None:
        # the measurement stays true on an honored row — only the ALARM WORD
        # yields, and it says why instead of pretending "ok"
        word = ("past threshold, not billed (CONFIRMATION row — the "
                "discharge instrument)"
                if lr.get("contrary_discharge") == "c"
                else "past threshold, not billed (HONORED through "
                "succession)") \
            if lr["stalled"] and honored_display(lr) \
            else "STALLED" if lr["stalled"] else "ok"
        tail = "  (threshold %s — %s)" % (_fmt_dwell(threshold), word)
    out.append("  dwell     %s in %s%s" % (
        _fmt_dwell(lr["dwell_s"]) if lr.get("dwell_known")
        else "UNKNOWN (this row's age was never measured: the record carries "
             "no usable instant to measure FROM, or — this row having already "
             "closed — none to measure TO)", lr["state"], tail))
    return "\n".join(out)


def _unavailable(unavailable):
    print("helm lr: dispatch ledger unavailable; land loops UNKNOWN: %s"
          % unavailable, file=sys.stderr)
    return 1


# Every field across the two ledgers whose value is a COMMIT ID. A history
# rewrite (git filter-repo, a squash-root, a rebase of trunk) changes all of
# them at once, and nothing in helm notices: the proofs keep their shape while
# silently pointing at objects that no longer exist. Measured 2026-07-28 on the
# live ledgers — 425 sha-valued fields across these names.
# NOT delivery_ref / verdict_ref: those are EVIDENCE strings (a chat row id
# proving a mention landed), cleaned as free text up to 256 chars, and a chat
# row id is 12 hex — indistinguishable by shape from an abbreviated commit.
# Including it produced 78 guaranteed false dangles on a repository whose
# history had never been rewritten. The discriminator is in the code, not the
# shape: fields that really are commits are validated with _TIP.fullmatch.
_REF_FIELDS = ("ref", "tip", "reviewed_tip", "original_tip", "superseding_tip",
               "landing_trunk_sha", "retarget_from", "retarget_to",
               "review_sha", "trunk_sha")
_SHA_RE = re.compile(r"\A[0-9a-f]{7,40}\Z")
_FULL_SHA_RE = re.compile(r"\A(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")


def _batch_exists(gitdir, shas):
    """{sha: True|False} for every sha, or None when Git could not be asked.

    ONE `cat-file --batch-check` for the whole set, not one process per sha —
    the live ledgers carry hundreds and a per-sha probe turns an audit into a
    minute of forks. Unresolvable input lines answer "missing", which
    batch-check reports rather than failing the batch."""
    shas = [s for s in dict.fromkeys(shas) if _SHA_RE.match(s or "")]
    if not shas:
        return {}
    p = _git(gitdir, "cat-file", "--batch-check", input_text="\n".join(shas) + "\n")
    if p is None or p.returncode != 0:
        return None                    # UNKNOWN — never "they all resolve"
    # MATCH BY POSITION, NOT BY NAME. batch-check echoes the RESOLVED FULL sha
    # for a valid abbreviation and only echoes the input verbatim when it is
    # `<input> missing`. Keying the result by parts[0] therefore fails to find
    # every abbreviated ref that resolved perfectly — measured here as 119
    # false dangles on a repository whose history had never been rewritten.
    # Output is one line per input, in input order, so the zip is exact.
    lines = [l for l in (p.stdout or "").splitlines() if l.strip()]
    if len(lines) != len(shas):
        return None                    # shape we do not understand => UNKNOWN
    out = {}
    for sha, line in zip(shas, lines):
        parts = line.split()
        out[sha] = not (len(parts) >= 2 and parts[1] == "missing")
    return out


def _commit_exists(gitdir, sha):
    """True/False/None for one exact immutable commit object.

    Only Git's explicit batch-check `missing` grammar means MISSING. A command
    failure, malformed response, non-commit object, or unsupported hash shape
    is UNKNOWN and can never authorize a terminal ledger event.
    """
    sha = str(sha or "").strip().lower()
    if not _FULL_SHA_RE.fullmatch(sha):
        return None
    p = _git(gitdir, "cat-file", "--batch-check",
             input_text=sha + "^{commit}\n")
    if p is None or p.returncode != 0:
        return None
    lines = [line for line in (p.stdout or "").splitlines() if line.strip()]
    if len(lines) != 1:
        return None
    parts = lines[0].split()
    if len(parts) >= 2 and parts[1] == "missing":
        return False
    if len(parts) == 3 and parts[1] == "commit" and parts[2].isdigit():
        return True
    return None


def read_commit_map(path):
    """({old: new}, err) — git filter-repo's commit-map, as it actually writes it.

    filter-repo emits `.git/filter-repo/commit-map` automatically: a header
    line, then `<old-sha> <new-sha>` per rewritten commit. A commit DROPPED by
    the rewrite maps to forty zeros, which is not a translation and must never
    be applied as one — a ref pointed at 000…0 is worse than a dangling ref
    because it resolves to nothing while looking migrated.

    A commit the rewrite did NOT change maps to ITSELF, and the map is full of
    those — measured, not assumed: filter-repo lists every commit it walked, so
    a run that touched one message still emitted `X X` for each untouched
    ancestor. `X -> X` is not a translation either. Recording one would tell
    `lr refs` that a still-dangling ref had moved somewhere (it names where it
    already was), and it would leave a SELF-MAP in the sidecar — which chained
    composition must then refuse as a cycle, permanently un-composing a ref
    that never needed composing. Excluded here, at the one place that reads the
    map, so neither consumer has to know."""
    try:
        with open(os.path.expanduser(path), encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except OSError as e:
        return None, "cannot read commit-map: %s" % e
    out, dropped, unchanged = {}, 0, 0
    for ln in lines:
        parts = ln.split()
        if len(parts) != 2 or not _SHA_RE.match(parts[0]) or not _SHA_RE.match(parts[1]):
            continue                      # header and anything unparsed
        old, new = parts[0], parts[1]
        if set(new) == {"0"}:
            dropped += 1
            continue
        if new == old:
            unchanged += 1
            continue
        out[old] = new
    if not out:
        return None, ("no usable old->new pairs in %s (%d dropped-commit rows, "
                      "%d unchanged)" % (path, dropped, unchanged))
    return out, None


def _map_lookup(table, sha):
    """(new, ambiguous) — `sha` through a {old: new} table, refusing to guess.

    Ledgers record ABBREVIATIONS; commit-maps and the translation sidecar are
    keyed by full shas, so an exact miss is retried as a prefix. Two recorded
    shas sharing that prefix answer AMBIGUOUS rather than the first hit —
    picking one forges a binding, which is the one thing this file exists to
    prevent.

    AMBIGUOUS IS NOT A MISS, and that distinction is the whole reason this
    returns a pair. A caller walking a chain has to tell "nothing further was
    recorded" (stop here, this is where the ref sits now) from "the recorded
    history forks" (abandon the ref). Collapsing them into None would make a
    fork read as the end of the road and translate from a sha we cannot show
    the ref ever reached."""
    hit = table.get(sha)
    if hit is not None:
        return hit, False
    if len(sha) >= 40:
        return None, False
    hits = [o for o in table if o.startswith(sha)]
    if len(hits) == 1:
        return table[hits[0]], False
    return None, len(hits) > 1


_MAX_CHAIN_HOPS = 16


def _chain_end(sha, translations):
    """Where `sha` sits after every rewrite recorded so far — or None, meaning
    DO NOT COMPOSE: nothing was recorded for it, or what was recorded cannot be
    trusted. Both answers are the same instruction to the caller, so they share
    a return rather than making it decide which None it received.

    THE CHAINED-REWRITE PROBLEM, measured on a repo filtered twice. Pass 1
    recorded original->A. Pass 2 ran from the rewritten state, so ITS map is
    keyed by A — verified: zero of pass 1's inputs appear in pass 2's map. The
    ledgers still hold the originals, so every ref missed and the run reported
    `0 commit ids translatable, 539 left alone`. The chain had to be composed
    by hand. This is that composition: follow what earlier passes already
    recorded, and let the caller ask the new map about the sha we land on.

    IT MUST TERMINATE. The sidecar is append-only text that nothing validates
    on the way in, so a cycle — or a single row translating a sha to itself —
    would otherwise spin forever inside what is meant to be an audit. A
    revisited sha, a fork, or a walk longer than _MAX_CHAIN_HOPS all refuse,
    and refusing means the ref is left alone and counted unmapped: the same
    answer this migration already gives to everything it cannot prove. Sixteen
    is far past any real history (each hop is a whole filter-repo pass) and
    exists so an unforeseen shape stops rather than hangs."""
    seen = {sha}
    cur = sha
    for _ in range(_MAX_CHAIN_HOPS):
        nxt, ambiguous = _map_lookup(translations, cur)
        if ambiguous:
            return None                   # a fork is refused, never sampled
        if nxt is None:
            # The end of what has been recorded. Still standing on the sha we
            # were handed means no earlier pass moved it, so there is nothing
            # to compose and the caller must not look it up a second time.
            return cur if cur != sha else None
        if nxt in seen:
            return None                   # a cycle, `sha -> sha` included
        seen.add(nxt)
        cur = nxt
    return None


def migrate_refs(commit_map_path, repo=None, apply=False):
    """(report, err) — re-point every recorded commit id through a rewrite.

    THE COMPANION TO `lr refs`. That audit reports which proofs a rewrite
    orphaned; this translates them, and the audit is its acceptance test: a
    correct migration ends with `helm lr refs` back at zero.

    DRY-RUN BY DEFAULT, like every other ledger mutation here. Both ledgers are
    APPEND-ONLY event logs, so a translation is written as new events rather
    than by editing history in place — the original rows stay readable, which
    is what lets a bad migration be inspected instead of mourned.

    PARTIAL BY DESIGN: a ref absent from the map is LEFT ALONE and counted, not
    guessed at. filter-repo maps only what it rewrote, so an untouched commit
    legitimately has no entry — and inventing one would forge a proof, which is
    the one thing this file exists to prevent.

    CHAINED, from the second rewrite onward. A repo filtered twice produces a
    second map keyed by what the FIRST pass emitted, while the ledgers still
    hold the originals — measured live as `0 commit ids translatable, 539 left
    alone`, a migration that ran, reported success, and moved nothing. So a ref
    the map does not know is followed through the translations already recorded
    and looked up again, and the composed `original -> newest` is what lands.
    The sidecar therefore answers "where did this ref end up" in ONE hop no
    matter how many rewrites happened, which is what `lr refs` reads.

    NOT SCOPED TO THIS REPO, deliberately. The sidecar is global and rows carry
    the repo they came from, so filtering by it looks obviously right — and
    would break the exact case this fixes, because filter-repo's own documented
    workflow runs the next pass on a FRESH CLONE and a clone has a different
    repo id. The chain stays unscoped and safety comes from elsewhere: a full
    sha is unique across repos, an abbreviation matching two is refused as
    ambiguous, and a composed sha still has to appear in THIS rewrite's map
    before anything is written."""
    mapping, err = read_commit_map(commit_map_path)
    if err:
        return None, err
    info = dispatches._repo_info(repo)
    if not info:
        return None, ("%s is not a readable Git working tree"
                      % (repo or os.getcwd()))
    lrs, unavailable = project()
    if unavailable:
        return None, "land-request ledger unavailable (%s)" % unavailable
    drows = dispatches.rows()
    # Read ONCE, before planning. Composition then sees one fixed picture of
    # what earlier passes recorded, so this run's answer cannot depend on the
    # order the ledgers happened to be walked in.
    recorded = ref_translations()
    planned, unmapped, chained = [], 0, 0
    for source, rows in (("lr", lrs), ("dispatch", drows)):
        for row in (rows.values() if isinstance(rows, dict) else rows):
            for f in _REF_FIELDS:
                v = row.get(f)
                if not (isinstance(v, str) and _SHA_RE.match(v)):
                    continue
                # An abbreviation cannot key the map directly; _map_lookup
                # matches the unique full sha it prefixes and refuses an
                # ambiguous one.
                new, ambiguous = _map_lookup(mapping, v)
                if new is None and not ambiguous:
                    # Not in this map — which, after a second rewrite, is what
                    # EVERY original sha looks like. Follow the recorded chain
                    # to where the ref sits now and ask the map about that.
                    # An ambiguous DIRECT lookup is deliberately not retried
                    # this way: routing around a fork is still choosing which
                    # commit the ref meant.
                    end = _chain_end(v, recorded)
                    if end is not None:
                        new, _fork = _map_lookup(mapping, end)
                        chained += bool(new)
                if new is None:
                    unmapped += 1
                    continue
                planned.append({"source": source, "id": row.get("id"),
                                "field": f, "old": v, "new": new})
    report = {"repo": info["repo"], "mapped": len(mapping),
              "translations": planned, "unmapped": unmapped,
              "chained": chained, "applied": bool(apply)}
    if not apply:
        return report, None
    # A SIDECAR, NOT A MUTATION — and this is the load-bearing design call.
    # dispatches is hardened so nothing can move the tip a verdict attested:
    # `retarget` exists in replay for already-written rows only and its own
    # docstring says "there is no shipping verb", with the module header
    # listing retarget among the operations deliberately not supported.
    # Rewriting tips here would reopen precisely that hole — a migration that
    # can re-point what a review attested is a migration that can forge one.
    #
    # So the recorded tip stays exactly as attested (it is a true historical
    # fact: that IS what the reviewer saw), and the translation lands beside
    # it. Resolution prefers content identity anyway (receipt_for_content), so
    # this sidecar is an explanation for humans and audits, never an authority.
    path = os.path.join(home.global_dir(), ".state", "ref-migrations.jsonl")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    written = 0
    try:
        with open(path, "a", encoding="utf-8") as f:
            for t in planned:
                f.write(json.dumps({
                    "ts": pk.now_ts(), "repo": info["repo_id"],
                    "source": t["source"], "id": t["id"], "field": t["field"],
                    "old": t["old"], "new": t["new"],
                    "commit_map": os.path.abspath(
                        os.path.expanduser(commit_map_path)),
                }, ensure_ascii=False) + "\n")
                written += 1
    except OSError as e:
        return None, "could not write the translation sidecar: %s" % e
    report["written"] = written
    report["sidecar"] = path
    return report, None


def _ref_translations_checked():
    """(map, err) — the recorded translation sidecar, with UNREADABLE kept
    distinct from ABSENT. `err` is set on any OSError other than a missing
    file: the tolerant reader's blanket `except OSError: return {}` conflated
    "no sidecar" with "cannot read the sidecar", and for a BLOCK-ONLY consumer
    (the close ladders read this to refuse a terminal) that is the dangerous
    direction — an unreadable map reading as empty clears the very rung it
    exists to hold. Row-level junk stays skipped exactly as before: a bad row
    predates any guard and must not poison the readable rest."""
    path = os.path.join(home.global_dir(), ".state", "ref-migrations.jsonl")
    out = {}
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for ln in f:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    r = json.loads(ln)
                except ValueError:
                    continue
                old, new = str(r.get("old") or ""), str(r.get("new") or "")
                if not (old and new) or old == new or set(new) == {"0"}:
                    continue
                out[old] = new
    except FileNotFoundError:
        return {}, None
    except OSError as e:
        return {}, str(e)
    return out, None


def ref_translations():
    """{old_sha: new_sha} recorded by migrate_refs --apply, for explaining a
    dangling ref. Advisory: the attested tip remains the authority for what a
    reviewer saw, and content identity remains the authority for resolution.

    THE TOLERANT WRAPPER over `_ref_translations_checked` — advisory readers
    (`lr refs`, migrate planning) keep the historical fail-open shape; the
    close ladders call the checked form directly and REFUSE on `err`.

    LAST ROW WINS, and that is load-bearing rather than incidental. The file is
    append-only across rewrites, so a ref migrated twice appears twice; the
    composed `original -> newest` row is appended after the `original -> A` row
    it was composed from, so overwriting as we read leaves the NEWEST hop —
    which is what makes this a one-hop answer and what stops a third rewrite
    from walking a chain whose head is already stale.

    A row is refused here as well as at the map, because the sidecar is
    append-only text: rows predate any given guard and nothing validates one on
    the way in. `old == new` claims a ref moved to where it already was and
    would be a self-map for the chain walk to trip over; a forty-zero `new` is
    filter-repo's DROPPED marker, and letting one back in through this door
    would hand composition a translation to nothing."""
    out, _err = _ref_translations_checked()
    return out


def dangling_refs(repo=None):
    """(report, err) — every recorded commit id that no longer resolves.

    THE PROBLEM THIS EXISTS FOR: helm binds its proofs to git SHAs, and a SHA
    is content-addressed over HISTORY. Rewrite history and every stored ref
    dangles at once — a land request still names its reviewed tip, a verdict
    still names what it attested, and not one of them can be verified again.
    Nothing detected that, so the first symptom would have been a gate quietly
    unable to prove something it had already proven.

    TRI-STATE, because the alternative is the failure this audit is for: a
    repo we cannot ask reports UNKNOWN and is counted separately. A check that
    passes because it could not look reads exactly like a check that passed.

    `_patch_id` already computes a rebase/ff-STABLE content identity for
    exactly this reason, and land records already store it — but the ledger is
    KEYED by sha, so the stable identity sits beside the fragile one without
    ever being the thing looked up. This audit is what makes that gap visible
    and what verifies any migration that closes it."""
    info = dispatches._repo_info(repo)
    if not info:
        return None, ("%s is not a readable Git working tree"
                      % (repo or os.getcwd()))
    gitdir = info["repo_id"]
    lrs, unavailable = project()
    if unavailable:
        # The ledger itself could not be read. Say so — an audit that reports
        # zero dangling refs because it saw zero rows is the exact shape of
        # failure it was written to detect.
        return {"repo": info["repo"], "checked": 0, "unknown": 0,
                "dangling": [], "by_field": {},
                "note": "land-request ledger unavailable (%s) — nothing was "
                        "checked, nothing is cleared" % unavailable}, None
    seen = {}                                   # sha -> [(source, id, field)]
    for lr in lrs.values():
        for f in _REF_FIELDS:
            v = lr.get(f)
            if isinstance(v, str) and _SHA_RE.match(v):
                seen.setdefault(v, []).append(("lr", lr.get("id"), f))
    # BOTH ledgers hand back {id: row}, so iterate values — walking the mapping
    # itself yields id STRINGS, which silently have no fields to inspect and
    # would have reported a clean audit over nothing.
    drows = dispatches.rows()
    for d in (drows.values() if isinstance(drows, dict) else drows):
        for f in _REF_FIELDS:
            v = d.get(f)
            if isinstance(v, str) and _SHA_RE.match(v):
                seen.setdefault(v, []).append(("dispatch", d.get("id"), f))
    exists = _batch_exists(gitdir, list(seen))
    if exists is None:
        return {"repo": info["repo"], "checked": 0, "unknown": len(seen),
                "dangling": [], "by_field": {},
                "note": "git could not be asked — every ref is UNKNOWN, "
                        "not verified"}, None
    dangling = []
    by_field = {}
    translated = ref_translations()
    for sha, uses in seen.items():
        if exists.get(sha) is True:
            continue
        for source, rid, field in uses:
            # A dangle with a recorded translation is EXPLAINED, not resolved:
            # the attested tip is still gone, we simply know what it became.
            dangling.append({"sha": sha, "source": source, "id": rid,
                             "field": field,
                             "translated_to": translated.get(sha)})
            by_field[field] = by_field.get(field, 0) + 1
    return {"repo": info["repo"], "checked": len(seen),
            "unknown": len([s for s in seen if s not in exists]),
            "dangling": dangling, "by_field": by_field}, None


def _cmd_legacy_completion_hints(rest):
    """Read-only audit of historical reason-text completion hints."""
    from .cli import guard_tail
    rc = guard_tail("helm lr legacy-completion-hints", rest,
                    flags=("--json",), usage=USAGE)
    if rc is not None:
        return rc
    rows, unavailable = legacy_completion_hints()
    if unavailable:
        return _unavailable(unavailable)
    if "--json" in rest:
        print(json.dumps({"why": _LEGACY_COMPLETION_WHY, "rows": rows},
                         ensure_ascii=False, indent=1))
        return 0
    print("helm lr — %d legacy completion hint%s; every row remains "
          "CANCELLED and UNVERIFIED" % (len(rows), "" if len(rows) == 1 else "s"))
    print("  " + _LEGACY_COMPLETION_WHY)
    for row in rows:
        print("  %s  %s  eligible=%s" % (
            row["id"], row["classification"], row["delivered_report_eligible"]))
        print("    cancel_reason: %s" % row["cancel_reason"])
    return 0


def cmd_lr(args):
    args = list(args or [])
    if not args or args[0] in ("-h", "--help"):
        print(USAGE, file=sys.stderr)
        return 2
    verb, rest = args[0], args[1:]
    from .cli import guard_tail, suggest
    if verb == "refs":
        rc = guard_tail("helm lr refs", rest, flags=("--json",),
                        valued=("--repo",), usage=USAGE)
        if rc is not None:
            return rc
        i = rest.index("--repo") if "--repo" in rest else -1
        report, err = dangling_refs(repo=(rest[i + 1] if i >= 0 else None))
        if err:
            print("helm lr refs: " + err, file=sys.stderr)
            return 1
        if "--json" in rest:
            print(json.dumps(report, ensure_ascii=False, indent=1))
            return 1 if report["dangling"] else 0
        if report.get("note"):
            print("helm lr refs: " + report["note"], file=sys.stderr)
            return 1
        n = len(report["dangling"])
        print("helm lr refs — %d commit id%s recorded across the land-request "
              "and dispatch ledgers, %d unresolvable in %s" % (
                  report["checked"], "s"[:report["checked"] != 1], n,
                  report["repo"]))
        for d in report["dangling"][:20]:
            print("  DANGLING %-9s %-34s %-18s %s" % (
                d["source"], d["id"], d["field"], d["sha"]))
        if n > 20:
            print("  (+%d more — --json for the full set)" % (n - 20))
        if n:
            print("  a recorded proof that cannot be resolved can never be "
                  "re-verified; after a history rewrite, migrate the ledgers "
                  "through git filter-repo's commit-map rather than leaving "
                  "these", file=sys.stderr)
        elif report["unknown"]:
            print("  %d UNKNOWN (git could not be asked) — not cleared"
                  % report["unknown"], file=sys.stderr)
        return 1 if n else 0
    if verb == "migrate":
        rc = guard_tail("helm lr migrate", rest, flags=("--apply", "--json"),
                        valued=("--commit-map", "--repo"), usage=USAGE)
        if rc is not None:
            return rc
        if "--commit-map" not in rest:
            print("helm lr migrate: --commit-map PATH is required (git "
                  "filter-repo writes .git/filter-repo/commit-map)",
                  file=sys.stderr)
            return 2
        cm = rest[rest.index("--commit-map") + 1]
        rp = rest[rest.index("--repo") + 1] if "--repo" in rest else None
        report, err = migrate_refs(cm, repo=rp, apply="--apply" in rest)
        if err:
            print("helm lr migrate: " + err, file=sys.stderr)
            return 1
        if "--json" in rest:
            print(json.dumps(report, ensure_ascii=False, indent=1))
            return 0
        n = len(report["translations"])
        print("helm lr migrate%s — %d commit id%s translatable, %d left alone "
              "(absent from the map), over %d mapped rewrite%s" % (
                  "" if report["applied"] else " [dry-run (--apply to record)]",
                  n, "s"[:n != 1], report["unmapped"], report["mapped"],
                  "s"[:report["mapped"] != 1]))
        for t in report["translations"][:15]:
            print("  %-9s %-34s %-16s %s -> %s" % (
                t["source"], t["id"], t["field"], t["old"][:12], t["new"][:12]))
        if n > 15:
            print("  (+%d more)" % (n - 15))
        if report.get("chained"):
            # Say it out loud. The number that exposed the bug was a plausible
            # zero, so a run that only works because earlier passes were
            # composed should never look like a run that matched directly.
            print("  %d of those were COMPOSED through an earlier rewrite this "
                  "map does not know about — the recorded translation stays "
                  "one hop: original -> newest" % report["chained"])
        if report.get("sidecar"):
            print("  recorded %d translation(s) -> %s" % (
                report["written"], report["sidecar"]))
            print("  the attested tips are UNCHANGED by design — a migration "
                  "that could move what a verdict attested could forge one; "
                  "resolution goes through content identity, this only "
                  "explains. Verify with: helm lr refs")
        return 0
    if verb == "legacy-completion-hints":
        return _cmd_legacy_completion_hints(rest)
    if verb == "list":
        rc = guard_tail("helm lr list", rest, flags=("--all", "--json"),
                        usage=USAGE)
        if rc is not None:
            return rc
        # loops() inlined so the header can also say how many rows the ledger
        # HOLDS — same single project_raw read, no second walk. The web card's
        # filed strip (/api/lr) states the same population; a CLI header that
        # could not would let the two surfaces disagree about one record.
        # ONE NUMERIC INSTANT, bound BEFORE the projection and threaded into
        # it. Binding only the STAMP was half the cure: `project_raw()` with
        # no argument samples the clock ITSELF, so the header could name T0
        # while the dwell and stall classification inside the projection used
        # T1. @codex-2 found that the stamp-count tests cannot see it — they
        # assert how many stamps print, not which clock classified the rows.
        # project_raw's own docstring warns about the two-instants bug; its
        # caller was reintroducing it one level up.
        read_now = time.time()
        lrs_, raw_, unavailable = project_raw(read_now)
        if unavailable:
            return _unavailable(unavailable)
        try:
            rows_ = _loop_rows(lrs_, raw_, include_landed="--all" in rest)
        except _ChainUntrustworthy as e:
            return _unavailable(str(e))
        if "--json" in rest:
            print(json.dumps(rows_, ensure_ascii=False, indent=1))
            return 0
        filed_ = filed_split(lrs_, raw_)
        # The stamp is DERIVED from the same number the projection used, so
        # the instant a reader sees is the instant that classified the rows.
        read_at = dispatches._read_stamp(read_now)
        if not rows_:
            # STAMPED BECAUSE IT IS AN ABSENCE CLAIM. "no land loops in
            # flight" quoted forward reads as a standing fact about the board,
            # and that is the reading an integrator acts on by standing down —
            # hours after it stopped being true. The WHOLE filed strip rides
            # along, same shape as the card's (one owner, filed_line): an
            # empty BOARD over a populated LEDGER is exactly where "nothing
            # here" needs the population beside it, and a pasted header must
            # read identically to the surface it gets compared against.
            print("helm lr: no land loops in flight — %s  (read %s)"
                  % (filed_line(filed_), read_at))
            return 0
        # "in flight" IS a claim, and under `--all` it was a false one: the
        # header counted every row the listing carries, retirements included,
        # so the live board read `718 land loops in flight` over 653 rows that
        # owe nobody anything (measured 2026-08-03). The default listing
        # filters terminals out, so `retired` is exactly 0 there and that line
        # is unchanged; only the header that has retirements to account for
        # says so. The polarity provenance rides alongside — both facts are
        # about the same header and dropping either to resolve a rebase would
        # trade one operator's truth for another's.
        retired = sum(1 for lr in rows_ if lr["terminal"])
        # ONE PREDICATE FOR "IN FLIGHT" ACROSS BOTH FRONT-ENDS, which is the
        # whole point of this lane and which its first cut did not finish
        # (@codex): the browser partitions honored rows OUT of its in-flight
        # count (00-core.js lrHonored, `live.length + " in flight"`) while
        # this header counted them IN, so one word still named two numbers —
        # the exact defect one surface down. The direction is not a taste
        # call: the owner ruled it (2026-08-04, quoted at 00-core.js:1089)
        # "superseded, if verified, should just be like another type of
        # closed", so honored is CLOSED and the CLI follows the ruling.
        # The docstring above already promised "minus honored" and was, until
        # this line, describing a subtraction nobody performed.
        #
        # THE ROWS ARE STILL PRINTED. Only the accounting moves — same as the
        # board, where an honored row keeps its SUPERSEDED-CLOSED mark and
        # moves to the closed strip. So the header states all three numbers
        # rather than quietly printing more rows than it counts.
        honored_n = len(rows_) - retired - len(inflight_rows(rows_))
        counted = [(retired, "RETIRED — owed by nobody"),
                   (honored_n,
                    "honored — closed through succession, not work")]
        said = ["%d %s" % (n, word) for n, word in counted if n]
        # The populated header carries the instant too: every row below it
        # prints a RELATIVE dwell, and a relative age with no origin
        # re-anchors to whenever the listing is read next.
        print("helm lr — %d land loop%s%s  (projection; %s; "
              "verdict polarity: %s; read %s)"
              % (len(rows_), "" if len(rows_) == 1 else "s",
                 "  (%s; %d in flight)"
                 % ("; ".join(said), len(inflight_rows(rows_)))
                 if said else " in flight",
                 filed_line(filed_),
                 dispatches._source_label(dispatches.POLARITY_SOURCE),
                 read_at))
        for lr in rows_:
            print(_line(lr))
        if any(lr["stalled"] for lr in rows_):
            print("STALLED = past its per-stage threshold (helm lr stalls)")
        return 0
    if verb == "foldcheck":
        # The five fold checks as ONE refusing rung. Every fold ran these by
        # hand and the fifth — did it reach ORIGIN — is the one that gets
        # skipped when tired, which is how two lands were announced that
        # origin did not have. Exit 1 on REFUSE *or* UNKNOWN: not-measured is
        # not consent, so a caller that gates on rc gets the same stop for
        # "no" and for "I could not tell", which are both reasons not to
        # announce a land.
        fc_usage = ("usage: helm lr foldcheck <tip> [--gate gate:TOKEN] "
                    "[--repo PATH] [--remote R] [--branch B] [--no-fetch]")
        # CLOSED-SET TAIL, and the reason it is not the hand-rolled scan it
        # was: reading options by `opts.index(name) + 1` raised IndexError on
        # a trailing `--gate` and silently IGNORED `--bogus`. A rung that
        # refuses on measurement has to refuse on its own arguments first —
        # an ignored flag means the caller asked for something this verb did
        # not do, and then read the answer as though it had.
        tip = rest[0] if rest and not rest[0].startswith("-") else None
        rc = guard_tail("helm lr foldcheck", rest[1:] if tip else rest,
                        flags=("--no-fetch",),
                        valued=("--gate", "--repo", "--remote", "--branch"),
                        usage=fc_usage)
        if rc is not None:
            return rc
        if not tip:
            print(fc_usage, file=sys.stderr)
            return 2
        opts = rest[1:]

        def _opt(name, default=None):
            return opts[opts.index(name) + 1] if name in opts else default

        rungs = foldcheck.check(
            _opt("--repo", "."), tip,
            gate_ref=_opt("--gate"),
            remote=_opt("--remote", "origin"),
            branch=_opt("--branch", "main"),
            fetch="--no-fetch" not in opts)
        for line in foldcheck.report(rungs):
            print(line)
        return 0 if foldcheck.ok(rungs) else 1
    if verb == "stalls":
        rc = guard_tail("helm lr stalls", rest, flags=("--json",), usage=USAGE)
        if rc is not None:
            return rc
        # ONE READ INSTANT for every section this verb prints, so the stalled
        # rows, the unmeasurable rows and the tail cannot be read as three
        # observations taken at three different times — and bound AT the read
        # rather than after it. It used to be taken below the --json early
        # return, i.e. after the whole projection, so the published instant
        # named the RENDER; on a large board that projection is the slow part,
        # and the stamp drifted by however long it took.
        # Same cure as `lr list`: ONE numeric instant feeds BOTH the stamp and
        # the projection. Binding the stamp first fixed the drift between the
        # stamp and the render, but left project_raw() sampling its own clock,
        # so the published instant and the stall classification still came
        # from different reads.
        read_now = time.time()
        read_at = dispatches._read_stamp(read_now)
        lrs, raw, unavailable = project_raw(read_now)
        if unavailable:
            return _unavailable(unavailable)
        try:
            rows_ = _stalled_rows(lrs, raw)
            unmeasurable_rows = _unmeasurable_rows(lrs, raw)
        except _ChainUntrustworthy as e:
            return _unavailable(str(e))
        closed_rows = sorted(
            (lr for lr in lrs.values() if lr.get("closed_by_landing")),
            key=lambda lr: str(lr.get("landing_ts") or ""))
        if "--json" in rest:
            unmeasurable_json = [{"row": lr, "reason": why}
                                 for lr, why in unmeasurable_rows]
            print(json.dumps({"stalled": rows_, "unmeasurable": unmeasurable_json,
                              "closed_by_landing": closed_rows},
                             ensure_ascii=False, indent=1))
            return 0
        if not rows_:
            print("helm lr: no stalled land loops — every loop is inside its "
                  "threshold  (read %s)" % read_at)
        else:
            print("helm lr — %d STALLED land loop%s (workflow gaps):  (read %s)"
                  % (len(rows_), "" if len(rows_) == 1 else "s", read_at))
            # ONE store read for the whole listing (ref_branch lives on the
            # dispatch row, not the projection), one repo dir from the row's
            # own recorded repository. The marker is silent whenever it cannot
            # measure, and says so nowhere — see _moved_tip_marker.
            store_rows = dispatches.rows()
            for lr in rows_:
                gitdir = lr.get("repo_id")
                repo_dir = gitdir[:-5] if isinstance(gitdir, str) \
                    and gitdir.endswith("/.git") else gitdir
                store_row = store_rows.get(lr["id"])
                marker = _moved_tip_marker(lr, store_row, repo_dir)
                # THE SECOND MARKER ANSWERS A DIFFERENT QUESTION FROM THE
                # FIRST. Moved-tip asks whether the review is still about this
                # work; landed asks whether the work is still owed at all. A
                # row can be both, and a reader billed for a job already on
                # trunk needs to be told so before either.
                # THE LANDED TAIL REPLACES THE OWED-BY CLAUSE, never sits
                # beside it: a row whose work is on trunk is owed by nobody,
                # and printing a debtor next to the correction leaves the
                # routing debt standing.
                tail = _landed_marker(store_row, lr["state"])
                print(_line(lr) + "  (>= %s in %s, %s)%s" % (
                    _fmt_dwell(lr["stall_threshold_s"]), lr["state"],
                    tail or ("owed by %s" % _owed_by_whom(lr)), marker))
        if unmeasurable_rows:
            print("helm lr — %d loop%s NOT stall-checked:" % (
                len(unmeasurable_rows), "" if len(unmeasurable_rows) == 1 else "s"))
            for lr, why in unmeasurable_rows:
                print(_line(lr) + "  (%s)" % why)
            # For REVIEWED: no remedy — a verdict is immutable. For OPEN unbilled:
            # the fix is re-sending or cancelling.
            if any("UNDECLARED" in why for _lr, why in unmeasurable_rows):
                print("  these carry UNDECLARED verdicts: the ledger records that "
                      "a review happened, not whether it approved. A verdict is "
                      "immutable; new verdicts send --approve|--fix|--supersede.")
        if closed_rows:
            print("helm lr — %d REVIEWED (UNDECLARED) row%s CLOSED BY LANDING; "
                  "excluded from stall accounting (helm lr list --all)" % (
                      len(closed_rows), "" if len(closed_rows) == 1 else "s"))
        return 0
    if verb == "show":
        if not rest:
            print(USAGE, file=sys.stderr)
            return 2
        rid = rest[0]
        rc = guard_tail("helm lr show", rest[1:], flags=("--json",), usage=USAGE)
        if rc is not None:
            return rc
        lr, err = get(rid)
        if err:
            print("helm lr: " + err, file=sys.stderr)
            return 1
        if "--json" in rest[1:]:
            print(json.dumps(lr, ensure_ascii=False, indent=1))
            return 0
        print(_render_show(lr))
        return 0
    if verb == "land":
        return _cmd_land(rest)
    if verb == "compose":
        return _cmd_compose(rest)
    if verb == "close":
        return _cmd_close(rest)
    if verb == "annotate-delivered-report":
        return _cmd_annotate_delivered_report(rest)
    if verb == "discharge":
        return _cmd_discharge(rest)
    if verb == "withdraw":
        return _cmd_withdraw(rest)
    if verb == "abandon":
        return _cmd_abandon(rest)
    if verb == "close-landed":
        return _cmd_close_landed(rest)
    print("helm lr: unknown subverb '%s'%s (%s)" % (
        verb, suggest(verb, ("list", "show", "stalls", "foldcheck",
                             "legacy-completion-hints", "land", "compose",
                             "close",
                             "annotate-delivered-report", "discharge",
                             "withdraw", "abandon", "close-landed")),
        USAGE),
        file=sys.stderr)
    return 2


def _cmd_abandon(rest):
    """Terminal judgement for a reviewed commit Git explicitly reports missing."""
    pos, opts, i = [], {}, 0
    while i < len(rest):
        arg = rest[i]
        if arg == "--json":
            if arg in opts:
                print(USAGE, file=sys.stderr)
                return 2
            opts[arg] = True
            i += 1
            continue
        if arg in ("--reason", "--repo"):
            if arg in opts or i + 1 >= len(rest) \
                    or str(rest[i + 1]).startswith("--"):
                print(USAGE, file=sys.stderr)
                return 2
            opts[arg] = rest[i + 1]
            i += 2
            continue
        if str(arg).startswith("--"):
            print(USAGE, file=sys.stderr)
            return 2
        pos.append(arg)
        i += 1
    if len(pos) != 1 or "--reason" not in opts:
        print(USAGE, file=sys.stderr)
        return 2
    lr, err = abandon(pos[0], opts["--reason"], repo=opts.get("--repo"))
    if err:
        if opts.get("--json"):
            print(json.dumps({"abandoned": False, "reason": err},
                             ensure_ascii=False, indent=1))
        else:
            print("helm lr abandon: " + err, file=sys.stderr)
        return 1
    if opts.get("--json"):
        print(json.dumps(lr, ensure_ascii=False, indent=1))
        return 0
    print("helm lr: %s ABANDONED — LAND STATE UNKNOWN; WRITES OFF REVIEWED "
          "WORK: %s" % (lr["id"][:12], lr.get("abandon_reason") or "-"))
    return 0


def _cmd_annotate_delivered_report(rest):
    """Explicit correction path for historical cancelled BUILD rows."""
    from .cli import guard_tail
    if not rest or str(rest[0]).startswith("-"):
        print(USAGE, file=sys.stderr)
        return 2
    rid, tail = rest[0], rest[1:]
    rc = guard_tail(
        "helm lr annotate-delivered-report", tail, flags=("--json",),
        valued=("--artifact-ref", "--report-ref", "--evidence"), usage=USAGE)
    if rc is not None:
        return rc

    def val(flag):
        return tail[tail.index(flag) + 1] if flag in tail else None

    if any(val(flag) is None for flag in (
            "--artifact-ref", "--report-ref", "--evidence")):
        print("helm lr annotate-delivered-report: --artifact-ref REF, "
              "--report-ref CHAT_REF, and --evidence LINE are required (%s)"
              % USAGE, file=sys.stderr)
        return 2
    out, err = annotate_delivered_report(
        rid, val("--artifact-ref"), val("--report-ref"), val("--evidence"))
    if err:
        if "--json" in tail:
            print(json.dumps({"annotated": False, "reason": err},
                             ensure_ascii=False, indent=1))
        else:
            print("helm lr annotate-delivered-report: " + err, file=sys.stderr)
        return 1
    if "--json" in tail:
        print(json.dumps(out, ensure_ascii=False, indent=1))
    else:
        print("helm lr: %s historical cancellation annotated CLOSED "
              "(DELIVERED_REPORT)" % out["id"][:12])
    return 0


def _cmd_close(rest):
    """helm lr close <id> --reason R — the one terminal verb. Exit contract:
    0 recorded / idempotent, 1 refusal, 2 usage (missing/unknown --reason,
    flag-reason incoherence, superseded without --tip)."""
    from .cli import guard_tail, suggest
    if not rest or str(rest[0]).startswith("-"):
        print(USAGE, file=sys.stderr)
        return 2
    rid, tail = rest[0], rest[1:]
    rc = guard_tail("helm lr close", tail, flags=("--json", "--dry-run",
                                                  "--live"),
                    valued=("--reason", "--evidence", "--artifact-ref",
                            "--report-ref", "--tip", "--repo", "--trunk",
                            "--needs-restart"), usage=USAGE)
    if rc is not None:
        return rc

    def val(flag):
        return tail[tail.index(flag) + 1] if flag in tail else None

    reason, as_json, dry = val("--reason"), "--json" in tail, "--dry-run" in tail
    if reason is None:
        print("helm lr close: --reason is required — one of %s (%s)"
              % ("|".join(CLOSE_CLI_REASONS), USAGE), file=sys.stderr)
        return 2
    if reason not in CLOSE_CLI_REASONS:
        print("helm lr close: unknown --reason '%s'%s (%s)"
              % (reason, suggest(reason, CLOSE_CLI_REASONS), USAGE),
              file=sys.stderr)
        return 2
    if val("--tip") is not None and reason != "superseded":
        print("helm lr close: --tip belongs to --reason superseded (%s)"
              % USAGE, file=sys.stderr)
        return 2
    if reason == "superseded" and val("--tip") is None:
        print("helm lr close: --reason superseded requires --tip FULL_SHA "
              "(%s)" % USAGE, file=sys.stderr)
        return 2
    if (val("--repo") is not None or val("--trunk") is not None) \
            and reason not in ("landed", "stranded", "subsumed"):
        print("helm lr close: --repo/--trunk belong to --reason "
              "landed/stranded/subsumed (%s)" % USAGE,
              file=sys.stderr)
        return 2
    live, restart = "--live" in tail, val("--needs-restart")
    if (live or restart is not None) and reason != "landed":
        print("helm lr close: --live/--needs-restart belong to --reason "
              "landed — no other terminal claims a change reached the "
              "running fleet (%s)" % USAGE, file=sys.stderr)
        return 2
    refs = (val("--artifact-ref"), val("--report-ref"))
    if reason == "delivered-report" \
            and (not all(refs) or val("--evidence") is None):
        print("helm lr close: --reason delivered-report requires --artifact-ref "
              "REF, --report-ref CHAT_REF, and --evidence LINE (%s)" % USAGE,
              file=sys.stderr)
        return 2
    if reason != "delivered-report" and any(ref is not None for ref in refs):
        print("helm lr close: --artifact-ref/--report-ref belong to --reason "
              "delivered-report (%s)" % USAGE, file=sys.stderr)
        return 2
    out, err = close(rid, reason, evidence=val("--evidence"),
                     tip=val("--tip"), repo=val("--repo"),
                     trunk=val("--trunk"), dry_run=dry,
                     live=live, needs_restart=restart,
                     artifact_ref=val("--artifact-ref"),
                     report_ref=val("--report-ref"))
    if err:
        # D10c: a dry-run refusal prints the refusing rung's EXACT message
        # with the mirrored exit — and appends nothing, same as the live path.
        if as_json:
            print(json.dumps({"closed": False, "dry_run": dry, "reason": err},
                             ensure_ascii=False, indent=1))
        else:
            print("helm lr close%s: %s" % (" [dry-run]" if dry else "", err),
                  file=sys.stderr)
        return 1
    if as_json:
        print(json.dumps(out, ensure_ascii=False, indent=1))
        return 0
    if isinstance(out, dict) and out.get("dry_run"):
        print("helm lr close [dry-run]: %s WOULD close — --reason %s%s%s; "
              "nothing appended" % (
                  out["id"][:12], out["reason"],
                  " (proof %s)" % out["proof_mode"]
                  if out.get("proof_mode") else "",
                  " (%s)" % _delivery_text(
                      out.get("close_delivery_class"),
                      out.get("close_delivery_restart"))
                  if out.get("close_delivery_class") else ""))
        return 0
    if reason == "out-of-scope":
        print("helm lr: %s closed out-of-scope — cancelled: %s"
              % (out["id"][:12], out.get("cancel_reason") or ""))
        return 0
    label = "LANDED_REWRITTEN" if reason == "landed" and str(
        out.get("close_proof_mode") or "").startswith("translated-") \
        else reason.upper()
    print("helm lr: %s CLOSED (%s)%s" % (
        out["id"][:12], label,
        "  %s" % _delivery_phrase(out)
        if out.get("close_delivery_class") else ""))
    return 0


def _cmd_close_landed(rest):
    """Proof-gated terminal closure — deprecated alias of lr close."""
    print("helm lr close-landed is deprecated: use helm lr close --reason "
          "landed", file=sys.stderr)
    pos, opts, i = [], {}, 0
    while i < len(rest):
        arg = rest[i]
        if arg in ("--json", "--live"):
            if arg in opts:
                print(USAGE, file=sys.stderr)
                return 2
            opts[arg] = True
            i += 1
            continue
        if arg in ("--repo", "--trunk", "--needs-restart"):
            if arg in opts or i + 1 >= len(rest) \
                    or str(rest[i + 1]).startswith("--"):
                print(USAGE, file=sys.stderr)
                return 2
            opts[arg] = rest[i + 1]
            i += 2
            continue
        if str(arg).startswith("--"):
            print(USAGE, file=sys.stderr)
            return 2
        pos.append(arg)
        i += 1
    if len(pos) != 1 or "--trunk" not in opts:
        print(USAGE, file=sys.stderr)
        return 2
    lr, err = close_landed(pos[0], repo=opts.get("--repo"),
                           trunk=opts["--trunk"],
                           live=bool(opts.get("--live")),
                           needs_restart=opts.get("--needs-restart"))
    if err:
        if opts.get("--json"):
            print(json.dumps({"closed": False, "reason": err},
                             ensure_ascii=False, indent=1))
        else:
            print("helm lr close-landed: " + err, file=sys.stderr)
        return 1
    if opts.get("--json"):
        print(json.dumps(lr, ensure_ascii=False, indent=1))
        return 0
    print("helm lr: %s %s — CLOSED (%s) via %s at %s@%s" % (
        lr["id"][:12],
        "REVIEWED (UNDECLARED)" if lr.get("polarity") is None
        else lr["state"],
        _close_label(lr) if lr.get("close_reason") else "BY LANDING",
        lr.get("close_proof_mode") or lr.get("landing_proof_mode"),
        lr.get("closing_trunk_ref") or lr.get("landing_trunk_ref"),
        (lr.get("closing_trunk_sha") or lr.get("landing_trunk_sha")
         or "")[:12]))
    return 0


def _cmd_discharge(rest):
    """Proof-gated contrary reconciliation — deprecated alias of lr close."""
    print("helm lr discharge is deprecated: use helm lr close --reason "
          "superseded", file=sys.stderr)
    flags = [arg for arg in rest if str(arg).startswith("--")]
    pos = [arg for arg in rest if not str(arg).startswith("--")]
    if any(flag != "--json" for flag in flags) or len(flags) > 1 or len(pos) < 3:
        print(USAGE, file=sys.stderr)
        return 2
    lr, err = discharge(pos[0], pos[1], " ".join(pos[2:]))
    if err:
        if "--json" in flags:
            print(json.dumps({"discharged": False, "reason": err},
                             ensure_ascii=False, indent=1))
        else:
            print("helm lr discharge: " + err, file=sys.stderr)
        return 1
    if "--json" in flags:
        print(json.dumps(lr, ensure_ascii=False, indent=1))
        return 0
    print("helm lr: discharged contrary %s via approved tip %s" % (
        lr["id"][:12], (lr.get("superseding_tip") or "")[:12]))
    return 0


def _cmd_withdraw(rest):
    """Proof-gated do-not-land reconciliation — deprecated alias of lr close."""
    print("helm lr withdraw is deprecated: use helm lr close --reason "
          "withdrawn", file=sys.stderr)
    flags = [arg for arg in rest if str(arg).startswith("--")]
    pos = [arg for arg in rest if not str(arg).startswith("--")]
    if any(flag != "--json" for flag in flags) or len(flags) > 1 or len(pos) < 2:
        print(USAGE, file=sys.stderr)
        return 2
    lr, err = withdraw(pos[0], " ".join(pos[1:]))
    if err:
        if "--json" in flags:
            print(json.dumps({"withdrawn": False, "reason": err},
                             ensure_ascii=False, indent=1))
        else:
            print("helm lr withdraw: " + err, file=sys.stderr)
        return 1
    if "--json" in flags:
        print(json.dumps(lr, ensure_ascii=False, indent=1))
        return 0
    print("helm lr: withdrew %s — the verdict stands, the lane is abandoned" %
          lr["id"][:12])
    return 0


def _commit_content_id(root, sha):
    """Content identity for ONE commit as (patch_id, payload_digest).

    patch_id is git's own per-commit `show | patch-id --stable` — the same
    instrument `git cherry` uses — so mode changes on different paths,
    deletion variants, renames and binary edits all discriminate (codex-2's
    r3 collision family, probed pairwise: mode-f != mode-g, del-f !=
    del-empty, bin1 != bin2). It is the PRIMARY answer.

    The digest is the secondary, and it exists because patch-id hashes
    CONTEXT lines: the same changed text picked onto a tree whose
    neighbourhood moved earns a different patch-id (the batch-2
    moved-target eviction — an innocent lane accused by an instrument that
    could not tell its content from its context). The digest covers the
    per-file payload with the hunk-header line-number fields and the
    `index`/`diff --git` blob-addressing lines stripped: every +/- content
    line hashed under ITS OWN file's path, every metadata line (mode,
    deletion, rename, binary-ness) likewise. It stays stable across exactly
    the context shift patch-id flinches at, while still binding path,
    payload and patch shape.

    TWO BINDINGS ARE MEASURED, NOT CLAIMED (@helm-claude-2's r4 attack, both
    reproduced live against lane tips before the cure). (a) `path` is
    PER-FILE state and must reset on every `diff --git` header: held across
    files, a multi-file commit attributes file N's payload to file 1, and
    two commits deleting DIFFERENT same-content files hashed byte-identical
    — pid differed, digest agreed, and one instrument's collision is all a
    launder needs. (b) a binary edit's digest binds its PATH and its
    binary-ness, never its content — `git show` without --binary emits no
    payload, and there is nothing to hash; pid remains the only
    content-discriminator for binaries, and unchanged-content binary
    carries are exactly as measurable as unchanged-content text carries.

    CARRY = patch-ids equal, OR digests equal (the documented context-shift
    rescue). Neither alone is trusted: patch-id alone evicts the innocent;
    digest alone would forgive a hunk-positioned change that patch-id
    catches. True empty commits ("EMPTY") are comparable, never None —
    compose treats None as REFUSAL, never agreement.
    """
    be = vcs.backend(root)
    import hashlib
    rc, raw, _err = be.run(root, "-c", "diff.noprefix=false",
                           "show", "--format=", sha)
    if rc != 0:
        return None
    body = raw if isinstance(raw, bytes) else raw.encode("utf-8", "replace")
    if not body.strip():
        return ("EMPTY", "EMPTY")
    rc, out, _err = be.text(root, "patch-id", "--stable", stdin=body)
    pid = out.split()[0] if rc == 0 and out.split() else None
    if not pid:
        return None                      # a diff git itself cannot hash
    # The secondary: sha256 over (path, +/-/meta line) pairs, headers and
    # context dropped — the payload axis that survives a context shift.
    parts = []
    path = None
    for ln in body.split(b"\n"):
        if ln.startswith(b"diff --git "):
            path = None                 # per-file state, not per-diff
            continue
        if ln.startswith(b"+++ "):
            tgt = ln[4:]
            if tgt != b"/dev/null":
                path = tgt[2:] if tgt.startswith(b"b/") else tgt
            continue
        if ln.startswith(b"--- "):
            srcf = ln[4:]
            if path is None and srcf.startswith(b"a/"):
                path = srcf[2:]
            continue
        if ln.startswith((b"deleted file mode", b"new file mode",
                          b"old mode", b"new mode", b"similarity index",
                          b"rename from", b"rename to", b"Binary files",
                          b"GIT binary patch")):
            parts.append(b"META\x00" + (path or b"?") + b"\x00" + ln)
            continue
        if ln[:1] in (b"+", b"-"):
            parts.append((path or b"?") + b"\x00" + ln)
    digest = hashlib.sha256(b"\n".join(parts)).hexdigest() if parts else None
    return (pid, digest)


def _range_patch_id(root, base, tip):
    """git patch-id --stable of base..tip through the CHECKOUT (`git -C`)
    substrate — one instrument for BOTH sides of compose's carry question, so
    approved-vs-composed equality is never two tools agreeing by accident.

    None on any failure, and compose treats None as REFUSAL, never agreement:
    two unmeasurable sides "matching" is the vacuous-oracle shape, not a
    carry."""
    be = vcs.backend(root)
    # the diff stays BYTES end to end: `run` -> `stdin` is the seam's own
    # patch-id path (vcs.run docstring), and fsdecode+strip could alter the
    # exact stream the id is supposed to bind
    # -c pins the DIFF SHAPE the id hashes: diff.noprefix=true yields a
    # DIFFERENT patch-id for identical content (measured 2026-08-05 on one
    # commit — two distinct ids, same bytes), and these ids are EXPORTED —
    # manifest rows and receipts cross boxes where user config is not ours
    # to assume.
    rc, raw, _err = be.run(root, "-c", "diff.noprefix=false",
                           "diff", base + ".." + tip)
    if rc != 0 or not raw:
        return None
    rc, out, _err = be.text(root, "patch-id", "--stable", stdin=raw)
    parts = out.split() if rc == 0 else []
    return parts[0] if parts else None


def _cmd_compose(rest):
    """helm lr compose <id> [<id>...] — stand N APPROVED lanes on ONE composed
    tip, measuring per member that the approved CONTENT is what composed
    (the merge-queue owner directive, lane merge-queue-adopt-the-lazy-hybrid).

    THE EVIDENCE SPLIT IT MECHANIZES: an APPROVE binds CONTENT (patch-id), a
    gate binds a TREE. Compose re-measures each member's patch-id across the
    cherry-pick and REFUSES on conflict or drift, NAMING the member — "the
    batch is bad" is not an actionable verdict. It runs NO suite: the one
    gate the caller then runs on the composed tip is the only per-tree
    evidence there is. File-disjointness predicts a clean compose and
    licenses nothing about evidence.

    The room is a detached worktree under `<repo>-wt/compose/` — the peek
    container's shape, but WRITABLE and left standing on success, because its
    HEAD is what keeps the composed tip reachable until the integrator gates
    and lands it (no branch is minted: the shared-checkout guard reserves
    branch creation for the integrator). --dry-run composes, measures,
    reports, and removes the room. On a later red gate, localization is by
    PREFIX: re-run compose with the first K members — the longest green
    prefix lands, the first red member is evicted to its own re-gate."""
    pos, opts, i = [], {}, 0
    while i < len(rest):
        arg = rest[i]
        if arg in ("--dry-run", "--json", "--stop-on-first"):
            if arg in opts:
                print(USAGE, file=sys.stderr)
                return 2
            opts[arg] = True
            i += 1
            continue
        if arg in ("--trunk", "--repo"):
            if arg in opts or i + 1 >= len(rest) \
                    or str(rest[i + 1]).startswith("--"):
                print(USAGE, file=sys.stderr)
                return 2
            opts[arg] = rest[i + 1]
            i += 2
            continue
        if str(arg).startswith("--"):
            print(USAGE, file=sys.stderr)
            return 2
        pos.append(arg)
        i += 1
    if not pos:
        print(USAGE, file=sys.stderr)
        return 2

    def refuse_batch(msg, members=None, room_kept=None):
        """The all-excluded / failed-batch report. hc2's FIX on the reviewed tip:
        four early returns printed prose to stderr and returned BEFORE the
        --json branch, so a scripted caller got empty stdout and a
        JSONDecodeError — the silent-empty disease cured for the human and
        preserved for the machine. rc stays 1 (a failed batch is a failed
        batch); the account is parseable in BOTH modes."""
        if "--json" in opts:
            print(json.dumps({"composed_tip": None, "refused": msg,
                              "members": members or [],
                              "excluded": excluded,
                              "room": room_kept, "dry_run": "--dry-run" in opts},
                             ensure_ascii=False, indent=1))
        else:
            print("helm lr compose: " + msg, file=sys.stderr)
            for x in excluded:
                print("  EXCLUDED %s (%s): %s" % (x["id"][:12], x["lane"],
                                                  x["reason"]),
                      file=sys.stderr)
        return 1
    # ONE projection for every id: get() pays the whole-ledger hydration per
    # call (measured 2026-08-05, 164s; cured the same night by the lr-show-hydrates-the-whole-ledger lane), so an N-member
    # compose must never multiply it.
    lrs, unavailable = project()
    if unavailable:
        print("helm lr: " + unavailable, file=sys.stderr)
        return 1
    picks = []
    excluded = []
    for rid in pos:
        lr, err = dispatches._resolve_row(lrs, rid, noun="land request",
                                          list_hint="helm lr list")
        if err:
            excluded.append({"id": rid, "lane": "?", "unresolvable": True,
                             "reason": err})
            continue
        # THE PROJECTION'S OWN WORD, never raw polarity: a gate-capable
        # writer's untokened approve projects REVIEWED, not READY, and raw
        # polarity=approve would compose it anyway (codex FIX, the
        # compose-verb-slice-1-review chain). READY is the door's authorization word; the reader
        # rungs (ready_word) are printed as caution, not re-judged here —
        # the land door still guards the land.
        if str(lr.get("state")) != "READY":
            excluded.append({"id": lr["id"], "lane": lr.get("lane") or "?",
                             "reason": "state is %s — compose takes only "
                                       "READY rows (an authorizing approve, "
                                       "per the projection)"
                                       % (lr.get("state") or "UNKNOWN")})
            continue
        word = ready_word(lr)
        if word != "READY":
            print("helm lr compose: caution — %s is %s; composing anyway, "
                  "the land door verifies the gate leg"
                  % (lr["id"][:12], word), file=sys.stderr)
        tip = lr.get("reviewed_tip") or lr.get("review_sha")
        if not tip:
            excluded.append({"id": lr["id"], "lane": lr.get("lane") or "?",
                             "reason": "has no reviewed tip to compose"})
            continue
        picks.append({"lr": lr, "tip": tip})
    bindings = set(p["lr"].get("repo_id") for p in picks
                   if p["lr"].get("repo_id"))
    if len(bindings) > 1:
        print("helm lr compose: members bind DIFFERENT repos (%s) — one "
              "composition, one repository" % ", ".join(sorted(bindings)),
              file=sys.stderr)
        return 1
    if not picks:
        return refuse_batch("no member is composable — every row refused "
                            "at admission")
    bound = bindings.pop() if bindings else None
    root = opts.get("--repo") or (
        bound[:-5] if isinstance(bound, str) and bound.endswith("/.git")
        else bound)
    if not root or not os.path.isdir(root):
        print("helm lr compose: no usable repository (row binding %r; "
              "--repo overrides)" % (bound,), file=sys.stderr)
        return 1
    be = vcs.backend(root)
    ref = opts.get("--trunk")
    if not ref:
        for cand in UPSTREAM_TRUNK + LOCAL_TRUNK:
            rc, out, _e = be.text(root, "rev-parse", "--verify", "--quiet",
                                  cand)
            if rc == 0 and out:
                ref = cand
                break
    if not ref:
        print("helm lr compose: trunk unresolvable in %s (--trunk names one)"
              % root, file=sys.stderr)
        return 1
    rc, out, _e = be.text(root, "rev-parse", ref)
    if rc != 0 or not out:
        print("helm lr compose: trunk ref %s unreadable" % ref,
              file=sys.stderr)
        return 1
    trunk_sha = out
    for p in list(picks):
        def drop(reason):
            excluded.append({"id": p["lr"]["id"],
                             "lane": p["lr"].get("lane") or "?",
                             "reason": reason})
            picks.remove(p)
        rc, _o, _e = be.text(root, "cat-file", "-e", p["tip"] + "^{commit}")
        if rc != 0:
            drop("reviewed tip %s is not readable here" % p["tip"][:12])
            continue
        rc, out, _e = be.text(root, "merge-base", trunk_sha, p["tip"])
        if rc != 0 or not out:
            drop("tip %s shares no history with %s" % (p["tip"][:12], ref))
            continue
        if out == p["tip"]:
            drop("tip %s is already contained in %s — close it, do not "
                 "compose it" % (p["tip"][:12], ref))
            continue
        p["mb"] = out
        rc, out, _e = be.text(root, "rev-list", p["mb"] + ".." + p["tip"])
        if rc != 0 or not out:
            drop("approved range is unreadable — nothing to carry")
            continue
        p["range_shas"] = out.split()
        p["approved_ids"] = [_commit_content_id(root, s)
                             for s in p["range_shas"]]
        if not all(p["approved_ids"]):
            drop("a commit in the approved range has an unmeasurable "
                 "content id — nothing to carry")
            continue
        p["approved"] = _range_patch_id(root, p["mb"], p["tip"])
    if not picks:
        return refuse_batch("no member is composable — every row refused "
                            "at pre-flight")
    # A REBASED land leaves a row whose tip is NO trunk ancestor while its
    # CONTENT is on trunk — ancestry stays silent and the cherry-pick then
    # mis-frames ledger residue as a live conflict (measured on the very
    # first live run: gateroute, landed rebased minutes earlier). Patch
    # identity is the discriminator; the rung acts only on a POSITIVE hit —
    # an unreadable or capped index proves nothing and falls through to the
    # pick, whose conflict refusal still backstops.
    gd = bound if isinstance(bound, str) and bound.endswith("/.git") \
        else root.rstrip(os.sep) + os.sep + ".git"
    index, capped = _stored_patch_index(gd, trunk_sha)

    def range_commit_pids(p):
        """{sha: patch-id} for the member's own range, streamed in two
        spawns — the SCREEN must speak the index's per-commit shape: a
        multi-commit lane rebased in lands as N trunk commits with N
        per-commit ids, and the aggregate id matches none of them (codex
        FIX: the screen missed exactly the class it was built for)."""
        rc, out, _e = be.text(root, "rev-list", p["mb"] + ".." + p["tip"])
        if rc != 0 or not out:
            return None
        shas = out.split()
        rc, raw, _e = be.run(root, "-c", "diff.noprefix=false", "log", "-p",
                             p["mb"] + ".." + p["tip"])
        if rc != 0 or not raw:
            return None
        rc, ids, _e = be.text(root, "patch-id", "--stable", stdin=raw)
        if rc != 0 or not ids:
            return None
        got = {}
        for line in ids.splitlines():
            parts = line.split()
            if len(parts) == 2:
                got[parts[1]] = parts[0]
        return got if len(got) == len(shas) else None
    # A skipped screen must stay VISIBLE: unreadable-or-capped means an older
    # rebased land is NOT ruled out, and the conflict refusal below would
    # then mis-frame residue with full confidence — so that refusal carries
    # the screen's status instead of inheriting its silence.
    screen = "unreadable" if index is None else (
        "capped at %d commits" % STORED_PID_SCAN_CAP if capped else "")
    for p in list(picks):
        pids = range_commit_pids(p) if index is not None else None
        if pids is None and index is not None and not screen:
            screen = "member range unreadable"
        if pids and len(set(pids.values())) < len(pids):
            # two range commits with one patch-id (revert/reapply, duplicated
            # cherry-pick): the trunk index maps each id to ONE sha, so both
            # copies would "hit" the same commit and ALREADY ON would
            # overclaim — the screen declines and says why (codex FIX r2)
            pids = None
            if not screen:
                screen = "duplicate patch-ids in member range"
        landed = [(index or {}).get(pid) for pid in (pids or {}).values()]
        if pids and all(landed):
            excluded.append({"id": p["lr"]["id"],
                             "lane": p["lr"].get("lane") or "?",
                             "reason": "content is ALREADY ON %s (%d commit%s "
                                       "by patch-identity; its tip %s was "
                                       "rebased in) — close it, do not "
                                       "compose it"
                                       % (ref, len(landed),
                                          "" if len(landed) == 1 else "s",
                                          p["tip"][:12])})
            picks.remove(p)
            continue
        if pids and any(landed):
            hits = sum(1 for x in landed if x)
            if capped:
                excluded.append({"id": p["lr"]["id"],
                                 "lane": p["lr"].get("lane") or "?",
                                 "reason": "%d of %d commits are provably on "
                                           "%s by patch-identity and the rest "
                                           "are UNPROVABLE (screen capped at "
                                           "%d commits) — an older land is "
                                           "not ruled out; resolve before "
                                           "composing"
                                           % (hits, len(landed), ref,
                                              STORED_PID_SCAN_CAP)})
                picks.remove(p)
                continue
            excluded.append({"id": p["lr"]["id"],
                             "lane": p["lr"].get("lane") or "?",
                             "reason": "PARTIALLY on %s — %d of %d commits "
                                       "match trunk by patch-identity; that "
                                       "state must keep its branch — resolve "
                                       "the split before composing"
                                       % (ref, hits, len(landed))})
            picks.remove(p)
            continue
    if not picks:
        return refuse_batch("no member is composable — every row was "
                            "excluded")
    box = root.rstrip(os.sep) + "-wt" + os.sep + "compose"
    room = os.path.join(box, "+".join(p["lr"]["id"][:4] for p in picks))
    if os.path.exists(room):
        print("helm lr compose: room %s already exists — land it or remove "
              "it first (its HEAD may be the only anchor of a prior "
              "composition)" % room, file=sys.stderr)
        return 1
    os.makedirs(box, exist_ok=True)
    # The shared-checkout ref guard refuses worktree/HEAD creation from
    # anything but the sanctioned creator (measured live: `worktree add`
    # REFUSED, "ref updates aborted by hook"). Compose IS integration
    # staging, so it hands the guard that bit the way vcs.run documents —
    # scoped to this one call, never leaked into the process.
    sanction = {"HELM_WORK_INTEGRATOR": "1"}
    rc, _o, err = be.text(root, "worktree", "add", "--detach", room,
                          trunk_sha, env=sanction)
    if rc != 0:
        print("helm lr compose: cannot mint the compose room: %s"
              % (err or "worktree add failed"), file=sys.stderr)
        return 1

    def scrap(msg):
        # --abort only when a pick is actually IN PROGRESS: scrap also fires
        # on post-pick failures (drift, unreadable HEAD) where --abort has
        # nothing to abort and its rc says nothing about the room — the
        # unconditional form stamped 'abort failed / room may hold' onto
        # refusals whose room was already clean (codex FIX r3, reproduced
        # on the drift refusal's own output)
        rc, _o, _e = be.text(room, "rev-parse", "-q", "--verify",
                             "CHERRY_PICK_HEAD")
        abort_err = None
        if rc == 0:
            rc, _o, err = be.text(room, "cherry-pick", "--abort")
            if rc != 0:
                abort_err = ((err or "no diagnostic").splitlines()[0]
                             if err else "no diagnostic")
        rc, _o, err = be.text(root, "worktree", "remove", "--force", room,
                              env=sanction)
        # The message derives from the FINAL state, never an intermediate
        # one: an abort failure followed by a successful forced removal
        # leaves NO room, and saying 'may hold' over an absent room is the
        # r3 defect one path over (codex FIX r4).
        if rc != 0:
            msg += (" — %sthe room resisted removal (%s); %s may hold a "
                    "conflicted half-pick, remove it by hand"
                    % ("cherry-pick --abort failed (%s) AND " % abort_err
                       if abort_err else "AND ",
                       (err or "no diagnostic").splitlines()[0]
                       if err else "no diagnostic", room))
        elif abort_err:
            msg += (" — cherry-pick --abort failed (%s) but the forced "
                    "removal succeeded; no room remains" % abort_err)
        # scrap carries the cleanup-halt now (hc2 r2), so it owes the same
        # parseable account as every other refusal — a scripted integrator
        # watching --json must see the room-integrity stop, not empty
        # stdout on the one failure that most needs parsing.
        if "--json" in opts:
            print(json.dumps({"composed_tip": None, "refused": msg,
                              "members": members, "excluded": excluded,
                              # hc2 r3: derive from the SAME rc the message
                              # branched on — a resisted removal tells the
                              # human "the room may hold" and must not tell
                              # the machine there is no room.
                              "room": room if rc != 0 else None,
                              "dry_run": "--dry-run" in opts},
                             ensure_ascii=False, indent=1))
        else:
            print("helm lr compose: " + msg, file=sys.stderr)
        return 1

    members, head = [], trunk_sha
    stop_first = "--stop-on-first" in opts

    def evict(p, reason):
        """Exclude one member mid-compose and roll the room back to the last
        good head, so LATER MEMBERS STILL GET A VERDICT (task/165, owner:
        'forloop over open lanes'). The default is best-effort at any N — a
        batch that reports on 2 of 8 reads as a batch of 2 at 2am, which is
        the silent-exclusion failure this verb exists to end. Fail-fast is
        the opt-in (--stop-on-first), never the default.

        EVERY cleanup step's rc is checked (hc2 FIX on the reviewed tip): the first
        cut checked --abort and discarded reset/clean — so a failed reset
        left member A's residue in the room, member B's pick then failed
        against THAT, and B was evicted naming B's OWN files as the
        conflict. A cleanup failure stops the batch with the room state
        UNKNOWN rather than letting later verdicts measure a dirty room."""
        note = ""
        rc, _o, _e = be.text(room, "rev-parse", "-q", "--verify",
                             "CHERRY_PICK_HEAD")
        if rc == 0:
            rc, _o, err = be.text(room, "cherry-pick", "--abort")
            if rc != 0:
                note = (" [cleanup: cherry-pick --abort failed (%s); "
                        "reset --hard continued over it]"
                        % ((err or "no diagnostic").splitlines()[0]
                           if err else "no diagnostic"))
        rc, _o, err = be.text(room, "reset", "--hard", "-q", head)
        if rc != 0:
            return ("reset --hard to the last good head FAILED (%s) — the "
                    "room's state is UNKNOWN and later verdicts would "
                    "measure residue, so the batch stops here"
                    % ((err or "no diagnostic").splitlines()[0]
                       if err else "no diagnostic"))
        rc, _o, err = be.text(room, "clean", "-fdq")
        if rc != 0:
            return ("git clean FAILED (%s) — the room's state is UNKNOWN "
                    "and later verdicts would measure residue, so the batch "
                    "stops here"
                    % ((err or "no diagnostic").splitlines()[0]
                       if err else "no diagnostic"))
        excluded.append({"id": p["lr"]["id"],
                         "lane": p["lr"].get("lane") or "?",
                         "reason": reason + note})
        return None

    for p in list(picks):
        lane = p["lr"].get("lane") or "?"
        rc, _o, err = be.text(room, "cherry-pick", p["mb"] + ".." + p["tip"],
                              timeout=120)
        if rc != 0:
            rc2, out, _e = be.text(room, "diff", "--name-only",
                                   "--diff-filter=U")
            doubt = " (patch-identity screen %s — an older rebased " \
                    "land is not ruled out)" % screen if screen else ""
            if rc2 == 0 and out:
                why = ("conflict in %s at %s — evict or reorder it%s"
                       % (", ".join(out.split()), head[:12], doubt))
            else:
                # No unmerged paths: the pick stopped for a NON-conflict
                # reason (an empty pick — content already present — or an
                # interrupted one). Naming it a conflict would assert a
                # cause the state refutes; UNKNOWN stays UNKNOWN (codex FIX).
                why = ("pick stopped at %s WITHOUT conflicts — an empty pick "
                       "(content already present?) or an interrupted one; "
                       "state UNKNOWN%s: %s"
                       % (head[:12], doubt,
                          (err or "no git diagnostic").splitlines()[0]))
            fatal = evict(p, why)
            if fatal:
                return scrap(fatal)
            if stop_first:
                return scrap("member %s (%s) failed and --stop-on-first was "
                             "given: %s"
                             % (p["lr"]["id"][:12], lane,
                                excluded[-1]["reason"]))
            continue
        rc, out, _e = be.text(room, "rev-parse", "HEAD")
        if rc != 0 or not out:
            fatal = evict(p, "composed HEAD unreadable after its pick")
            if fatal:
                return scrap(fatal)
            if stop_first:
                return scrap("composed HEAD unreadable after %s%s"
                             % (lane, excluded[-1]["reason"].replace(
                                 "composed HEAD unreadable after its pick",
                                 "")))
            continue
        after = out
        # THE CARRY CHECK IS PER-COMMIT AND CONTEXT-FREE (the batch-2
        # moved-target eviction): patch-id hashes context lines, so the
        # aggregate — and even a per-commit patch-id — drifts whenever an
        # earlier member moved the same region, and the eviction accused
        # an innocent lane with a claim the instrument could not support.
        # An APPROVE binds what each commit DOES; the +/- lines are that.
        rc, out, _e = be.text(room, "rev-list", head + ".." + after)
        if rc != 0 or not out:
            fatal = evict(p, "composed range unreadable after its pick")
            if fatal:
                return scrap(fatal)
            continue
        new_shas = out.split()
        if len(new_shas) != len(p["range_shas"]):
            why = ("the pick produced %d commit%s from an approved range of "
                   "%d — a squash, split, or empty-drop the reviewer never "
                   "saw" % (len(new_shas), "" if len(new_shas) == 1 else "s",
                            len(p["range_shas"])))
            fatal = evict(p, why)
            if fatal:
                return scrap(fatal)
            continue
        drift = None
        for orig_sha, new_sha, orig_id in zip(p["range_shas"], new_shas,
                                              p["approved_ids"]):
            new_id = _commit_content_id(room, new_sha)
            if not new_id:
                drift = "%s: composed content unmeasurable" % new_sha[:12]
                break
            # (patch_id, digest): the primary carries every discrimination
            # git itself makes; the digest rescues exactly the context shift
            # patch-id flinches at. BOTH differing is real drift.
            if new_id[0] != orig_id[0] and new_id[1] != orig_id[1]:
                drift = ("%s -> %s: content drift — the APPROVE binds this "
                         "commit's +/- lines and they are not what composed"
                         % (orig_sha[:12], new_sha[:12]))
                break
        if drift:
            fatal = evict(p, drift + "; re-review the delta rather than "
                            "composing it")
            if fatal:
                return scrap(fatal)
            if stop_first:
                return scrap("%s (%s): %s"
                             % (p["lr"]["id"][:12], lane,
                                excluded[-1]["reason"]))
            continue
        members.append({"id": p["lr"]["id"], "lane": lane,
                        "approved_tip": p["tip"],
                        "patch_id": p["approved"],
                        "carries": True})
        head = after
    dry = "--dry-run" in opts
    manifest = {"composed_tip": head, "trunk": {"ref": ref, "sha": trunk_sha},
                "room": None if dry else room, "members": members,
                "excluded": excluded, "dry_run": dry}
    if not members:
        rc, _o, _e = be.text(root, "worktree", "remove", "--force", room,
                             env=sanction)
        return refuse_batch("NO member composed — the batch stands empty; "
                            "every exclusion listed")
    if dry:
        rc, _o, err = be.text(root, "worktree", "remove", "--force", room,
                              env=sanction)
        if rc != 0:
            # a dry run that left state broke its own contract — say so and
            # fail, never return 0 over a registered room (codex FIX)
            print("helm lr compose: DRY-RUN could not remove its room (%s) "
                  "— %s is still registered; remove it by hand"
                  % ((err or "no diagnostic").splitlines()[0] if err
                     else "no diagnostic", room), file=sys.stderr)
            return 1
    if not dry:
        # The manifest lives BESIDE the room, never inside it (task/228,
        # measured on the first live batch): an untracked compose-manifest
        # in the room makes the room dirty at gate time, and fab snapshots
        # untracked files — the receipt would bind a tree NO COMMIT HAS,
        # and the fold then refuses the batch for what reads like
        # corruption, after the slot was spent.
        try:
            with open(room + ".manifest.json", "w", encoding="utf-8") as f:
                json.dump(manifest, f, ensure_ascii=False, indent=1)
        except OSError as exc:
            print("helm lr compose: composition stands at %s but its "
                  "manifest could not be written (%s) — the room is KEPT "
                  "(it anchors the tip); rerun or write the manifest by "
                  "hand" % (room, exc), file=sys.stderr)
            return 1
    if "--json" in opts:
        print(json.dumps(manifest, ensure_ascii=False, indent=1))
        return 0
    stood = "DRY — would stand" if dry else "standing"
    print("helm lr compose: %d member%s %s at %s (onto %s @ %s)%s"
          % (len(members), "" if len(members) == 1 else "s", stood, head[:12],
             ref, trunk_sha[:12],
             (", %d excluded" % len(excluded)) if excluded else ""))
    for m in members:
        print("  %s  %s  tip %s  patch-id %s  CARRIES"
              % (m["id"][:12], m["lane"], m["approved_tip"][:12],
                 m["patch_id"][:12]))
    for x in excluded:
        print("  EXCLUDED %s (%s): %s" % (x["id"][:12], x["lane"],
                                          x["reason"]))
    if not dry:
        print("  room %s — its HEAD anchors the composed tip; keep it until "
              "the land" % room)
        print("  next: gate the composed tip; on green, merge it and run "
              "`helm lr land <id>` per member (the receipt RECORDS the "
              "carried patch-id — diagnostic and fail-open; Git stays the "
              "lifecycle authority); on red, localize by prefix: compose "
              "the first K members, longest green prefix lands, first red "
              "member evicts to its own re-gate")
        # SLICE 3 (task/165): the post-land leg is per-member handwork no
        # longer — the manifest already names who composed, so the verb
        # prints the exact commands. The merge and the closes still belong
        # to the integrator; this is the list, not the act.
        print("  on green, per member, in order:")
        for m in members:
            print("    helm lr land %s   # %s (patch-id %s)"
                  % (m["id"][:12], m["lane"], m["patch_id"][:12]))
        for x in excluded:
            if x.get("unresolvable"):
                print("    # excluded: %s (unresolvable row token) — %s"
                      % (x["id"][:12], x["reason"][:80]))
            else:
                print("    # excluded: helm lr show %s   # %s — %s"
                      % (x["id"][:12], x["lane"], x["reason"][:80]))
    return 0


def _cmd_land(rest):
    """helm lr land <id> — emit a signed helm.land candidate at integration and
    write through its local diagnostic receipt. FAIL-OPEN: a missing signer/node
    never fails the land; Git remains the lifecycle authority.

    THE VERB SPLIT (D5): `lr land` is the AT-INTEGRATION receipt verb — run at
    the moment the merge exists locally, pre-push, where its fail-open witness
    and fail-CLOSED deletion guard both mean something. `lr close --reason
    landed` is the separate POST-OBSERVATION terminal, recorded once the land
    is provable on the named trunk. Neither aliases, deprecates, or absorbs
    the other: aliasing land onto close would flip exit 0->1 in this verb's
    primary window (a local merge not yet pushed reads absent under the
    upstream trio), and the deletion guard is hollow post-merge."""
    from .cli import guard_tail
    if not rest:
        print(USAGE, file=sys.stderr)
        return 2
    rid = rest[0]
    # --ack-deletions MUST be in this tuple: the deletion refusal at the
    # bottom of this function instructs the operator to pass it, and for as
    # long as it was missing here guard_tail returned rc 2 on the very flag
    # the refusal demanded — every deletion-bearing merge was unwitnessable
    # from the CLI, refusal (1) without the flag and usage-error (2) with it.
    rc = guard_tail("helm lr land", rest[1:],
                    flags=("--json", "--ack-deletions"), usage=USAGE)
    if rc is not None:
        return rc
    lr, err = get(rid)
    if err:
        print("helm lr: " + err, file=sys.stderr)
        return 1
    gitdir = lr.get("repo_id")
    reviewed_tip = lr.get("reviewed_tip") or lr.get("review_sha")
    as_json = "--json" in rest[1:]
    if not gitdir or not reviewed_tip:
        msg = "%s has no repo binding / reviewed tip to record a receipt for" \
            % lr["id"]
        if as_json:
            print(json.dumps({"recorded": False, "receipt": None,
                              "reason": msg}, ensure_ascii=False, indent=1))
            return 0
        print("helm lr land: " + msg, file=sys.stderr)
        return 1
    ack_dels = "--ack-deletions" in rest[1:]
    trunk_ref = _resolve_ref(gitdir, LOCAL_TRUNK)
    # Deletion guard: a merge that deletes TRACKED files from a remote is
    # irreversible. The quality gate asks "is this merge SAFE"; this asks
    # "am I AUTHORIZED to delete." UNKNOWN fails SAFE: if the deletion set
    # cannot be computed, REFUSE — an unscannable deletion is not a known-
    # safe one. The flag is tedious on purpose: it should be deliberate,
    # not the default next flag after --json.
    del_hits, del_err = _tracked_deletions(gitdir, trunk_ref, reviewed_tip)
    if del_err:
        print("helm lr land: " + del_err, file=sys.stderr)
        return 1
    if del_hits and not ack_dels:
        print(_deletion_refusal(del_hits), file=sys.stderr)
        return 1
    up_ref = _resolve_ref(gitdir, UPSTREAM_TRUNK)
    # The actual trunk for the historical event identity, sampled at integration.
    # Upstream fields remain diagnostic snapshots outside the payload and replay
    # never consults them for lifecycle state.
    trunk_sha = _trunk_sha(gitdir, trunk_ref)
    rec, why = record_land(
        lr["lane"], lr["branch"], reviewed_tip,
        _patch_id(gitdir, reviewed_tip, trunk_ref), trunk_sha, repo_id=gitdir,
        has_upstream=bool(up_ref),
        upstream=_ancestry(gitdir, trunk_sha, up_ref) == ANCESTOR)
    if as_json:
        print(json.dumps({"recorded": bool(rec), "receipt": rec, "reason": why},
                         ensure_ascii=False, indent=1))
        return 0
    # BOTH LINES NAME THE NON-ACTION, because this verb's NAME is an
    # imperative and its job is not. `helm lr land` emits a RECEIPT; the merge
    # is the integrator's own git work, and nothing here moves a ref.
    #
    # The old wording made that invisible in both directions. Success read
    # "recorded ... land candidate", failure read "land not blocked" — and an
    # integrator scanning output sees the word `land` next to an exit 0 and
    # reasonably concludes the lane landed. Measured 2026-07-30: I ran this
    # against an approved row, got exit 0 and one benign line, and only caught
    # it because I checked origin/main afterwards and found it unmoved,
    # ancestry NO, row still landed=False. The ledger DOES refuse to agree
    # (it derives landedness from live Git, so the row stays READY) — but
    # nothing TELLS the caller, so the gap between "this printed fine" and
    # "nothing happened" is exactly one habit wide.
    if rec:
        print("helm lr: receipt recorded for %s (chain %s, turn %s) — NO MERGE "
              "PERFORMED; run the merge yourself, this verb only witnesses it"
              % (lr["id"][:12], rec["chain"], (rec["turn"] or "")[:12]))
        return 0
    # "fail-open" is kept VERBATIM: it is a real documented property (a missing
    # signer must never block a land) and dropping it to make room for the
    # non-action would trade one true thing for another.
    print("helm lr: NO MERGE PERFORMED and no receipt written (%s) — fail-open, "
          "the receipt is optional and its absence blocks nothing. This verb "
          "never merges; it witnesses a land you perform in git, and the row "
          "stays unlanded until that merge reaches trunk." % why)
    return 0
