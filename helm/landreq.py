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
HAPPENED, never from its having said YES. On 2026-07-25 that put five loops in
READY for over a day carrying SUPERSEDED, SUPERSEDED, "FIX (3 blockers)",
"helm#210 FIX" and "helm#217 FIX" — not one an approval — and `stalls` billed
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
import contextlib
import functools
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata

from . import (carriageckpt, dispatches, foldcheck, foldcompose, home, pk,
               projscope, query, vcs)
from .seats_common import recipient_matches
from .store import load as store_load
from .work import _lanes

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

    Both earlier designs failed on the same axis, and a probe measured both. A
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
    # function exists to make. A probe found the shape by grammar-fuzzing the
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
    stamped nothing — measured 2026-08-02: nine live worktrees predated the
    gate_caps writer, five of them SEAT HOMES, and `./bin/helm` resolves its
    package from its own realpath, so a reviewer inside a stale home ran the
    stale writer without knowing. 34 post-epoch verdicts carry no stamp and
    every one came from the four seats with stale homes. UNREADABLE means the
    field is there and corrupt — a genuine ledger repair.

    Telling an integrator to "repair the ledger row" when the row is intact
    and the defect is three days upstream costs a whole diagnosis; it cost
    exactly one, which is why this split exists (#111/#119)."""
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


# THE FIVE SENTENCES, IN ONE PLACE, BECAUSE THE SENTENCES ARE THE DELIVERABLE.
#
# WHO READS THESE. An integrator at 5am deciding whether to fold. The only
# question they are actually asking is "is this row worth waiting for, or is
# something broken that I have to go fix?" — and one wrong word there costs a
# whole diagnosis. It cost one already: an earlier cut printed "heals on
# re-read" for every unknown, and on the live roster ZERO of the eleven
# unknowns healed on a re-read. Six needed a repair; five needed a provider to
# come back. That surface would have sent a tired reader to re-run a command
# eleven times and then to blame the fold speed.
#
# THE BAR IS DISCRIMINATION, NOT ACCURACY. Every one of these sentences could
# be individually true and the set still be a defect, because a reader who
# cannot tell two of them apart acts on the wrong one. So each opens by naming
# the thing that DIFFERS — is anything stored, does re-reading reach it, who
# can clear it — and only then says what to do. Read the five in a column
# before changing any of them; if two could be swapped without the reader
# noticing, the pair is the bug.
#
# NONE OF THEM PROMISES HEALING. Even TRANSIENT says "run it once more", not
# "it heals": a stale proxywatch record is transient only while the watcher is
# alive, and a reader told "it heals" retries forever against a dead one.
def tier_unknown_cure(kind):
    """What the reader should DO about this kind of unevaluable tier."""
    if kind == dispatches.TIER_TRANSIENT:
        return ("This one failed at a LIVE step — the stored evidence is "
                "intact and the next read may answer, so re-run this command "
                "ONCE before routing anything. ONCE, NOT IN A LOOP: if the "
                "same sentence comes back the read is not flaky, and the "
                "reason above is the real condition")
    if kind == dispatches.TIER_DARK:
        return ("NOTHING IS STORED to re-read — no proxywatch pass has ever "
                "recorded a runtime proof for this seat, so re-running cannot "
                "reach an answer that does not exist. It is NOT stale "
                "evidence, and no command you run at this row changes it: it "
                "clears only when the seat is live and a pass stamps it. Both "
                "live shapes end here — an EMPTY SLOT that must actually be "
                "seated, and a SEATED seat whose upstream is walled "
                "(rate-limited, auth-unavailable, malformed responses) and "
                "must come back. `helm seat where` says which. Nothing is "
                "repairable from the ledger — route the review to a seat that "
                "resolves")
    if kind == dispatches.TIER_DAMAGED:
        return ("STORED EVIDENCE EXISTS AND CONTRADICTS ITSELF — every "
                "re-read returns this same answer, forever, and no amount of "
                "waiting changes it. The sentence above names the object "
                "(policy prior, proxywatch record, roster stamp, verdict "
                "snapshot); repairing THAT is the only thing that moves this "
                "row")
    if kind == dispatches.TIER_UNNAMED:
        return ("THE ROW'S OWN RECIPIENT does not resolve to exactly one "
                "roster seat, so there is no seat to be inside or outside the "
                "tier — this is not a tier condition at all. Fix the name on "
                "the row, or seat the roster, and it resolves immediately")
    return ("helm did NOT classify this unknown, so nothing here says whether "
            "it clears. Read the reason above and decide; do not assume a "
            "re-read reaches it")


def _approval_refusal(row, index=None, epoch=None):
    """(why, tier_state) for one APPROVE authorization decision.

    Projection and discharge consume the SAME decision. A raw polarity string
    is not authority: the reviewer must be inside the approval tier, the
    writer's gate capability must be readable, and a required receipt must be
    bound. Keeping this in one predicate prevents discharge from retiring a
    FIX with an approval that the land path itself refuses."""
    try:
        tier_state, tier_why = dispatches.approval_tier_for_verdict(row)
    except Exception as ex:                 # noqa: BLE001 — fail closed
        # A RAISE IS A FACT ABOUT THE MOMENT — projscope's own law ("a raise
        # is never memoised") and the same reading here: the checker fell
        # over, the subject did not answer. TRANSIENT, so the surface says run
        # it again once and the memo re-asks; if it repeats, the exception
        # text travels on the sentence and repeating IS the diagnosis.
        tier_state, tier_why = dispatches.TierUnknown(
            dispatches.TIER_TRANSIENT, "unknown"), \
            "approval-tier check raised: %s" % ex
    if dispatches.tier_unknown_kind(tier_state) == dispatches.TIER_PRE_TIER:
        return (tier_why, tier_state)
    if tier_state == "outside":
        return (("approved by a reviewer the approval tier does not permit — "
                 + (tier_why or "the tier could not be evaluated")
                 + "; re-review inside the tier rather than landing past "
                 "it. This is a MEASUREMENT, not an unresolved read — "
                 "re-running changes nothing"),
                tier_state)
    if tier_state == "unknown":
        # NOT PERMIT — COULD NOT SAY, and the two must not share a sentence.
        # Both refuse (an unread tier never authorizes a land), but "does not
        # permit" states a measurement nobody took, and it reads as an
        # accusation against the reviewer on a row whose real defect may be a
        # corrupt policy file or a provider outage. The refusal keeps its
        # force; only the claim shrinks to what was actually measured, and the
        # kind's own cure is appended so the reader is not left to guess which
        # of the worlds this is.
        return (("approved by a reviewer whose approval tier helm COULD NOT "
                 "EVALUATE (not a measurement that they are outside it) — "
                 + (tier_why or "the tier could not be evaluated")
                 + ". " + tier_unknown_cure(
                     dispatches.tier_unknown_kind(tier_state))),
                tier_state)
    requirement = gate_requirement(row, index=index, epoch=epoch)
    if requirement == "unknown":
        return (_unknown_gate_caps_why(row), tier_state)
    bound = bool(dispatches._GATE_ID.fullmatch(str(row.get("gate") or "")))
    if requirement == "required" and not bound:
        # THE REPAIR THE LAND SEQUENCE RUNS, and the same one the verdict door
        # names (dispatches.mark_verdict). A whole suite on the reviewed tip
        # is NOT that repair: nothing lands that tree, since the integrator
        # rebases it, and the gate verb refuses a lane whole suite
        # (task/3039).
        return (("approved with no minted gate receipt — an approve binds the "
                 "whole suite on the tree that LANDS, which is the "
                 "integrator's train. If the source read is clean, hold it: "
                 "`helm dispatch hold <row> --source-clean <tip> <reason>`, "
                 "and record the approve against the token the train's land "
                 "gate mints"), tier_state)
    return None, tier_state


def approval_authority(row, index=None, epoch=None, repo=None):
    """(True/False/None, detail) for one fold-time review authority.

    This is the owner-layer predicate for consumers that need a VERDICT rather
    than `_approval_refusal`'s display sentence. It preserves three states:
    measured non-authority (False), unreadable authority (None), and a canonical
    APPROVE whose recorded tier and writer capability authorize it (True).

    A gate-capable approval is also replayed against the receipt store when
    `repo` is supplied. Verdict append checked that binding once, but per-car
    provenance is a fold-time proof and must re-derive the receipt's whole-suite
    tree authority rather than treating a shape-valid token as evidence.
    """
    if not isinstance(row, dict) or row.get("status") != "verdict" \
            or row.get("kind") != "review":
        return False, "the row is not a closed review verdict"
    if row.get("polarity") != "approve":
        return False, ("polarity %r authorises nothing; only APPROVE can land"
                       % row.get("polarity"))
    if not isinstance(index, int) or index < 0:
        return None, "the accepted verdict event has no readable append index"
    refusal, tier = _approval_refusal(row, index=index, epoch=epoch)
    requirement = gate_requirement(row, index=index, epoch=epoch)
    if refusal:
        kind = dispatches.tier_unknown_kind(tier)
        measured = tier == "outside" or kind == dispatches.TIER_PRE_TIER \
            or (requirement == "required" and not dispatches._GATE_ID.fullmatch(
                str(row.get("gate") or "")))
        return (False if measured else None), refusal
    if requirement not in ("none", "required"):
        return None, "the verdict's gate requirement is unreadable"
    if requirement == "required":
        if not repo:
            return None, "the required gate receipt has no repository to verify"
        rung = foldcheck.gate_authority(
            repo, str(row.get("reviewed_tip") or ""),
            str(row.get("gate") or ""))
        if rung.verdict == foldcheck.PASS:
            return True, rung.discriminator
        if rung.verdict == foldcheck.REFUSE:
            return False, rung.discriminator
        return None, rung.discriminator
    return True, "canonical APPROVE authority is recorded and gate-exempt"


# --------------------------------------------------------------------------
# READY says "this may be merged", and it says it for rows a land door will
# refuse. Three ways, all MEASURED on the live ledger rather than imagined:
# an unmarked SELF-REVIEW (no seat that wrote NONE of this row's chain has
# approved its final tip, so nobody independent looked),
# and a
# STALE BASE (the receipt was minted against a trunk that has since moved).
#
# THE SELF-REVIEW RUNG IS SCOPED TO THE CHAIN, NOT THE ROW, and the rung kept
# its name when the test widened. It began as `reviewer == author` on one row,
# which a reviewer defeats without meaning to: patch round N through a FIX
# carrying `--patch-tip`, receive the successor row, APPROVE round N+1 — the
# comparison passes and the seat wrote part of what lands. `independent_review`
# asks the whole chain instead.
#
# THIS LAYER ONLY TELLS THE TRUTH. It never refuses and never downgrades the
# state word other consumers key on — STAGE_ORDER, OWED_BY and LAND_STALL_S
# all index "READY", so rewriting it here would quietly drop the row out of
# stall accounting and owed-by routing. Enforcement stays in the land door,
# which owns its own rungs; this is the display saying which one will bite.
_READY_RUNG_ORDER = ("SELF-REVIEW", "CONTESTED", "STALE-BASE")

# How many trunk commits a READY row's base may lack before the base is called
# DEAD rather than merely older. The unit is "trunk commits the gate receipt
# never saw", because that is the thing staleness actually risks: the receipt
# proved the lane's tree against ITS base, and every commit trunk gained since
# is proof the compose leg must re-carry (rebase, re-gate, the CURRENT suite).
#
# 450 IS MEASURED, NOT ROUND (re-measured 2026-08-06 against the live ledger;
# the first cut said 120 off a "busiest night = 94 commits" reading that used
# AUTHOR dates — rebases keep those, so land-time velocity was undercounted
# 3.5x, and 120 sat INSIDE the live compose queue: five approve-bearing rows
# awaiting the nightly compose measured 102..133 behind, one with a lease live
# at the moment of measurement. A bar that accuses the working audience is the
# rejected reachability rung back again). By COMMITTER date — when trunk
# actually gained the commit — the busiest day yet moved 363 (2026-08-05) and
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
    projection is already a ~27s pass (#99) and a per-row read would multiply
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
    rows, unavailable, _skipped = gate.receipts()
    if unavailable:
        return {}
    index = {str(rec["id"]): rec for rec in rows}
    _GATE_INDEX_MEMO.clear()
    _GATE_INDEX_MEMO.update(key=key, index=index)
    return index


def _contesting_verdicts(lr, index=None):
    """([(polarity, seat, row_id)] that REFUSE this row's tip, err).

    Excludes the row's own verdict — that approve is what made it READY, and
    counting it would make every READY row contest itself. FIX and SUPERSEDE
    refuse; CONCUR and APPROVE do not, and UNDECLARED is not a refusal either
    (an absent polarity is not a negative — the whole file's law).
    """
    tip = str(lr.get("reviewed_tip") or "").strip().lower()
    if not tip:
        return [], None               # nothing to join on is not a refusal
    if index is None:
        index, _works, err = contest_index()
        if err:
            return [], err
    own = {str(lr.get("id") or ""), str(lr.get("dispatch_id") or "")}
    return [v for v in index.get(tip, ())
            if v[0] in ("fix", "supersede") and v[2] not in own], None


# A TIP CITED BY MANY PIECES OF WORK IS A BASE, NOT ONE PIECE OF WORK, and this
# is the line between a rung and a nag. Measured over the live ledger
# 2026-08-06: 20 tips carry an APPROVE and a refusal together, and they split
# cleanly —
#   15 span 1-2 chains -> a genuine SECOND OPINION on one piece of work
#                         (the incident: todos-sweep-report-fixes approved
#                          while todos-sweep-declined-t1-read said FIX)
#    5 span 3+ chains  -> a SHARED BASE; one is 15 unrelated `*-sub-*` fan-out
#                         lanes at one tip, where a single FIX on
#                         `rebindwire-sub-*` would make fourteen strangers
#                         read CONTESTED
# Refusing on all 20 is 25% false, and a land surface that cries "contested"
# at unrelated work is muted in a day — the same failure the NDP block and the
# cured-fix rung were both narrowed to avoid on this same night.
#
# THE COUNT IS OVER WORK IDENTITY, NEVER THE LANE LABEL (task/734, a review's
# confirmation gate). The span groups on `chain_root` — the immutable per-work
# identity `--supersedes` carries on every dispatch row — because a lane LABEL is
# renamable free text and is not identity. Keying on the label was the defect:
# (a) one piece of work continued under a RENAMED lane counted as MANY, so its
# span crossed the shared-base line and a binding FIX escaped silently; and
# (b) two unrelated chains that happened to share a label collapsed to ONE, so
# a genuine shared base deflated under the line and falsely gated a stranger.
# A tip cited by many DISTINCT chains is a base by construction; a chain
# renamed across N lanes is still one piece of work. A row with no well-formed
# chain_root falls back to its OWN id — "a root names itself", the same seal
# `_append_dispatch` writes — never to the lane.
#
# THE PRINT STAYS WIDE AND ONLY THE REFUSAL NARROWS, which is what the brief
# actually needs: a reader always sees "N other verdicts bind this tip", so a
# shared-base refusal is never HIDDEN — it just does not gate a stranger.
CONTEST_WORK_SPAN = 2


def contest_report(lr, index=None):
    """(refusing_verdicts, span, err) — what to PRINT beside any LIVE row.

    Always the full join, never narrowed: hiding a refusal because it sits on
    a busy tip would reintroduce the silence this exists to end. `span` is the
    number of distinct pieces of WORK (chain_root, never the lane label)
    citing the tip, so the reader can see for themselves whether it is a
    second opinion or a shared base.

    A TERMINAL ROW ANSWERS EMPTY BY DESIGN, and that is a different axis from
    the never-narrow law above: not WHICH refusals show, but which ROWS live
    data may annotate. This index is the CURRENT ledger, so a mark beside
    LANDED is not history — it is a later, unrelated FIX retroactively
    re-wording a closed record (a fab probe, round four, dispatch
    da34a297; 54 terminal rows wore one at the tip that verdict reviewed,
    measured before curing). The gate lives HERE, in the one door every
    reader passes through, so the next renderer cannot re-commit round
    three's error by forgetting to scope itself.

    ONE LEDGER READ PER CALLER, NEVER PER ROW. The first draft read the whole
    dispatch snapshot INSIDE this function, so a board rendering N rows paid N
    full ledger reads — my own probe over 1016 tips timed out at two minutes,
    which is the defect announcing itself before any reviewer had to. helm's
    own dispatch list renderer states the same law ("NO re-measure on the hot
    list path"). Callers build the pair ONCE with `contest_index()` and thread
    it in as `index=(verdicts, works)`."""
    if lr.get("terminal"):
        return [], 0, None
    verdicts, works_by_tip = index if isinstance(index, tuple) \
        else (index, None)
    hits, err = _contesting_verdicts(lr, verdicts)
    if err or not hits:
        return [], 0, err
    if works_by_tip is None:
        _v, works_by_tip, err = contest_index()
        if err:
            return hits, 0, err
    tip = str(lr.get("reviewed_tip") or "").strip().lower()
    return hits, len(works_by_tip.get(tip) or ()), None


_CONTEST_MEMO = {}
_LEDGER_FOLD_MEMO = {}


def _fold_key():
    """(path, size, mtime_ns) of the DISPATCH ledger, or None when it cannot be
    stat'd. Every read-side memo over that ledger keys on this value, so an
    append invalidates all of them without anyone deciding to.

    NOT `_ledger_key`, which is this module's LAND-RECEIPT ledger key and takes
    a path. Two same-named module-level functions do not collide loudly: the
    later definition simply wins and every earlier caller gets the wrong
    arity — measured here as 124 errors across one module."""
    try:
        from . import dispatches
        path = dispatches.ledger_path()
        st = os.stat(path)
        return (path, st.st_size, st.st_mtime_ns)
    except Exception:                                         # noqa: BLE001
        return None


def _ledger_fold():
    """(rows, verdict positions, err) — ONE canonical fold of the dispatch ledger per ledger
    state, shared by every read-side join here.

    TWO JOINS ON ONE LEDGER MUST NOT COST TWO FOLDS. `contest_index` and
    `chain_contributor_index` answer different questions off the same rows,
    and each owning its own `snapshot_with_verdicts` call meant a board paid
    the fold twice on the first row it rendered — the exact cost
    `contest_index` was rebuilt to stop paying, arriving again through a
    second reader. Sharing the fold also means both joins describe ONE ledger
    moment, so they can never disagree about which rows exist.

    A REFUSAL IS NEVER MEMOISED. An unreadable ledger is a fact about this
    moment; caching it would hand a transient failure to every later reader of
    an unchanged file.

    THE ROWS ARE READ-ONLY TO EVERY CALLER. A memoised fold is one dict handed
    to several joins instead of a fresh one per call, so a consumer that edited
    a row would be editing what the next join reads. Both joins here only
    project fields out; a future one must copy before it writes."""
    key = _fold_key()
    if key is not None and _LEDGER_FOLD_MEMO.get("key") == key:
        return (_LEDGER_FOLD_MEMO["rows"],
                _LEDGER_FOLD_MEMO["verdicts"], None)
    try:
        from . import dispatches
        rows, verdicts, unavailable = dispatches.snapshot_with_verdicts()
    except Exception as exc:                                  # noqa: BLE001
        return None, {}, "dispatch ledger unreadable: %s" % exc
    if unavailable:
        return None, {}, str(unavailable)
    if key is not None:
        _LEDGER_FOLD_MEMO.clear()
        _LEDGER_FOLD_MEMO.update(key=key, rows=rows, verdicts=verdicts)
    return rows, verdicts, None


def contest_index():
    """((tip -> verdicts), (tip -> distinct chains), err) — built ONCE PER
    LEDGER STATE, memoised exactly like `_gate_receipt_index`.

    THE SPAN COUNTS DISTINCT WORK, NOT LANES (task/734, a review's confirmation
    gate). The second map groups each reviewing row under its `chain_root` — the
    immutable per-work identity `--supersedes` carries — never its renamable
    lane label,
    because a label is free text and is not identity: a chain renamed across N
    lanes must still count as ONE piece of work, and two unrelated chains that
    share a label must count as TWO. A row lacking a well-formed chain_root
    falls back to its OWN id ("a root names itself"), never to the lane.

    THREADING AN INDEX PARAMETER WAS NOT ENOUGH AND A PROBE MEASURED IT: `list`
    and `compose` call `ready_word(lr)` with no index, so every READY row
    refolded the whole dispatch ledger while all seven of my arms INJECTED one
    and never exercised the real caller. A fixture proves the code handles the
    input you gave it, never that production mints that shape.
    So the memo lives HERE, where no caller has to remember it. The key is the
    ledger file's own (size, mtime_ns), so an append invalidates it without
    anyone deciding to — the same law the receipt index states one screen up.

    Both halves come from ONE snapshot because they answer one question
    together: which verdicts bind this tip, and how many distinct pieces of
    work cite it. Splitting them into two reads would let the two disagree
    about the same ledger moment."""
    from . import dispatches
    key = _fold_key()
    if key is not None and _CONTEST_MEMO.get("key") == key:
        return (_CONTEST_MEMO["verdicts"], _CONTEST_MEMO["works"],
                _CONTEST_MEMO["err"])
    rows, _verdicts, err = _ledger_fold()
    if err:
        return {}, {}, err
    verdicts, works = {}, {}
    for row in (rows or {}).values():
        tip = str(row.get("reviewed_tip") or "").strip().lower()
        if not tip:
            continue
        # WORK IDENTITY, NEVER THE LANE LABEL (task/734): group on chain_root,
        # the immutable per-work id. ABSENT legacy roots and PRESENT malformed
        # roots replayed as CHAIN_UNKNOWN both name themselves by their own id —
        # UNKNOWN is a typed refusal, never an identity shared by every corrupt
        # row. Keying on `lane` here was the original defect; treating the
        # truthy UNKNOWN sentinel as a root would recreate its false-collapse
        # half for malformed rows.
        root = row.get("chain_root")
        if root in (None, dispatches.CHAIN_UNKNOWN):
            root = row.get("id")
        works.setdefault(tip, set()).add(str(root))
        polarity = str(row.get("polarity") or "").strip().lower()
        if polarity:
            verdicts.setdefault(tip, []).append(
                (polarity, str(row.get("recipient") or ""),
                 str(row.get("id") or "")))
    if key is not None:
        _CONTEST_MEMO.clear()
        _CONTEST_MEMO.update(key=key, verdicts=verdicts, works=works, err=None)
    return verdicts, works, None


def _same_seat(one, other):
    """Are these two recorded names the SAME seat? Canonical first, exact
    string underneath it.

    THE CANONICAL COMPARISON IS THE RULE — one seat spelled two ways is one
    seat, and every reader of a recorded name must agree about that. But
    `recipient_matches` answers False when EITHER name fails the canonical
    seat token, and False at these two rungs means "a different seat", which
    is the one direction where an unreadable name LOOSENS a refusal: an author
    helm cannot canonicalise would stop being the reviewer of their own row,
    and a contributor spelled that way would read as an outsider whose
    approval makes the chain independent.

    The fallback preserves the earlier whitespace-normalised comparison:
    two non-empty strings equal after stripping are the same seat even when
    the token grammar refuses them. Both operands use the same normalization,
    including when a caller has already stripped one. This only ADDS a
    self-review or REMOVES an outsider; empty names never accuse.

    Both the writer and replay reject malformed recipients. This fallback
    protects direct projections outside the ledger, not a measured live case.
    """
    if recipient_matches(one, other):
        return True
    if not isinstance(one, str) or not isinstance(other, str):
        return False
    one, other = one.strip(), other.strip()
    return bool(one) and one == other


def self_reviewed(author, reviewer):
    """Did this row's own author review it? The ROW-LOCAL half of the
    SELF-REVIEW rung, named.

    IT IS NO LONGER THE WHOLE TEST. Independence is a property of the CHAIN
    (`independent_review`), because a reviewer who patched an earlier round
    and approved a later one passes this comparison while having written part
    of the artifact. This stays the ONE named rule for the row-local question
    — two readers of "is the reviewer the author" must never drift — and the
    chain join sits beside it rather than inside it.

    Empty names never accuse — an unrecorded author is UNKNOWN, and UNKNOWN
    is not a self-review (every pre-sender `dispatch add` row would otherwise
    gain an accusation nobody measured).

    IT LIVES HERE, WITH THE LADDER. An earlier cure homed it in `dispatches`
    because the land notifier compared sender to recipient on the raw row
    and could not import this module. That was the wrong cure at the wrong
    altitude: the notifier's real defect was reading ONE rung instead of the
    ladder, so it now asks `land_instruction_word` for the whole answer and
    the comparison has exactly one reader again — the rung below."""
    return _same_seat(author, reviewer)








_CHAIN_CONTRIB_MEMO = {}






def independent_review(lr, index=None, receipts=None):
    """(True / False / None, the sentence a surface prints) — has a seat that
    wrote NONE of this chain approved its FINAL tip?

    THREE STATES, AND THE MIDDLE ONE IS THE POINT. True is a measured outsider
    approval on the exact final tip. False means no eligible outsider APPROVE
    supplies that read. None is UNKNOWN — the chain's authorship or a candidate
    outsider's eligibility could not be read — and it is NOT a pass. An
    unreadable ledger must never collapse to an empty contributor set, because
    an empty set says "nobody wrote this chain", which would make every
    stranger's approval independent exactly when helm knows least.

    TWO SOURCES, BOTH NAMED, NEITHER INLINED. The row-local half is
    `self_reviewed` — the sender/recipient comparison that has always been THE
    shared rule — and the chain half is the ledger join above. A row with NO
    WORK IDENTITY AT ALL — neither an id nor a chain root, which is the shape
    of a projection built outside the ledger — joins to nothing and is judged
    by the row-local rule alone, which is why threading this through changes
    no answer for a single-round chain. A row that HAS an identity whose
    repository cannot be read is the OTHER case and it is not row-local:
    `chain_contributors` answers "chain identity is UNKNOWN" for it and this
    function turns that into None, because a chain helm cannot join is never a
    chain nobody else wrote.

    THE FINAL TIP IS THIS ROW'S REVIEWED TIP. This row is the round whose
    readiness is being decided, so its own APPROVE binds the final tip by
    construction; when its reviewer is an outsider that approval IS the
    independent read. Only when the reviewer is a contributor does the chain
    get searched for a sibling APPROVE, and that sibling must cite the SAME
    tip: an outsider who approved an earlier round read an artifact that no
    longer exists, and an outsider on another chain read other work entirely.

    CONSERVATIVE PROVENANCE, SAID OUT LOUD: the contributors are seats the
    ledger records as having SUBMITTED work into this chain, never a proof of
    Git authorship — see `chain_contributor_index`."""
    wrote, approved, err = chain_contributors(lr, index=index)
    if err:
        return None, ("UNKNOWN — helm could not read this chain's authorship "
                      "(%s), so it cannot say whether an outsider approved; "
                      "an unreadable chain is never an empty contributor set"
                      % err)
    reviewer = str(lr.get("reviewer") or "").strip()
    tip = str(lr.get("reviewed_tip") or "").strip().lower()
    # THE ROW ITSELF IS EVIDENCE about one author, and the ledger join may not
    # hold it: a projection built outside the ledger joins to nothing, and a
    # sibling APPROVE from the row's OWN author must not be counted as an
    # outsider read just because the join came back empty. So the roster the
    # SIBLING SEARCH and the printed sentence use carries the row's author;
    # the reviewer-is-the-author question stays with `self_reviewed`, the one
    # named rule, so a caller that disables that rule disables this rung.
    authors = set(wrote)
    if str(lr.get("author") or "").strip():
        authors.add(str(lr.get("author")).strip())
    if not self_reviewed(lr.get("author"), reviewer) and not any(
            _same_seat(reviewer, name) for name in wrote):
        return True, ("independent — %s wrote no round of this chain and "
                      "approved the final tip %s"
                      % (reviewer or "(unnamed)", tip[:12] or "(none)"))
    rows, positions, err = _ledger_fold()
    if err:
        return None, "UNKNOWN — outsider approval evidence: " + err
    epoch = dispatches.gate_epoch(rows, positions)
    outside, unknown = set(), []
    for row, position in approved:
        seat = str(row.get("recipient") or "").strip()
        cited = str(row.get("reviewed_tip") or "").strip().lower()
        if cited != tip or not seat or any(
                _same_seat(seat, name) for name in authors):
            continue
        # EXACTLY THE ROW-LOCAL CONTRACT: _lr's eligibility decision, then
        # ready_rung's receipt-presence check. No kind filter, deep receipt
        # replay, or historical repo_root. Contest, observability and staleness
        # belong once to the consuming row's context, not to independence.
        refusal, tier = _approval_refusal(row, index=position, epoch=epoch)
        if refusal:
            if tier == "outside" or dispatches.tier_unknown_kind(tier) == dispatches.TIER_PRE_TIER:
                continue
            if tier == "unknown" or gate_requirement(row, index=position, epoch=epoch) == "unknown":
                unknown.append("%s: %s" % (seat, refusal))
            continue
        receipts = _gate_receipt_index() if receipts is None else receipts
        refusal = _ready_receipt_refusal(row, receipts)
        if refusal:
            unknown.append("%s: %s" % (seat, refusal))
            continue
        outside.add(seat)
    if outside:
        return True, ("independent — %s wrote no round of this chain and "
                      "approved the final tip %s"
                      % (", ".join(sorted(outside)), tip[:12] or "(none)"))
    if unknown:
        return None, "UNKNOWN — outsider approval eligibility: " + "; ".join(unknown)
    return False, ("contributor approval present (%s), outsider approval "
                   "missing — this chain records %s as authors and no eligible "
                   "outsider has approved the final tip %s"
                   % (reviewer or "(unnamed)",
                      ", ".join(sorted(authors)) or "(unnamed)",
                      tip[:12] or "(none)"))


def _ready_receipt_refusal(row, receipts):
    """The receipt-presence rung, shared by a READY row and its sibling read.

    Binding belongs to verdict write time, not to this shallow readiness
    projection. This does not revalidate version, host, tree or checkout, and
    cannot replace the separate canonical whole-suite land gate.
    """
    token = str(row.get("gate") or "").strip()
    if not token:
        return "APPROVE has no gate token to check"
    if not isinstance(receipts.get(token), dict):
        return "the receipt ledger cannot speak for this gate token"
    return None


def live_ready(lr):
    """True for a row that is READY AND not terminal: approved work that can
    still land. THE ONE PREDICATE for that question, so every reader asks it
    the same way.

    THE STATE WORD ALONE IS THE WRONG KEY. The projection keeps the stored
    state READY on a row that has since closed (closed-by-landing keeps the
    verdict state by design) and marks the row `terminal`. The first live dry
    run of `helm train` found 459 READY rows for helm, 457 of them closed as
    landed, and `helm lr compose` admitted such a row when an integrator
    passed its id, with only its patch-id screen behind it. Its readers:
    `helm lr list` and `lr show` (the READY marks), `helm train` (its cars),
    `helm lr compose` (its admission) and `ready_rung_why` (the ladder)."""
    return isinstance(lr, dict) and str(lr.get("state")) == "READY" \
        and not lr.get("terminal")


def ready_rung(lr, index=None, contest=None, contributors=None):
    """The SPECIFIC rung this READY row fails, or None when all pass — the
    word alone. `ready_rung_why` is the ladder and carries the rung's reason
    beside it; this is its one-word projection, so the two cannot drift."""
    return ready_rung_why(lr, index=index, contest=contest,
                          contributors=contributors)[0]


def ready_rung_why(lr, index=None, contest=None, contributors=None):
    """(rung, why) — the SPECIFIC rung this READY row fails and the sentence
    that says why, or (None, None) when all pass.

    THE REASON RIDES WITH THE RUNG because UNVERIFIED FOLDS SEVERAL UNKNOWNS
    into one word — an unreadable contributor chain, an unreadable contest
    join, a row helm could not observe, a receipt the ledger cannot speak for
    — and a reader that must act differently on each (`helm train` excludes
    the first and keeps the others) cannot recover which one from the word.

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
      serves. It rendered my own approved, minutes-old, host-bound row as
      defective.

    STALE-BASE (task/266) is that question asked with the RIGHT measure, and
    the difference is the whole admission case. Reachability is boolean and
    true of every in-flight lane; `base_behind` is `rev-list --count
    tip..trunk` — how many trunk commits this row's history LACKS — which
    measured <= 133 for every active or awaiting-compose lane on the busiest
    week yet (trunk at 300+ commits/day) and 725..1372 for the READY zombies
    (re-measured 2026-08-06). It is computed from LIVE git by the projection
    (`_base_behind`), never guessed from receipt fields, and it accuses only
    when helm's own landedness ladder already said UNLANDED: a row whose work
    reached trunk by rebase reads LANDED (patch identity, `_landing_proof`)
    long before this rung is consulted, and a row helm could not observe
    carries no `base_behind` at all — UNKNOWN never accuses. It is a REPORT,
    not a refusal: the compose leg rebases and re-gates, so the door still
    works on a dead base — what died was anyone REMEMBERING the row. Two of
    five open rows sat ready-on-a-dead-base at once, receipts internally
    perfect, every instrument reporting health (the task/266 finding), because
    nothing on this board computed behind-ness for a ready row."""
    # NO LIVE RUNG REACHES A TERMINAL ROW (round four, dispatch da34a297:
    # "Scope report and ready_word to nonterminal rows"). The projection mints
    # closed rows whose STORED state is READY — closed-by-landing keeps the
    # verdict state by design — and every rung below reads a live instrument:
    # the contest join, the receipt ledger, `base_behind`. A rung is
    # door-caution for a row that can still land; a closed row has no door
    # left, so re-judging it turns its word into a value that changes when
    # the instruments do. Measured before curing, at the tip that verdict
    # reviewed: 104 terminal stored-READY rows, five reading READY-CONTESTED
    # off refusals filed AFTER they closed, 95 READY-UNVERIFIED, two
    # READY-SELF-REVIEW. The contest door already answers empty for terminal
    # rows; this guard is what scopes the OTHER rungs, and the whole ladder
    # with them. It is `live_ready`, the predicate every READY reader asks.
    if not live_ready(lr):
        return None, None
    # INDEPENDENCE IS A PROPERTY OF THE CHAIN. The rung keeps its name — every
    # consumer from the web board to the land nudge keys on SELF-REVIEW, and
    # the question it asks is still "did nobody independent look" — but the
    # test is no longer one row's sender against one row's recipient. A
    # reviewer who patched round N and then approved round N+1 passed that
    # comparison while having written part of what lands; `independent_review`
    # asks the chain instead and its sentence names the contributor. UNKNOWN
    # rides the ladder's existing word for a rung it could not EVALUATE: an
    # unreadable chain never reaches plain READY.
    index = _gate_receipt_index() if index is None else index
    independent, why = independent_review(lr, index=contributors, receipts=index)
    if independent is None:
        return "UNVERIFIED", why
    if not independent:
        return "SELF-REVIEW", why
    # CONTESTED — A REFUSAL ON THIS TIP THAT THIS ROW'S CHAIN CANNOT SEE.
    #
    # Measured 2026-08-06 and it nearly cost data: the todos-sweep lane read READY on
    # a claude seat's APPROVE of that lane's reviewed tip while a codex seat's T1
    # cross-family read of THE SAME 40-HEX TIP returned FIX with three defects,
    # the third a proven destructive TOCTOU. Both verdicts real, both binding
    # the same tip, and the land surface showed only the approve — because the
    # two reviews sit on DIFFERENT CHAINS and this projection consults the
    # row's OWN verdict. The refusal was never stale and never lost; there was
    # simply nowhere to put a second opinion.
    #
    # A TIP IS A CONTENT IDENTITY, so verdicts against it are findable
    # regardless of which chain recorded them. That is the whole cure: a
    # read-side join on a value already stored, mirroring `_receipts_by_tip`
    # one object over — same not-last-wins discipline, because two verdicts
    # disagreeing about one tip are a CONFLICT and must never resolve to
    # whichever was appended last.
    #
    # SECOND IN SEVERITY, ahead of the gate check and far ahead of STALE-BASE:
    # this bites at the DOOR. READY is an INSTRUCTION — it prints a land
    # command — so a row carrying an unanswered FIX must not reach it.
    contested, span, err = contest_report(lr, index=contest)
    if err:                           # could not look; never a pass
        return "UNVERIFIED", ("UNKNOWN — the refusals on this tip could not "
                              "be read (%s), so whether an unanswered FIX "
                              "stands on it is unknown" % err)
    if contested and span and span <= CONTEST_WORK_SPAN:
        return "CONTESTED", ("an unanswered FIX or SUPERSEDE stands on this "
                             "tip (%d piece%s of work cite it)"
                             % (span, "" if span == 1 else "s"))
    # UNOBSERVABLE IS A RUNG, AND ITS PLACE IN THE LADDER IS THE POINT. A row
    # helm could not observe has no measured base and no measured landedness,
    # so every rung BELOW this one reads instruments that were never taken —
    # and a READY minted past them is an instruction to merge built on a
    # reading nobody made. It sits HERE, after SELF-REVIEW and CONTESTED,
    # because those two are STORE facts: author, reviewer and the tip-join
    # need no repository, so an unobservable row can still be refused for the
    # loudest reasons. It sits BEFORE the token rung, which is where the
    # repo-dependent evidence begins.
    if lr.get("observable") is False:  # helm looked and could not; never a pass
        return "UNVERIFIED", ("UNKNOWN — helm could not observe this row's "
                              "repository: %s"
                              % observe_reason(lr.get("observe_why")))
    refusal = _ready_receipt_refusal(lr, index)
    if refusal:
        return "UNVERIFIED", refusal
    # RECEIPT VERSION IS DELIBERATELY NOT A RUNG HERE. It reads like one -- a
    # hostless pre-v4 receipt cannot bind to the tree it names -- and 81 of the
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
        return "STALE-BASE", ("the base lacks %d trunk commits (the line is "
                              "%d)" % (behind, STALE_BASE_BEHIND))
    return None, None


def ready_word(lr, index=None, contest=None, contributors=None):
    """The state word a reader should SEE: plain READY only when every rung
    passes, else READY-<RUNG>. Every other state is returned untouched — and
    so is a TERMINAL row's, because a rung is door-caution for a row that can
    still land (the terminal guard in `ready_rung_why`, round four)."""
    rung = ready_rung(lr, index=index, contest=contest,
                      contributors=contributors)
    return "READY-%s" % rung if rung else str(lr.get("state"))


def base_state(lr, stale_at=None):
    """LANDED / UNLANDED-CURRENT / UNLANDED-STALE / UNKNOWN — where this row's
    WORK stands relative to the landing target, on helm's own evidence ladder
    (ancestry > patch identity; a NO from the strongest instrument is not a NO
    from the claim).

    The ladder is not re-run here — it already ran. `landed`/`merged_local`
    are `_landing_proof`'s verdict (ancestry fast path, `git cherry` patch
    identity behind it), so a row whose sha was rewritten by the integrator's
    rebase reads LANDED, never stale: the task/266 census measured 131 of 153
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


TERMINAL = ("LANDED", "SUPERSEDED", "ABANDONED", "DELIVERED_REPORT",
            "RETIRED", "RETRACTED")
_TERMINAL_ANNOTATIONS = (
    # FIRST, because it is EXCLUSIVE: the retire writer refuses over any other
    # standing terminal and every other writer refuses over it, so a row can
    # carry this annotation and no other. Reading it first is what lets a
    # surface name the terminal without asking four questions.
    ("retired_admin", "retire_ts", "RETIRED"),
    # EXCLUSIVE THE SAME WAY (task/3060): a retraction refuses over any other
    # terminal and every other terminal refuses over it, so the land nudge and
    # the board say RETRACTED and never READY over a verdict that was taken
    # back.
    ("verdict_retracted", "retract_ts", "RETRACTED"),
    ("abandoned", "abandon_ts", "ABANDONED"),
    ("discharged", "discharge_ts", "DISCHARGED"),
    ("withdrawn", "withdraw_ts", "WITHDRAWN"),
    ("closed_by_landing", "landing_ts", "CLOSED_BY_LANDING"),
)


def terminal_annotation(row):
    """(LABEL, ts) for a retired row's ONE terminal annotation, else None.

    Reads the same field names on a RAW ledger row and on a projected `lr`,
    because `_lr` copies them through unchanged — which is what lets the
    projection MINT the timeline step here and a later reader NAME the same
    terminal without a second vocabulary. Exclusivity at the write boundary
    guarantees at most one annotation exists, so `next` is the whole search;
    `close --reason R` is the generalization of the same step (CLOSED_<R>,
    dated by the close stamp)."""
    hit = next(((label, row.get(ts_field))
                for flag, ts_field, label in _TERMINAL_ANNOTATIONS
                if row.get(flag)), None)
    if hit is not None:
        return hit
    reason = row.get("close_reason")
    if not reason:
        return None
    return ("CLOSED_" + str(reason).upper().replace("-", "_"),
            row.get("close_ts"))


def land_instruction_word(lr, index=None, contest=None, contributors=None):
    """The word a LAND INSTRUCTION may carry: plain "READY" ONLY for a row the
    door would actually open, else the state that says why not.

    THE NOTIFIER ASKS A DIFFERENT QUESTION FROM THE BOARD, which is why this
    exists beside `ready_word` instead of inside the notifier. `ready_word`
    answers "what should a READER SEE", and for a TERMINAL row it
    deliberately returns the stored state (a rung is door-caution for a row
    that can still land, and a closed row has no door left) — retired rows
    commonly carry a stored state of READY. A land nudge
    asks "may I tell someone to MERGE this", and for THAT question a closed
    row is the loudest NO on the board, so the terminal is named here rather
    than read as plain READY one caller along.

    ONE RULE, NOT A GROWING LIST OF SPECIAL CASES. Its caller compares this
    word to "READY" and carries whatever else it gets, so every state — the
    rungs (SELF-REVIEW, CONTESTED, STALE-BASE, UNVERIFIED), every non-READY
    lifecycle state, every terminal annotation, and anything the ladder
    learns to emit later — is minted HERE, beside the ladder that already
    knows them. The first cure fixed the notifier for SELF-REVIEW ALONE by
    comparing sender to recipient on the raw row; that was one arm of a
    ladder, and the SAME defect came back wearing a different word — the
    same line said "ready: helm lr land" for a row hundreds of commits
    behind trunk whose gate bound a tree that no longer existed."""
    if lr.get("terminal"):
        hit = terminal_annotation(lr)
        return hit[0] if hit else str(lr.get("state"))
    return ready_word(lr, index=index, contest=contest,
                      contributors=contributors)


def _nudge_command(lr, selector):
    """The command a nudge should print for THIS row, deletion flag included.

    Takes the row the caller already has, so the flag is computed against the
    SAME projection the word came from. Never raises: it runs on a delivery
    leg.

    RETURNS THE FAILURE INSTEAD OF SWALLOWING IT. An unmeasurable deletion
    set must not become a confident flag, and it must not silently become a
    bare command either: THE LAND DOOR REFUSES when it cannot compute that
    set, so a nudge that prescribes the bare command while its word still
    reads plainly READY is prescribing a refusal. The caller degrades the
    word on this reason.
    """
    command = "helm lr land %s" % str(
        (lr or {}).get("id") or selector or "")[:17]
    try:
        gitdir = (lr or {}).get("repo_id")
        tip = (lr or {}).get("reviewed_tip") or (lr or {}).get("review_sha")
        if not gitdir or not tip:
            return command, "no repo binding or reviewed tip to scan"
        hits, err = _tracked_deletions(
            gitdir, _resolve_ref(gitdir, LOCAL_TRUNK), tip)
        if err:
            return command, "deletion scan failed: %s" % err
        if hits:
            command += " --ack-deletions"
        return command, None
    except Exception as exc:            # noqa: BLE001 — a nudge never raises
        return command, "deletion scan raised: %s" % exc


def land_nudge_instruction(selector, now=None):
    """(word, why, command) for ONE dispatch row, from ONE projection.

    ONE BOUNDARY, ONE MINTING, AND THAT MEANS ONE PROJECTION. Asking the
    ladder for the word and then projecting AGAIN for the command costs the
    wake two selected-chain projections and — the part that is a defect
    rather than a cost — lets a nudge combine a word read at T1 with deletion
    state read at T2. The row changes between them and nothing says so, which
    is exactly the split this door exists to close.

    So the projection happens HERE, once, and both halves are minted from the
    row it returned. `land_instruction` keeps its own signature for callers
    that want the word alone; it shares these internals rather than being
    called again from inside this one.
    """
    key = str(selector or "")
    bare = "helm lr land %s" % key[:17]
    try:
        # ONE SNAPSHOT, NOT ONE CALL. Counting project() calls is not the
        # property: the deletion state is read with GIT, and a git read taken
        # after the projection's scope has closed samples the repository at a
        # LATER instant than the word did. The word could describe the row at
        # T1 while the command described the tree at T2, which is the split
        # this door exists to close — and an arm that counts calls cannot see
        # it, because the count was always one.
        #
        # Opening the scope HERE puts the projection and the deletion probe
        # inside the same memo: one answer per question per projection, so
        # both halves are derived from the same pinned reading.
        with projscope.scope():
            lrs, unavailable = project(now=now, selector=selector)
            if unavailable:
                return "UNVERIFIED", unavailable, bare
            lr = lrs.get(key) or next(
                (v for v in lrs.values()
                 if key and str(v.get("id") or "").startswith(key)), None)
            if lr is None:
                return ("UNVERIFIED",
                        "%s is not a land loop in this projection" % key, bare)
            command, unmeasured = _nudge_command(lr, selector)
            if unmeasured:
                # THE DOOR'S OWN ANSWER FOR AN UNSCANNABLE DELETION SET IS
                # REFUSE, so the nudge must not say READY over it.
                return "UNVERIFIED", unmeasured, command
            word = land_instruction_word(lr)
            # THE REASON RIDES WITH THE WORD, and this is the rung whose word
            # says least on its own: "READY-SELF-REVIEW" names a condition and
            # withholds the only part the reader can act on — WHICH seat wrote
            # the chain it also approved. The notifier already prints
            # "<word> (<why>)" for every non-READY word and enumerates none of
            # them, so carrying the sentence here reaches that line without
            # the notifier learning a second vocabulary.
            if word == "READY-SELF-REVIEW":
                return word, independent_review(lr)[1], command
            return word, None, command
    except Exception as exc:            # noqa: BLE001 — a nudge never raises
        return "UNVERIFIED", "land instruction raised: %s" % exc, bare


def land_instruction(selector, now=None):
    """(word, why) for ONE dispatch row, through the SAME projection the board
    reads — a land instruction and the board must never have two oracles.

    "UNVERIFIED" WHENEVER HELM COULD NOT LOOK, with the reason beside it: the
    ladder's own word for a question it could not evaluate, used honestly one
    layer up where the looking failed. A reading that could not be taken must
    never come out as READY — that is the whole defect this exists to close,
    and a fallback to the old prescription would rebuild it under a cure.

    Never raises. The caller is a delivery leg running after a DURABLE
    verdict, outside the ledger lock; an exception here would cost the wake
    that the notifier exists to send."""
    # THE BOUNDARY IS THE WHOLE BODY, not the projection call alone. Picking
    # the row, scanning prefixes and minting the word all read a structure
    # this function did not build: a receipt that is valid JSON but not an
    # object satisfies the parser and then raises on `.get`, one line past a
    # narrower try. The caller is a delivery leg, so that escape cost BOTH
    # legs the wake this function exists to send — a docstring promising
    # "never raises" is not a boundary, the try is.
    try:
        lrs, unavailable = project(now=now, selector=selector)
        if unavailable:
            return "UNVERIFIED", unavailable
        # The projection answers with the selector's whole WORK CHAIN, so the
        # row itself must be picked back out of it.
        #
        # NO PREFIX BOUND HERE, AND THE REASON IS MEASURED. A bound was
        # specified to stop a full id re-opening identity through prefix
        # semantics — an id that missed the exact lookup going on to match
        # some longer row. That widening CANNOT OCCUR: dispatch ids are
        # uniform 32-hex, so `startswith` against a same-width id is equality,
        # and equality is what the dict lookup already tried. Both a length
        # comparison and an exact-id pass were written here and BOTH proved
        # tautological under mutation — removing either reddened nothing.
        # A guard that cannot fail reads as the thing holding the line, so
        # the honest shape is to hold no line here and say why.
        key = str(selector or "")
        lr = lrs.get(key) or next(
            (v for v in lrs.values()
             if key and str(v.get("id") or "").startswith(key)), None)
        if lr is None:
            return "UNVERIFIED", (
                "%s is not a land loop in this projection" % key)
        return land_instruction_word(lr), None
    except Exception as exc:            # noqa: BLE001 — a nudge never raises
        return "UNVERIFIED", "land instruction raised: %s" % exc
STAGE_ORDER = {"OPEN": 0, "AWAITING_REVIEW": 1, "AWAITING_BUILD": 1,
               "REVIEWED": 2, "CHANGES_REQUESTED": 2, "READY": 2,
               "MERGED_LOCAL": 3, "LANDED": 4, "SUPERSEDED": 4,
               "ABANDONED": 4, "DELIVERED_REPORT": 4}
# Who owes the next move. This is the field a poke/escalation actuator needs
# (open owner-ask 21350297): "stalled" alone never says whose turn it is, and
# nagging the reviewer for a fix the AUTHOR owes is how a watchdog earns its
# reputation for noise.
OWED_BY = {"OPEN": "integrator", "AWAITING_REVIEW": "reviewer",
           "AWAITING_BUILD": "builder",
           "CHANGES_REQUESTED": "author", "READY": "lander",
           "MERGED_LOCAL": "lander", "REVIEWED": "nobody (undeclared)",
           "LANDED": "nobody", "SUPERSEDED": "nobody",
           "ABANDONED": "nobody", "DELIVERED_REPORT": "nobody"}

# Trunk is main-or-master; the landing target is the upstream trunk when a
# remote publishes one, else the local trunk (a local-first estate never
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
# ACTUALLY SAID. Its absence was a defect one chmod reproduces: a
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

    VERBATIM FIELDS, NEVER RE-CANONICALIZED (#142 r2): replay
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
# 2026-08-01); a 400 cap reported every one of them UNKNOWN.
STORED_PID_SCAN_CAP = 600


def _rescue_cache_path():
    # Beside the receipts it rescues, in the same durable estate that
    # "survives worktree prune" — a cache keyed by an immutable
    # patch-id is valid forever, so it must outlive any worktree.
    return home.global_dir() + "/land-rescue.json"


def _rescue_lock_path():
    return _rescue_cache_path() + ".lock"


def _monotonic():
    import time as _time
    return _time.monotonic()


def _answers_for(gitdir, pid, sha, trunk):
    """Would the reader accept `sha` for `pid` right now? BOTH of its legs.

    `_readable_correlation` is only the CONTENT leg. The reader also requires
    the target to be ON trunk, and a check that asks one of the two calls a
    dead entry healthy. A probe measured the gap this leaves: a key holding a
    sha that still CARRIES its patch-id but has been taken off trunk by a
    reset or force-move passes the content leg forever, so the never-demote
    clause protects it and the backfill skips it — permanent, and invisible,
    while a valid on-trunk carrier sits available.

    Ancestry is checked HERE and not inside `_readable_correlation` because
    it is only meaningful against a trunk the caller has pinned; the content
    leg is immutable and needs no such context.

    TRI-STATE: True / False / None. NONE MEANS THE ANCESTRY QUESTION WAS NOT
    ANSWERED, and it is not False. False here says "this sha does not answer
    for that pid", which DEMOTES a held entry and licenses a rewrite — so a
    repository we could not read must never produce it.

    WHAT THIS FUNCTION STILL CANNOT SAY, stated because the omission is a
    decision, AND IT IS THE CONTENT LEG ONLY. `_readable_correlation` answers
    None rather than False on a hash it could not read, which is the right
    direction — but underneath it `_patch_id` is fail-open and returns None
    both for a spawn that failed and for a range with no diff in it, so "git
    could not answer" and "git answered, there is no id" arrive here as one
    value. Separating them needs an availability-aware hash face and is
    task/2032's, not this slice's.

    ANCESTRY DOES NOT HAVE THAT SHAPE and saying it did was the error in this
    sentence's previous form. `_ancestry` returns three distinct values:
    ANCESTOR, NOT_ANCESTOR, and UNDETERMINED for exit 128, a timeout or an
    OSError — so a MEASURED non-ancestor and an unanswerable probe are
    already different facts there, and this function preserves the
    difference by returning None on UNDETERMINED. The gap is in the content
    leg's own None, not in ancestry.

    UNKNOWN is PRESERVED at every door; proof AVAILABILITY is not solved.
    """
    sha = str(sha or "").strip()
    if not sha or not trunk:
        return False
    # ANCESTRY IS TRI-STATE AND `!= ANCESTOR` FLATTENS IT. `_ancestry` returns
    # UNDETERMINED for exit 128 — a bad or missing object, a corrupt repo —
    # and for a timeout or OSError, and its own docstring says folding that
    # into "not an ancestor" asserts a negative on an unknown.
    anc = _ancestry(gitdir, sha, trunk)
    if anc == UNDETERMINED:
        return None
    if anc != ANCESTOR:
        return False
    return _readable_correlation(gitdir, pid, sha)


def _merge_rescue_entries(gitdir, entries, trunk, deadline=None):
    """Merge {patch-id: sha} into the durable store. -> (written, incomplete).

    INCOMPLETE MEANS THIS RUN DID NOT DECIDE EVERYTHING IT WAS ASKED, and a
    spent budget is only one of its causes. An entry whose admission question
    git could not answer is equally undecided: nothing was written for it and
    nothing about it is settled. Both reach the caller as the same status,
    because the caller acts on exactly one distinction — is there work left.

    THE ONE WRITE DOOR, AND IT EXISTS BECAUSE THERE ARE THREE WRITERS. The
    read path caches an index hit, the land feed records a fresh proof, and
    the backfill seeds history — each of them was doing its own read, edit and
    whole-file write, so two overlapping runs silently dropped one side's
    entries. `write_json` is atomic per FILE, which makes the loser's write
    complete and its content gone.

    NO GIT RUNS UNDER THE LOCK, AND THE LOCK NEVER BLOCKS. The first cut held
    an exclusive lock across every admission check, and one of the three
    writers is the READ path's cache-fill — so every reader queued behind
    another writer's git calls. Both halves are wrong for a cache whose own
    contract is "slow, never wrong". The git work happens outside, the lock
    covers only read-edit-write, and it is taken NON-BLOCKING: losing the race
    costs a re-derivation next time, which is exactly what a cold cache costs.

    THE WRITE IS COMPARE-AND-SET. Deciding outside the lock means the store
    can move underneath the decision, so each entry records the value it was
    judged against and is applied only if that value is still there. A key
    that changed under us is left alone — whoever wrote it did so with at
    least as fresh a view.

    ADMISSION IS THE READER'S CONTRACT, BOTH LEGS (`_answers_for`), and so is
    the never-demote test. An entry is admitted only if the reader would
    accept it, and an existing entry is preserved only if the reader would
    still accept THAT — a patch-id names one change, so the first genuinely
    usable answer is the answer, but a dead one is not an answer.

    THE BUDGET COVERS THE ADMISSION CHECKS, because they are git calls and
    there can be one per entry. Accepting a deadline and then spending
    unbounded git after the caller's loop had already stopped is a leak, and
    it was measured as one: three entries at ten seconds each returned
    (3, 3, False) against a one-second budget.

    FAIL-OPEN: a store that cannot be written is slow, never wrong.
    """
    if not entries:
        return 0, False
    incomplete = False
    try:
        snapshot = pk.read_json(_rescue_cache_path(), {}) or {}
    except Exception:                          # noqa: BLE001
        return 0, False
    plan = {}
    for pid, sha in entries.items():
        if deadline is not None and _monotonic() >= deadline:
            incomplete = True
            break
        # ONLY A MEASURED YES ADMITS A CANDIDATE. `_answers_for` is
        # tri-state, and an unanswered question is not a yes.
        #
        # AND AN UNANSWERED QUESTION IS NOT A FINISHED ONE EITHER. `is not
        # True` is the correct ADMISSION rule and it says nothing about
        # STATUS: a candidate git refused and a candidate git could not judge
        # both leave the plan untouched, and only the second one means this
        # run still owes an answer. Reporting them alike tells the caller the
        # work is done when the question was never put.
        admits = _answers_for(gitdir, pid, sha, trunk)
        if admits is not True:
            if admits is None:
                incomplete = True
            continue
        key = "%s\t%s" % (str(gitdir or ""), pid)
        held = snapshot.get(key)
        if held == sha:
            continue
        if isinstance(held, str) and held:
            # AND ONLY A MEASURED NO EVICTS ONE. `is not False` is the whole
            # point here and `if answer:` would be wrong: TRUE means the
            # reader still accepts what is held, NONE means we could not ask,
            # and both of those leave it alone. Rewriting on NONE is a write
            # authorised by a question that was never answered — the same
            # sentence as the tri-state above, one line down, at the only
            # call site that DESTROYS something.
            answer = _answers_for(gitdir, pid, held, trunk)
            if answer is not False:
                # Same split as the candidate door above: TRUE settles this
                # key, NONE leaves it open. Preserving on NONE is right and
                # is unchanged; calling the run finished on it is not.
                if answer is None:
                    incomplete = True
                continue
        plan[key] = (held, str(sha).strip())
    if not plan:
        return 0, incomplete
    import fcntl
    fd = None
    try:
        fd = os.open(_rescue_lock_path(), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return 0, incomplete      # another writer holds it; try next time
        c = pk.read_json(_rescue_cache_path(), {}) or {}
        written = 0
        for key, (seen, sha) in plan.items():
            if c.get(key) != seen:
                continue              # moved under us — theirs is no staler
            c[key] = sha
            written += 1
        if written:
            pk.write_json(_rescue_cache_path(), c)
        return written, incomplete
    except Exception:                          # noqa: BLE001
        return 0, incomplete
    finally:
        if fd is not None:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(fd)


def _correlated_sha(gitdir, pid, candidates, trunk):
    """The first candidate that answers for `pid` on `trunk`, or None.

    BOTH OF THE READER'S LEGS, in the order it applies them: the sha must be
    ON trunk, and it must still carry `pid`. Ancestry is checked HERE, where
    trunk is pinned by the land that is happening right now; it is deliberately
    absent from `_readable_correlation`, which is consulted against a trunk
    that has since moved.
    """
    for sha in candidates:
        # A MEASURED YES, never a truthy one: `_answers_for` returns None
        # when the question could not be asked, and correlating on that would
        # publish a sha nothing proved.
        if _answers_for(gitdir, pid, sha, trunk) is True:
            return str(sha).strip()
    return None


# THE SENTENCE FOR EACH CAUSE, because a reader is told WHY it got UNKNOWN
# and the old text said "the scan was CAPPED" for every reason a window could
# fail. Two of the three were never a cap: one is a budget that ran out and
# one is a commit inside the window that could not be hashed at all.
_INDEX_MISS_WHY = {
    "capped": ("absent from the newest %d trunk commits, but the scan was "
               "CAPPED there" % int(STORED_PID_SCAN_CAP)),
    "budget": ("the trunk scan was cut short by its own deadline, so part of "
               "the window was never walked"),
    "unmeasured": ("the trunk scan finished, but at least one commit inside "
                   "it could not be hashed, so this id may sit on a commit "
                   "the index never managed to read"),
}

# The same three causes as a SHORT status, for surfaces that print the
# screen's state inline rather than a sentence.
_INDEX_SCREEN_WHY = {
    "capped": "capped at %d commits" % int(STORED_PID_SCAN_CAP),
    "budget": "cut short by its deadline",
    "unmeasured": "a commit inside the window could not be hashed",
}

INDEX_CAPPED = "capped"          # the window ended before history did
INDEX_BUDGET = "budget"          # the walk was cut short by its deadline
INDEX_UNMEASURED = "unmeasured"  # inside the window, a hash could not be taken


def _stored_patch_index(gitdir, trunk, cap=STORED_PID_SCAN_CAP, deadline=None):
    """{patch-id: trunk sha} for the newest `cap` trunk commits, plus WHY a
    miss in it would not be proof. Built ONCE per call — the naive shape asks
    this question per receipt and pays the whole walk each time.

    THE SECOND ELEMENT IS A CAUSE, NOT A BOOLEAN, AND `None` MEANS COMPLETE.
    It was `capped`, which named ONE of the three ways this index can fail to
    settle a question, and callers read it as the whole story. The value now
    says which: INDEX_CAPPED (the window ended before history did),
    INDEX_BUDGET (the walk stopped on its deadline), INDEX_UNMEASURED (the
    walk finished, and inside it a commit's hash could not be taken).
    Completeness and its cause are ONE field, so "complete, because of the
    cap" cannot be built; every existing caller reads it for truth and keeps
    working unchanged, because complete is falsy and every cause is not.

    WHAT EACH ANSWER LICENSES, and the asymmetry is the point. A HIT is a hit
    under every cause: the id was successfully measured against a commit that
    is on trunk, and nothing an incomplete walk failed to see can take that
    back. A MISS is proof of absence ONLY when the cause is None. Under any
    cause a miss is UNKNOWN — the id may sit on a commit past the window, on
    one the budget never reached, or on one whose hash git declined to give.

    INDEX_UNMEASURED IS THE ONE THAT HAD NO CARRIER BEFORE. `if pid:` dropped
    an unmeasurable commit silently, the walk still reached the end, and the
    window reported itself whole — so a later miss on exactly that commit read
    as "not on trunk". The cap flag could never carry it: the cap describes
    how FAR BACK the walk went, and this is about what it failed to measure
    INSIDE that range.

    ASK ANCESTRY FIRST. THIS IS THE FALLBACK, NOT THE DOOR.
    `_ancestry(gitdir, tip, trunk) == ANCESTOR` answers "did this land" for
    free, UNBOUNDED, needing no index and no patch-id, and an ancestor is its
    own answer because that sha is already on trunk. This index exists only
    for the tips ancestry CANNOT answer — a rewritten sha, a pruned object.

    REACHING FOR IT FIRST HAS FAILED THREE TIMES ON ONE PROBLEM, and each
    time it produced a store that fills up and never answers:
      * the rescue cache's own writer took `sha = index.get(pid)`, so its feed
        was downstream of this scan and it held TEN entries;
      * a land-time feed keyed on a live-derived id missed the reader's key on
        6 of 10 measured receipts;
      * a backfill built on this index stored 0 of 148, because 0 of 154
        stored patch-ids hit it — including one landing at depth 107, INSIDE
        the window, whose stored id still missed.
    The last measurement is the general one: THE RECEIPT'S STORED PATCH-ID IS
    NOT THE LANDED COMMIT'S PATCH-ID, so an index keyed on trunk's ids cannot
    be looked up by a receipt's. Ancestry does not care.

    MEASURED on this repo 2026-08-01: 7.9 ms per commit, so 600 commits is
    ~4.7s. That is why the answer is cached (below) rather than recomputed:
    receipts are immutable and a landed patch stays landed, so a hit is true
    forever. Cost is paid once, by whoever renders first."""
    # THE POPULATION AND THE HASHES MUST BE READ THROUGH ONE VIEW. This walk
    # ENUMERATES the commits whose ids the index will hold, and the loop below
    # HASHES each one through the object view. Reading the population under the
    # ambient view instead lets a rewriter hide a commit from the enumeration
    # while its original-view hash is still what a lookup asks for: with a
    # graft making a child look parentless, the parent never enters the walk,
    # the walk still reports a COMPLETE window, and a later miss on the
    # parent's id reads as ABSENT — a negative asserted about a commit this
    # index was never shown. Same view for both, or the completeness claim is
    # about a different repository than the lookups are.
    rv = _object_view(gitdir, "rev-list", trunk, "-%d" % int(cap))
    if rv is None or rv.returncode != 0:
        return None, False
    shas = [ln.strip() for ln in (rv.stdout or "").splitlines() if ln.strip()]
    index = {}
    holes = 0
    for sha in shas:
        # THE WALK IS THE EXPENSIVE HALF, SO THE BUDGET HAS TO REACH INSIDE IT.
        # A caller that only checks its deadline BETWEEN index lookups has not
        # bounded anything: this loop is ~7.9ms per commit and the uncapped
        # cap is 100000, so one call could overrun a 20s budget by minutes.
        # A run cut short here is a BREACHED CAP, not a small index — the
        # partial answer cannot prove a patch is absent, which is exactly what
        # the capped flag means everywhere else.
        if deadline is not None and _monotonic() >= deadline:
            return index, INDEX_BUDGET
        # A COMMIT WHOSE HASH COULD NOT BE TAKEN IS A HOLE IN THE COVERAGE,
        # AND `if pid:` DROPPED IT SILENTLY. The walk then reaches the end,
        # reports a complete window, and a later MISS on the wanted id reads
        # as "not on trunk" — a negative asserted about a commit this index
        # never managed to look at. The cap flag cannot carry it either: the
        # cap describes how far back we walked, and this is about what we
        # failed to measure INSIDE that range.
        #
        # An EMPTY_RANGE is not a hole. git answered, there is no id for that
        # commit, and no lookup can ever want one — coverage is intact.
        state, pid = _measured_patch_id(gitdir, sha)
        if state == UNMEASURED:
            holes += 1
        elif pid:
            index.setdefault(pid, sha)
    if len(shas) >= int(cap):
        return index, INDEX_CAPPED
    if holes:
        return index, INDEX_UNMEASURED
    return index, None


def _stored_patch_on_trunk(gitdir, patch_id, trunk, index=None, why=None):
    """Did the change a receipt RECORDED land on `trunk`? -> (sha, why).

    THE CASE THIS EXISTS FOR: `_landing_proof` answers about a TIP, and both
    its rungs need that tip readable — ancestry resolves it, the patch
    comparison diffs it. Land receipts routinely outlive their tips: the
    integrator lands a rebased or cherry-picked commit, the gated object
    becomes reachable from nothing, git prunes it. Measured on this repo's own
    receipts: ALL SIX had BOTH anchors dead (reviewed_tip AND trunk_sha — 12
    of 12 objects unreadable), every rung returned unknown, and the owner's
    LANDED card read "? UNKNOWN" six times. He spotted it.

    The receipt carries `patch_id`, computed at WRITE time while the object
    was alive — 6 of 6 have one. It survives the pruning that killed the shas
    and is the only identity left that can answer. Measured: all six resolve,
    at trunk depths 505-579, which is why the scan bound is not small.

    A breached bound is UNKNOWN and never absent: a cap that skips commits
    cannot prove a patch is not among them.

    `why` IS THE INDEX'S COVERAGE CAUSE, None WHEN THE WINDOW IS COMPLETE, and
    it was `capped` — one of the three reasons a window cannot settle a
    question, standing in for all of them. The two directions are NOT
    symmetric and that is what makes one field enough:

      A HIT IS A HIT UNDER EVERY CAUSE. The id was measured, it matched, and
      the commit carrying it is on trunk. Nothing the walk failed to reach can
      unmake a positive it already has — so an incomplete window still proves
      PRESENCE, which is a review's C1.

      A MISS IS ABSENCE ONLY WHEN `why` IS None. Under any cause the id may
      sit past the window, on a commit the budget never reached, or on one
      whose hash git declined to give — so the answer is UNKNOWN, and the
      reason NAMES the cause rather than saying "capped" for all three. C2 is
      the case that used to slip: a nonempty index whose hole is the WANTED
      commit looks exactly like a complete window that has never heard of it."""
    pid = str(patch_id or "").strip().lower()
    if not pid or not _HEXID.fullmatch(pid):
        return None, None
    # KEYED BY REPO AND PATCH-ID, never patch-id alone: the estate
    # is global and one patch-id can exist in more than one repository, so a
    # bare-pid cache let repo B inherit repo A's foreign sha.
    ckey = "%s\t%s" % (str(gitdir or ""), pid)
    cached = (pk.read_json(_rescue_cache_path(), {}) or {}).get(ckey)
    if isinstance(cached, str) and cached:
        # A CACHED CORRELATION IS STILL RE-CHECKED against live trunk. The defect:
        # "after trunk reset a cached hit remains true." A patch-id is
        # immutable but TRUNK IS NOT — a reset or force-move can take the
        # correlated commit off it, and a cache that never re-asks would keep
        # naming a sha nobody can reach. One ancestry call per hit; the walk
        # is still what the cache saves.
        # A CACHED HIT MUST RE-PROVE BOTH LEGS, and the second one is the leg
        # I missed: ancestry proves the cached sha is still
        # ON trunk, and says NOTHING about whether that sha still carries the
        # patch-id it was cached UNDER. The poisoned-cache probe, one repo:
        # key=(gitdir, pid_A) -> sha_B where B is on trunk and patch_id(B) !=
        # pid_A. Ancestry passes, and the card then tells the reader "this
        # receipt's patch-id matches B" when it does not. I validated the
        # answer's LIVENESS and never its CORRECTNESS — a cache that checks
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
        # THE HOT PATH WRITES OPPORTUNISTICALLY. This is a read; the store is
        # a cache to it, so a lock it cannot take without waiting is a reason
        # to move on, not to block a render behind another writer.
        _merge_rescue_entries(gitdir, {pid: sha}, trunk)
        return sha, "content identity (the receipt's stored patch-id)"
    if why:
        return None, "%s — UNKNOWN, not absent" % _INDEX_MISS_WHY.get(
            why, "the trunk scan could not cover this question (%s)" % why)
    return None, None




def verified_lands(limit=8):
    """(rows, unavailable) — recorded landings, each RE-DERIVED against trunk NOW.

    THE OWNER-VERIFICATION SURFACE (0.2 council item 4: "give the owner ONE
    end-to-end verification he can do himself"). Every other land signal is an
    agent's claim rendered back — an agent says it landed, the row says LANDED,
    and the reader is still taking our word one layer down.

    PROOF IS CONTENT, NEVER THE MERGE COMMIT. A receipt's `trunk_sha` is the
    LOCAL trunk at land time, and local trunk is ephemeral by construction:
    local main is re-pointed at origin/main after each push, the old merge
    commit becomes unreachable, and git collects it. Measured 2026-08-01: 0 of
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
    # THE AGGREGATE BOUND, and it is the floor rather than the cure. Every
    # git spawn under this loop is individually fast and individually bounded
    # (subprocess timeout=5); the DERIVE over N rows had no bound at all, so
    # the cost was N times a per-item budget and the hook that calls this has
    # a 10s one. Per-item correctness is never the standard: something must
    # bound the aggregate, and until this line nothing did.
    #
    # On expiry the remaining rows report UNKNOWN, never ABSENT — a row we
    # ran out of time to prove is not a row proven not to have landed, and
    # those two must never share a value on a surface people read as truth.
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


_GIT_LOOKED_AND_MISSED = 1   # rev-parse looked and could not resolve it
                             # (any other nonzero: the read itself failed)


HASHED = "hashed"          # git answered and produced a content id
EMPTY_RANGE = "empty"      # git answered; the range holds no diff, so no id
UNMEASURED = "unmeasured"  # git did not answer, so nothing is known


# ONE OBJECT VIEW. Three independent mechanisms rewrite a commit's ancestry
# and NO ONE OF THEM SUBSTITUTES FOR ANOTHER -- measured on git 2.53.0 against
# a repository whose legacy graft file makes a child rootless:
#
#   plain                     parents -> empty   (the graft is in effect)
#   --no-replace-objects      parents -> empty   (replace refs only; NOT grafts)
#   GIT_GRAFT_FILE=/dev/null  parents -> the real parent
#   --is-shallow-repository   false              (the shallow check cannot see it)
#
# So a reader that wants the OBJECT's own ancestry must disable replace refs
# AND point the graft file at a known-readable empty source, and must still
# refuse outright when the repository is shallow, where no bypass exists
# because the parent objects are genuinely absent.
_NO_REPLACE = ("--no-replace-objects",)
_NO_GRAFTS = {"GIT_GRAFT_FILE": os.devnull}


def _object_view(gitdir, *args):
    """Ask git a question with every ancestry rewriter we CAN disable off."""
    return _git(gitdir, *(_NO_REPLACE + args), env=_NO_GRAFTS)


def _is_parentless(gitdir, pinned):
    """Does the OBJECT `pinned` have no parents? True / False / None(unknown).

    NONE IS THE ANSWER WHENEVER WE CANNOT ESTABLISH THE OTHER TWO, and that is
    most of this function. Parentlessness is the licence to hash a commit
    against the empty tree, so a wrong TRUE mints a whole-tree identity for a
    commit that has a parent we merely could not see.

    EMPTY OUTPUT IS NEVER THE EMPTY-PARENT RECORD. `--format=%P` alone answers
    "" both for a root and for a call that produced nothing, which is the same
    absence-means-negative substitution this whole module is about, one level
    down. The record is FRAMED and SELF-IDENTIFYING instead: `%H|%P` yields
    `<sha>|` for a root and `<sha>|<parents>` for a child, so the parent field
    is explicitly PRESENT and empty, and a reply about a different commit is
    refused rather than read.

    THE REPOSITORY MUST ALSO CARRY NO SHALLOW GRAFTS. A shallow boundary is
    parentless to every traversal git offers -- measured: both `%P` and
    `rev-list --max-parents=0` report it so -- and there is no bypass, because
    the parent objects are not present to be read. The refusal is deliberately
    COARSE: every root in a shallow repository reads UNKNOWN, which costs
    availability in partial clones and never mints a false identity.
    """
    typed = _object_view(gitdir, "cat-file", "-t", pinned)
    if typed is None or typed.returncode != 0:
        return None
    if (typed.stdout or "").strip() != "commit":
        return None
    shallow = _git(gitdir, "rev-parse", "--is-shallow-repository")
    if shallow is None or shallow.returncode != 0:
        return None
    if (shallow.stdout or "").strip() != "false":
        return None
    rec = _object_view(gitdir, "log", "--format=%H|%P", "-1", pinned)
    if rec is None or rec.returncode != 0:
        return None
    # THE TERMINATOR IS PART OF THE RECORD. `splitlines` discards it, and the
    # value that discarding admits is exactly the one this contract excludes:
    # a CHILD's `<pin>|<parent>` truncated immediately after the pipe is
    # byte-identical to a ROOT's complete `<pin>|`. Identity plus separator
    # proves some output arrived, never that the parent field FINISHED. So the
    # record must end where git ends it, and anything short of that is a read
    # we did not complete.
    out = rec.stdout or ""
    if not out.endswith("\n"):
        return None
    body = out[:-1]
    if "\n" in body:
        return None                    # more than one record for one commit
    ident, sep, parents = body.partition("|")
    if not sep or ident != pinned:
        return None
    return not parents.strip()


def _root_patch_id(gitdir, tip):
    """The content a PARENTLESS commit introduces. -> (state, id).

    Reached whenever the ordinary range diff failed, which is the shape a true
    root produces -- and the shape a shallow boundary, a replace-graft and a
    legacy graft all produce too. Those need the opposite answer from a root,
    and `_is_parentless` is the whole of that decision.

    True root  -> the diff against the empty tree, hashed like any other.
    Empty root -> EMPTY_RANGE: git answered, there is nothing to hash.
    Anything else, including every read we could not complete -> UNMEASURED.
    """
    pinned = _object_view(gitdir, *_REF_ARGV,
                          str(tip or "") + "^{commit}")
    if pinned is None or pinned.returncode != 0:
        return UNMEASURED, None
    sha = (pinned.stdout or "").strip()
    if not sha:
        return UNMEASURED, None
    if _is_parentless(gitdir, sha) is not True:
        return UNMEASURED, None
    d = _object_view(gitdir, "-c", "diff.noprefix=false", "diff-tree",
                     "--root", "-p", sha)
    if d is None or d.returncode != 0:
        return UNMEASURED, None
    if not d.stdout:
        return EMPTY_RANGE, None
    p = _git(gitdir, "patch-id", "--stable", input_text=d.stdout)
    if p is None or p.returncode != 0:
        return UNMEASURED, None
    parts = p.stdout.split()
    return (HASHED, parts[0]) if parts else (EMPTY_RANGE, None)


def _measured_patch_id(gitdir, tip, trunk_ref=None):
    """-> (state, id). THE ANSWER AND WHETHER THERE WAS ONE, kept apart.

    `_patch_id` answers None for FIVE different facts: no gitdir or tip, a
    merge-base or diff that could not run, a diff that ran and was EMPTY, a
    `patch-id` spawn that failed, and a `patch-id` that produced no output.
    Three of those are "git could not answer" and two are "git answered, and
    there is no id here" — opposite facts wearing one value. A reader cannot
    separate them, so every consumer that treats None as absence is asserting
    a negative on an unmeasured question, and the assertion is invisible.

    THE STATE IS THE ANSWER AND THE ID IS ONLY EVER A DETAIL OF ONE STATE.
    `id` is a non-empty string when and only when the state is HASHED; the
    other two states carry None by construction, from single return points,
    so "unmeasured but here is the id" cannot be built. That is the shape
    a review asked for: contradictory states unrepresentable, rather than two
    independent fields a caller can combine wrongly.

    EMPTY_RANGE IS A MEASUREMENT, NOT A FAILURE. A range with no diff is a
    real, complete answer — git ran, git looked, there is nothing to hash —
    and a walk that meets one has NOT lost coverage. Folding it in with the
    failures would make an ordinary empty commit look like a broken repo.

    WHAT IS STILL NOT SEPARATED, said because it is a decision: `_git`
    returning None covers a spawn failure, a timeout and an OSError alike, so
    UNMEASURED does not say WHY git was silent. It says the question was not
    answered, which is the distinction every caller here actually turns on.
    """
    if not gitdir or not tip:
        return UNMEASURED, None
    base = None
    if trunk_ref:
        # THE MERGE-BASE LEG HAS THREE OUTCOMES AND THE OLD CODE HAD ONE
        # FALLBACK FOR ALL OF THEM. `if rc == 0 and stdout.strip()` sends a
        # spawn failure, a malformed success and a measured no-base down the
        # SAME path -- the tip's own diff -- so an id computed after git broke
        # is indistinguishable from one computed after git answered.
        #
        # rc 1 IS THE ONLY MEASURED NO-BASE. It is this command's documented
        # way of saying the two commits share no ancestor, and falling back to
        # the tip's own diff on it is correct and always was.
        #
        # rc 0 WITH EMPTY OUTPUT IS MALFORMED, NOT A NO-BASE. Success means a
        # base was found and printed; success with nothing printed means the
        # answer did not survive to us. Treating it as "no base" invents a
        # measurement out of a broken read, and it is the exact shape that
        # makes a range silently become the wrong range.
        mb = _object_view(gitdir, "merge-base", tip, trunk_ref)
        if mb is None:
            return UNMEASURED, None
        if mb.returncode == 0:
            base = mb.stdout.strip()
            if not base:
                return UNMEASURED, None
        elif mb.returncode != 1:
            return UNMEASURED, None
    rng = base + ".." + tip if base and base != tip else tip + "^.." + tip
    # same pin as _range_patch_id, same measurement: this id is SIGNED into
    # land receipts that cross boxes, so the diff shape cannot float on the
    # minting box's diff.noprefix
    d = _object_view(gitdir, "-c", "diff.noprefix=false", "diff", rng)
    if d is None or d.returncode != 0:
        # A ROOT COMMIT IS NOT A HOLE. `tip^..tip` names a parent, a root has
        # none, and git exits 128 on the bad revision -- so a walk that
        # reaches the beginning of a history reports an unmeasurable commit
        # forever, for a reason that is not a failure. Every repository has at
        # least one such commit and a repository built from unrelated
        # histories has several, so this is not a single-commit exception.
        #
        # BUT NO PARENT IS NOT NO CONTENT, and calling it EMPTY_RANGE was a
        # false measurement rather than a missing one. A root ADDS its whole
        # tree against the empty tree and has a perfectly real patch-id, as
        # any other commit does. Dropping it while certifying the window
        # COMPLETE manufactures an absence: a
        # later commit that re-adds what the root introduced hashes to the
        # root's id, misses an index that never held it, and the ladder above
        # reports ABSENT for content that is on trunk.
        #
        # AND `rev-list --parents` CANNOT TELL A ROOT FROM A SHALLOW
        # BOUNDARY. Traversal obeys the graft: measured on a real --depth 1
        # clone, the boundary prints ONE token exactly like a root. Believing
        # it is worse than the omission it replaced, because `diff-tree
        # --root` there does not fail -- it succeeds against the whole tree
        # (28 MB in that clone) and mints a large WRONG id.
        #
        # THE COMMIT OBJECT IS THE DISCRIMINATOR. `rev-list` is a view;
        # `cat-file commit` is the object, and the shallow boundary still
        # carries its own `parent` header. Headers are read only up to the
        # blank line, so a message body cannot forge one.
        # REACHED ON ANY FAILED RANGE, not only when `base` is empty. A
        # merge-base that resolves to the tip ITSELF also falls back to
        # `tip^..tip`, so a root reached that way used to skip this helper and
        # report UNMEASURED while the same commit asked about directly hashed
        # fine -- the answer depending on which caller asked. The helper
        # self-guards: a commit whose object names a parent leaves as
        # UNMEASURED, which is what an ordinary failed range should say.
        return _root_patch_id(gitdir, tip)
    if not d.stdout:
        return EMPTY_RANGE, None
    p = _git(gitdir, "patch-id", "--stable", input_text=d.stdout)
    if p is None or p.returncode != 0:
        return UNMEASURED, None
    parts = p.stdout.split()
    if not parts:
        # git ran and printed nothing. That is `patch-id` declining to name an
        # id for this input, not a failure to ask — the same class as an empty
        # range, and a walk that meets it has still covered this commit.
        return EMPTY_RANGE, None
    return HASHED, parts[0]


def _patch_id(gitdir, tip, trunk_ref=None):
    """git patch-id of the landed commits — a rebase/ff-STABLE content identity
    for the receipt (a rebased land recomputes to the same id). Best-effort: the
    range from the branch's fork point (merge-base with trunk) to the reviewed
    tip, falling back to the tip commit's own diff when that range is empty (the
    tip already sits on trunk after a fast-forward). Fail-open None — a receipt
    is keyed by the reviewed tip, so a missing patch-id never gates it.

    THE FAIL-OPEN PROJECTION OF `_measured_patch_id`, kept because its
    contract is right for the callers that have it. A receipt keyed by the
    reviewed tip does not need to know WHY there is no id; it needs one value
    meaning "no usable id", and every existing caller is written to that.
    Callers whose answer changes with availability — anything that would read
    a miss as ABSENCE — ask `_measured_patch_id` instead."""
    _state, pid = _measured_patch_id(gitdir, tip, trunk_ref)
    return pid


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
    p = _git(gitdir, *_REF_ARGV, ref)
    return p.stdout.strip() if p is not None and p.returncode == 0 else None


# WHY minting is suppressed, per strict-resolver state. Each names the exact
# non-absence it found, because "not witnessed" and "I could not establish
# whether it is witnessed" are different sentences to put in front of an
# operator — and R_CONFLICT in particular means the record ALREADY disagrees
# with itself, where minting another row makes it strictly worse.
_WITNESS_BLOCKED = {
    R_UNREADABLE: ("the land-receipt index could not be READ, so whether this "
                   "land is already witnessed is UNKNOWN"),
    R_REJECTED: ("a recorded receipt for this tip FAILS strict replay, so the "
                 "record is corrupt rather than absent — minting beside it "
                 "would bury the corruption"),
    R_CONFLICT: ("recorded receipts for this tip DISAGREE (or a valid row has "
                 "an invalid sibling), so the record already contradicts "
                 "itself — another row makes it worse, not better"),
    # R_CONTRADICTED IS NOT LISTED, and its absence is the point.
    # I mapped it by NAME, having never seen the resolver emit it — and it
    # cannot: every return in _receipt_for and _receipt_for_rows yields
    # R_NONE / R_REJECTED / R_CONFLICT / R_LOCAL, and the ledger markers add
    # only R_UNREADABLE. A named branch with no reachable fixture OVERSTATES
    # what the resolver can tell you, and reads to the next author as evidence
    # that someone observed this state. The generic fallback below covers a
    # future state safely and claims nothing about today's.
}


def _existing_receipt(tip):
    """Is this reviewed tip ALREADY witnessed? -> (row, None, R_LOCAL) |
    (None, None, R_NONE) | (None, reason, state).

    THE THIRD ELEMENT IS THE RESOLVER STATE and it is why this returns three
    things instead of two. A caller handed only a sentence cannot tell "I
    looked and there is no receipt" from "I could not look", and those want
    OPPOSITE actions: the first is cured by minting, the second is made
    strictly worse by it.

    DELEGATES TO `_receipt_for`, THE STRICT RESOLVER, and that is the whole
    point of this version. My first cut walked `_receipts_by_tip` and took the
    first row that passed `_validate_receipt` — which LAUNDERS exactly the
    states the resolver exists to name: a valid row with an INVALID SIBLING is
    R_CONFLICT, and two individually-valid rows with DIVERGENT payloads are
    R_CONFLICT, and both of those returned "witnessed" from a first-match
    scan. A review found it; a second reader of a durable file starts at zero
    on every edge the first already paid for, and I had just written that
    sentence in the previous commit.

    ONLY R_LOCAL SUPPRESSES A MINT and ONLY R_NONE MINTS. Every other state is
    a non-absence: the record says something, and what it says is not "no
    receipt". Minting into a conflicted or unreadable record cannot improve it.
    """
    state, row, _why = _receipt_for(tip, _receipts_by_tip())
    if state == R_LOCAL:
        return row, None, state
    if state == R_NONE:
        return None, None, state
    # THE STATE TRAVELS WITH THE REASON (row 141d46c5a6ab). Returning
    # only a sentence leaves the caller unable to tell "I looked
    # and there is no receipt" from "I could not look" — and the comment right
    # above _WITNESS_BLOCKED already said those are different sentences to put
    # in front of an operator. Flattening them here is what let an UNKNOWN be
    # rendered as a NEGATIVE two frames later, under text prescribing the very
    # duplicate mint this function exists to prevent.
    return None, _WITNESS_BLOCKED.get(
        state, "the receipt record for this tip is in state %r, which is not "
               "absence" % (state,)), state


def _witness_land(lr, reviewed_tip, gitdir, trunk_ref, trunk_sha):
    """Write the land receipt for a fold, and SAY SO LOUDLY when it cannot be.

    RECEIPTING IS FAIL-OPEN BY LAW AND THAT IS NOT THE DEFECT. A land must
    never be blocked because a signer is unavailable. The defect is that
    fail-open was also SILENT: 56 folds produced ONE receipt on 2026-08-05
    and nothing anywhere said so. So this keeps the fail-open and removes the
    silence — an unwitnessed land now REPORTS itself, as data on the close's
    own result, so every surface can render it and none has to be told.

    WHAT THIS IS FOR IS THE AUDIT TRAIL, NOT A DISPLAY, and the distinction
    was corrected by the owner the same morning. The frozen LANDED card was
    the symptom that surfaced this, but its cure is elsewhere: a card must
    DERIVE its rows from what actually landed, live, with the receipt as a
    per-row witnessed/unwitnessed BADGE. A missing signature must never make
    a real land invisible. This function does not make any surface correct
    and must not be extended to try — reaching for it would re-create the
    inversion (an append-only durability LOG serving as a display's read
    path) that the derived card exists to end.

    Its job is narrower and still worth having: every land should carry a
    signed attestation of its content identity, so the record can later prove
    WHAT was landed rather than merely that something was.

    IT ALSO NEVER RAISES. A witnessing bug must not be able to fail a land
    that has already been recorded as closed — the close is durable by the
    time we get here, and an exception would report failure for work that
    succeeded.

    BUT NOT THE SAME REASON FOR EVERY CATCH, AND THIS PARAGRAPH USED TO SAY
    OTHERWISE (a correction of an APPROVE on row 7c04b8437c00). It
    read: "Broad catch, then the SAME reported reason, because an unwitnessed
    land is unwitnessed whatever prevented it." That is the exact model this
    function now disproves, left standing as load-bearing prose beside code
    that contradicts it — which is worse than a stale comment, because the
    next author reads the docstring as the contract and collapses the states
    back together in good faith.

    THE TWO CATCHES REPORT DIFFERENT THINGS. A crash in the pre-mint READ is
    UNKNOWN — we could not establish what the record says — and returns
    R_UNREADABLE, because minting beside a record you cannot read is how one
    land ends up with two signed rows. A crash in the MINT, after R_NONE was
    established, is an honest unwitnessed: we looked, there was nothing, and
    we could not write one. Same fail-open law, two different sentences.

    Returns (receipt-or-None, reason-or-None, blocked-state-or-None). It
    PRINTS NOTHING: a library that writes to a stream decides for every caller
    at once, and this one has three with different needs — a CLI wanting a
    human line, `--json` wanting a field a parser survives, and a card wanting
    a badge.

    THE THIRD ELEMENT SEPARATES SUPPRESSED FROM FAILED, and every one of those
    three surfaces needs it. `None` means we looked, found no receipt, and the
    mint did not succeed — an honest NEGATIVE, cured by minting. A non-None
    state means the record is unreadable, corrupt or self-contradicting, so
    whether a receipt EXISTS is UNKNOWN and minting is the worst move
    available. Reporting the second as the first asserts an absence nobody
    established and prescribes the duplicate this function exists to prevent.
    """
    # ALREADY WITNESSED IS NOT UNWITNESSED, and minting again is worse than
    # doing nothing. A repro on the NORMAL lifecycle: `helm lr land`
    # succeeds, then close(landed) runs and mints a SECOND receipt for the
    # same reviewed tip — record_land call_count 2 — and when that second
    # attempt hits a signer failure the close reports unwitnessed WHILE A
    # VALID RECEIPT EXISTS. A false negative on the audit trail, plus the
    # duplicate signed rows the verb-split contract exists to prevent.
    #
    # So ask the index BEFORE minting. An unreadable index is UNKNOWN and
    # mints nothing: blind minting is exactly how one land ends up with two
    # rows, and this function's whole job is the record's honesty.
    # TWO BOUNDARIES, NOT ONE, AND THAT IS THE WHOLE CORRECTION (the
    # FIX bound on ef0c543800a6). One try around both steps made a CRASHED READ
    # and a CRASHED MINT indistinguishable, so the typed state I had just added
    # was still lost through the exception door: an OSError reading the receipt
    # index came out as (None, reason, None) and serialized as `unwitnessed` —
    # the negative, under the CLI line prescribing `helm lr land`. The defect I
    # had cured on the RETURN path survived on the RAISE path.
    #
    # My own comment argued the wrong thing here: "a crash is a genuine mint
    # failure, nothing was found to suppress it". That is true only when the
    # crash comes from the MINT. A crash from the READ is the strongest
    # possible UNKNOWN — we could not even establish what the record says —
    # and it is exactly the case where minting is the worst move available.
    # The commit rationale was the test case and I did not re-read the code
    # against it.
    #
    # INSIDE A BOUNDARY EITHER WAY, because the READ can crash too.
    # I once moved this lookup in FRONT of the try, so an OSError from the
    # receipt read propagated out and failed a close that was ALREADY DURABLE
    # — reporting failure for work that succeeded, the one thing this
    # function's contract forbids. Both hazards are live at once: the read must
    # not escape, and it must not be graded as a mint failure.
    try:
        already, unknown, state = _existing_receipt(reviewed_tip)
    except Exception as exc:                                  # noqa: BLE001
        return None, ("the land-receipt index could not be READ (%s: %s), so "
                      "whether this land is already witnessed is UNKNOWN"
                      % (exc.__class__.__name__, exc)), R_UNREADABLE
    if already:
        return already, None, None
    if unknown:
        # SUPPRESSED, NOT FAILED — and the caller must be able to tell.
        # Every state that reaches here is a NON-ABSENCE: the record says
        # something and what it says is not "no receipt". Reporting it as
        # unwitnessed asserts an absence nobody established.
        return None, unknown, state
    try:
        up_ref = _resolve_ref(gitdir, UPSTREAM_TRUNK)
        rec, why = record_land(
            lr.get("lane"), lr.get("branch"), reviewed_tip,
            _patch_id(gitdir, reviewed_tip, trunk_ref), trunk_sha,
            repo_id=gitdir, has_upstream=bool(up_ref),
            upstream=_ancestry(gitdir, trunk_sha, up_ref) == ANCESTOR)
    except Exception as exc:                                  # noqa: BLE001
        rec, why = None, "%s: %s" % (exc.__class__.__name__, exc)
    # A CRASH IS A GENUINE MINT FAILURE, not a suppression: nothing was found
    # to suppress it, we tried and could not write. So the third element stays
    # None here and only the states above set it.
    return rec, (None if rec else (why or "no reason given")), None


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


def _git_key(gitdir, args, input_text=None, env=None):
    """THE memo key for one git question. ONE CONSTRUCTOR, and it has to be.

    Four places used to spell this tuple: the read, the peel-key helper, and
    two evictions. Each defended against a DRIFTED LITERAL by sharing its argv
    with the read -- and that defence does not cover a change to the key's
    SHAPE, which is what adding `env` was. Measured: the read wrote a 5-tuple,
    every hand-built 4-tuple stopped matching, and `projscope.forget` -- which
    is documented silent on a missing key -- evicted nothing while the code
    read as though it retried. One arm caught it; nothing else would have.

    THE ENV IS PART OF THE QUESTION. Two callers asking the same argv under
    different ancestry views (one with grafts isolated, one not) are asking
    DIFFERENT questions, and a key that ignored `env` would serve the first
    answer to the second -- a cached wrong ancestry, the exact class the
    callers of this helper exist to avoid.
    """
    return ("landreq._git", gitdir, tuple(args), input_text,
            tuple(sorted(env.items())) if env else None)


def _git(gitdir, *args, input_text=None, env=None):
    # A REPO BINDING THAT IS NOT A STRING IS NOT A PATH. `repo_id` is copied
    # verbatim out of the ledger row and never re-validated, so a junk value
    # arrives here as itself — and subprocess raises TypeError on an argv
    # member that is not a string, which is not one of the failures this
    # function's callers are told to expect. Not-a-path answers exactly the
    # way an absent path does: there is no repository here to ask.
    if not isinstance(gitdir, str) or not gitdir:
        return None
    projscope.spend_or_raise("land request git memo lookup")
    # THIS DOOR IS OUTSIDE THE vcs SEAM, so a checkpointed dispatch fold
    # cannot see what it asked. Say so, and that fold refuses to reuse what it
    # decided (task/2770); with nothing observing, this is one attribute read.
    vcs.unseamed("landreq._git")
    # ONE ANSWER PER QUESTION PER PROJECTION. Inside `projscope.scope()` the
    # same argv against the same repository is spawned once and every later
    # asker gets that answer; outside a scope this is byte-identical to the
    # bare spawn below and nothing is remembered. Measured 2026-08-06 over the
    # live ledger: 4140 spawns in one `helm lr list --all`, 650 of them exact
    # repeats (one `merge-base --is-ancestor` asked 48 times). The saving is
    # the smaller half of why this is here — the larger half is that a
    # projection which asks git the SAME question twice can be told two
    # different things by a trunk that moved between them, and then publishes
    # both under one read stamp.
    answer = projscope.memo(_git_key(gitdir, args, input_text, env),
                            lambda: _git_spawn(
                                gitdir, args, input_text, env))
    projscope.spend_or_raise("land request git memo result")
    return answer


def _git_spawn(gitdir, args, input_text, env=None):
    """The bare spawn. Split out ONLY so the memo above has something to call
    — every existing test that patches `landreq._git` still replaces the whole
    function, memo included, and sees no change.

    `env` OVERLAYS the ambient environment rather than replacing it: git needs
    the inherited HOME, PATH and locale to run at all, so a bare env dict
    would change far more than the one variable a caller means to set."""
    left = projscope.spend_or_raise("land request git subprocess")
    ambient_limited = left is not None and left <= 5
    try:
        run_env = None
        if env:
            run_env = dict(os.environ)
            run_env.update(env)
        answer = subprocess.run(["git", "--git-dir", gitdir, *args],
                                input=input_text, capture_output=True, text=True,
                                timeout=left if ambient_limited else 5,
                                env=run_env)
    except subprocess.TimeoutExpired as exc:
        if ambient_limited:
            raise projscope.Expired(
                "budget spent during land request git subprocess") from exc
        projscope.spend_or_raise("land request git caller timeout result")
        return None
    except OSError:
        projscope.spend_or_raise("land request git failure result")
        return None
    projscope.spend_or_raise("land request git result")
    return answer


def _resolve_ref(gitdir, names):
    for name in names:
        p = _git(gitdir, *_REF_ARGV, name)
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
    got = _batched_ancestry(gitdir, commit, ref)
    if got is not None:
        return got
    p = _git(gitdir, "merge-base", "--is-ancestor", commit, ref)
    if p is None:
        return UNDETERMINED
    if p.returncode == 0:
        return ANCESTOR
    return NOT_ANCESTOR if p.returncode == 1 else UNDETERMINED


def _batched_ancestry(gitdir, commit, ref):
    """Projection-scoped BATCH ancestry, or None for "ask per pair".

    The `--git-dir` twin of vcs `_batched_ancestry` (same two sets, same
    tri-state): reachable-from-`ref` answers ANCESTOR, present-but-unreachable
    answers NOT_ANCESTOR, and a sha in NEITHER set stays UNDETERMINED — the
    vanished-object case merge-base reports as 128, kept distinct for the same
    reason `_ancestry`'s docstring gives for never folding 128 into "no".
    cProfile 2026-08-07: the per-pair spawns were 140 of `lr list`'s 155
    seconds; two listings per projection replace them. None on: no open scope,
    a non-full-sha commit, or either listing unavailable — every exit falls
    back to the spawn, never to a guess."""
    if not projscope.active():
        return None
    sha = commit.strip().lower()
    if not _sha(sha):
        return None
    reach = _sha_set(gitdir, ("rev-list", ref), ("lr.revset", gitdir, ref))
    if reach is None:
        return None
    if sha in reach:
        return ANCESTOR
    objs = _sha_set(gitdir, ("cat-file", "--batch-check=%(objectname)",
                             "--batch-all-objects", "--unordered"),
                    ("lr.objset", gitdir))
    if objs is None:
        return None
    return NOT_ANCESTOR if sha in objs else UNDETERMINED


def _sha_set(gitdir, args, key):
    """frozenset of shas from one git listing, memoized per projection.

    A failed or EMPTY listing memoizes as None and is FORGOTTEN: an empty set
    would answer every membership question toward NOT_ANCESTOR, which is the
    false-negative direction a landedness guard must never fail in."""
    def compute():
        p = _git(gitdir, *args)
        if p is None or p.returncode != 0:
            return None
        got = frozenset(p.stdout.lower().split())
        return got or None
    hit = projscope.memo(key, compute)
    if hit is None:
        projscope.forget(key)
    return hit


def _is_ancestor(gitdir, tip, ref):
    """Boolean fast-path projection for patch-identity landedness."""
    return _ancestry(gitdir, tip, ref) == ANCESTOR


def _vanished_proof(gitdir, tip):
    """`absent` when NO REACHABLE SOURCE HOLDS this object; else `unknown`.

    THE LOGIC ERROR THIS RETIRES RAN BACKWARDS. A git object that does not
    exist used to read `unknown`, and the subsumed door requires `absent` — so
    the STRONGEST evidence of absence was scored WEAKER than an ordinary
    negative. helm's two oldest rows (4fdd32fcf664 at 7.9d, b970911edbe6 at
    7.8d, audited 2026-08-03) pass every other clause — chain matches,
    cross-family holds, and their cure 77e4021415e7 is a proven ancestor of
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
    `absent`. That is the rule the fleet re-derived on five separate surfaces
    on 2026-08-03: a check that could not look must never answer as if it had.

    WHAT THIS PROVES, AND WHAT IT DOES NOT. It proves no source we can reach
    holds the object. It does NOT prove the change never landed: without the
    object there is no patch-id to compare, so a landed-under-a-rewrite tip
    is indistinguishable from one that never landed. That residual is bounded
    by the door this feeds, not by this function — `subsumed` independently
    requires a cross-family APPROVE whose reviewed tip IS proven on trunk, so
    the work being answered is established by the confirmation rather than by
    this absence. A future caller that wants `absent` WITHOUT that corroborating
    clause must not reuse this result as if it carried one.

    THE LANDING LADDER IS EXACTLY SUCH A CALLER, so it never asks. Routing a
    tip `rev-parse` cannot resolve through here would hand this function's
    `absent` to every door reading `_landing_proof` — `withdrawn` among them,
    the door whose whole record is "this work is NOT on trunk" and which
    carries no confirmation at all — and a pruned object, or one never
    fetched into this clone, would retire a live row with a false negative.
    The ladder answers `unknown` for an object it cannot see, and a door that
    DOES carry a corroborating clause asks this function itself, through
    `_vanished_absence`."""
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


def _vanished_absence(gitdir, tip):
    """`_vanished_proof` for a tip git LOOKED FOR HERE AND COULD NOT RESOLVE;
    `unknown` for every other tip. Never a landing proof.

    THE ONE DOOR THAT MAY TURN A MISSING OBJECT INTO `absent` ASKS HERE, and
    it asks by name. `_landing_proof` answers `unknown` for an object this
    repository cannot see, because a door that reads it without a
    corroborating clause — `withdrawn` records "not on trunk" on nothing else —
    would otherwise publish a negative about an object nobody measured.
    `subsumed` is the door `_vanished_proof` was built for, and it carries the
    clause: a later cross-family APPROVE whose own tip IS proven on trunk.
    `superseded` asks too, but only to ROUTE a destroyed reviewed object into
    the translated door's destroyed-object arm, which adjudicates it on its
    own rungs and records no absence.

    ONLY rc 1 ENTERS, the same split the ladder's own resolution makes:
    `rev-parse --verify --quiet` exits 1 when it looked and did not resolve the
    name, and anything else is the read itself failing. A tip that DOES resolve
    is never this function's question — its absence is the ladder's to
    measure, and a capped patch-index miss on a live object must stay
    `unknown` rather than be finished by a remote or reflog miss."""
    if not _sha(tip):
        return "unknown"      # not a full sha: nothing to look an object up by
    p = _git(gitdir, *_REF_ARGV, tip + "^{commit}")
    if p is None or p.returncode != _GIT_LOOKED_AND_MISSED:
        return "unknown"      # it resolves, or the read failed: not vanished
    return _vanished_proof(gitdir, tip)


# ARMED INSIDE THE LAYER, NOT AT A CALLER — and scoped to the PROJECTION,
# which is the aggregate that actually needs bounding.
#
# Round one bounded the derive loop in `verified_lands`, the caller I had
# found, and the next capture blew entering through `landed_ever`. Bounding
# callers means the bound is only as complete as the caller census, so the
# budget belongs at the door every path passes: `_landing_proof`.
#
# THE FIRST ATTEMPT AT THAT DOOR WAS PROCESS-WIDE AND IT BROKE 82 TESTS.
# A module-level deadline armed on first entry expires four seconds into ANY
# long-running process and then answers UNKNOWN for everything after — the
# suite, and `helm web`, and any reader that derives more than a few seconds'
# worth. That is not a smaller outage, it is a permanent one wearing the
# cure's clothes. The measurement was unambiguous and immediate.
#
# The unit that needs a budget is ONE PROJECTION, not one process: an inject
# turn runs a projection, and the 10s budget belongs to that turn. So the
# deadline rides `projscope`, armed once per scope by the same memo the git
# calls already use and discarded with the scope. Outside a scope there is no
# aggregate to bound and no shared budget to poison.
_DERIVE_BUDGET_S = 4.0

# What a reader that MEANS to finish declares instead. The 4 seconds above
# were sized for a 10s inject turn and were charged to every reader alike,
# including `helm web`'s background revalidate, which has no deadline at all
# and serves the previous body while it runs. MEASURED on the live ledger,
# one process, full projection, the unbudgeted run first so the bounded run
# had the warmest cache:
#
#     budget      wall     rows   in-flight   UNDERIVED
#     unbounded   140.1s   2926      899          1
#     4.0s          7.4s   2926     1289        106
#
# The in-flight COUNT moves, not only a mark: an underived row cannot be
# classified as landed, so it stays in flight and the board's whole
# population is wrong. This number is a STATED BOUND rather than no bound —
# a reader that cannot finish inside it still says BUDGET EXPIRED — and it
# is the ceiling on a cold pass only: the carrier-proof ledger below makes
# every later pass against the same trunk cheap.
BOARD_DERIVE_BUDGET_S = 300.0


_DERIVE_DEADLINE_KEY = ("landreq._derive_deadline",)


def arm_derive_budget(seconds):
    """Seed THIS projection's soft derive deadline; the first seeder wins.

    THE SOFT CHANNEL, DELIBERATELY, AND NOT `projscope.scope(deadline=...)`.
    The two look interchangeable and are not. A projscope deadline is HARD:
    `landreq._git` calls `spend_or_raise` on every memo lookup, so arming one
    turns a spent budget into an EXCEPTION out of `project_raw` rather than a
    row that reads UNDERIVED. Measured by making exactly that mistake — the
    board stopped degrading and started raising from `_trunk_refs`, six arms
    deep in code that has nothing to do with this lane. The derive budget is
    a per-door check whose whole contract is that running out produces an
    honest UNKNOWN, so it stays on its own channel.

    IT SEEDS THE SAME MEMO `_derive_expired` READS, so a caller that wants to
    finish says so once, before any projection work, and every door below
    reads its number instead of the module default. `projscope.memo` computes
    once per key per scope, so a second seeder inside the same projection is
    ignored rather than able to extend a bound already in force.
    """
    return projscope.memo(_DERIVE_DEADLINE_KEY,
                          lambda: time.monotonic() + seconds)


def _derive_expired():
    """True once THIS PROJECTION's derives have spent their budget.

    THE NUMBER IS THE CALLER'S WHEN THE CALLER SAID ONE. `_DERIVE_BUDGET_S`
    was sized for a 10s inject turn and was charged to every reader alike —
    including `helm web`'s background revalidate, which has no deadline of
    its own, serves the previous body while it runs, and was therefore the
    reader that most needed to FINISH paying the tightest bound. A caller
    that means to finish seeds its own through `arm_derive_budget`.

    Outside a projection scope the memo computes fresh every call, so the
    deadline is always in the future and this is always False — correct,
    because there is no aggregate there to bound and a latched budget would
    poison every later caller in the process.
    """
    deadline = projscope.memo(
        _DERIVE_DEADLINE_KEY,
        lambda: time.monotonic() + _DERIVE_BUDGET_S)
    return time.monotonic() >= deadline


def _landed_index(gitdir, upstream):
    """(patch-id -> trunk sha, coverage cause) for `upstream`, memoized.

    Keyed on the RESOLVED upstream sha, so a trunk that moves produces a new
    key and can never be answered from the old scan. Returns (None, False)
    when the index cannot be built, which sends the caller back to the
    per-row ladder rather than to a guess.

    THE INDEX IS DURABLE AND EXTENDED, NOT REBUILT, and that is the whole
    reason the patch-identity rung is reachable at all. MEASURED on this repo
    before it was: `_stored_patch_index` costs 5.19s for its 600-commit window
    while `_DERIVE_BUDGET_S` is 4.0s, so the build could never finish inside
    one projection's spend — 1636 `_landing_proof` calls in a single pass
    returned 51 `ancestor` and ZERO `patch-equivalent`, on a board carrying
    383 tips that reached trunk by patch identity. The rung was not slow, it
    was unreachable, and every pass paid five seconds to prove it again.
    """
    if not upstream:
        return (None, False)
    def _build():
        try:
            return _trunk_index_extend(gitdir, upstream)
        # EXPIRY IS NOT A FAILED BUILD, and this half arrived from task/2033
        # while this lane was in review. It matters MORE against the extending
        # index than against the rebuild it replaced: the extend spends its
        # budget deliberately and yields, so an Expired swallowed by the
        # blanket clause below would report NO INDEX for a walk that was
        # working when the clock ran out. That is the negative-versus-
        # unreadable collapse both lanes exist to prevent — a caller seeing
        # (None, False) goes back to the per-row ladder as though nothing had
        # been built, and the durable cover it did build goes unread.
        except projscope.Expired:
            raise
        except Exception:                      # noqa: BLE001 — fall back
            return (None, False)
    got = projscope.memo(("landreq._landed_index", gitdir, upstream), _build)
    if isinstance(got, tuple) and len(got) == 2:
        return got
    return (None, False)


def _tip_patch_id(gitdir, sha):
    """This tip's own patch-id, derived by git NOW — never the stored one.

    THROUGH THE OBJECT VIEW, and the reason is measured, not defensive. A
    `git show` under a replace ref for `sha` renders the REPLACEMENT's
    content, so the id this returns would be a true patch-id of the WRONG
    COMMIT, and every landedness answer built on it would be about an object
    the caller never named. Measured on a real repository: the ambient render
    and the object-view render of the same pinned tip disagreed under a
    replace ref, and the object view agreed with the un-replaced value.
    `patch-id --stable` itself needs no view — it hashes the text it is
    handed — so only the rendering call moves.

    THROUGH THE BATCH FIRST, and the per-sha ladder below is the fall-through
    rather than the normal path. Measured on the live board: 313 distinct tips
    reached here in one `/api/lr` projection and each one spawned TWO git
    processes (the render and the hash), 626 of the projection's 663 spawns —
    the git-per-row shape the inject hook was already bounded for, running in
    the web process' own rebuild loop at roughly eleven a second. The batch
    answers all of them in two spawns; see `_tip_patch_ids_batch`.
    """
    got = _patchid_map(gitdir)
    if sha in got:
        return got[sha]
    return _tip_patch_id_spawn(gitdir, sha)


def _tip_patch_id_spawn(gitdir, sha):
    """ONE tip's patch-id, spawning for it alone. The contract `_tip_patch_id`
    documents; split out so the batch above has a fall-through that is the
    same two calls it always was, byte for byte."""
    p = _object_view(gitdir, "show", "--format=%H", sha)
    if p is None or p.returncode != 0 or not p.stdout:
        return None
    q = _git(gitdir, "patch-id", "--stable", input_text=p.stdout)
    if q is None or q.returncode != 0 or not q.stdout.strip():
        return None
    parts = q.stdout.split()
    return parts[0].strip().lower() if parts else None


# THE ARMED LIST LIVES IN THE PROJECTION CACHE AND THE BOUGHT MAP DOES NOT,
# and the split is the whole wiring. The armed list is a derivation of row
# dicts, so it belongs beside `objexist-pending` where the Web read-set can
# classify it. The bought map is the result of a live git read, and the map is
# read by `_tip_patch_id` — four frames below the last caller that holds the
# cache — so it lives in `projscope`, which is already open around the whole
# projection, already what `_git` memoises into, and already degrades to
# "compute every time" outside a scope.
_PATCHID_PENDING = "patchid-pending"
_PATCHID_MAP = "landreq.patchid-map"

# Distinguishes "no batch has been bought for this repository" from a batch that
# was bought and came back empty. Without it a reader arriving before the buy
# would memoise its own empty answer under the buy's key and the batch could
# never fire — a silent no-op that reports the unbatched world.
_PATCHID_UNBOUGHT = object()


def _prime_tip_patch_ids(gitdir, cache):
    """Buy the armed patch-id batch, once per repository per projection.

    CALLED FROM `_git_observe`, which is the one frame that holds BOTH the
    projection cache and a row that is genuinely about to ask — and that has
    just proved the tip exists, so the existence batch this buy reads is
    already bought. Lazy for the same reason the existence prefetch is: a
    projection whose rows all answer from their recorded closures must perform
    no git read at all."""
    key = (_PATCHID_MAP, gitdir)
    if projscope.memo(key, lambda: _PATCHID_UNBOUGHT) is not _PATCHID_UNBOUGHT:
        return
    projscope.forget(key)
    projscope.memo(key, lambda: _tip_patch_ids_batch(gitdir, cache))


def _patchid_map(gitdir):
    """The bought map for `gitdir`, or {} when nothing has been bought.

    NEVER BUYS. A reader that could buy would make the batch's contents depend
    on which caller happened to ask first, and would put a spawn on a path
    (`_landed_idempotent`, `_landing_proofs`) that holds no projection cache
    and arms nothing."""
    got = projscope.memo((_PATCHID_MAP, gitdir), lambda: _PATCHID_UNBOUGHT)
    return got if isinstance(got, dict) else {}


def _prefetch_tip_patch_ids(rows, cache):
    """ARM the patch-id batch; do not fire it — the same contract, and the same
    reason, as `_prefetch_object_existence` one door over.

    A projection whose rows all answer from their recorded closures performs NO
    git read, and arming eagerly would spawn inside a repository those rows are
    defined not to consult. So this records WHICH tips would be asked about and
    spawns nothing; the first row that genuinely reaches the patch-identity
    rung buys them all.

    FULL OBJECT NAMES ONLY. The map is keyed by the sha the caller asks about,
    and `git show --format=%H` prints the RESOLVED sha — so an abbreviated or
    symbolic tip would answer under a name nobody looks up, and worse, its
    absence from the output could not be told apart from an empty diff. Every
    tip on the live board is a full object name (measured: 329 of 329), and one
    that is not simply falls through to its own spawn, which is where it was.

    A TIP THE DURABLE PROOF LEDGER ALREADY COVERS IS LEFT OUT, and that is the
    difference between a batch that helps and one that costs more than it
    saves. `_landing_proof` reads the kept ledger ABOVE the budget door and
    returns without deriving anything, so a covered tip never reaches the rung
    this batch feeds; rendering it would buy megabytes of diff for a question
    nobody asks. MEASURED on the live board: 1954 observable tips, 1351 of them
    covered, 603 left — and all 314 tips the projection actually asked about
    were inside those 603, none outside. The read is the in-memory ledger and
    spawns nothing. A tip whose kept proof turns out not to carry forward to
    this trunk simply falls through to its own two spawns, which is exactly
    where it was before any of this existed.
    """
    ledger = _land_proof_ledger()
    pending = {}
    for row in rows or ():
        gd = (row or {}).get("repo_id")
        tip = (row or {}).get("tip")
        # THE SAME DOOR AS THE EXISTENCE PREFETCH, for the same invariant: the
        # batch may hold only tips of rows this projection will actually
        # observe git for. Computing that condition a second time here is how
        # the other prefetch came to carry monotonic historical closures.
        if not (isinstance(gd, str) and _sha(tip) and _will_observe_git(row)):
            continue
        sha = tip.lower()
        if ledger.get((gd, sha)):
            continue
        pending.setdefault(gd, []).append(sha)
    for gd, shas in pending.items():
        cache[(_PATCHID_PENDING, gd)] = shas


# One `git show` renders about 20 KB of diff per commit, measured over the live
# board's tips, and `_git` gives any one call five seconds. So the render is cut
# into chunks rather than sent whole: 1609 tips in ONE call timed out and
# answered nothing, which is the failure mode that turns a batch back into the
# per-row storm it replaced. At this size a chunk renders in well under a
# second and a pathological commit costs its own chunk, never the projection.
_PATCHID_BATCH_CHUNK = 200


def _tip_patch_ids_batch(gitdir, cache):
    """{sha: patch-id or None} for MANY tips in a SMALL CONSTANT of spawns.

    `git show --format=%H A B C` renders every commit in one process and
    `git patch-id --stable` hashes the whole stream, printing
    `<patch-id> <commit>` per patch — so the pairing is git's own and not a
    positional zip this code would have to keep true. MEASURED against the live
    board's tips: one render of 6.2 MB in 1.01s plus one hash in 0.05s, and all
    329 answers identical to the per-sha ladder's, none missing and none
    different.

    EXISTENCE IS FILTERED FIRST, THROUGH THE BATCH THAT IS ALREADY BOUGHT,
    because `git show` is ALL-OR-NOTHING: a single name it cannot resolve exits
    128 with `fatal: bad object` and the whole render is lost. Measured on the
    live board, 345 of 1954 observable tips name objects this repository does
    not have — rows whose own per-row path never reaches git because
    `_git_observe` proves existence first — so without this filter the batch
    failed every pass and every row fell back to its two spawns.

    IT READS `_objexist_map` RATHER THAN ASKING AGAIN, and that is a law and
    not a saving: ONE existence question per repository per projection. A
    second `cat-file --batch-check` over a different sha list is a second
    reading of a repository that can gain an object by fetch or lose one by gc
    between them, and `tests.test_landreq.BatchIsSharedAcrossRowsTest` holds
    the single-batch property directly. `_prime_tip_patch_ids` buys this only
    from `_git_observe`, AFTER that frame has proved a tip through the same
    map, so the map is always already there.

    A COMMIT MISSING FROM ITS CHUNK'S OUTPUT IS A REAL `None`, NOT A GAP.
    `patch-id` prints nothing for an empty diff, and the per-sha ladder answers
    None for exactly that commit (its `patch-id` stdout is empty too). So once
    a chunk has rendered SUCCESSFULLY, an armed sha with no line is answered
    None rather than sent back for a spawn that would return None again — which
    is what keeps a merge or an empty commit from re-buying a process every
    pass. A chunk that FAILED records nothing, so its shas fall through to the
    per-row ladder and keep their own reading.

    THE RENDER IS EVICTED FROM THE MEMO AS SOON AS IT IS PARSED. `_git`
    memoises per argv and the memo lives as long as the projection does, so
    megabytes of diff text would otherwise be pinned for the whole rebuild for
    a question nobody asks twice. The parsed map is the answer; the render is
    not, and `projscope.forget` exists for the caller that can recognise that.
    """
    if not isinstance(cache, dict):
        return {}
    shas = cache.pop((_PATCHID_PENDING, gitdir), None) or []
    shas = [s for s in dict.fromkeys(shas) if _sha(s)]
    if not gitdir or not shas:
        return {}
    # AN EMPTY OR FAILED EXISTENCE MAP LEAVES EVERY ROW ON ITS OWN LADDER,
    # which is the pre-batch behaviour exactly. Rendering an unverified list
    # would be the all-or-nothing failure this filter exists to prevent, and
    # asking a second time would be the second reading it exists to avoid.
    exists = _objexist_map(gitdir, cache)
    live = [s for s in shas
            if exists.get(s, (False, None))[0] and exists[s][1] == "commit"]
    out = {}
    for start in range(0, len(live), _PATCHID_BATCH_CHUNK):
        chunk = live[start:start + _PATCHID_BATCH_CHUNK]
        args = (*_NO_REPLACE, "show", "--format=%H", *chunk)
        p = _git(gitdir, *args, env=_NO_GRAFTS)
        projscope.forget(_git_key(gitdir, args, None, _NO_GRAFTS))
        if p is None or p.returncode != 0 or not p.stdout:
            continue
        q = _git(gitdir, "patch-id", "--stable", input_text=p.stdout)
        projscope.forget(_git_key(gitdir, ("patch-id", "--stable"), p.stdout))
        if q is None or q.returncode != 0:
            continue
        asked = set(chunk)
        out.update(dict.fromkeys(chunk))
        for line in q.stdout.splitlines():
            parts = line.split()
            if len(parts) != 2:
                continue
            commit = parts[1].strip().lower()
            if commit in asked:
                out[commit] = parts[0].strip().lower()
    return out


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

    Measured on this repo's own history, 2026-07-25: the delim land (on
    trunk as c3f4e7e, "delim: the capture grammar could not express a pipe,
    so it ate one") was gated at a pre-land lane tip that no later rebase
    kept. Ancestry on the gated sha said NOT LANDED while the patch was
    demonstrably on trunk, and `helm lr` showed the loop READY.

    Ancestry stays the FAST path — when it is true it is decisive and costs
    one call. A negative OR unknown falls through to the independent patch-id
    comparison: an exact '-' proves present and '+' proves absent. If that
    fallback is also unreadable, the result stays unknown.

    `absent` IS A MEASUREMENT OF AN OBJECT THIS REPOSITORY HOLDS, and nothing
    else. A tip git cannot resolve here — pruned by gc, or never fetched into
    this clone — answers `unknown`: the object's absence from the clone says
    nothing about whether its change reached trunk, and `withdrawn` retires a
    row on `absent` alone. `_vanished_absence` is the separate question a
    door with its own corroboration may ask about a vanished object.

    MEASURED 2026-08-02, min of 5 warm runs, 1281 commits on main. Ancestry
    costs 1.2ms. The cherry fallback costs 3.4ms for a tip 10 commits from
    trunk, and 44.1ms for one 577 commits out.

    THE METHOD IS STATED BECAUSE THE NUMBERS DEPEND ON IT. A cold or averaged
    run reads several times higher. The previous wording gave figures with no
    method and no date, so nobody could reproduce them or tell when they aged.
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

    THE DURABLE LEDGER WRAPS THIS LADDER RATHER THAN LIVING INSIDE IT, so the
    three places a positive is reached cannot drift into two that record and
    one that does not. `_derive_landing_proof` is the ladder; this is the door.
    """
    # THE KEPT POSITIVE SITS ABOVE THE BUDGET DOOR ON PURPOSE. A kept proof
    # costs no spawn, so charging it against a spend meant to bound git would
    # answer UNKNOWN for a question already settled — and the rows past the
    # cutoff are exactly the ones the ledger exists to serve. Measured before
    # it existed: a 2700-row projection answered 50 rows and rendered the
    # other 1937 it had QUEUED as UNKNOWN, and did it again every pass.
    kept = _kept_landing_proof(gitdir, tip, ref)
    if kept:
        return kept
    proof = _derive_landing_proof(gitdir, tip, ref)
    # ONE WRITE DOOR, and it is `_keep_landing_proof` that decides what is
    # keepable — never this call site, which would be a second opinion about
    # which verdicts are monotonic.
    _keep_landing_proof(gitdir, tip, ref, proof)
    return proof


def _derive_landing_proof(gitdir, tip, ref):
    """The live ladder: ancestry, then the patch index, then `git cherry`.

    Answers the same four words as `_landing_proof` and keeps nothing. Split
    out so the durable ledger has exactly one place to read and one to write.
    """
    # THE DOOR. Checked before any spawn, so an expired burst costs nothing
    # further and answers UNKNOWN — never ABSENT, because running out of time
    # is not evidence the patch is missing.
    if _derive_expired():
        return "unknown"
    ancestry = _ancestry(gitdir, tip, ref)
    if ancestry == ANCESTOR:
        return "ancestor"
    if ancestry == NOT_ANCESTOR and _sha(tip):
        sha = tip
    else:
        full = _git(gitdir, *_REF_ARGV,
                    tip + "^{commit}")
        if full is None:
            return "unknown"      # git itself did not run: no reading at all
        # THREE OUTCOMES, WRITTEN AS THREE, and the success is one of them.
        # An earlier shape of this reader stated only the two failures and let
        # the success fall out of the bottom -- which stopped being true the
        # moment the second failure clause also returned, leaving the SHA
        # assignment unreachable and every resolved name answering unknown.
        # A reader had to find that; the arms could not, because they all
        # entered through the full-sha fast path above. So the branch that
        # SUCCEEDS is spelled out beside the ones that fail.
        #
        # A SUCCESSFUL-EMPTY READ IS NOT A RESOLUTION: `rev-parse --verify
        # --quiet` exits NON-ZERO when it cannot resolve the name, so rc 0
        # with no sha on stdout is a reading that did not happen.
        #
        # NONZERO is two facts wearing one sign. MEASURED on git 2.53.0
        # through the same command:
        #
        #   absent-but-valid sha  rc 1    stdout empty   stderr empty
        #   malformed name        rc 1    stdout empty   stderr empty
        #   broken/absent repo    rc 128  stdout empty   stderr `fatal:`
        #
        # AND NEITHER OF THEM IS A LANDING ANSWER. rc 128 is the read itself
        # failing. rc 1 is git having LOOKED and found no such object HERE --
        # which says what this clone holds and nothing about trunk: a pruned
        # lane commit, or one never fetched into this clone, reads rc 1 while
        # its work may sit on trunk under a cherry-picked sha. Sending rc 1
        # through `_vanished_proof` here lets remote-and-reflog silence
        # publish ABSENT, and ABSENT is what the `withdrawn` door retires a
        # row on: a vanished object would close a live row with a false "not
        # on trunk". UNREADABLE AND ABSENT MUST NEVER SHARE A VALUE, so both
        # faces answer `unknown` here, and a door whose own clauses
        # adjudicate a vanished object asks `_vanished_absence` by name.
        if full.returncode == 0:
            sha = full.stdout.strip()
            if not sha:
                return "unknown"          # a malformed success, not absence
        else:
            return "unknown"              # no object here: not a measurement
                                          # of trunk (rc 1), or no read (128)
    # THE UPSTREAM IS NORMALISED TO A SHA BEFORE IT REACHES THE MEMO KEY, and
    # this is a spawn fix rather than a correctness one. `projscope.memo` keys
    # on ARGV (:1668), and the projection asks this question about each row
    # TWICE — once against the local trunk name and once against the upstream
    # one (LOCAL_TRUNK / UPSTREAM_TRUNK, resolved to NAMES at :2085). When both
    # names point at the same commit, which is the normal state of a synced
    # checkout, the two spellings mint two keys for one question and the memo
    # sails past the duplicate.
    #
    # MEASURED on trunk 2026-08-11 via a git-argv shim over one `lr list --all`:
    # 3908 spawns total, of which `cherry refs/heads/main <SHA>` 548 and
    # `cherry refs/remotes/origin/main <SHA>` 547, with 547 shas asked under
    # BOTH spellings while both refs resolved to 781259f0. That is 14% of every
    # git call the projection makes, spent proving something already known.
    #
    # Safe because `git cherry` resolves its upstream argument to a commit
    # before doing anything: <ref> and <sha-of-ref> are the same question by
    # construction. An unresolvable ref falls through UNCHANGED, so the failure
    # mode is today's behaviour rather than a wrong answer. The extra rev-parse
    # goes through the same memo, so it costs one spawn per (gitdir, ref).
    up = _git(gitdir, *_REF_ARGV, ref + "^{commit}")
    upstream = (up.stdout.strip()
                if up is not None and up.returncode == 0 and up.stdout.strip()
                else ref)
    # `git cherry <upstream> <head>`: '-' marks a commit whose patch already
    # exists upstream under another sha, '+' one that does not.
    # THE BATCH PATH. `git cherry <upstream> <head>` answers about ONE commit
    # by computing patch-ids for the ENTIRE symmetric difference, so asking it
    # per row recomputes the same upstream scan once per row. Measured on the
    # live ledger: 1301 cherry spawns in a single inject, ~50ms each, ~65s of
    # git against a 10s hook budget — every spawn individually fast, the
    # aggregate unbounded. `_stored_patch_index` already builds the upstream
    # side ONCE per (gitdir, upstream) and the loop above already uses it for
    # the rescue path; this consults the same index instead of re-deriving it.
    #
    # THE STORED patch_id IS NOT USED HERE AND MUST NOT BE. It is correlation
    # evidence, and a hand-built receipt carrying a dead tip plus any unrelated
    # live patch-id once claimed its own landing. The id compared below is
    # derived from THIS tip, by git, now.
    index, why = _landed_index(gitdir, upstream)
    # NON-EMPTY, not merely non-None. `_stored_patch_index` returns (None,
    # False) when git fails outright, but a git that exits 0 having produced
    # nothing yields an EMPTY index — and a miss against an empty index is
    # "I looked at nothing", which must never render as ABSENT. That is the
    # same negative-vs-unreadable collapse this ladder exists to avoid, and
    # an existing arm (unanswerable ancestry stays unobservable) forbids
    # exactly it: a fallback ATTEMPT is not a fallback ANSWER.
    if index:
        own = _tip_patch_id(gitdir, sha)
        if own:
            if own in index:
                return "patch-equivalent"
            # AN INCOMPLETE SCAN THAT MISSED IS UNKNOWN, NEVER ABSENT, and
            # the reason it is incomplete does not change that: the window
            # ended early, the budget ran out, or a commit inside it could
            # not be hashed. Any of the three means "not in what I saw",
            # which is not "not on trunk". Only a COMPLETE window (why is
            # None) turns a miss into an absence.
            return "unknown" if why else "absent"
    p = _git(gitdir, "cherry", upstream, sha)
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


def ancestry(gitdir, tip, ref):
    """Is `tip` REACHABLE from `ref`. True / False / None(unknown).

    THE ONLY MONOTONIC ONE, and therefore the only one safe to record. Under
    ff-only, a commit that is an ancestor stays an ancestor forever: no
    permitted event takes it back. Force-push and history rewrite would, and
    both are forbidden here.

    It is also the NARROWEST. It answers about this exact object, so it says
    nothing about work that reached trunk by cherry-pick under a new sha —
    which is how normal lands arrive. A caller that wants "did this WORK land"
    and asks this will get False for a successful land.

    MAPPED STRAIGHT ONTO `_ancestry`, NOT THROUGH `_landing_proof`, and that is
    a correctness fix rather than a shortcut. `_landing_proof` continues into
    `git cherry` even after ancestry has DECISIVELY answered, so an unreadable
    patch-identity probe made it return unknown — and this function then
    answered None for a reachability question it had already proved False. A
    broken instrument for a DIFFERENT question must never erase this one."""
    got = _ancestry(gitdir, tip, ref)
    return None if got == UNDETERMINED else got == ANCESTOR


def patch_identity(gitdir, tip, ref):
    """Does the same CHANGE exist on `ref` under a different sha.

    THE ONE LANDS ACTUALLY NEED, because a normal land cherry-picks the gated
    commit and mints a new sha, so ancestry alone reports absent for work that
    plainly arrived. Survives cherry-pick and rebase by construction.

    FALSIFIABILITY: UNPROVEN, and written that way on purpose. A claim is in
    the room that two identical replacements at DIFFERENT OCCURRENCES
    fingerprint the same — which would mean a stricter instrument can flip
    this from True to False, making it unrefuted-so-far rather than true. That
    claim belongs to another seat, from its content-equivalence lane, and
    nobody has yet said whether it was RUN or REASONED. (That lane's id is
    deliberately not quoted here: it names a DISPATCH row, not a commit, so
    no other clone could dereference it — the claim is the SHAPE, and the
    docref guard is right to refuse the citation.) Until someone does, do not record
    this answer as durable and do not cite the property as measured."""
    proof = _landing_proof(gitdir, tip, ref)
    return None if proof == "unknown" else proof == "patch-equivalent"


def landed_ever(gitdir, tip, ref):
    """Did this work reach `ref` AT SOME POINT — reachable OR patch-present.

    RENAMED FROM content_presence, BECAUSE IT NEVER MEASURED PRESENCE AND I
    SHOULD NOT HAVE CLAIMED IT DID. I argued in the 2026-08-10 council that a
    REVERT falsifies content-presence, won the vote on that falsifier, and then
    implemented the function on proofs that cannot see a revert: `_landing_proof`
    answers `ancestor` the moment the original tip is reachable, and its own
    contract says a later revert STILL reads landed. `git cherry` is historical
    too. A normal ff-only revert therefore leaves BOTH proofs true while the
    bytes are gone from the tree. A reviewer caught it; my arms could not,
    because they mock `_landing_proof` and so ENCODE the claim instead of
    testing it.

    SO THIS IS A HISTORICAL FACT, NOT A CURRENT ONE: it says the work reached
    `ref` at some point, which is exactly what its two inputs can support.

    AND THE THIRD FACT HAS NO IMPLEMENTATION IN THIS CODEBASE. Net tree
    presence — "are these bytes on trunk RIGHT NOW" — is not answerable from
    ancestry or patch-identity, and nothing else here answers it either. That
    gap is the real finding of the split: helm derives TWO of the three facts
    its callers conflate, and the one a revert kills is the one nobody
    computes. Implementing it needs a tree-content probe and a land-then-revert
    arm against REAL git; until that exists, no caller may read this name as
    presence and no record may persist it as such."""
    proof = _landing_proof(gitdir, tip, ref)
    return None if proof == "unknown" else proof in ("ancestor",
                                                     "patch-equivalent")


# ── THE ONE SPELLING OF "resolve this ref to its commit" ─────────────────
# EXACT ARGV IS THE MEMO IDENTITY. `_git` memoises on
# ("landreq._git", gitdir, args, input) inside a `projscope.scope()`, so two
# spellings of one question are two spawns and — because the ref they name can
# MOVE between them — potentially two different answers under one read stamp.
# Five places ask it (`_resolve_trunk_commit`, `_canonical_trunk`,
# `_landing_proofs._pin`, its own `forget_pin_reads` eviction key, and
# `_pin_ref`), so the spelling is a CONSTANT rather than five string literals
# that agree today.
#
# BOTH FLAGS, IN THIS ORDER, AND THEY ARE COMPLEMENTARY RATHER THAN
# ALTERNATIVES. Measured on git 2.53.0 in this repository, 2026-08-11:
#
#   rev-parse --verify --end-of-options <missing>^{commit}          rc=128 + "fatal:"
#   rev-parse --verify --quiet --end-of-options <missing>^{commit}  rc=1, SILENT
#   rev-parse --verify --end-of-options --quiet HEAD^{commit}       rc=128 + "fatal:"
#
# `--quiet` is what makes an absent ref a quiet rc 1 instead of a fatal 128
# indistinguishable from a broken repository, and `--end-of-options` is what
# stops an option-shaped ref name from being parsed as an option. Placed AFTER
# `--end-of-options`, `--quiet` is itself a revision and the call dies 128 — so
# the order is load-bearing, not cosmetic.
#
# `^{commit}` is appended by the PEEL callers: it makes the answer a COMMIT
# rather than whatever object the ref happens to name, which is what those
# consumers mean by trunk. The plain-resolve callers share this argv and pass
# the ref bare, because the option hazard is a property of the ARGV and not of
# what the caller wants back — which is why one constant serves both and a
# second constant with the same tokens would only be a place to drift.
_REF_ARGV = ("rev-parse", "--verify", "--quiet", "--end-of-options")


def _peel_key(gitdir, ref):
    """The `projscope` memo key ONE peel read occupies.

    An eviction key that disagrees with the read key evicts NOTHING, and
    evicts it silently — `projscope.forget` is documented silent on a missing
    key, so a drifted spelling leaves a failed probe frozen for the whole
    projection while the code reads as though it retried. Sharing `_REF_ARGV`
    with the read keeps the ARGV honest; building the tuple through `_git_key`
    keeps its SHAPE honest, which is the half that broke when the read gained
    a field and every hand-built tuple silently stopped matching."""
    return _git_key(gitdir, _REF_ARGV + (str(ref) + "^{commit}",))


def _resolved_ref(gitdir, ref, cache):
    """The COMMIT `ref` names, resolved ONCE per (gitdir, ref) per projection.

    Cached in the same per-projection dict `_trunk_refs` uses. Returns "" when
    it cannot be resolved, and "" never equals "" for the purpose above because
    the caller requires a TRUTHY value before comparing — an unresolvable ref
    must never make two refs look identical.

    THE ARGV IS SPELLED TO MATCH `_landing_proof`'S, NOT MERELY TO WORK. That
    function normalises its own `ref` argument the same way before it reaches
    `git cherry`, and `_git`'s projscope memo keys on ARGV — so a different
    spelling of the identical question mints a SECOND key and spawns a second
    `rev-parse` per ref per projection. This lane was written against a trunk
    that had no such normalisation; trunk grew one while the lane sat, and
    re-expressing over it means sharing its key rather than racing it. Measured
    as spawns: two refs per gitdir, so the mismatch cost exactly the two
    rev-parses this lane's whole point is to stop paying.

    So: resolve once, hand the RESOLVED SHA to every later reader. A sha names
    an immutable commit, so two questions asked seconds apart cannot be about
    two different trunks. Same law as `gate.trunk_standing` ("resolve once,
    pass the resolved value, never re-read the name") and as the pin
    `_landing_proofs` already keeps per repository.

    Memoised in the SAME per-projection dict `_trunk_refs` uses, so a caller
    that already holds a projection cache pins ref names and their shas in one
    place and one lifetime — and a caller that hands that dict in (see
    `project_raw`'s `cache` argument) extends the pin to its own reads.

    THE ARGV IS `_REF_ARGV`, THE ONE SPELLING EVERY PEEL SITE SHARES, not a
    fifth string literal. Inside `projscope.scope()` an identical argv is
    spawned ONCE, so this function and `_landing_proofs._pin` are answered by
    the same reading rather than by two reads of a moving name. See that
    constant for the measured reason both flags are present and why their
    order matters.

    `^{commit}` is also the stronger question, which is why it is safe to adopt
    rather than merely cheaper: the caller compares these values to decide
    whether two trunk names are ONE COMMIT, and an unpeeled annotated tag would
    make two refs at the same commit compare unequal.

    Returns "" when the ref cannot be resolved. Callers must treat "" as
    "no pin" and fall back to the name — never as an identity, because two
    unresolvable refs must not compare equal.

    A GITDIR THAT IS NOT A STRING IS REFUSED BEFORE THE CACHE LOOKUP, and the
    ordering is `_trunk_refs`' law rather than a defensive habit: `repo_id` is
    copied verbatim out of the ledger row and never re-validated, so a row
    carrying `repo_id: ["not", "a", "path"]` reaches here as itself — and it
    would go into a cache KEY, where an unhashable member raises TypeError one
    statement before `_git` would have declined it. That is precisely how a
    junk repo binding once took a whole projection down through `_trunk_refs`
    (reproduced), and a second door into the same cache must not
    re-open it. It is not a path; it has no trunk to resolve."""
    if not isinstance(gitdir, str) or not gitdir or not ref:
        return ""
    if not isinstance(cache, dict):
        return _resolve_trunk_commit(gitdir, ref)
    key = ("refsha", gitdir, ref)
    if key not in cache:
        cache[key] = _resolve_trunk_commit(gitdir, ref)
    return cache[key]


def _resolve_trunk_commit(gitdir, ref):
    """One unmemoised ref->commit reading. Split out so `_resolved_ref` has a
    single spelling of the question whether or not it holds a cache.

    THE ARGV IS DELIBERATELY THE ONE ABOVE IT ALREADY WAS — `rev-parse
    --verify --quiet <ref>^{commit}` — and the split does not get to change it.
    `_git`'s projscope memo keys on ARGV, and `_landing_proof` normalises its
    own ref the same way, so a second spelling of the identical question mints
    a SECOND memo key and spawns a second `rev-parse` per ref per projection:
    exactly the cost this pin exists to stop paying. Factoring the call out of
    the two branches above is therefore a de-duplication of ONE question, not a
    new one."""
    if not ref:
        return ""
    p = _git(gitdir, *_REF_ARGV, str(ref) + "^{commit}")
    if p is None or p.returncode != 0:
        return ""
    return (p.stdout or "").strip()


def _landed(gitdir, tip, ref):
    """DEPRECATED — the lossy wrapper this step exists to retire.

    It collapses THREE facts that die differently into one word: `ancestor`
    and `patch-equivalent` both return True here while answering different
    questions, and `absent` and `unknown` both return falsey while one is a
    measurement and the other is the lack of one.

    A REVERT FALSIFIES NEITHER POSITIVE, and saying it falsified only the
    second was a wrong distinction rather than a subtle one. Both are
    HISTORICAL: the reverted commit stays reachable, and its patch stays in
    the range `git cherry` compares, so both keep answering True — which
    `_landing_proof` states in its own words. What separates them is the
    question asked, not their survival: ancestry is about THIS OBJECT being
    reachable, patch identity is about the same change arriving under another
    sha. Neither is a claim that the bytes are still in the tree, and no
    caller may read one as though it were.

    Census at the time of writing: 30 callers of this, against 12 of the typed
    `_landing_proof` it wraps. helm already derives the right answer and then
    throws the distinction away, at better than two to one.

    NOT DELETED YET, and that is deliberate rather than timid: 30 sites across
    four modules cannot be migrated correctly in one blind sweep, and a
    refusal that fires per-site names each caller at the moment it runs. New
    callers must pick one of ancestry / patch_identity / landed_ever and
    say which fact they meant."""
    return landed_ever(gitdir, tip, ref)


def _will_observe_git(row):
    """Will this row's projection actually ASK git about its tip?

    ONE DOOR, BECAUSE TWO CALLERS NEED THE SAME ANSWER. `_lr` needs it to
    decide whether to observe; the prefetch needs it to decide whose tips may
    go into a batch. Computing it twice is how the batch came to contain rows
    the projection explicitly refuses to observe — the prefetch was
    REPOSITORY-lazy (every eligible same-repo tip) where the invariant is
    ROW-observation-lazy, so one live row's batch carried monotonic historical
    closures whose own path passes git=False. A reviewer measured exactly that:
    a probe querying only the live tip captured historical SHAs in the batch's
    stdin. Beyond the no-reobserve invariant it can lazy-fetch or stall on
    dormant objects in a partial or promisor repository.

    Every input is a direct read off the raw row, so this is the same answer at
    prefetch time and at ask time.

    `_observation_owned` IS ONE OF THOSE INPUTS AND NOT AN ADDITION TO THEM.
    It arrived on trunk as a conjunct of the inline `git=` expression this
    function replaced, and taking either side of that rebase alone would have
    deleted a live rule: dropping it would restore the foreign-repository
    spawn, and leaving it only at the `_lr` call site would make the door lie
    to its OTHER caller. The prefetch is the caller that makes it worse, not
    merely unmerged — it groups tips BY `repo_id` and buys one
    `cat-file --batch-check` per gitdir, so an unowned row in the armed list is
    a git process spawned inside a repository this board has no standing to
    read. The authority predicate therefore belongs INSIDE the one door, where
    both callers inherit it, which is the whole reason the door exists."""
    if not (row or {}).get("tip"):
        return False
    if not _observation_owned(row):
        return False
    closed = row.get("status") in ("verdict", "closed")
    return bool(closed
                and not row.get("closed_by_landing")
                and not row.get("abandoned")
                and row.get("close_reason") not in (
                    "landed", "superseded", "stranded", "subsumed",
                    "delivered-report"))


def _prefetch_object_existence(rows, cache):
    """ARM the batch; do not fire it. Records which tips WOULD be asked about,
    per gitdir, so the first row that genuinely needs an answer can buy them
    all in one spawn.

    LAZY BECAUSE OBSERVING GIT IS ITSELF A BEHAVIOUR, NOT ONLY A COST. A
    historical closure is a RECORDED FACT and must survive trunk movement
    without re-deriving itself from a live repository — two arms pin exactly
    that, and they pin it on `_git` rather than on a function name so no new
    route can satisfy them by being called something else. An eager batch
    spawned git for a projection of nothing but closed rows, which is a
    correctness change wearing a performance change's clothes: it made those
    rows re-observe a repository they are defined not to consult.

    So nothing is spawned here. A projection whose rows all answer from their
    recorded closures performs NO git read at all, exactly as before, and the
    batch is bought only when something is actually going to ask."""
    pending = {}
    for row in rows or ():
        gd = (row or {}).get("repo_id")
        tip = (row or {}).get("tip")
        # ONLY TIPS THIS PROJECTION WILL ACTUALLY ASK ABOUT. Arming every
        # eligible tip made the batch REPOSITORY-lazy while the invariant is
        # ROW-observation-lazy: the first live row then bought answers for
        # monotonic historical closures that the projection refuses to observe.
        if isinstance(gd, str) and tip and _will_observe_git(row):
            pending.setdefault(gd, []).append(tip)
    for gd, shas in pending.items():
        cache[("objexist-pending", gd)] = shas


class _ObjectExistenceBatchFailure(dict):
    """Empty-map behaviour with a failure identity the Web witness can see.

    Callers must still fall through to their exact per-SHA probes, so this is an
    empty mapping. It is NOT an ordinary `{}`: collapsing an unreadable batch to
    a successful empty result made build and restart replay compare equal while
    both hid the unrecorded fallback probes. The cache retains this marker so a
    read-set can refuse persistence instead of certifying that unknown world."""


def _objexist_map(gitdir, cache):
    """The batch for `gitdir`, bought on FIRST NEED and then reused.

    Returns an empty mapping when there is nothing armed or the spawn failed, so
    every caller falls through to its own per-sha probe. Failure uses an empty
    marker subtype rather than ordinary `{}` so observers can retain UNKNOWN
    while preserving that mapping behaviour. The armed list is dropped either
    way — a failed batch must not be retried once per row, which would be worse
    than the per-row probe it replaced."""
    if not isinstance(cache, dict):
        return {}
    key = ("objexist", gitdir)
    if key in cache:
        return cache[key]
    shas = cache.pop(("objexist-pending", gitdir), None)
    if not shas:
        return {}
    got = _object_exists_batch(gitdir, shas)
    cache[key] = (_ObjectExistenceBatchFailure() if got is None else got)
    return cache[key]


def _commit_exists_cached(gitdir, sha, cache):
    """Does `sha` exist AND peel to a commit — the `<sha>^{commit}` question.

    THE ONLY CACHED EXISTENCE QUESTION, AND THAT IS DELIBERATE. A bare
    `cat-file -e <sha>` wrapper used to sit beside this one, reading its
    answer out of the same map — and the map cannot answer it. The batch asks
    `<sha>^{commit}` and nothing else, so a sha naming a BLOB or a TREE comes
    back refused; the bare wrapper turned that into False while the real bare
    probe says True. It had no callers and shipped a contract the data could
    not honour, so it is gone rather than reconstructed. A future bare reader
    needs its OWN batch under its OWN key — never a second reading of this one.

    Falls through to the original spawn on any miss, so the prefetch can only
    make this faster and never differently-answered."""
    got = _objexist_map(gitdir, cache)
    if sha in got:
        present, kind = got[sha]
        return bool(present and kind == "commit")
    p = _git(gitdir, "cat-file", "-e", sha + "^{commit}")
    return bool(p is not None and p.returncode == 0)


def _object_exists_batch(gitdir, shas):
    """{sha: (present, kind)} for MANY objects in ONE spawn, or None if it failed.

    ONE QUESTION PER PROJECTION, NOT ONE PER ROW. The live per-row caller is
    `_commit_exists_cached` (used by `_git_observe`), which spawns
    `cat-file -e <sha>^{commit}` per sha when it has no batch to read; the
    projection asks it once per land loop:
    measured on the live ledger, 946 of the 1952 spawns in a 248s build. Every
    one of those questions is DISTINCT, so the projscope memo — which is doing
    its job perfectly, 18 resolutions over 18 keys — cannot help. Only a
    different SHAPE can.

    `cat-file --batch-check` answers all of them down one pipe. Its contract is
    line-oriented and exact: a present object prints "<oid> <type> <size>", a
    missing one prints "<name> missing". Anything else about a line is UNKNOWN
    for that line, and a failed spawn is UNKNOWN for ALL of them — signalled by
    returning None so the caller falls back to the per-sha path rather than
    treating an unanswered batch as a page of absences.

    PEEL FORM ONLY — `<sha>^{commit}`, and this contract paragraph said the
    exact OPPOSITE until a reviewer caught it. It described the bare form I
    first intended, on a function that had already been written to peel, which
    is the same intended-design-versus-running-code class the wrapper deletion
    was supposed to end. It survived that cleanup because I corrected the two
    comments INSIDE the body and never re-read the docstring above them, and a
    function's public contract is the one a caller reads instead of the code.

    SO THE ANSWERS ARE ABOUT COMMIT-NESS, NOT EXISTENCE. A sha naming a blob or
    a tree comes back refused, and a caller wanting bare existence needs its
    OWN batch under its OWN key — never a second reading of this one."""
    shas = [s for s in dict.fromkeys(shas) if s]
    if not gitdir or not shas:
        return {}
    # THE PEEL FORM GOES INTO THE BATCH, so it answers the SAME question the
    # per-row probe asked. Feeding bare shas and comparing the advertised TYPE
    # is not equivalent to `cat-file -e <sha>^{commit}` in EITHER polarity, and
    # a reviewer measured both: an ANNOTATED TAG peels to a commit fine but
    # types `tag`, so a type check calls a readable row blind; and a truncated
    # loose commit advertised `commit 169` while the peel failed rc=128, so a
    # type check turned UNOBSERVABLE into observable-with-corrupt-state and let
    # lifecycle reasoning run on it. `<sha>^{commit}` is a peel expression
    # batch-check accepts, so GIT does the peeling and says `missing` for
    # anything that cannot.
    p = _git(gitdir, "cat-file", "--batch-check",
             input_text="\n".join(x + "^{commit}" for x in shas) + "\n")
    if p is None or p.returncode != 0:
        return None
    # ANSWERS ARE ZIPPED TO QUESTIONS, not read out of the output's first
    # field: a refused line echoes the peel EXPRESSION and a resolved line
    # reports the PEELED oid, neither of which is the sha we asked about. A
    # length mismatch means the answers cannot be aligned at all, which is
    # UNKNOWN for every sha rather than a partial map.
    out = {}
    lines = (p.stdout or "").splitlines()
    if len(lines) != len(shas):
        return None
    for sha, line in zip(shas, lines):
        parts = line.split()
        asked = sha + "^{commit}"
        # EXACT GRAMMAR OR NO ANSWER AT ALL. A reviewer measured both halves
        # of this being loose, and loose in the direction that MANUFACTURES a
        # verdict rather than withholding one.
        #
        # FAILURE: git echoes the QUERY it could not resolve, so a refusal is
        # exactly `<expression> missing|ambiguous`. Accepting any line whose
        # LAST token is missing/ambiguous zips a SHIFTED OR FOREIGN answer
        # onto this sha and caches it as definite absence — the batch would
        # confidently report a live commit gone because some other row's line
        # landed here.
        #
        # SUCCESS: a resolved line is `<40-hex-oid> <type> <decimal-size>`.
        # Accepting any three tokens let `garbage blob nope` manufacture
        # commit observability, and protocol corruption is precisely when a
        # confident answer is most dangerous.
        #
        # ANYTHING ELSE OMITS THE KEY, which is not a gap: the caller falls
        # through to its own per-sha probe and asks git directly. Refusing to
        # answer costs one spawn; answering wrongly costs a wrong verdict.
        if len(parts) == 2 and parts[0] == asked and parts[1] in (
                "missing", "ambiguous"):
            out[sha] = (False, "")
        elif (len(parts) == 3 and _sha(parts[0]) and parts[2].isdigit()):
            # THE TYPE IS KEPT because two different questions are asked of
            # these objects. `cat-file -e <sha>` asks only "does it exist";
            # `cat-file -e <sha>^{commit}` additionally asserts it PEELS to a
            # commit, and answers non-zero for an object that exists but is a
            # blob or a tree. THIS BATCH ASKS THE PEEL QUESTION ONLY, so its
            # answers are about commit-ness and nothing else. Reading them as
            # bare existence is the exact widening this comment used to claim
            # was avoided — a blob would read as ABSENT.
            # It PEELED, so it is a commit by construction — git resolved
            # the expression rather than us trusting an advertised type.
            out[sha] = (True, "commit")
        # anything else: no answer for that line, so the caller re-asks
    return out


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


@functools.lru_cache(maxsize=8192)
def _stem(name):
    """One ref/lane/path name reduced to its family stem.

    MEMOIZED, AND THE CACHE CARRIES NO WORLD STATE. This is a pure string
    reduction — no git, no ledger, no clock — so a cached answer cannot become
    stale and cannot carry state between two tests, which is the hazard a
    durable memo usually is. What it removes is real: the off-frontier walk
    asks the family question for 1202 rows against a 895-ref table, which is
    over a million calls to this regex ladder for 895 distinct inputs, and
    measured that was 2.3s of a 2.7s header.

    THE CANONICAL SUFFIX VOCABULARY, not a private one (#142 r2,
    finding 2): this probe once knew only -rN/-dHEX, so feature-review and
    feature-build read as strangers to the refusal-only liveness joins while
    `_lane_stem` called them family — and stranded is irreversible, so the
    probe must never under-match. It now composes `_LANE_FAMILY_SUFFIX_RE`
    (the one role/round vocabulary) plus the -dHEX worktree spelling only
    this probe meets. Matching is casefolded; the sliced spelling keeps its
    case, because refs are case-sensitive on disk.

    EVERY `refs/<namespace>/` PREFIX IS SLICED, not the two this knew. The
    liveness scan below now enumerates the WHOLE ref table (a lane preserved
    at `refs/helm-retired/lane/<name>` was invisible to a heads-and-remotes
    walk, so stranded called two live-but-retired lanes destroyed), and a
    namespace it does not slice keeps its whole path in the stem — which
    makes the PATH WORDS matchable: every retired ref contains the literal
    `helm-retired`, so a lane whose own stem is a substring of the namespace
    path matched all 341 of them. Slicing generically keeps the match on
    CONTENT. It changes nothing for a lane or branch field, which never
    begins with `refs/`."""
    s = str(name or "")
    if s.startswith("refs/remotes/"):
        s = s[len("refs/remotes/"):]
        s = s.split("/", 1)[1] if "/" in s else s
    elif s.startswith("refs/") and s.count("/") >= 2:
        s = s.split("/", 2)[2]
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

    CASEFOLDED on both sides (#142 r3): `_stem` slices the
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


def _lane_family_refs(gitdir, lane, branch, table=None):
    """(matches, err) — every live ref in the row's OWN repo whose stem is in
    the row's lane family, as (refname, objectname) pairs. `err` non-None
    means the ref table could not be read: UNKNOWN, and every caller REFUSES
    on it (tri-state law — a probe that could not look never clears).

    THE WHOLE REF TABLE, NOT refs/heads + refs/remotes. This walked exactly
    the two namespaces a lane is normally branched in, and helm's own worktree
    GC does not delete a lane — `work/_gc.py` COPIES it to
    `refs/helm-retired/lane/<branch>` FIRST and removes the branch only then.
    So the one namespace helm itself invented to PRESERVE a lane was the one
    this probe could not see, and the probe's only caller that authors
    anything is `stranded`, whose claim is that the work was DESTROYED.
    Two rows on the live board answered "WOULD close --reason stranded" while
    their lanes sat intact under refs/helm-retired/lane/ — an irreversible
    false terminal, one `for-each-ref` argument wide. They are a4133aed1dc6
    (lane foreign-repo-write-door) and e85ee1566272 (lane
    helm-runtime-identity-rebinds), and NAMING THEM HERE IS THE CITATION
    ITSELF: `docref_guard.LEDGER_CITED` is an allowlist for hex tokens the
    scanner finds under helm/, its own module is exempt from that scan, and a
    registered key cited in no other file is what `test_no_exemption_is_stale_
    or_double_booked` calls stale — so an entry that stood alone in the
    registry was a citation nothing resolved, and the pair could not serve as
    this rung's pass/fail control because nothing joined them to the rung. A
    regression re-admits exactly these two.
    NO NAMESPACE LIST, deliberately: refs/rescue, refs/backup, refs/withdrawn,
    refs/dropped, refs/snapshots and refs/tags/archive are all in use on that
    board too, an enumerated list is the second spelling that goes stale on
    the next invented namespace, and interlock law 2 already rules which
    direction to err in — a match only ever REFUSES, so over-matching costs an
    operator one read of a refusal that names the ref.

    `table` IS THE SAME READING, BOUGHT ONCE. A sweep asks this question of
    every row on the board and the answer for two rows in one repository comes
    out of ONE ref table; re-reading it per row is 1300 `for-each-ref` spawns
    for six distinct repositories. The caller passes what `ref_table` returned
    and the matching below is unchanged — so the cached path and the direct
    path cannot answer differently, which is the only property that matters
    for a probe whose hits block an irreversible terminal."""
    needles = _family_stems(lane, branch)
    if table is None:
        table, err = ref_table(gitdir)
        if err:
            return None, err
    matches = []
    for refname, obj in table:
        stem = _stem(refname)
        if any(_stems_match(stem, needle) for needle in needles):
            matches.append((refname, obj))
    return matches, None


def ref_table(gitdir):
    """([(refname, objectname)], err) — the repository's WHOLE ref table.

    Split out of `_lane_family_refs` so a sweep can buy one read per
    repository and hand it to every row. Same tri-state contract: `err`
    non-None means the table could not be read, which is UNKNOWN and never an
    empty table — "this repository has no refs" and "I could not look" are the
    two answers a fail-closed probe may never confuse."""
    p = _git(gitdir, "for-each-ref", "--format=%(refname) %(objectname)")
    if p is None or p.returncode != 0:
        return None, ("Git could not enumerate the repository's refs%s"
                      % ((": " + p.stderr.strip()) if p is not None
                         and p.stderr.strip() else ""))
    rows = []
    for line in p.stdout.splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        rows.append((parts[0], parts[1]))
    return rows, None


# ---------------------------------------------------------------------------
# THE LIVE FRONTIER — the predicate behind `helm lr retire --off-frontier`.
#
# A non-terminal row is ON the frontier while a ref in its own lane family
# still exists in its own repository: somebody can still check that work out,
# move it, land it. A row whose lane family is GONE is off the frontier — the
# work either reached trunk under a later tip, or it was abandoned and the
# lane reaped — and no verb retired it, so it stayed in the header's `open`
# count and the owner read ledger debris as queued improvements.
#
# THE PREDICATE FAILS CLOSED, and that is the whole safety argument. Four
# separate readings can each come back UNKNOWN (the repository is unreadable,
# the ref table is unreadable, the tip no longer resolves because git gc
# dropped it — task/2383 — or the landing ladder ran out of budget), and every
# one of them yields UNCLASSIFIED, which is counted, printed, and NEVER
# retired by `--apply`. Only a positive measurement retires a row.
ON_FRONTIER = "on-frontier"
OFF_FRONTIER_LANDED_ANCESTRY = "landed-by-ancestry"
OFF_FRONTIER_LANDED_PATCH_ID = "landed-by-patch-id"
OFF_FRONTIER_ABANDONED = "abandoned-unreachable"
OFF_FRONTIER_UNCLASSIFIED = "unclassified"

#: THE UNCLASSIFIED RUNGS WHOSE ROW IS GONE, not merely unread. An
#: UNCLASSIFIED answer is "counted as work owed" on the header (`filed_line`),
#: and for most rungs that is exactly right: a reading failed (`lane-family`,
#: `room`, `lease`, `trunk`, `live-hold`, `landing`), a ref outside the lane
#: family still holds the tip (`reachability`), or — the common one — the row
#: has no verdict tip to place (`tip`), which is every fresh review whose free
#: text label matches no branch name. MEASURED on the live board: six
#: AWAITING_REVIEW rows dispatched that day answered `tip`, each a review
#: somebody owed. Only two rungs describe a row nobody can move: `object`,
#: reached only after the lane, its room and its lease were all measured gone,
#: where the reviewed commit no longer resolves in the repository at all; and
#: `polarity`, where git PLACED the work off the frontier and only the close
#: route could not be read. The owner board folds those two into a line of
#: their own (`scheduler.collapse_class`) and keeps every other UNCLASSIFIED
#: row listed — AN ALLOWLIST, so a rung added tomorrow keeps its rows on the
#: list, the direction that cannot hide an obligation. The arm
#: `test_the_gone_rungs_are_the_census_own_words` drives both rungs through
#: the census so a renamed rung goes red.
FRONTIER_GONE_RUNGS = ("object", "polarity")

#: The three measurements that ADMIT a retirement. `unclassified` is
#: deliberately absent: it is the bucket for every reading that did not
#: happen, and a door keyed on this tuple can therefore never open on one.
OFF_FRONTIER_REASONS = (OFF_FRONTIER_LANDED_ANCESTRY,
                        OFF_FRONTIER_LANDED_PATCH_ID,
                        OFF_FRONTIER_ABANDONED)

#: THE VERDICT'S POLARITY DECIDES THE DOOR, not only the git measurement.
#: A close reason answers a question about the WORK, and "did this reach
#: trunk" and "does anybody still owe a cure" are two questions: `landed`
#: refuses a FIX/SUPERSEDE row on purpose, because a contrary that entered
#: trunk is contrary DEBT and a lane going away answers no finding.
CONTRARY_POLARITIES = ("fix", "supersede")

#: WHICH EXISTING CLOSE REASON EACH MEASUREMENT SUPPORTS — no new vocabulary,
#: keyed on (measurement, is this a contrary). Every word here is one
#: `dispatches.CLOSE_REASONS` already holds and whose ladder re-proves the
#: claim under the lock rather than believing the sweep:
#:
#:   landed     a NON-contrary row whose tip git puts on trunk. The delivery
#:              declaration is still required and still the ladder's to judge.
#:              MEASURED, AND THE COLUMN IS EMPTY TODAY: such a row is
#:              TERMINAL before this verb sees it — the projection observes the
#:              landing, reads the row LANDED, and it leaves
#:              `open_bucket_rows`. The entry stays because it is the right
#:              door for that measurement, and an arm asserts the emptiness
#:              (`test_a_NON_contrary_landing_never_reaches_this_verb_at_all`)
#:              rather than leaving a route nobody ever exercises — which is
#:              exactly what `stranded` was.
#:   carried    a CONTRARY row whose work reached trunk anyway — the door
#:              task/756 built for exactly this class ("a FIX-verdicted row
#:              whose work landed anyway"), admitting every polarity because
#:              its gate is a measured carriage witness that no polarity can
#:              forge. MEASURED against the live ledger: its witness answers
#:              TRUE for 130 of the 161 patch-id rows here and the full ladder
#:              admits them; it cannot be measured for an EXACT-ancestor tip
#:              (the replay range is empty), which is why that bucket's
#:              refusals are this verb's product rather than its failure.
#:   withdrawn  the absence door, for a tip that is on NO ref and not on
#:              trunk. It replaces `stranded`, which was unreachable by
#:              construction: stranded demands a PRUNED object and
#:              `off_frontier_reason` proves the object PRESENT before it will
#:              ever answer `abandoned-unreachable`, so every routed row met a
#:              refusal the route itself had guaranteed. `withdrawn` asks the
#:              question this bucket actually measured — off trunk by
#:              ancestry AND absent by patch id — and admits every polarity.
#:
#: `test_every_off_frontier_reason_maps_into_the_close_registry` binds this
#: table to `OFF_FRONTIER_REASONS`, so a reason added here with no home in the
#: registry turns the suite red instead of minting a word at the writer.
OFF_FRONTIER_CLOSE_REASON = {
    OFF_FRONTIER_LANDED_ANCESTRY: {"resolution": "landed",
                                   "contrary": "carried"},
    OFF_FRONTIER_LANDED_PATCH_ID: {"resolution": "landed",
                                   "contrary": "carried"},
    OFF_FRONTIER_ABANDONED: {"resolution": "withdrawn",
                             "contrary": "withdrawn"},
}


def off_frontier_route(reason, polarity):
    """Which close reason this row's measurement AND verdict support.

    Two facts, never one: the git measurement says where the work is, the
    verdict's polarity says whether anybody is still owed a cure. The first
    cut of this verb read only the measurement and proposed `landed` for a
    board that is 295/301 FIX — every row met the same correct refusal, and
    `--apply` closed nothing at all."""
    doors = OFF_FRONTIER_CLOSE_REASON.get(reason)
    if not doors:
        return None
    return doors["contrary" if polarity in CONTRARY_POLARITIES
                 else "resolution"]


def _off_frontier_tip(lr):
    """The commit this row's classification is ABOUT.

    The reviewed tip first, because on a verdicted row that is the object the
    review bound and the only one whose arrival on trunk says anything. A row
    with no verdict falls back to the dispatch ref it was sent against. A row
    with neither cannot be classified at all, which is UNCLASSIFIED and not a
    licence to guess from the lane name.

    A BUILD'S DISPATCH REF IS THE BASE IT WAS SENT AGAINST, NOT ITS WORK — the
    same fact `_annotate_build_lanes` states for the containment mark. A build
    sent at trunk pins a commit trunk holds the moment it is sent, so reading
    that ref here walked a build nobody had started, its lane not yet claimed,
    to `landed-by-ancestry`: the owner board folded it into "left over after
    landing" and `helm lr retire --off-frontier` offered to close it as landed.
    An unverdicted build has no commit to place, which is the `tip` rung's
    honest answer (work owed, never retirable); a verdicted one is placed by
    its reviewed tip exactly as a review is."""
    keys = ("reviewed_tip",) if lr.get("kind") == "build" \
        else ("reviewed_tip", "tip")
    for key in keys:
        value = lr.get(key)
        if isinstance(value, str) and _FULL_SHA_RE.fullmatch(value.strip()):
            return value.strip().lower()
    return None


def lane_branch_presence(lr, table=None):
    """(state, why) — does a ref in this row's lane family still exist?

    `present` / `absent` / `unknown`, and `unknown` is the answer to every
    reading that did not happen. The family match is `_lane_family_refs`, the
    same generous, refusal-only probe the `stranded` ladder's fourth rung
    uses — over-matching costs this sweep one row left on the frontier and
    under-matching would cost a false terminal, so the direction is already
    ruled and this borrows the ruling rather than re-deciding it.

    `table` IS THE `(rows, err)` PAIR `ref_table` RETURNS, not a bare list, so
    a caller that already read the table hands over BOTH halves of it. A bare
    list could only carry the rows, and an empty list would then be
    indistinguishable from a failed read — which is exactly the collapse
    between "nothing matches" and "I could not look" that turns `unknown` into
    a retirement."""
    gitdir = lr.get("repo_id")
    if not isinstance(gitdir, str) or not gitdir:
        return "unknown", "the row names no repository, so its lane cannot be looked up"
    if not _observation_owned(lr):
        return "unknown", ("this board does not own git observation for %s"
                           % gitdir)
    rows, err = table if table is not None else ref_table(gitdir)
    if err:
        return "unknown", err
    matches, err = _lane_family_refs(gitdir, lr.get("lane"), lr.get("branch"),
                                     table=rows)
    if err:
        return "unknown", err
    if matches:
        ref, obj = matches[0]
        return "present", "lane-family ref %s @ %s" % (ref, obj[:12])
    return "absent", "no ref in the lane family exists in %s" % gitdir


#: THE REF NAMESPACES THAT MEAN A LANE IS STILL LIVE. Everything else this
#: repository carries under `refs/` — `refs/helm-retired/`, `refs/lane-backup/`,
#: `refs/rescue/`, `refs/backup/`, `refs/dropped/`, archive tags — is a
#: PRESERVED COPY of a lane helm already reaped: `work/_gc.py` copies the
#: branch to `refs/helm-retired/lane/<branch>` before it deletes it, so an
#: archived ref is the NORMAL end state of every landed lane on this board
#: (measured: 391 of 899 refs here). Counting those as frontier would put
#: every reaped lane back on the owner's "work owed" number, which is the
#: residue this verb exists to name.
#:
#: AN ALLOWLIST, NOT A LIST OF ARCHIVES, and the direction is deliberate.
#: `_lane_family_refs` refuses to enumerate archive namespaces because that
#: list goes stale on the next invented one — and there the staleness costs a
#: REFUSAL. Here it is inverted: a namespace missing from this tuple cannot
#: hold a row ON the frontier, so a future live namespace would cost a false
#: terminal. That is why the rung below is not the only liveness probe: the
#: lane family, the room registry and the claims ledger each answer first,
#: and each of them reads the WHOLE ref table.
LIVE_REF_NAMESPACES = ("refs/heads/", "refs/remotes/")


def _worktree_records(gitdir):
    """(rows, err) — this repository's worktree registry, parsed ONCE.

    Split out of `_lane_family_worktrees` for the same reason `ref_table` was
    split out of `_lane_family_refs`: a sweep asks about every row on the
    board and the answer for two rows in one repository comes out of one
    read. `vcs.parse_worktree_records` already refuses a truncated porcelain
    stream, so an unreadable registry is `err` and never an empty list."""
    from . import vcs
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
    return rows, None


def frontier_cache():
    """The readings ONE frontier walk buys once and hands to every row.

    Four registries answer the frontier question — the ref table, the
    worktree registry, the claims ledger and the set of commits live refs
    hold off trunk — and all four are per REPOSITORY (the claims ledger is
    per host) while the question is per ROW. Measured on the live board, 1314
    open rows name one repository.

    THE LIFETIME IS THE CALLER'S WALK AND NEVER LONGER, the law `ref_tables`
    already states: between two board reads a lane may have been cut or
    reaped, and a cached answer about that is the one thing a probe whose
    hits block a terminal must never give. A FAILED reading is cached exactly
    like a successful one, so a repository that already refused is not
    re-spawned per row and a repeated refusal cannot turn `unknown` into an
    answer."""
    return {"tables": {}, "rooms": {}, "holds": {}, "leases": None}


def _cached_rooms(gitdir, cache):
    """This repository's worktree registry, at most once per repository."""
    if cache is None:
        return _worktree_records(gitdir)
    rooms = cache.setdefault("rooms", {})
    if gitdir not in rooms:
        rooms[gitdir] = _worktree_records(gitdir)
    return rooms[gitdir]


def _cached_leases(cache):
    """The host's live claim table, read at most once per walk.

    `claims_list(gc=False)` is the READ-ONLY spelling: the sweep of expired
    rows still happens in memory, so the table is identical to what every
    other surface sees, and nothing is persisted from a classification path
    (the second-census law — a reader deciding what to retire must not mutate
    the store it is deciding from).

    THE STRICT READER DECIDES WHETHER THE FILE WAS READ AT ALL, and asking it
    is not belt-and-braces — it is the only thing standing between an
    unreadable ledger and a retirement. `claims_list` reads through
    `pk.read_json(path, {})`, whose LENIENT branch catches every exception and
    answers the DEFAULT, so a claims file that is corrupt, truncated,
    mode-000 or a FIFO comes back as an EMPTY TABLE and this rung's own
    `except` never runs. The lease rung then says "no live lease names this
    lane" about a file nobody could open, and that sentence is the one the
    frontier treats as decisive — an ABSENCE that clears a row for retirement.
    `claims_unavailable` is the tree's answer to exactly this question (its
    own docstring: "a census narrower than its mover is the defect"), it takes
    no lock and persists nothing, and it refuses on every shape the WRITER
    refuses on, so this reader can never be more permissive than the ledger's
    own mover.

    IT COSTS ONE EXTRA OPEN PER WALK, not per row, and it fails the WHOLE
    walk closed: every row in a census taken over an unreadable claims file
    reads UNCLASSIFIED and `--apply` touches none of them. That is the
    direction this rung must fail in — the alternative is clearing a lane a
    seat is holding, which is the defect that put two GUARDED, locked, leased
    rooms in the first cut's retire plan."""
    if cache is not None and cache.get("leases") is not None:
        return cache["leases"]
    try:
        from . import seats_claims
        unreadable = seats_claims.claims_unavailable()
        answer = ((None, "the claims ledger is unreadable: %s" % unreadable)
                  if unreadable else (seats_claims.claims_list(gc=False), None))
    except Exception as exc:                                   # noqa: BLE001
        answer = (None, "the claims ledger is unreadable: %s"
                  % type(exc).__name__)
    if cache is not None:
        cache["leases"] = answer
    return answer


def lane_room_presence(lr, rooms=None):
    """(state, why) — is a WORKTREE ROOM of this row's lane checked out?

    THE SECOND PLACE WORK LIVES, and the `stranded` ladder has asked it for
    longer than this verb has existed (its rung 5): a room can hold
    uncommitted or unpushed work that no ref in the repository carries. A row
    whose room is still on disk is not residue whatever its refs say.

    Tri-state like every other rung: an unreadable registry is `unknown`,
    which is UNCLASSIFIED and never a retirement."""
    gitdir = lr.get("repo_id")
    if not isinstance(gitdir, str) or not gitdir:
        return "unknown", "the row names no repository, so its rooms cannot be looked up"
    if not _observation_owned(lr):
        return "unknown", "this board does not own git observation for %s" % gitdir
    matches, err = _lane_family_worktrees(gitdir, lr.get("lane"),
                                          lr.get("branch"), rooms=rooms)
    if err:
        return "unknown", ("the repository's worktree registry could not be "
                           "read (%s)" % err)
    if matches:
        return "present", "a lane-family worktree is checked out at %s" % matches[0]["path"]
    return "absent", "no worktree of this lane's family is checked out"


def lane_lease_presence(lr, leases=None):
    """(state, why) — does a LIVE LEASE name this row's lane?

    THE THIRD PLACE, and the one that is decisive on its own. `helm work
    claim` writes `worktree:<project>:<lane>` into the claims ledger and
    `helm work list` renders it as `resume: helm work claim <lane>`; a lease
    is a seat's standing statement that it is holding this lane. Measured on
    this board: two rows the first cut of this verb listed for RETIRE were
    GUARDED, locked, leased rooms — their branch existed under a spelling the
    row's free-text lane label does not stem-match, so every NAME probe said
    absent about work a seat was holding.

    The claims table `claims_list` returns is already swept of expired rows,
    so a row here IS an unexpired lease. A holder whose liveness cannot be
    measured still holds it — the fence outlives the process, which is the
    whole point of a lease — so this rung reads the RESOURCE, not the
    holder's pulse."""
    gitdir = lr.get("repo_id")
    if not isinstance(gitdir, str) or not gitdir:
        return "unknown", "the row names no repository, so its lane cannot be leased"
    rows, err = leases if leases is not None else _cached_leases(None)
    if err:
        return "unknown", err
    root = _strip_gitdir(gitdir)
    if not root:
        return "unknown", "%s does not resolve to a repository root" % gitdir
    # THE PROJECT HALF OF THE RESOURCE HAS ONE OWNER and this is not it:
    # `work._lanes.project_token` is the single derivation of that token (its
    # own docstring forbids a second), and the import is deferred because
    # `work` imports this module. An import that cannot be taken is UNKNOWN —
    # a rung that raises into this walk would take the whole census down over
    # one row, where the tri-state contract says it must refuse.
    try:
        from .work import _lanes
        project = _lanes.project_token(root)
    except Exception as exc:                                   # noqa: BLE001
        return "unknown", ("the lane resource name could not be derived: %s"
                           % type(exc).__name__)
    needles = _family_stems(lr.get("lane"), lr.get("branch"))
    for claim in rows or []:
        resource = str(claim.get("resource") or "")
        parts = resource.split(":", 2)
        if len(parts) != 3 or parts[0] != "worktree" or parts[1] != project:
            continue
        lane = parts[2]
        if lane == str(lr.get("lane") or "") \
                or any(_stems_match(_stem(lane), n) for n in needles):
            return "present", ("a live lease holds %s (holder %s)"
                               % (resource, claim.get("holder") or "unknown"))
    return "absent", "no live lease names this lane"


def _strip_gitdir(gitdir):
    """The repository ROOT behind a git common-dir, or None.

    A row's `repo_id` is a common-dir (`…/repo/.git`), and the lane registries
    are keyed on the root beside it — the claims resource carries the root's
    basename and the rooms live in its `-wt` sibling. A gitdir that is not
    spelled that way answers None rather than a guess."""
    if not isinstance(gitdir, str) or not gitdir:
        return None
    path = gitdir.rstrip(os.sep)
    if os.path.basename(path) == ".git":
        return os.path.dirname(path) or None
    return path or None


def _frontier_trunk(gitdir, refs):
    """(trunk ref, PINNED commit, err) — this repository's authoritative trunk,
    resolved to one immutable object.

    THE NAME IS FOR THE SENTENCE, THE PIN IS FOR THE ARGV. Every git question
    this classification asks is answered about whatever the name pointed at as
    it ran, so a fetch between two rows judges them against two different
    trunks — the defect `_resolved_ref` and `gate.trunk_standing` both exist to
    close, stated once as "resolve once, pass the resolved value, never re-read
    the name". A trunk that will not peel is UNMEASURED and says so; it never
    falls back to the moving name, because two failures compare equal and the
    fallback would be invisible."""
    trunk, terr = _abandon_trunk(gitdir)
    if terr:
        return None, None, terr
    pin = (_resolved_ref(gitdir, trunk, refs) or "").lower()
    if not _sha(pin):
        return None, None, ("the authoritative trunk %s does not resolve to "
                            "one commit, so this lane can only be measured "
                            "against a moving name" % trunk)
    return trunk, pin, None


def _cached_trunk(gitdir, cache):
    """This repository's (ref, pin), resolved at most once per walk.

    `_abandon_trunk` reads two ref names and `_resolved_ref` peels one, so a
    per-row call is three git questions per row for one answer per repository
    — and outside a projection scope, where `_git`'s memo does not apply, that
    is thousands of spawns on a board read."""
    if cache is None:
        return _frontier_trunk(gitdir, {})
    trunks = cache.setdefault("trunks", {})
    if gitdir not in trunks:
        trunks[gitdir] = _frontier_trunk(gitdir, cache.setdefault("refs", {}))
    return trunks[gitdir]


def live_frontier_commits(gitdir, trunk, table=None, rooms=None):
    """(frozenset of shas, err) — every commit a LIVE branch or a checked-out
    room holds that `trunk` does not. `trunk` is the PINNED COMMIT, never the
    ref name: see `_frontier_trunk`.

    ONE WALK PER REPOSITORY ANSWERS EVERY ROW, and that is why this is a set
    rather than a per-row `--contains`. Measured on this repository: the walk
    is 20ms for 899 refs and yields ~2100 off-trunk commits, while
    `for-each-ref --contains` is ~17ms PER ROW — 20 seconds across the open
    board, on the web read path.

    `--not <trunk>` IS THE WHOLE CORRECTNESS OF IT. A tip that already
    reached trunk is contained by every ref that contains trunk, so a bare
    reachability question answers "still held" for every landed row in the
    ledger and the verb dies. The question this set answers is narrower and
    is the one the frontier means: is this commit held OFF trunk, where only
    a lane can be holding it?"""
    rows, err = table if table is not None else ref_table(gitdir)
    if err:
        return None, err
    tips = {obj for refname, obj in (rows or [])
            if str(refname).startswith(LIVE_REF_NAMESPACES) and _sha(obj)}
    records, werr = rooms if rooms is not None else _worktree_records(gitdir)
    if werr:
        return None, ("the repository's worktree registry could not be read "
                      "(%s)" % werr)
    # A DETACHED ROOM HAS NO REF AND IS STILL A PLACE THE WORK LIVES. Its
    # HEAD is the only name it has, and dropping it is how eight rows whose
    # tip was held only by a checked-out room read as debris.
    tips.update(str(r.get("head") or "").lower() for r in (records or [])
                if _sha(str(r.get("head") or "").lower()))
    if not tips:
        return frozenset(), None
    p = _git(gitdir, "rev-list", *sorted(tips), "--not", trunk)
    if p is None or p.returncode != 0:
        return None, ("Git could not walk what live refs hold off %s%s"
                      % (trunk,
                         (": " + p.stderr.strip()) if p is not None
                         and p.stderr.strip() else ""))
    return frozenset(line.strip().lower() for line in p.stdout.splitlines()
                     if line.strip()), None


def _cached_hold(gitdir, trunk, cache, table=None, rooms=None):
    """The off-trunk live commit set for one (repository, trunk), once."""
    if cache is None:
        return live_frontier_commits(gitdir, trunk, table=table, rooms=rooms)
    holds = cache.setdefault("holds", {})
    key = (gitdir, trunk)
    if key not in holds:
        holds[key] = live_frontier_commits(gitdir, trunk, table=table,
                                           rooms=rooms)
    return holds[key]


def _live_holder(gitdir, sha):
    """One LIVE ref that contains this commit, for the evidence sentence.

    Asked only when the set above already said the commit is held, so the
    per-row cost is paid by the handful of rows it names rather than by the
    board. A room holding it on a detached HEAD has no ref to name, which is
    why the caller's sentence works without one."""
    p = _git(gitdir, "for-each-ref", "--count=1", "--format=%(refname)",
             "--contains", sha, *LIVE_REF_NAMESPACES)
    if p is None or p.returncode != 0:
        return None
    hit = (p.stdout or "").strip().splitlines()
    return hit[0].strip() if hit else None


def _reaching_ref(gitdir, sha):
    """(refname-or-None, err) — one ref this commit is reachable from.

    `--count=1` because the question is existential: the FIRST reaching ref
    ends the walk, and naming it is what makes the refusal readable. A ref
    table git could not read is `err`, never an empty answer — the difference
    between "nothing retains this commit" and "I could not ask" is the whole
    difference between ABANDONED and UNCLASSIFIED."""
    p = _git(gitdir, "for-each-ref", "--count=1", "--format=%(refname)",
             "--contains", sha)
    if p is None or p.returncode != 0:
        return None, ("Git could not ask which refs reach %s%s"
                      % (sha[:12],
                         (": " + p.stderr.strip()) if p is not None
                         and p.stderr.strip() else ""))
    hit = (p.stdout or "").strip().splitlines()
    return (hit[0].strip() if hit else None), None


def off_frontier_reason(lr, table=None, cache=None):
    """{reason, evidence, ...} — where this non-terminal row stands.

    `reason` is ON_FRONTIER, one of OFF_FRONTIER_REASONS, or UNCLASSIFIED.
    `evidence` is the sentence naming what proved it, and it travels onto the
    close event so a reader years later sees the measurement and not a label.

    THE ORDER OF THE RUNGS IS THE SAFETY. Every place work can still live is
    asked BEFORE the landing ladder runs, so a row whose work is checked out,
    leased or held off trunk is never even classified and no terminal is ever
    proposed for it. Only once all four are measurably gone does the tip get
    asked whether it reached trunk, and only once trunk has measurably refused
    it does anything ask whether a ref still retains it.

    FOUR RUNGS, BECAUSE A NAME IS NOT AN IDENTITY. A row's `branch` field
    carries the dispatch REF — measured, 286 of 286 retire-plan rows hold a
    bare sha there — so every name-keyed probe runs on the free-text `lane`
    LABEL alone, and a label that reads `review-derive-row-state` does not
    stem-match the branch `lane/derive-row-state-from-artifacts` it was
    written about. The first cut listed two GUARDED, locked, leased rooms for
    retirement on exactly that miss. The rungs below do not depend on one
    another: the lane family and the room registry are name-keyed, the claims
    ledger is the seat's own statement, and the last one is keyed on the
    COMMIT and needs no name at all.

    WHICH READING EACH RUNG FAILS TOWARD — audited rung by rung against the
    accessor each one actually calls, because ONE of them did not fail the way
    its own docstring said. The lease rung called `claims_list`, whose reader
    swallows a file-level failure and answers an EMPTY TABLE, so an unreadable
    claims ledger read as "no lease" — the one direction a liveness probe may
    never fail in, since its `absent` is what clears a row. `_cached_leases`
    now asks the STRICT reader first. Every rung, and the accessor whose
    refusal decides it:

      rung          accessor                     a failed reading answers
      ----          --------                     ------------------------
      ownership     `_observation_owned`         UNCLASSIFIED (foreign repo)
      lane-family   `ref_table`                  UNCLASSIFIED (err, never [])
      room          `_worktree_records`          UNCLASSIFIED (err, never [])
      lease         `claims_unavailable`         UNCLASSIFIED (strict refusal)
      tip           `_off_frontier_tip`          UNCLASSIFIED (no full sha)
      trunk         `_frontier_trunk`            UNCLASSIFIED (no pin)
      live-hold     `live_frontier_commits`      UNCLASSIFIED (err, never ∅)
      object        `_object_exists`             UNCLASSIFIED (False or None)
      landing       `_landing_proof`             UNCLASSIFIED (not `absent`)
      reachability  `_reaching_ref`              UNCLASSIFIED (err, or a ref)
      polarity      `_chain_polarity`            UNCLASSIFIED (`_routed_verdict`)

    THE TWO PLACES THE TABLE IS NOT THE WHOLE STORY, both read in source
    rather than assumed:

    - `live_frontier_commits` answers an EMPTY SET, not an error, when the
      repository has no live refs and no rooms at all — and an empty set is
      "nothing is held", which is the OPEN direction. It cannot be reached
      with anything to lose: a repository with no refs cannot peel a trunk,
      so `_frontier_trunk` has already answered UNCLASSIFIED one rung above.
    - the landing rung's `absent` is a POSITIVE measurement and is the only
      answer that continues to the reachability rung; `unknown` — the ladder's
      word for a budget it could not spend — stops here as UNCLASSIFIED, which
      is why a busy board under-reports residue rather than over-reporting
      it."""
    state, why = lane_branch_presence(lr, table=table)
    if state == "unknown":
        return {"reason": OFF_FRONTIER_UNCLASSIFIED, "evidence": why,
                "rung": "lane-family"}
    if state == "present":
        return {"reason": ON_FRONTIER, "evidence": why, "rung": "lane-family"}
    gitdir = lr.get("repo_id")
    # OWNERSHIP IS ASKED BEFORE THE SPAWN, not after it — the law the ref-table
    # cache already carries. `git worktree list` inside another project's
    # repository is an authority claim helm does not hold, and a cache that
    # fills itself first and checks later has already made it. The rung below
    # refuses such a row on its own (`_observation_owned`), so it never reads
    # the None this hands it.
    rooms = _cached_rooms(gitdir, cache) if _observation_owned(lr) else None
    state, why = lane_room_presence(lr, rooms=rooms)
    if state == "unknown":
        return {"reason": OFF_FRONTIER_UNCLASSIFIED, "evidence": why,
                "rung": "room"}
    if state == "present":
        return {"reason": ON_FRONTIER, "evidence": why, "rung": "room"}
    state, why = lane_lease_presence(lr, leases=_cached_leases(cache))
    if state == "unknown":
        return {"reason": OFF_FRONTIER_UNCLASSIFIED, "evidence": why,
                "rung": "lease"}
    if state == "present":
        return {"reason": ON_FRONTIER, "evidence": why, "rung": "lease"}
    tip = _off_frontier_tip(lr)
    if not tip:
        return {"reason": OFF_FRONTIER_UNCLASSIFIED, "rung": "tip",
                "evidence": ("%s records no full-sha tip, so there is nothing "
                             "to compare against trunk" % lr["id"])}
    trunk, pin, terr = _cached_trunk(gitdir, cache)
    if terr:
        return {"reason": OFF_FRONTIER_UNCLASSIFIED, "rung": "trunk",
                "evidence": terr}
    # THE RUNG THAT NEEDS NO NAME, and the only one that could have caught the
    # rooms the three above missed. `--not <pin>` is what makes it mean
    # something: a tip that already reached trunk is contained by every ref
    # that contains trunk, so the question is not "does anything reach this
    # commit" but "is this commit held where only a lane can be holding it".
    held, herr = _cached_hold(gitdir, pin, cache, table=table, rooms=rooms)
    if herr:
        return {"reason": OFF_FRONTIER_UNCLASSIFIED, "rung": "live-hold",
                "tip": tip, "trunk": trunk, "evidence": herr}
    if tip in held:
        holder = _live_holder(gitdir, tip)
        return {"reason": ON_FRONTIER, "rung": "live-hold", "tip": tip,
                "trunk": trunk,
                "evidence": ("%s is held off %s by %s — the work is still "
                             "checked out somewhere"
                             % (tip[:12], trunk,
                                holder or "a live branch or checked-out room"))}
    # THE LADDER IS ASKED ABOUT THE PIN, NOT THE NAME, for the reason
    # `_frontier_trunk` states and the reason the row-proof leg already
    # resolves before it asks: a name in the argv lets two rows in one read be
    # judged against two different trunks. The SENTENCE still names the ref,
    # because that is what a reader recognises, and it carries the pin beside
    # it so the measurement can be re-run exactly.
    #
    # THE FREE PROOF FIRST, THEN THE CHEAP LOCAL READ, AND ONLY THEN THE
    # DERIVE. A kept proof costs no spawn and can place a row whose object is
    # now GONE — it was taken while the object was there, which is exactly the
    # case the durable ledger exists for, so asking it before the object rung
    # keeps those placements. A tip that then fails to resolve LOCALLY is
    # task/2383's population and cannot be placed by any later reading, so it
    # stops here — before `_landing_proof`'s derive, which can only answer
    # `unknown` for an object this clone cannot resolve. That derive once sent
    # such a tip through `_vanished_proof`: one `ls-remote origin` plus a full
    # reflog scan PER ROW. MEASURED on a copy of the live board, header
    # only: 111 rows reached that leg and cost 119s of ls-remote and 22s of
    # reflog out of a 150s classification. The network was being asked to
    # confirm what a local `cat-file -e` already knew.
    proof = _kept_landing_proof(gitdir, tip, pin)
    if proof not in ("ancestor", "patch-equivalent"):
        present = _object_exists(gitdir, tip)
        if present is not True:
            return {"reason": OFF_FRONTIER_UNCLASSIFIED, "rung": "object",
                    "tip": tip, "trunk": trunk, "trunk_sha": pin,
                    "evidence": ("%s no longer resolves in %s (task/2383: a "
                                 "reviewed tip is not protected from git gc), "
                                 "so its land state is unmeasurable"
                                 % (tip[:12], gitdir) if present is False else
                                 "Git could not determine whether %s still "
                                 "exists in %s" % (tip[:12], gitdir))}
        proof = _landing_proof(gitdir, tip, pin)
    if proof == "ancestor":
        return {"reason": OFF_FRONTIER_LANDED_ANCESTRY, "rung": "landing",
                "tip": tip, "trunk": trunk, "trunk_sha": pin,
                "close_proof_mode": "ancestor",
                "evidence": ("lane family gone; %s is an ancestor of %s (%s)"
                             % (tip[:12], trunk, pin[:12]))}
    if proof == "patch-equivalent":
        return {"reason": OFF_FRONTIER_LANDED_PATCH_ID, "rung": "landing",
                "tip": tip, "trunk": trunk, "trunk_sha": pin,
                "close_proof_mode": "patch-equivalent",
                "evidence": ("lane family gone; %s carries the same patch id "
                             "as a commit on %s (%s)"
                             % (tip[:12], trunk, pin[:12]))}
    # THE GC RUNG RAN ABOVE, BEFORE THE DERIVE, and its position is what makes
    # the refusal useful rather than merely safe. A PRUNED tip reaches the
    # derive only as `unknown` — the ladder cannot resolve the name at all,
    # and an object missing from this clone is never scored `absent` — which
    # is task/2383's population, the object gone so no reader can ever
    # re-adjudicate it. Asking about the object BEFORE the
    # derive names that cause instead of reporting "the ladder answered
    # unknown", keeps the pruned rows out of ABANDONED (where `--contains` on
    # a name git does not know answers the same empty table as a commit
    # nothing reaches), and spares each of them a network round trip.
    if proof != "absent":
        return {"reason": OFF_FRONTIER_UNCLASSIFIED, "rung": "landing",
                "tip": tip, "trunk": trunk,
                "evidence": ("the landing ladder answered %s for %s against "
                             "%s — not a measurement" % (proof, tip[:12],
                                                         trunk))}
    ref, rerr = _reaching_ref(gitdir, tip)
    if rerr:
        return {"reason": OFF_FRONTIER_UNCLASSIFIED, "rung": "reachability",
                "tip": tip, "trunk": trunk, "evidence": rerr}
    if ref:
        # NOT ON TRUNK AND NOT UNREACHABLE IS NEITHER ANSWER. A retired lane
        # copy (`refs/helm-retired/lane/…`), an archive tag, a rescue branch —
        # something in this repository still retains the work under a name the
        # lane-family stem did not match. That is not debris and it is not a
        # landing; it is a row a human should look at.
        return {"reason": OFF_FRONTIER_UNCLASSIFIED, "rung": "reachability",
                "tip": tip, "trunk": trunk,
                "evidence": ("%s is not on %s but %s still reaches it — the "
                             "work is retained under a name outside the lane "
                             "family" % (tip[:12], trunk, ref))}
    return {"reason": OFF_FRONTIER_ABANDONED, "rung": "reachability",
            "tip": tip, "trunk": trunk, "trunk_sha": pin,
            "close_proof_mode": "unreachable",
            "evidence": ("lane family gone; %s is on no ref in %s and is not "
                         "on %s" % (tip[:12], gitdir, trunk))}


def _lane_family_worktrees(gitdir, lane, branch, rooms=None):
    """(matches, err) — every worktree of the row's OWN repo whose branch is
    in the lane family OR whose path basename stem-matches, as row dicts from
    `vcs.parse_worktree_records` (which already refuses a truncated porcelain
    stream). Same tri-state contract as the ref probe.

    `rooms` IS THE `(rows, err)` PAIR `_worktree_records` RETURNS, not a bare
    list, for the reason `lane_branch_presence` states about `table`: a bare
    list cannot tell "this repository has no rooms" from "I could not look",
    and that collapse is how an `unknown` becomes a retirement."""
    needles = _family_stems(lane, branch)
    rows, err = rooms if rooms is not None else _worktree_records(gitdir)
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
    p = _git(gitdir, *_REF_ARGV, ref + "^{commit}")
    sha = p.stdout.strip().lower() if p is not None and p.returncode == 0 else ""
    if not _sha(sha):
        return None, None, "--trunk %s does not resolve to one commit" % raw
    return ref, sha, None




# THE FOUR ANSWERS TO "WHY IS THIS ROW NOT OBSERVABLE", spelled once so the
# producer and the accounting cannot drift. NOT_ASKED is the one that is not a
# failure at all: helm reads no trunk for a row that has no verdict yet,
# because there is nothing that could have landed — see `_will_observe_git`.
OBSERVE_NOT_ASKED = "not-asked"
OBSERVE_NO_REPO = "no-repo-or-tip"
OBSERVE_NO_TRUNK = "no-trunk-or-tip"
OBSERVE_UNDERIVED = "underived"

# WHY A ROW IS NOT OBSERVABLE, IN THE WORDS A READER OF THE BOARD NEEDS. The
# four causes already exist as distinct values and both surfaces collapsed
# them: the terminal printed a bare "landing unobservable" for all four, and
# the browser printed "git could not be read for this repo" for all four —
# which is TRUE of exactly one of them and is a false accusation against the
# repository for the other three. UNDERIVED in particular is not a git
# failure at all: git was never asked, because the reader ran out of the
# budget it declared. One mapping, two renderers, so the sentence cannot
# drift between the surface the owner reads and the one an agent reads.
OBSERVE_REASON = {
    OBSERVE_UNDERIVED: "BUDGET EXPIRED — the reader ran out of its derive "
                       "budget before this row; git was not asked and this "
                       "is not a claim about the work",
    OBSERVE_NO_REPO: "git could not be read for this repo",
    OBSERVE_NO_TRUNK: "no trunk or tip to compare against",
    OBSERVE_NOT_ASKED: "not asked — no verdict yet, so nothing could have landed",
}


def observe_reason(why):
    """The sentence for one `observe_why`, or the honest fallback.

    An UNRECOGNISED value renders as unknown rather than as any of the four,
    because a word this build does not know is exactly the case where naming
    a cause would be inventing one.
    """
    return OBSERVE_REASON.get(why) or "reason unrecorded"


def _git_observe(gitdir, tip, cache):
    """Observe whether the reviewed CHANGE reached local/upstream trunk.

    Patch identity, not object reachability alone, is the fact: normal lands may
    cherry-pick the gated commit and therefore mint a new sha. ``_landed`` keeps
    ancestry as its fast path and falls through to the per-commit patch-id answer.
    A missing repo/tip/object is unobservable, and every relevant trunk leg must
    have a real True/False answer before the combined lifecycle is observable.
    """
    # WHY IT COULD NOT ANSWER IS PART OF THE ANSWER. Three exits share one
    # `observable: False`, and they are not the same fact: no repository to
    # ask, no trunk or tip to compare against, and A LEG THAT CAME BACK None —
    # which is what an expired derive budget produces, because `_landing_proof`
    # answers "unknown" rather than "absent" when it runs out of time. Only
    # the third means "helm asked and could not finish"; the accounting needs
    # to tell it from the others, and from `_UNOBSERVED`'s "never asked".
    blind = {"observable": False, "local": False, "upstream": False,
             "has_upstream": False, "observe_why": OBSERVE_NO_REPO}
    if not gitdir or not tip:
        return blind
    local_ref, up_ref = _trunk_refs(gitdir, cache)
    blind["has_upstream"] = bool(up_ref)
    if not (local_ref or up_ref) or not _commit_exists_cached(gitdir, tip, cache):
        return dict(blind, observe_why=OBSERVE_NO_TRUNK)
    # THE PATCH-ID BATCH IS BOUGHT HERE, at the first row that is really going
    # to ask, and only after that row's tip has been proved through the
    # existence map the buy reads. Below this line the ladder runs four frames
    # deep (`_landed` -> `landed_ever` -> `_landing_proof` ->
    # `_derive_landing_proof` -> `_tip_patch_id`) with no projection cache in
    # any signature, which is exactly why the buy cannot live down there.
    _prime_tip_patch_ids(gitdir, cache)
    # ASK ONCE WHEN BOTH REFS NAME THE SAME COMMIT. `_landed` falls through to
    # `git cherry` whenever ancestry does not settle it, and this asked twice
    # per row — once against refs/heads/main and once against
    # refs/remotes/origin/main. Measured on the live ledger those two resolve
    # to the IDENTICAL sha, zero ahead in either direction. `cherry A tip` and
    # `cherry B tip` cannot disagree when A and B are the same commit, so this
    # is an exact collapse rather than an approximation — and when the refs DO
    # diverge, which is the case this repo simply is not in today, both legs
    # run exactly as before.
    #
    # WHAT THIS STILL BUYS, RESTATED AFTER TRUNK GOT THERE ANOTHER WAY. This
    # was written when the duplicate reached `git cherry` and the claim was an
    # exact halving of 981 spawns. Trunk has since normalised `ref` to a sha
    # INSIDE `_landing_proof`, which collapses the two cherry memo keys on its
    # own — so that headline number is now trunk's, not this lane's, and
    # repeating it here would be claiming a saving twice.
    #
    # The remaining saving is the part trunk's fix does not reach: `_ancestry`
    # still keys its whole-trunk `rev-list` listing on the ref NAME
    # (`("lr.revset", gitdir, ref)`), so two spellings mean TWO full-history
    # walks per gitdir per projection. Collapsing here means one. That is a
    # per-projection constant rather than a per-row cost, and it is written
    # down at its real size instead of the size it had before the rebase.
    #
    # QUERY THE SHA WHOSE EQUALITY WAS PROVED, NEVER THE SYMBOLIC REF. Proving
    # refs/heads/main == refs/remotes/origin/main and then asking `_landed`
    # about refs/heads/main asks about a MOVING name: local can advance between
    # the proof and the query, and copying that answer upstream publishes a
    # LOCAL-ONLY land as authoritative. Measured by a reviewer — both refs at A,
    # local advanced to B containing the reviewed tip after the equality held,
    # origin still A, and the row read local=True/upstream=True while direct
    # origin measurement said False.
    #
    # The resolved sha is immutable, so the equality and the landedness question
    # bind ONE snapshot. This is the same law as gate.trunk_standing: resolve
    # once, pass the resolved value, never re-read the name.
    #
    # AND A FAILED PEEL DOES NOT FALL BACK TO THE NAME — which is what the
    # paragraph above always SAID and what the code did not do. That fallback
    # was this module's version of the defect the pin exists to close: the
    # instant a peel failed, the argv silently became the moving binding again,
    # and — worse — it did so INVISIBLY. Two failures compare EQUAL, so a
    # witness recording "unresolvable" twice reads as a world that did not move
    # while the body underneath was answered about two different trunks. `""`
    # is the honest value: `_landed` answers None for it, which is already this
    # function's UNOBSERVABLE, and unobservable is what an unpinnable trunk
    # means. Cured once at `_lr_recent_lands` and left live here — same shape,
    # same file.
    #
    # THE `same_trunk` COLLAPSE SURVIVES THE CURE UNCHANGED: it already required
    # BOTH shas truthy, so an unresolvable ref makes it False and each leg is
    # asked separately — with "" now, which reads UNOBSERVABLE rather than
    # silently re-asking the moving name.
    local_sha = _resolved_ref(gitdir, local_ref, cache) if local_ref else ""
    up_sha = _resolved_ref(gitdir, up_ref, cache) if up_ref else ""
    # THE ONE PLACE A PASS LEARNS THIS REPOSITORY'S TRUNK, which is where the
    # carry-forward's missing input gets written. `_kept_landing_proof` can
    # return a proof taken against an older trunk whenever that trunk is an
    # ancestor of the one being asked about, but it READS the ancestry hop
    # from the durable pair ledger and never computes it -- and nothing wrote
    # the trunk-to-trunk pairs it needs, so it could not hit and every
    # fast-forward retired the whole warm-up. See `_teach_trunk_pairs`.
    #
    # BOTH SHAS, BECAUSE BOTH ARE ASKED. `_landing_proof` is called below with
    # local_sha and with up_sha, so a teach for only one leaves the other
    # cold; the teach is memoised per (gitdir, sha) and returns immediately
    # when they are the same commit.
    for _trunk_sha in (local_sha, up_sha):
        if _trunk_sha:
            _teach_trunk_pairs(gitdir, _trunk_sha)
    same_trunk = bool(local_sha and up_sha and local_sha == up_sha)
    local = _landed(gitdir, tip, local_sha) if local_ref else False
    if same_trunk:
        upstream = local
    else:
        upstream = _landed(gitdir, tip, up_sha) if up_ref else False
    if local is None or upstream is None:
        return dict(blind, observe_why=OBSERVE_UNDERIVED)
    return {"observable": True, "local": local, "upstream": upstream,
            "has_upstream": bool(up_ref), "observe_why": None}


# What a row helm did not read Git for knows about trunk: nothing, and it says
# so in every field. ONE definition, because `_lr` used to carry a second copy
# inline — and a hand-built "blind" dict that omits the receipt keys is exactly
# how those rows came to answer "receipt: none" by DEFAULT rather than by
# lookup.
_UNOBSERVED = {"observable": False, "local": False, "upstream": False,
               "has_upstream": False, "observe_why": OBSERVE_NOT_ASKED}


def _base_behind(gitdir, tip, cache):
    """How many landing-target commits `tip`'s history LACKS, or None.

    `rev-list --count tip..trunk` is the commits reachable from trunk and not
    from the tip — every one of them landed after this lane's base, which is
    exactly what a gate receipt minted at that base never saw. It is 0 for a
    lane cut from trunk's tip, small for a same-night lane however unreachable
    its own head is (the trap the rejected reachability rung fell into), and
    measured 725..1372 for the live zombies task/266 is about (2026-08-06).

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
    # THE PINNED COMMIT, and the memo key is the pin too. A count keyed on the
    # ref NAME is a count of "how far behind whatever trunk was when this row
    # asked" — two rows in one projection could be measured against different
    # trunks and both cached under the same key. The resolved sha makes the
    # question and its memo key name the same immutable commit.
    #
    # AN UNPINNABLE TRUNK IS UNMEASURED, NOT "measure it against the name". The
    # `or ref` here put the moving binding into BOTH the argv and the memo key,
    # and the memo key is what makes it worse than the other sites: the
    # `base_behind` cache slot is declared DERIVED in the read-set
    # (`_LR_DERIVED_CACHE_KEYS`) and therefore recorded nowhere, on the stated
    # grounds that its operands are "two immutable object names and a commit
    # this record already carries under refsha". A ref name in that key makes
    # that justification FALSE — the exemption is only sound while the pin
    # holds. None is this function's own declared failure value and `base_state`
    # already renders it UNKNOWN.
    ref = _resolved_ref(gitdir, ref, cache)
    if not ref:
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
    # THE SAME PINNED TRUNK THE LANDEDNESS LEGS USED. This row's receipt anchor
    # and its landedness must be measured against ONE trunk instant, or a row
    # can read LANDED against trunk B while its anchor is judged CONTRADICTED
    # against trunk A — two verdicts about one row from two reads of one name.
    #
    # SO AN UNPINNABLE TRUNK DECLINES TO JUDGE THE ANCHOR AT ALL. This site is
    # the sharpest of the family because its output is a VERDICT about the
    # receipt: falling back to the name let R_CONTRADICTED — "the locally
    # claimed trunk_sha left trunk's history" — be minted from a read of a
    # moving binding, which is an accusation against a receipt on evidence that
    # cannot be re-found. `git=False` already has the exact wording for "the
    # anchor was not checked"; an unresolvable trunk is that same state reached
    # by a different route, and it says so rather than borrowing the checked
    # branch's silence.
    local_sha = _resolved_ref(gitdir, local_ref, cache) if local_ref else ""
    # A TRUNK THAT EXISTS AND WILL NOT PEEL IS ITS OWN FACT, and it is the only
    # one this branch may claim. A row with NO trunk ref at all — the repo-less
    # legacy loop — reached the generic terminal state below long before this
    # cure and still must: `_ancestry` answers UNDETERMINED for an empty ref, so
    # passing the empty sha is already the whole fix for the fallback. Saying
    # "trunk could not be pinned" there would be inventing a failed peel that
    # never happened, and it silently rewrote that row's diagnostic.
    if local_ref and not local_sha:
        return dict(git_facts, receipt=False, receipt_state=R_LOCAL,
                    receipt_reason="local payload self-consistency only; trunk "
                                   "could not be pinned to a commit, so the "
                                   "receipt's anchor was NOT checked")
    anchor = _ancestry(gitdir, rec["trunk_sha"], local_sha)
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
    the REFUSED one's stamp. A probe stamped it 1 Jan 2001 and the card printed
    a verdict from 2001 with a 25-year dwell and nothing amiss beside it.

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
    fields, ZERO hold confirmation_id alone. I tried to test this rung by
    planting the state directly and the fixture could not construct it; the
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
    # ADMINISTRATIVE RETIREMENT — terminal, and the ONE terminal that claims
    # nothing about the work (see the `helm lr retire` section). It is folded
    # into `retired` below like every other annotation, which is what moves
    # the row out of the stalled / contrary / unmeasurable counts in one step
    # rather than four.
    admin_retired = bool(row.get("retired_admin"))
    # A RETRACTED VERDICT (task/3060) is terminal the same way: its authority
    # was withdrawn by a later event, the polarity replays UNDECLARED, and the
    # row ends here rather than reading REVIEWED and owed by nobody-undeclared.
    retracted = bool(row.get("verdict_retracted"))
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
    # AND NEVER RUN GIT IN A REPOSITORY THIS BOARD DOES NOT OWN. `_observe`
    # spawns git in the gitdir the ROW names, so a foreign row made helm read —
    # and on one path CONTACT THE NETWORK — inside somebody else's repository.
    # Traced when it was found: this call reached `_git_observe` -> `_landed`
    # -> `_landing_proof`, and a tip that could not be resolved LOCALLY fell
    # through to `_vanished_proof`, which ran `ls-remote origin` against that
    # repository's remote with whatever credentials are ambient here. The rare
    # path and the foreign path were THE SAME PATH, because a foreign tip is
    # precisely the tip this checkout does not have. The ladder no longer
    # takes that leg; the ownership rule below never depended on it, because
    # every spawn inside another repository is the same authority claim.
    #
    # The predicate is `_observation_owned` — the STRICT authority twin, NOT
    # the visibility one (a review's FIX on this lane: `_this_boards_row`
    # keeps unresolved rows VISIBLE, and visibility must never authorize a
    # spawn inside a repository this board cannot positively call its own).
    # Foreign and unresolved rows keep their RECORDED state, which is all
    # this board could honestly say about them anyway — it has no standing
    # to derive anything live about another repository's trunk.
    obs = _observe(row.get("repo_id"), tip, cache, receipts,
                   git=_will_observe_git(row))
    observable = obs["observable"]
    observe_why = obs.get("observe_why")
    landed = (obs["upstream"] if obs["has_upstream"] else obs["local"]) \
        if observable else False
    merged_local = observable and obs["local"] and not landed
    live_contrary = closed and polarity in ("fix", "supersede") \
        and (landed or merged_local)
    discharged = bool(row.get("discharged"))
    withdrawn = bool(row.get("withdrawn"))
    annotation = terminal_annotation(row)
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
    #
    # THE FALSIFIED PREMISE IS THE WITHDRAWAL'S OWN, NOT THE VERDICT'S, and
    # that distinction is what keeps this correct now that withdrawn admits
    # every declared polarity. A withdrawal asserted THE WORK IS ABSENT; a
    # later landing falsifies THAT assertion whatever sign the verdict
    # carried, so the obligation re-opens for an APPROVE and a CONCUR exactly
    # as it does for a FIX.
    #
    # IT DELIBERATELY DOES NOT ROUTE THROUGH `live_contrary`, and widening
    # that tuple instead would be the obvious wrong fix: `live_contrary` means
    # a row landed DESPITE a do-not-land verdict, and an APPROVE landing is
    # the normal desired outcome rather than a violation. Adding approve there
    # would badge every landed approved row as contrary — the false-positive
    # flood the consumed-evidence link above already had to dig this board out
    # of. Contradicting an absence-based withdrawal is NOT retroactively a
    # do-not-land verdict: what re-opens is the OBLIGATION, never the
    # verdict's meaning, so `contrary` below still reads from `live_contrary`.
    withdrawal_falsified = closed and (landed or merged_local)
    withdraw_contradicted = withdrawn and withdrawal_falsified
    # The SAME falsifiability clause generalizes to close --reason withdrawn:
    # its git observation stays live (see the _observe gate above), so a later
    # land re-exposes the row as CONTRARY, non-terminal, owed by the
    # integrator, flagged close_contradicted. The other three close reasons
    # are monotonic — their write-time ladders proved the terminal once.
    close_contradicted = close_reason == "withdrawn" and withdrawal_falsified
    # CONSUMED AS EVIDENCE — the door's own exhaust, and the reason the board
    # could not reach zero. A confirmation round is dispatched --supersedes
    # <original>; the reviewer files SUPERSEDE/FIX on it; that verdict closes
    # the ORIGINAL through subsumed/resolved — and the CONFIRMATION ROW itself
    # is never closed by anything. Its reviewed tip is on trunk (it was written
    # ON the landed successor, which is the whole point of it) and its polarity
    # is non-approve, which is the contrary signature exactly. So EVERY USE OF
    # THE DOOR MINTED ONE PERMANENT DEBT. A probe measured it:
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
    retired = abandoned or closed_by_landing or admin_retired or retracted \
        or close_reason is not None and not close_contradicted \
        or (discharged or withdrawn) and not withdraw_contradicted \
        or evidence_consumed
    contrary = live_contrary or discharged \
        or row.get("close_contrary_state") == "landed"
    active_contrary = live_contrary and not (
        discharged or (withdrawn and not withdraw_contradicted)
        or close_reason is not None and not close_contradicted
        or evidence_consumed or admin_retired or retracted)
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
    # MECHANICAL ENFORCEMENT AT THE LAND PATH — the 0.2 council's item 3, and
    # the half a verdict-side UNVERIFIED did not deliver. A probe reproduced it:
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
    if admin_retired:
        # DATED BY THE RETIREMENT, not by the stage it was stuck in. The row
        # entered RETIRED at the instant the event was appended; carrying the
        # old stage instant here would keep a 27-day dwell growing under a
        # terminal that ended it.
        state, entered = "RETIRED", row.get("retire_ts")
    elif retracted:
        # DATED BY THE RETRACTION, for the retirement's reason one arm up.
        state, entered = "RETRACTED", row.get("retract_ts")
    elif delivered_report:
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
        #
        # ...EXCEPT WHERE A TERMINAL REASON HAS ALREADY DECIDED THE WORD.
        # `rowstate._CLOSE_TERMINAL` is the canonical
        # map from close reason to rendered state, and this branch ignored it
        # for FIX/SUPERSEDE rows — so a chain-proof close rendered
        # CHANGES_REQUESTED here and SUPERSEDED there. Two projections of one
        # row disagreeing about whether anyone still owes work is worse than
        # either answer alone: the burn-down counts one and the operator reads
        # the other. Derived from the register, not restated beside it.
        from . import rowstate
        terminal = rowstate._CLOSE_TERMINAL.get(close_reason) \
            if close_reason in _TERMINAL_OVERRIDES_VERDICT else None
        state, entered = (terminal or verdict_state), post_verdict
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
    # meaning is "waiting to be merged", and the one the task/266 zombies wore
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
    # recently" window while it was still warm (a probe reproduced exactly that).
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
    # impossible date and acted on it. I had left this, reasoning that 2099 is
    # absurd rather than plausible; a review's point is that the row is still
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
    # I fixed this for a RETIRED row (an absent, unreadable or impossible
    # retirement stamp) and left the larger case standing, reasoning that a
    # terminal row never stalls so a growing dwell misleads without alarming.
    # A review rules it load-bearing under this card's own bar and I agree: a
    # git-OBSERVED landing is terminal with no closure stamp ANYWHERE, so it
    # read dwell_known TRUE and grew 3600 -> 7200 across two projections of a
    # row that had already closed. Quiet is exactly the failure mode this card
    # exists to remove. Git proves the interval ENDED; it supplies no END
    # INSTANT, and a confident number over that is the same shape as the other
    # four. One owner for every terminal row now, rather than the retired
    # subset I happened to be looking at.
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
    # WHICH BRANCH ABOVE ANSWERED — because two of them emit the SAME STRING
    # from entirely different evidence and no reader could tell them apart.
    #
    # "landed" is returned both by a LIVE git observation and by a string
    # recorded at close time. Measured over 2223 rows: 418 contrary rows carry
    # it from an observation and 135 from the stored `close_contrary_state`,
    # byte-identical. All 135 of the recorded ones have `observable` False —
    # git could not be consulted for them at all, so the string is not a stale
    # copy of something checkable, it is the ONLY evidence those rows have.
    #
    # THE FALLBACK IS CORRECT AND STAYS. Dropping the fact would lose real
    # history; what was missing is a way to know which fact you are reading.
    #
    # THIS IS A SIBLING FIELD RATHER THAN A NEW `contrary_state` VALUE, and
    # that is a constraint rather than a preference: `contrary_state` is an
    # ENFORCEMENT input, not merely display. dispatches.py refuses a discharge
    # whose state is `not in ("landed", "merged-local")`, matches
    # `discharge_contrary` against it exactly, and the owner console plus 36
    # references in tests/test_web_lr.py read the same literals. A new value
    # would turn a labelling fix into a refusal regression.
    #
    # IT DESCRIBES THE VALUE, NOT THE BEST AVAILABLE EVIDENCE. A row carrying
    # BOTH a recorded string and a live landing reads "recorded", because the
    # recorded branch is the one that produced `contrary_state`. Mirroring the
    # precedence is the whole contract: a provenance that disagreed with the
    # branch it describes would be a second lie beside the first.
    #
    # TWO VALUES, NOT THREE, AND THE THIRD WAS A CATEGORY ERROR (r1). This
    # field answers HOW THE FACT IS KNOWN. The legacy `discharge`
    # path and the modern close path BOTH replay a string persisted at the time
    # someone else consulted git, so both are `recorded` — they differ in
    # LIFECYCLE, not in evidence. A `discharged` value would have been a
    # lifecycle label smuggled into an evidence field, and it was redundant on
    # top: the `discharged` FIELD already names that path, so a reader wanting
    # to distinguish them has always been able to. Collapsing them keeps this
    # field orthogonal to lifecycle, which is what makes it composable with the
    # `discharged`/`close_reason` axis instead of partially duplicating it.
    # THE DISCHARGE BRANCH EARNS "recorded" ONLY WHEN IT ACTUALLY PRODUCED A
    # STATE (a FIX verdict). `discharged` belongs in this mapping and my
    # first cure was wrong to remove it: `contrary_state` above reads
    # `discharge_contrary` on that branch, which is a PERSISTED STRING, so it
    # maps to "recorded" exactly like `close_contrary_state` does. The
    # source-mirroring arm pins that invariant deliberately and it caught the
    # removal.
    #
    # THE REAL DEFECT WAS NARROWER: a discharged row whose `discharge_contrary`
    # is ABSENT falls all the way through `contrary_state` to None, so the fact
    # renders "STATE UNREAD" while the provenance still claimed "recorded" —
    # one sentence saying a state was recorded and that no state can be read.
    # Requiring the branch to have produced a value keeps the mapping and
    # removes the false claim: with nothing recorded and nothing observed the
    # provenance is absent, and the bare fact stands alone.
    contrary_provenance = "recorded" \
        if (discharged and row.get("discharge_contrary")) \
        or row.get("close_contrary_state") == "landed" \
        else "observed" if landed or merged_local else None
    # A SOURCE-CLEAN HOLD IS A STRUCTURED CLAIM THAT THE REVIEW IS OVER, so
    # the stage no longer answers this question. `OWED_BY[AWAITING_REVIEW]`
    # says "reviewer" and that was true until the reviewer answered; the hold
    # is how they answered. Two surfaces gave OPPOSITE answers about one row
    # for as long as this was missing -- `dispatch triage` printed HELD naming
    # the integrator's land gate while `lr show` printed AWAITING_REVIEW.
    owed_by = "nobody" if retired else "integrator" if active_contrary \
        else "integrator" if row.get("source_clean_tip") \
        else "nobody (undeclared)" if state == "REVIEWED" and polarity is None \
        else "unknown (declared verdict held)" if state == "REVIEWED" \
        else OWED_BY.get(state, "unknown")
    out = {"id": rid, "state": state, "observable": observable,
           "observe_why": observe_why,
            "polarity": polarity, "owed_by": owed_by,
            "author": row.get("sender"), "reviewer": row.get("recipient"),
            # THE MODEL THE REVIEWER ANSWERED ON, frozen in the verdict rather
            # than read off the seat now: a reviewer relaunched onto another
            # rung must not retro-label the review it already wrote.
            "reviewer_model": dispatches.verdict_resolved_model(row),
            "kind": row.get("kind"),
            "lane": row.get("lane"), "branch": row.get("ref"),
            "chain_root": row.get("chain_root"),
            "supersedes": row.get("supersedes"),
            # THE ROW'S OWN PINNED TIP, under ONE name whatever the kind.
            # `base_sha` and `review_sha` already carry it, but each is None
            # for the other kind, so every consumer that wants "the sha this
            # row is about" has to branch on `kind` and they drift. The
            # containment annotator below needs exactly this and must not be
            # the second place that branches.
            "pinned_tip": tip,
            "base_sha": tip if row.get("kind") == "build" else None,
            "review_sha": row.get("landing_review_tip") if build_landed else
            None if row.get("kind") == "build" else tip,
            "repo_id": row.get("repo_id"),
            "verdict_ref": row.get("verdict_ref"),
            # THE SECOND AUTHOR. A FIX verdict may name the cure the REVIEWER
            # committed off the reviewed tip; the lane then carries two authors
            # and every surface that renders the row owes both names.
            "patch_tip": row.get("patch_tip"),
            "patch_author": row.get("patch_author"),
            # WHICH EXIT ANSWER THE VERDICT GAVE. A FIX answering
            # WORSE-THAN-MAIN blocks the paths it names; a FIX answering
            # IMPERFECT names nothing that regresses and exists only to carry
            # the reader's cure, so what it asks for is the author's agreement
            # on that patch. Both are polarity FIX, so the polarity alone
            # cannot tell a surface which sentence to print.
            "exit_answer": row.get("exit_answer"),
            "no_patch_because": row.get("no_patch_because"),
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
            # WHERE THE CLOSE SAT IN THE LEDGER, because the STAMP cannot
            # order two closes inside one second and the row order is the
            # order rows OPENED. `dispatches._close_position` carries the
            # fold's own append index onto the closed state; relaying it here
            # is what lets a newest-first reader break that tie on the record
            # instead of on a guess. None where the projection had no index.
            "close_seq": row.get("close_seq"),
            # The transitions the record holds and this projection REFUSED.
            # Empty on every healthy row; naming them is what keeps a refusal
            # from being a silent disagreement between the state and the
            # timeline drawn beneath it.
            "ledger_refused": refused,
            "deadline_s": row.get("deadline_s"),
            "stall_threshold_s": threshold,
            # A ROW WAITING ON THE OWNER IS NOT A MACHINE STALL, and the
            # difference is the whole reason this field exists. `owner_gated`
            # is set by `dispatch hold --owner-gated` — a structured claim,
            # never a reading of the hold prose — and it says the dependency
            # is a decision only he can make. Measured 2026-08-09: three of
            # seven in-flight rows on his console read "STALLED past its stage
            # threshold — owed by builder / owed by the integrator" while all
            # three were waiting on HIS authorization, so the board reported
            # the fleet failing at exactly the moment the fleet was blocked on
            # him. Excluding them here is not hiding the row: it still renders,
            # still carries its dwell, and now names the right holder.
            "owner_gated": bool(row.get("owner_gated")),
            # THE THIRD ANSWER ON THE SAME AXIS, and it was added to the
            # WRITER and to none of the readers. `owner_gated` is read in
            # eight modules and six times in this one; `source_clean_tip`
            # was read in exactly one -- `dispatches`, which writes it. A
            # hold axis that grew a value its consumers never learned is
            # the new-enum-value shape: every `else` keeps answering for the
            # two it knows.
            #
            # WHAT IT MEANS HERE: the reviewer read the delta and found
            # nothing, and cannot mint an approve because an approve binds a
            # whole-suite token only the integrator's land gate produces. So
            # the review is DONE and the next move is the integrator's.
            "source_clean_tip": row.get("source_clean_tip"),
            # The instant this specific hold began. Stage entry and owner debt
            # are different clocks: a ten-day review held one minute ago owes
            # the owner one minute, not ten days. The card carries the stamp;
            # scheduler validates it against its one response clock.
            "hold_ts": row.get("hold_ts"),
            # The reason rides WITH the flag. A surface that knows a row is
            # owner-gated and cannot say WHY sends him to a second tool to
            # find his own ask, which is the same defect one layer along.
            "hold_reason": row.get("hold_reason"),
            # A ROW WHOSE REVIEW IS FINISHED IS NOT A STALLED REVIEW. The
            # dwell above measures time in AWAITING_REVIEW, and for a
            # source-clean row that clock has been running since before the
            # reviewer answered -- so billing it here names the one seat that
            # has already done its part. THE FAILURE MODE: a board full of
            # rows reading AWAITING_REVIEW (STALLED) for hours, every one of
            # them held source-clean with the review finished, and anybody
            # sweeping for stalls reading a queue of slow reviewers.
            # It is excluded for the same reason `owner_gated` is, and it is
            # NOT hidden: the row still renders, still carries its dwell, and
            # `owed_by` below now names who actually owes it. The integrator's
            # own debt runs from `hold_ts`, which this card already carries,
            # and it is a different clock from stage entry.
            "stalled": False if retired or row.get("owner_gated")
            or row.get("source_clean_tip") else bool(
                threshold and dwell >= threshold and assertable),
            "terminal": terminal,
            "land_state": "NOT_CLAIMED" if delivered_report else
            "UNKNOWN" if abandoned or not observable else
            "LANDED" if landed else "MERGED_LOCAL" if merged_local else "ABSENT",
            "landed": landed, "merged_local": merged_local,
            # None on every non-READY row and on any READY row whose drift
            # could not be measured — UNMEASURED, never zero (task/266).
            "base_behind": behind,
            "contrary": contrary, "contrary_state": contrary_state,
            "contrary_provenance": contrary_provenance,
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
            # THE RETIREMENT, WHOLE. A surface that can say a row is retired
            # and cannot say WHY, on what measurement, by whom, teaches a
            # reader nothing and invites them to re-open it by hand.
            "retired_admin": admin_retired,
            "retire_reason": row.get("retire_reason"),
            "retire_measurement": row.get("retire_measurement"),
            "retire_seat": row.get("retire_seat"),
            "retire_note": row.get("retire_note"),
            "retire_ts": row.get("retire_ts"),
            # THE RETRACTION, WHOLE, for the retirement's reason above: what
            # was withdrawn, by whose door, what it now reads, how the hand
            # knows, and which row carries the review now (task/3060).
            "verdict_retracted": retracted,
            "retracted_polarity": row.get("retracted_polarity"),
            "retract_reason": row.get("retract_reason"),
            "retract_reads": row.get("retract_reads"),
            "retract_basis": row.get("retract_basis"),
            "retract_seat": row.get("retract_seat"),
            "retract_role": row.get("retract_role"),
            "retract_successor": row.get("retract_successor"),
            "retract_ts": row.get("retract_ts"),
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
            # THE CONTENT RUNG'S PROOF, carried through the projection for the
            # same reason it is admitted by the close schema: a witness that
            # survives the WRITE and is dropped by the READ is just as
            # unverifiable, and this is the THIRD filter between minting a
            # proof and a caller seeing it (writer kwargs -> close schema ->
            # here). Each one is a silent editor; only an arm that re-reads
            # the projected row can see them.
            "content_witness": row.get("content_witness"),
            "content_witness_anchor": row.get("content_witness_anchor"),
            # THE CHAIN PROOF TUPLE, carried for exactly the reason the witness
            # above is: the edges and endpoints survive the WRITE and were
            # DROPPED BY THE READ, so `helm lr show` rendered a chain-proof
            # close that could not show which supersedes links carried it — and
            # the ladder's own retry check could not see them either, which is
            # why it compared repo+evidence and silently ignored the trunk.
            "carried_base": row.get("carried_base"),
            "carried_tip": row.get("carried_tip"),
            "chain_path": row.get("chain_path"),
            "chain_fork_census": row.get("chain_fork_census"),
            "chain_census_cutoff": row.get("chain_census_cutoff"),
            "chain_tier_state": row.get("chain_tier_state"),
            "chain_attestation": row.get("chain_attestation"),
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
            # WHOSE WITHDRAWAL, carried so a surface can say it (task/2619).
            # The writer has stamped both of these for a while — the ladder
            # records `withdrawing_seat` and the close lock records
            # `close_actor` on every reason — and the projection carried
            # NEITHER, so every renderer downstream of it could print the
            # absence proof and the closer's own sentence while being unable
            # to name the hand. A reviewer reading a FIX row that ended
            # `withdrawn` then has no way to tell the author's agreement from
            # a third party retiring the row, which is the reading that makes
            # a correctly-accepted verdict look ignored.
            "withdrawing_seat": row.get("withdrawing_seat"),
            "close_actor": row.get("close_actor"),
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
            # WHICH KIND OF UNKNOWN, CARRIED ONTO THE ROW. `tier` alone said
            # "unknown" for states whose cures are patience, a repair, an
            # edit to this row, and nothing-you-can-do — and the compose
            # refusal, the one surface that words it, had to guess. It guessed
            # transient for all of them. `tier_state` is a str subclass
            # carrying the classification; the carrier does not survive JSON,
            # so the word is materialised HERE, where the row is built, rather
            # than left for each consumer to re-derive. None whenever `tier`
            # is not "unknown" — an ok/none/outside row has no kind, and a
            # kind printed beside a definitive answer would read as doubt.
            "tier_kind": dispatches.tier_unknown_kind(tier_state),
            "timeline": _timeline(state, open_ts, delivered_ts, verdict_ts,
                                  verdict_state, now, annotation),
            # DELIVERY IS AN EVENT, NOT A TIMESTAMP. Both of these used to be
            # truthiness on the stamp, so a delivery the ledger recorded
            # without a ts reported `notified: false` — the record's own
            # delivery event denied because helm could not date it.
            "notify_failed": _notify_failed(events)
            if delivered_ts is None else None,
            "notified": delivered_ts is not None}
    if row.get("close_proof_version") == 3:
        out.update({key: row.get(key) for key in (
            "close_proof_version", "compose_land_proof", "compose_land_anchor")})
    # A LAND LOOP WHOSE ROW THIS HELM CANNOT READ IN FULL CARRIES THE KINDS it
    # does not know, and ONLY such a loop carries the key, so every other
    # projection is byte for byte what it was. `lr show` prints it.
    if dispatches.unknown_event_kinds(row):
        out[dispatches.UNKNOWN_KINDS_FIELD] = dispatches.unknown_event_kinds(row)
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
# A SEPARATE PURSE FOR THE TRUNK-PAIR TEACH, deliberately not ANCESTRY_BUDGET
# itself. Succession spends that budget on the rows an alarm surface is asking
# about; a teach that shared it would silently take capacity from the question
# a reader is waiting on to answer one nobody asked yet. Same size, own spend.
TRUNK_PAIR_BUDGET = 120
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
    against the projection's per-row-spawn law (#99): the law forbids an
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
        except projscope.Expired:
            raise
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


# A MEASURED CONTENT HASH IS FULL LENGTH. `_HEXID` admits 8-64 characters
# because it also reads ABBREVIATED ids a human may have typed, and a short
# id in this store would be a key nothing can ever match — while still
# counting toward a cover that calls itself complete and therefore
# licenses ABSENT. Only what git actually emits is admitted here.
_MEASURED_ID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
TRUNK_PATCH_INDEX = "trunk-patch-index.json"   # see _trunk_index_extend
TRUNK_PID_BACKFILL = 150                  # frontier commits hashed per pass
_TRUNK_INDEX_MEMO = {}


def trunk_patch_index_path():
    return home.global_dir() + "/" + TRUNK_PATCH_INDEX


def _shas(value):
    """A list of full object ids, or None. Anything else is not a list of
    object ids and must not be repaired into one."""
    if not isinstance(value, list):
        return None
    out = []
    for item in value:
        if not _sha(item):
            return None
        out.append(item)
    return out


def _trunk_index_read():
    """{gitdir: {"newest": sha, "frontier": [sha], "seen": [sha],
                 "ids": {patch-id: sha}}}.

    THE WHOLE RECORD IS VALIDATED BEFORE ANY ASSERTION IN IT IS BELIEVED, and
    a record that does not validate is DROPPED rather than repaired. A
    half-understood window claims coverage it cannot back, and the only cost
    of dropping one is that its repository re-walks. The shapes this refuses
    are not hypothetical: a bare list or null where a dict belongs crashes a
    `.get`, an unhashable id poisons the map, and the string "false" is TRUE
    to `bool` -- each one turning a corrupt file into a confident answer.

    Memoised on the file's own (size, mtime); unreadable reads as EMPTY."""
    path = trunk_patch_index_path()
    try:
        st = os.stat(path)
        key = (path, st.st_size, st.st_mtime_ns)
    except OSError:
        return {}
    if _TRUNK_INDEX_MEMO.get("key") == key:
        return _TRUNK_INDEX_MEMO["store"]
    store = {}
    try:
        raw = pk.read_json(path, {}) or {}
    except Exception:                          # noqa: BLE001 — cold, not wrong
        return {}
    if isinstance(raw, dict):
        for gitdir, rec in raw.items():
            if not (isinstance(gitdir, str) and gitdir
                    and isinstance(rec, dict)):
                continue
            newest = rec.get("newest")
            frontier = _shas(rec.get("frontier"))
            seen = _shas(rec.get("seen"))
            ids = rec.get("ids")
            if not (_sha(newest) and frontier is not None
                    and seen is not None and isinstance(ids, dict)):
                continue
            if not all(isinstance(k, str) and _MEASURED_ID.fullmatch(k)
                       and _sha(v) for k, v in ids.items()):
                continue
            store[gitdir] = {"newest": newest, "frontier": frontier,
                             "seen": seen, "ids": dict(ids)}
    _TRUNK_INDEX_MEMO.clear()
    _TRUNK_INDEX_MEMO.update(key=key, store=store)
    return store


def _commit_parents(gitdir, sha):
    """The commit's OWN parent headers, or None when git did not answer.

    `cat-file commit` shows the object; `rev-list --parents` shows a
    TRAVERSAL, and a shallow graft edits what that traversal will walk. So a
    shallow boundary reads parentless to rev-list while its own header still
    carries its parent line, and only one of those two is a fact about the
    commit. Parsing stops at the blank line that ends the header block, so a
    message body cannot forge a parent.
    """
    # THE OBJECT'S OWN VIEW, through the same isolation the parentless
    # resolver uses: replace refs and grafts both REWRITE ancestry, so a plain
    # read can be handed a replacement header naming no parent while the
    # object underneath names one. Asking with the rewriters off is what makes
    # this an answer about the commit rather than about the repository's
    # current opinion of it.
    got = _object_view(gitdir, "cat-file", "commit", sha)
    if got is None or got.returncode != 0:
        return None
    body = got.stdout or ""
    # A COMPLETE HEADER BLOCK OR NOTHING. The absence of parent-looking text
    # in a PREFIX is not the absence of a parent: a truncated read, a short
    # write or a body that never arrived all look parentless. So the evidence
    # required is the block's own shape — an identity-bound first line naming
    # the tree, and the blank delimiter that ENDS the headers. Without both,
    # this commit stays unsettled and is retried; parsing stops at the
    # delimiter so a message body cannot forge a parent.
    header, sep, _rest = body.partition("\n\n")
    if not sep:
        return None
    lines = header.splitlines()
    # THE WHOLE HEADER IS VALIDATED, not merely scanned for what we want. A
    # first line that merely BEGINS with "tree " proves nothing — the id after
    # it must be a real object id, or this block is not a commit header we
    # understand and its silence about parents means nothing either. A bare
    # `parent` with no id is the same class: evidence that refuses to parse is
    # not evidence of absence, so the whole read is handed back UNKNOWN and
    # the commit is retried rather than settled.
    if not lines:
        return None
    kind, _space, ident = lines[0].partition(" ")
    if kind != "tree" or not _sha(ident.strip()):
        return None
    parents = []
    for line in lines:
        if line == "parent" or line.startswith("parent "):
            candidate = line.partition(" ")[2].strip()
            if not _sha(candidate):
                return None
            parents.append(candidate)
    return parents


def _trunk_index_extend(gitdir, trunk):
    """(patch-id -> trunk sha, incomplete) — a DURABLE cover, extended per pass.

    A LEDGER, NOT A CACHE: a commit's patch id is a property of that immutable
    object, so every (sha, patch-id) pair is true forever and there is no
    invalidation to get wrong. What changes is only how much of the trunk the
    cover REACHES, and that is carried explicitly rather than assumed.

    THE COVER IS A SET WITH A FRONTIER, NOT A RANGE WITH TWO ENDS. A single
    oldest sha cannot describe a DAG: stop the walk inside a merge and the
    next pass resumes down ONE parent, declares itself finished, and a patch
    that only ever existed on the other parent then MISSES a set that calls
    itself complete -- an absent verdict manufactured out of a bookkeeping
    shortcut. `seen` is what has been hashed; `frontier` is what is reachable
    and not yet hashed; and COMPLETE MEANS THE FRONTIER IS EMPTY. There is no
    root test to latch, and a shallow boundary cannot masquerade as one.

    THE COVER BELONGS TO ONE TRUNK. `rev-list A..B` is empty exactly when B is
    an ancestor of A, so a front walk that adds nothing and does not ARRIVE
    means the requested trunk is behind this cover or divergent from it, and
    the cover then holds ids for commits that trunk's history does not
    contain. Answering from it made a row on LOCAL trunk and not upstream read
    LANDED instead of MERGED_LOCAL. Such a call gets NO index and pays one
    `git cherry` instead.

    NO RIVAL WINDOW IS EVER UNIONED. The write is compare-and-set on the
    cover's own identity: a pass whose stored record moved underneath it
    DISCARDS its extension rather than merging ends or ids, because a union
    with a divergent cover imports foreign ids into this trunk's answer. This
    is NOT atomic -- `pk.write_json` is an atomic replace with no lock, so the
    compare narrows the race and does not close it -- and that is exactly why
    the losing side throws work away instead of blending: the worst outcome
    stays "slower", never "wrong".
    """
    if not gitdir:
        return (None, True)
    # A REF NAME IS RESOLVED, NOT REFUSED, and this replaced a bare `_sha`
    # test that turned every name into (None, True). MEASURED on a
    # two-commit repo: `main` and `refs/heads/main` both returned no index
    # while the builder this replaced answered with a full one.
    #
    # WHAT A REFUSED COVER COSTS, AND IT IS NOT ONE SENTENCE. Two flat
    # readings are both wrong: "it renders UNKNOWN" and "it only costs speed".
    # `_derive_landing_proof` guards the index block with `if index:`, so a
    # (None, True) cover falls past it and the consumer TRIES `git cherry`
    # instead. What follows depends on what that attempt can do:
    #
    #   a USABLE cherry answer settles the row exactly as the index would
    #     have — measured on one subject that reached trunk by cherry-pick,
    #     ancestry NOT_ANCESTOR so the patch rung is the one talking: index
    #     AVAILABLE -> "patch-equivalent", index REFUSED -> "patch-equivalent";
    #   an UNAVAILABLE or UNANSWERABLE fallback yields UNKNOWN — `_git`
    #     returning None, a non-zero exit, and rc 0 whose output this reader
    #     cannot use are all the same insufficiency;
    #   an AMBIENT `Expired` UNWINDS rather than answering at all. It is not
    #     confined to one moment in the call: the budget is checked before a
    #     spawn AND a timeout can expire around it, so an Expired may arrive
    #     with the work not done or with it done and unreported. What is
    #     durable is the OUTCOME, not the instant — an unwind is never a
    #     verdict, and no caller may read one as an absence.
    #
    # AND THERE IS NO UNIVERSAL SPAWN COUNT. The fallback goes through the
    # same argv memo, so inside one projection a repeated question costs
    # nothing further, and a budget already refused costs no spawn at all. The index is the cheaper instrument AND the more
    # available one; which of those two costs a given row pays is a property
    # of that row's projection, not of this refusal.
    #
    # THE RESOLUTION IS ONE-INSTANT AND DOES NOT RETRY. `projscope.memo`
    # keys on ARGV and caches FAILURES as readily as successes, so a peel
    # that fails once inside a projection is the answer every later caller
    # issuing that same argv receives: THIS BUILDER DOES NOT RECOVER A
    # TRANSIENT PEEL FAILURE WITHIN A SCOPE, and must not. A projection is
    # one moment, and a question asked twice in one moment may not come back
    # with two different answers. Nothing here is a retry and nothing here
    # should be read as one; the scope boundary is what clears it.
    #
    # THE COVER IS STILL KEYED ON A COMMIT. Resolving here means the walk and
    # the divergence test both work on an object id, so the one-trunk
    # invariant above is untouched: a name is an ADDRESS for a commit and
    # this turns it into one, rather than admitting names into the cover.
    # An unresolvable name is still (None, True), because a trunk git cannot
    # name is a trunk nothing can be proved against.
    if not _sha(trunk):
        resolved = _git(gitdir, *_REF_ARGV,
                        str(trunk or "") + "^{commit}")
        if resolved is None or resolved.returncode != 0:
            return (None, True)
        trunk = (resolved.stdout or "").strip()
        if not _sha(trunk):
            return (None, True)
    store = _trunk_index_read()
    rec = store.get(gitdir) or {}
    ids = dict(rec.get("ids") or {})
    seen = set(rec.get("seen") or ())
    frontier = list(rec.get("frontier") or ())
    newest = rec.get("newest")
    learned = 0
    stopped = False

    def _cover(sha):
        """Hash one commit into the cover and push its parents. True when the
        commit is now COVERED; False when git did not answer, which leaves it
        exactly where it was so the next pass retries it."""
        parents = _commit_parents(gitdir, sha)
        if parents is None:
            return False
        # THE TRI-STATE REACHES THE COVER AS A STATE, never projected on the
        # way. `_patch_id` answers id-or-None, so an unavailable measurement
        # and a genuinely empty one arrive as the same byte — and a commit
        # whose patch could not be taken then settles as covered while the
        # cover goes on to call itself complete, which is how a miss turns
        # into a false
        # ABSENT. `_measured_patch_id` is the same instrument one layer up,
        # and it is root-aware: a parentless commit hashes its whole tree
        # against the empty tree and has a real id like any other.
        state, pid = _measured_patch_id(gitdir, sha)
        if state == UNMEASURED:
            return False
        if state == HASHED:
            # A SETTLEMENT REQUIRES A WHOLE, USABLE OBJECT. The parser labels
            # any non-empty first output token HASHED, which is its own
            # contract and right for its other callers — but an id this reader
            # cannot use is not a smaller answer, it is NO answer, and
            # omitting it while marking the commit covered is how a trunk one
            # of whose commits was never indexed still empties its frontier
            # and licenses ABSENT. Insufficiency has ONE exit here, the same
            # one UNMEASURED takes: unsettled, still in the frontier, retried.
            if not (pid and _MEASURED_ID.fullmatch(pid)):
                return False
            ids.setdefault(pid, sha)
        seen.add(sha)
        for parent in parents:
            if parent not in seen and parent not in frontier:
                frontier.append(parent)
        return True

    if not (newest and seen):
        if not _cover(trunk):
            return (None, True)
        newest = trunk
        learned += 1
    elif newest != trunk:
        # DIVERGENCE IS MEASURED BEFORE ANY EXTENSION, never inferred from
        # the walk. `rev-list A..B` is NON-EMPTY whenever B has commits A
        # lacks, which is true of a DIVERGENT trunk exactly as it is of a
        # descendant one — so a forward walk that keeps the old ids carries
        # patch ids for commits the requested trunk does not contain, and a
        # cover built against one trunk then reports a change CONTAINED by
        # another that is neither its ancestor nor its patch match. Only an
        # ancestry answer separates the two cases; the superseded close door
        # is the consumer that turns the confusion into a wrong terminal.
        anc = _git(gitdir, "merge-base", "--is-ancestor", newest, trunk)
        if anc is None or anc.returncode not in (0, 1):
            return (None, True)          # unreadable: no index, no guess
        if anc.returncode != 0:
            # DROPPED AND REBUILT, NEVER PATCHED UP. Nothing in this cover is
            # known to be in the requested trunk's history, so keeping any of
            # it is exactly the union the trunk-membership rule forbids. The
            # cost is a re-walk; the cost of the alternative is a wrong LANDED.
            ids, seen, frontier = {}, set(), []
            if not _cover(trunk):
                return (None, True)
            newest = trunk
            learned += 1
            fwd = None
        else:
            if _derive_expired():
                # THE BOUNDARY COUNTS TOO. Enumerating a long front is itself
                # work, so the budget is asked BEFORE the walk as well as
                # inside it; a pass that arrives here spent answers UNKNOWN
                # rather than starting an enumeration it cannot finish.
                return (None, True)
            fwd = _git(gitdir, "rev-list", "--reverse", newest + ".." + trunk)
            if fwd is None or fwd.returncode != 0:
                return (None, True)
        for sha in ((fwd.stdout or "").split() if fwd is not None else ()):
            if _derive_expired():
                stopped = True
                break
            if not _cover(sha):
                stopped = True
                break
            newest = sha
            learned += 1
        if newest != trunk:
            # AN INTERRUPTED FORWARD WALK STORES NO COVER IT DID NOT CLOSE.
            # Retargeting a partial record to each visited sha looks thrifty
            # and is a false claim: with a root R whose independent children A
            # and B are merged by M, a walk that covers B and stops before M
            # would store newest=B while A's ids and A's seen entries are
            # still in the record — and the frontier can be EMPTY, because R
            # was already covered. The next question about B then skips
            # extension entirely and answers a positive out of A's ids, for a
            # trunk that does not contain them. Nothing raced and nothing was
            # corrupt; the bookkeeping simply promised more than the walk
            # closed. What this pass learned is discarded and re-derived.
            return (None, True)

    while frontier and learned < int(TRUNK_PID_BACKFILL) + 1:
        # THE COUNT IS NOT A TIME BOUND. A loaded box can make the same number
        # of commits cost far more wall clock than the derive budget allows,
        # and the backfill is optional progress rather than an answer anyone
        # is waiting on, so it yields and banks what it has.
        if _derive_expired():
            stopped = True
            break
        sha = frontier.pop(0)
        if sha in seen:
            continue
        if not _cover(sha):
            frontier.append(sha)     # retried next pass; nothing completes
            stopped = True
            break
        learned += 1
    if learned:
        banked = _trunk_index_write(gitdir, rec, ids, seen, frontier, newest)
        if banked is None:
            return (None, True)      # a rival cover won the write; re-derive
        ids, seen, frontier, newest = banked
    # INCOMPLETE UNTIL THE FRONTIER IS EMPTY AND THE FRONT ARRIVED, and a walk
    # that STOPPED is incomplete whatever the frontier says: a miss under any
    # of those is UNKNOWN and can never read ABSENT.
    incomplete = bool(frontier) or newest != trunk or stopped
    return (ids or None, incomplete)


def _trunk_index_write(gitdir, rec, ids, seen, frontier, newest):
    """COMPARE AND SET on the cover's own identity; never a union.

    A pass whose stored record moved underneath it discards its extension
    rather than blending, because blending imports another cover's ids into
    this trunk's answer — the exact defect the trunk-membership rule exists
    to prevent.

    RETURNS ITS OWN COVER WHENEVER THE COMPARE HELD, and None when a rival
    record won it. An ordinary write FAILURE — a full disk, a read-only
    estate — keeps this pass's own valid cover, which is correct and is not
    the same case: nothing else claimed the slot, so what this pass measured
    is still true of the trunk it measured.
    On a rival the caller answers UNKNOWN and re-derives: the stored record
    may describe a DIFFERENT target, so handing it back would be the
    forbidden union arriving through the write path instead of the read one."""
    live = _trunk_index_read().get(gitdir)
    base_new = (rec or {}).get("newest")
    base_front = sorted((rec or {}).get("frontier") or ())
    if live is not None and (live.get("newest") != base_new
                             or sorted(live.get("frontier") or ())
                             != base_front):
        # THE LOSING SIDE HANDS BACK NOTHING, not the rival's cover. The
        # stored record may describe a DIFFERENT target, and returning it
        # would let a record answer a question it was never valid for — the
        # same union this rule exists to forbid, arriving through the write
        # path instead of the read one. `None` makes the caller answer UNKNOWN
        # for this pass and re-derive on the next.
        return None
    try:
        raw = pk.read_json(trunk_patch_index_path(), {}) or {}
        if not isinstance(raw, dict):
            raw = {}
        raw[gitdir] = {"newest": newest, "frontier": sorted(frontier),
                       "seen": sorted(seen), "ids": ids}
        os.makedirs(home.global_dir(), exist_ok=True)
        pk.write_json(trunk_patch_index_path(), raw)
        _TRUNK_INDEX_MEMO.clear()
    except Exception:                          # noqa: BLE001 — cold, not wrong
        pass
    return ids, seen, frontier, newest


LAND_PROOFS = "land-proofs.jsonl"         # append-only; see _land_proof_ledger
_LAND_PROOF_MEMO = {}
# The two verdicts `_landing_proof` may return that are safe to keep. Spelled
# once, read by the reader and the writer, so a third positive can never be
# admitted on one side of the seam only.
KEPT_PROOFS = ("ancestor", "patch-equivalent")


def land_proofs_path():
    return home.global_dir() + "/" + LAND_PROOFS


def _land_proof_ledger():
    """{(gitdir, tip): {trunk: proof}} — POSITIVE landing proofs only.

    A LEDGER, NOT A CACHE, on `_ancestry_ledger`'s argument. The key is a
    repository plus two FIXED object ids, and "was this tip's change present
    in trunk AT THIS COMMIT" cannot change once both objects exist: trunk
    moving does not stale an entry, it mints a different key. So there is no
    invalidation problem to get wrong and no staleness window to bound.

    EVERY KEY MUST BE A FULL OBJECT ID, NEVER A REF NAME. A name is not an
    identity — it moves, and a proof filed under one would be reused after the
    thing it was proven against had changed. Several callers still pass trunk
    NAMES down this path, so the refusal lives at BOTH doors rather than in a
    caller census: a line whose tip or trunk is not a full sha is dropped
    here, and `_keep_landing_proof` never writes one.

    ONLY THE POSITIVES ARE KEPT, and that asymmetry is the safety argument
    rather than an optimisation. `absent` and `unknown` are weather — an
    absent tip lands a minute later, and an unknown one is a timeout, a
    vanished object or an unreadable ref that clears on the next probe.
    `_landing_proofs` states the same law for its per-projection memo:
    caching either would turn a transient failure into permanent truth."""
    path = land_proofs_path()
    try:
        key = _ledger_key(path)
    except OSError:
        return {}
    if _LAND_PROOF_MEMO.get("key") == key:
        return _LAND_PROOF_MEMO["proofs"]
    proofs = {}
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
                if not isinstance(rec, dict):
                    continue
                gitdir, tip = rec.get("repo"), rec.get("tip")
                trunk, proof = rec.get("trunk"), rec.get("proof")
                if not (isinstance(gitdir, str) and gitdir
                        and _sha(tip) and _sha(trunk)):
                    continue
                # A LINE NAMING A PROOF THIS BUILD DOES NOT KEEP IS DROPPED,
                # never trusted for its own sake. The file outlives any one
                # release, so a future verdict word — or a hand-written line —
                # must not become an answer here by merely being present.
                if proof not in KEPT_PROOFS:
                    continue
                proofs.setdefault((gitdir, tip), {})[trunk] = proof
    except (OSError, UnicodeDecodeError):
        # THE DECODE IS INSIDE THE HANDLER THAT FALLS BACK. Iterating the file
        # is where UTF-8 is actually decoded, so one invalid byte raises from
        # the loop — and a handler catching only OSError lets that escape past
        # the empty-ledger fallback, out of a reader whose whole contract is
        # that an unreadable ledger reads as EMPTY.
        return {}
    _LAND_PROOF_MEMO.clear()
    _LAND_PROOF_MEMO.update(key=key, proofs=proofs)
    return proofs


def _kept_landing_proof(gitdir, tip, ref):
    """A durable POSITIVE for this exact question, or None. Spawns nothing.

    TWO WAYS TO HIT, and the second is what makes the ledger worth keeping
    across a train. The EXACT hit is `ref` itself — the same trunk commit the
    proof was taken against — and it is the case a projection re-run inside
    one unmoved trunk always takes.

    THE CARRIED-FORWARD HIT is a proof taken against an OLDER trunk `t` that
    is an ancestor of `ref`: under ff-only, a change present in trunk at `t`
    is present in every descendant of `t`, so the proof survives the move.
    `ancestry`'s own docstring is the authority for that monotonicity and
    names its one falsifier — force-push and history rewrite, both forbidden
    here — which is also why a rewrite is not a silent hazard: `t` stops being
    an ancestor of the new trunk and the entry simply stops matching.

    THE ANCESTRY HOP IS READ FROM THE DURABLE PAIR LEDGER AND NEVER COMPUTED.
    Computing it would put a git spawn on the free path, which is the one
    thing this function exists not to do; an undecided pair just means no
    carried-forward hit this pass and the live ladder answers as before."""
    if not (gitdir and _sha(tip) and _sha(ref)):
        return None
    kept = _land_proof_ledger().get((gitdir, tip))
    if not kept:
        return None
    exact = kept.get(ref)
    if exact:
        return exact
    pairs = _ancestry_ledger()
    # Appends extend the shared memo in place. Iterate a stable local view;
    # a proof arriving after this snapshot can be consumed on the next read.
    for trunk, proof in tuple(kept.items()):
        if pairs.get((trunk, ref)):
            return proof
    return None


def _teach_trunk_pairs(gitdir, ref, cache=None):
    """Decide (older trunk, `ref`) pairs for the trunks the proof ledger holds.

    THE CARRY-FORWARD IN `_kept_landing_proof` WAS BUILT AND WIRED TO NOTHING.
    It returns a proof taken against an older trunk `t` whenever `t` is an
    ancestor of `ref` -- correct under ff-only -- and it READS that ancestry
    hop from the durable pair ledger rather than computing it, to keep a git
    spawn off the free path. The ledger did not hold the pairs it needs.
    MEASURED on the live stores: 4,444 pairs against 83 distinct trunks in the
    proof ledger, with ZERO pairs carrying EITHER member among those 83,
    because the ledger's only other writer spends its budget on SUCCESSION
    pairs. The reader asked for a class of pair nobody produced, so it could
    not hit, and every fast-forward retired the whole warm-up.

    THIS IS THE WRITER FOR THAT CLASS. It runs where a pass first learns a
    repository's trunk, once per (gitdir, trunk sha), and asks only for pairs
    the ledger has not already decided.

    IT SERVES BOTH KEPT-PROOF LEDGERS, ON SEPARATE SPENDS.
    `_kept_carrier_proof` carries a relieving verdict over exactly this hop,
    and a teach that walked only the landing ledger's trunks would leave the
    carrier reader asking for a class of pair nobody produced — the same
    shape, one door over. Merging the two trunk sets into one capped slice is
    what must not happen: the cap is per CALL, so one slice of the union lets
    whichever ledger holds the lexically smaller shas take the whole
    allowance and leave the other's hops undecided. The body says what that
    costs and what the live sets currently look like.

    WHY A BOUNDED BACKFILL IS LEGITIMATE HERE and the argument is not mine:
    `_ancestry_ledger`'s own docstring makes it -- ancestry between two FIXED
    shas is immutable, so "the law forbids an UNBOUNDED walk every pass, not a
    converging one that never repeats work". This converges: a pair once
    decided is never recomputed, and a fast-forward adds exactly ONE trunk.

    IT IS A SEPARATE SPEND, NOT A SHARE. `_ancestry_append` caps each CALL,
    so a teach never consumes the allowance succession spends on the rows an
    alarm surface is asking about.

    `TRUNK_PAIR_BUDGET` IS INERT AT TODAY'S VALUES AND THE ASYMMETRY IS WORTH
    KNOWING BEFORE ANYONE MOVES EITHER NUMBER. This caller slices first and
    `_ancestry_append` slices again, so the EFFECTIVE cap is the smaller of
    the two. At 120 against ANCESTRY_BUDGET's 120 the outer one never binds;
    and because the inner slice happens after, lowering this constant lowers
    the spend while RAISING it changes nothing. It is a floor-setter in one
    direction only, not a knob that moves independently.

    THE COST GROWS WITH THE LEDGER'S TRUNK COUNT, one pair per old trunk per
    land, and the budget caps it -- so a ledger with more trunks than the
    budget converges over several passes instead of spending more. That is the
    honest degradation: partial coverage, never unbounded work.

    Fail-soft throughout: an undecidable pair is not written (that is
    `_ancestry_append`'s own contract), and a teach that cannot run costs
    speed and never correctness, because every reader falls back to the live
    ladder.
    """
    if not (gitdir and _sha(ref)):
        return
    seen = projscope.memo(("landreq._teach_trunk_pairs", gitdir, ref), dict) \
        if cache is None else cache
    if seen.get("done"):
        return
    seen["done"] = True
    pairs = _ancestry_ledger()

    def _older(store):
        """The trunks `store` holds a positive against that this ledger has
        not yet related to `ref`. Either dictionary can grow when another
        thread keeps a proof, so each traversal is snapshotted rather than
        copying the retained map on every append."""
        out = set()
        for (proof_gitdir, _tip), by_trunk in tuple(store.items()):
            if proof_gitdir != gitdir:
                continue
            for trunk in tuple(by_trunk):
                if trunk != ref and (trunk, ref) not in pairs:
                    out.add(trunk)
        return out

    # ONE SPEND PER LEDGER, NEVER ONE SHARED BETWEEN THEM. `_ancestry_append`
    # caps each CALL, so two calls are two allowances, while one sorted slice
    # of the UNION lets whichever ledger holds the lexically smaller trunk
    # shas take the whole cap and leave the other's carry-forwards undecided.
    # That crowding is a property of the two trunk SETS rather than of either
    # ledger: measured over the live stores it is currently invisible,
    # because every one of the 119 undecided trunks the carrier ledger holds
    # for this repository is also one of the landing ledger's 235 — the two
    # sets are not independent today and nothing keeps them so. The landing
    # spend is therefore exactly what it was before this reader existed, and
    # the carrier ledger buys its own.
    landing = _older(_land_proof_ledger())
    if landing:
        # PAIRS, NOT SHAS. `_ancestry_append` unpacks each item as
        # (older, newer) and slices to its own per-call cap.
        _ancestry_append(gitdir,
                         [(t, ref) for t in sorted(landing)][:TRUNK_PAIR_BUDGET])
    _carrier_proof_ledger()
    carried = _older(_CARRIER_PROOF_MEMO.get("relieving") or {}) - landing
    if carried:
        # The pair memo still shows the file as it was before the append
        # above, so the landing set is subtracted rather than re-decided.
        _ancestry_append(gitdir,
                         [(t, ref) for t in sorted(carried)][:TRUNK_PAIR_BUDGET])


def _keep_landing_proof(gitdir, tip, ref, proof, cache=None):
    """Append one positive, under FULL OBJECT IDS PROVED READABLE.

    The identity check is not defensive tidiness: callers reach this ladder
    with trunk NAMES, and a proof filed under a name would be reused after the
    name had moved. A key that does not resolve to a commit in THIS repository
    is not an identity and is not written. Fail-soft on the write itself: a
    ledger helm cannot write costs speed and never correctness, because every
    reader falls back to the live ladder."""
    if proof not in KEPT_PROOFS or not gitdir:
        return
    if not (_sha(tip) and _sha(ref)):
        return
    # THE COMMIT PREDICATE, NOT BARE EXISTENCE. `cat-file -e <sha>` is true of
    # a TREE and of a BLOB, so it establishes that something is there and not
    # that this key names a commit — which is the whole content of the
    # promise. `_commit_exists_cached` asks the `^{commit}` question through
    # the projection's own batch, so a repository asked about one trunk on
    # every row pays for it once.
    seen = projscope.memo(("landreq._keep_exists", gitdir), dict) \
        if cache is None else cache
    for sha in (tip, ref):
        if not _commit_exists_cached(gitdir, sha, seen):
            return
    if _land_proof_ledger().get((gitdir, tip), {}).get(ref):
        return                        # already recorded; do not grow the file
    rec = {"repo": gitdir, "tip": tip, "trunk": ref, "proof": proof}
    # THE SIBLING GETS THE SAME CURE, through the same helper, because this is
    # the same shape and it predates the carrier ledger. It is lower volume
    # today, which is exactly why nobody measured it; the carrier ledger is
    # what made the class expensive enough to see. One helper so the two
    # cannot drift.
    _append_kept(land_proofs_path(), _LAND_PROOF_MEMO,
                 json.dumps(rec, sort_keys=True) + "\n",
                 lambda proofs: proofs.setdefault((gitdir, tip), {}).__setitem__(ref, proof))


CONFIRMATION_EVIDENCE = dispatches._RESOLUTION   # the door's OWN sentence

# THE RESOLVED DOOR'S ADMITTED CONFIRMATION POLARITIES (amendment A): approve
# or supersede, NEVER fix — a FIX verdict's meaning is that the work is NOT
# resolved. ONE constant, two readers (the `resolved` close rung and
# `confirmation_row`), so the door and the display classifier cannot drift
# apart. Minted for a review's finding (verdict on 1710265fd9a7): the first
# cut of confirmation_row never read polarity, so a FIX-polarity but
# otherwise confirmation-shaped row passed the predicate and minted a quiet
# self "c" + parent "b" out of the exact verdict that says "not resolved".
CONFIRMATION_POLARITIES = ("approve", "supersede")


def confirmation_row(lr):
    """Is this row the door's own CONFIRMATION ROUND — the #149 value-space
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
    is a WRITTEN CONTRACT of the resolved door, not sniffed sentiment. The
    #177 alternate shape ("supersede honored through independent landing:")
    was checked against the live ledger 2026-08-05: ZERO occurrences, so it
    is deliberately NOT accepted."""
    return lr.get("kind") == "review" and bool(lr.get("supersedes")) \
        and lr.get("polarity") in CONFIRMATION_POLARITIES \
        and dispatches.resolution_statement(lr.get("verdict_ref")) is not None


def _supersedes_reaches(candidate, target, current):
    """True | False | None — does CANDIDATE cite TARGET through one valid chain?

    The supersedes edge is the writer's explicit statement that one round
    continues another, but a raw edge is not work identity: every hop must pass
    dispatches' replayed same-chain predicate. Missing, cyclic, or cross-root
    ancestry is UNKNOWN, never a negative and never permission to retire work."""
    seen = set()
    node = candidate
    while True:
        parent_id = str(node.get("supersedes") or "")
        if not parent_id:
            return False
        if parent_id == dispatches.CHAIN_UNKNOWN or parent_id in seen:
            return None
        parent = current.get(parent_id)
        if parent is None or not dispatches._same_chain(parent, node):
            return None
        if parent_id == target:
            return True
        seen.add(parent_id)
        node = parent


def _carrier_evidence(row, relation, landed_proof="ancestor"):
    # `landed_proof` names WHICH landing proof admitted the carrier's tip —
    # "ancestor" (literal trunk reach) or "patch-equivalent" (cherry-picked
    # land, same patch under a new sha). Never flattened into one word: this
    # repo cherry-picks its lands (`_landing_proof`'s stated law), and relief
    # that cannot say which proof carried it is not falsifiable.
    return {
        "id": str(row.get("id") or ""),
        "reviewed_tip": str(row.get("reviewed_tip") or ""),
        "relation": relation,
        "landed_proof": landed_proof,
    }


def contrary_discharge(lr, chain, reach, pairs, unseen, current=None,
                       landed=None):
    """"a" | "b" | "c" | None | "unverified" — did succession DISCHARGE this
    row, so the contrary banner is a lie?

    All consumers share `_succession`: citation through the supersedes graph,
    or measured Git lineage from this row's tip to an approved on-trunk tip.
    Chain membership and timestamp order prove neither. The row itself remains
    the separate confirmation-instrument arm (`c`)."""
    if confirmation_row(lr):
        return "c"
    state, carrier = _succession(lr, chain, reach, pairs, unseen, current,
                                 landed)
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


def _ancestry_repo(owner, carrier, fallback=None):
    """Repository root for one inferred tip relation; False means cross-repo.

    A supersedes citation declares its own relation and is validated separately.
    Git-inferred lineage has no such declaration, so two rows naming different
    repositories are unrelated without spending I/O. One named repository is
    enough to locate legacy counterpart objects; fully legacy callers retain the
    explicit fallback they supplied."""
    orepo = owner.get("repo_id") if isinstance(owner, dict) else None
    crepo = carrier.get("repo_id") if isinstance(carrier, dict) else None
    orepo = orepo if isinstance(orepo, str) and orepo else None
    crepo = crepo if isinstance(crepo, str) and crepo else None
    if orepo and crepo and orepo != crepo:
        return False
    repo = crepo or orepo
    if repo and repo.endswith("/.git"):
        return repo[:-5]
    return repo or fallback


def succession_facts(lr, chain, reach, pairs=None, unseen=None, current=None,
                     landed=None):
    """(state, carrier evidence) from ONE citation/lineage walk.

    `carrier` is the immutable evidence payload consumers need: id, full tip,
    the relation that proved carriage, and which landing proof admitted the
    tip. HELD and UNKNOWN name none. `pairs` is the ancestry ledger; an absent
    pair stays UNKNOWN and is added to `unseen` for bounded measurement rather
    than guessed. `landed` is the carrier-landing oracle (`_carrier_landing`);
    without one, only literal reach membership admits a tip."""
    return _succession(lr, chain, reach, pairs, unseen, current, landed)


def succession_state(lr, chain, reach, pairs=None, unseen=None, current=None,
                     landed=None):
    """Did provable succession carry this row? moved | held | unknown.

    DISPLAY ONLY: `stalled` and `owed_by` remain untouched. UNKNOWN covers both
    missing chain identity and a carrier relation whose citation/ancestry
    cannot be read; neither may collapse into a confident HELD."""
    return _succession(lr, chain, reach, pairs, unseen, current, landed)[0]


def _succession(lr, chain, reach, pairs=None, unseen=None, current=None,
                landed=None, reasons=None, locations=None, gitdir=None):
    """The one walk. (state, carrier evidence).

    THE CARRIER GATE IS THE LANDING LADDER, NOT RAW REACHABILITY. The first
    cut admitted a sibling only when its exact sha sat in the trunk rev-list,
    while the row's OWN landed marker walks `_landing_proof` (ancestry, else
    patch id — this repo cherry-picks its lands). That gave one concept two
    incompatible vocabularies: a literal-ancestor carrier discharged, while
    an otherwise equivalent chain-linked approval bound to a cherry-picked
    carrier remained contrary even though the carrier's own row projected
    LANDED.

    So the tip gate is now three-valued through the SAME ladder the landed
    marker uses: literal reach stays the free fast path; off-reach tips ask
    the `landed` oracle, where a RELIEVING proof (ancestor beyond the reach
    cap, or patch-equivalent) admits the carrier, a MEASURED `absent` refuses
    it, and anything unreadable poisons HELD into UNKNOWN — a broken proof
    cannot authorize retirement, and it cannot honestly say no carrier exists
    either. ONE gate for every admitted sibling, both confirmation polarities
    and both target polarities: a second, polarity-shaped door here would be
    exactly the drift the confirmation constants were minted against. With no
    oracle (direct callers, fixtures whose reach IS the trunk truth) the old
    contract holds unchanged: off-reach means not a carrier."""
    reasons = reasons if reasons is not None else set()
    root = str(lr.get("chain_root") or "")
    if not root or root == dispatches.CHAIN_UNKNOWN:
        reasons.add("identity")
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
        admitted = sib.get("polarity") == "approve" or confirmation_row(sib)
        if not admitted:
            continue
        tip = str(sib.get("reviewed_tip") or "")
        if not tip:
            continue

        # RELATION BEFORE LANDING PROOF. An unrelated sibling whose repository
        # is unreadable has no bearing on this target and must not poison HELD.
        relation = None
        cited = _supersedes_reaches(sib, target, current)
        if cited is True:
            relation = "supersedes-ancestry"
        elif cited is None:
            unknown = True
            reasons.add("chain")
        if relation is None and sib.get("polarity") == "approve" and target_tip:
            if tip == target_tip:
                relation = "tip-equal"
            else:
                repo = _ancestry_repo(lr, sib, gitdir)
                if repo is False:
                    continue              # different repositories imply no lineage
                pair = (target_tip, tip)
                known = pairs.get(pair)
                if known is None:
                    unseen.add(pair)
                    if locations is not None:
                        locations.setdefault(pair, repo)
                    unknown = True
                    reasons.add("ancestry")
                elif known:
                    relation = "tip-descendant"
        if relation is None:
            continue

        if tip in reach:
            proof = PROOF_ANCESTOR
        elif landed is None:
            continue              # no oracle: reach IS the landing evidence
        else:
            proof = landed(lr, sib)
            if proof in (PROOF_ABSENT, PROOF_CROSS_REPO):
                continue
            if proof not in _PROOF_RELIEVES:
                unknown = True
                reasons.add("landing")
                continue

        # A descendant's own patch landing does NOT prove its ancestor's work
        # landed: trunk may carry only the descendant delta. Exact ancestry is
        # sufficient; patch-equivalence needs an independent target proof.
        if relation == "tip-descendant" and proof == PROOF_PATCH_EQUIVALENT:
            target_proof = landed(lr, lr)
            if target_proof in (PROOF_ABSENT, PROOF_CROSS_REPO):
                continue
            if target_proof not in _PROOF_RELIEVES:
                unknown = True
                reasons.add("landing")
                continue
            # `target_proof` vouches for the inherited history only. The
            # evidence still names how the CARRIER itself landed.
        return SUCCESSION_MOVED, _carrier_evidence(sib, relation, proof)
    return (SUCCESSION_UNKNOWN if unknown else SUCCESSION_HELD), None


def _annotate_succession(out, current, reach, gitdir=None, landed=None):
    """Stamp succession facts, measuring each ancestry pair in its repository."""
    gitdir = gitdir or os.getcwd()
    landed = landed or _carrier_landing(gitdir)
    chain = {}
    for row in current.values():
        root = str(row.get("chain_root") or "")
        if root:
            chain.setdefault(root, []).append(row)
    pairs = _ancestry_ledger()

    def learn(missing, locations):
        """Spend one global budget, grouped by the repository that owns a pair."""
        batches = {}
        for pair in list(missing)[:ANCESTRY_BUDGET]:
            root = locations.get(pair) or gitdir
            batches.setdefault(root, set()).add(pair)
        learned = {}
        for root, batch in batches.items():
            learned.update(_ancestry_append(root, batch))
        return learned

    # THE BUDGET GOES TO LIVE CONTRARY ROWS FIRST. They are the rows the alarm
    # surface asks succession to quiet; filling one unordered pool with every
    # live row before the first append loses this priority in practice.
    priority = [lr for lr in out.values()
                if lr.get("contrary") and not lr.get("terminal")]
    unseen, locations = set(), {}
    for lr in priority:
        _succession(lr, chain, reach, pairs, unseen, current, landed,
                    locations=locations, gitdir=gitdir)
    if unseen:
        learned = learn(unseen, locations)
        if learned:
            pairs = dict(pairs); pairs.update(learned)

    def stamp(lr, known, missing, missing_locations):
        reasons = set()
        state, carrier = _succession(
            lr, chain, reach, known, missing, current, landed, reasons,
            missing_locations, gitdir)
        lr["succession_state"] = state
        lr["succession_carrier"] = carrier
        labels = {
            "identity": "chain identity is missing or unreadable",
            "chain": "supersedes chain is malformed or unreadable",
            "ancestry": "carrier ancestry pair is not yet computed",
            "landing": "carrier landing proof is unreadable",
        }
        lr["succession_unknown_reason"] = "; ".join(
            labels[r] for r in ("identity", "chain", "ancestry", "landing")
            if r in reasons) if state == SUCCESSION_UNKNOWN else None

    first = {id(lr) for lr in priority}
    ordered = priority + [lr for lr in out.values() if id(lr) not in first]
    unseen, locations = set(), {}
    for lr in ordered:
        stamp(lr, pairs, unseen, locations)
    if unseen:
        learned = learn(unseen, locations)
        if learned:
            pairs = dict(pairs); pairs.update(learned)
            for lr in ordered:
                if lr.get("succession_state") == SUCCESSION_UNKNOWN:
                    stamp(lr, pairs, set(), {})


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


def _landing_proofs(gitdir=None, cache=None):
    """One projection's `(owner row, carrier row) -> typed landing proof`.

    Repository and trunk are proof identity, never ambient cwd. Each carrier is
    measured in its OWN repository against that repository's resolved trunk,
    whose object id is pinned once for the projection. The same context feeds
    succession and frontier debt, so one row cannot be ANCESTOR in one annotator
    and UNKNOWN in another because origin moved between two reads.

    `cache` IS THE CALLER'S PROJECTION CACHE, AND THIS PIN BELONGS IN IT. These
    ref reads used to live in a dict private to this closure, bound to the
    caller's snapshot only because one `projscope` spanned the cycle and the
    peel argv matched `_resolved_ref`'s to the character — an indirect binding
    that a respelling on either side would have broken silently. Sharing the
    dict makes them the SAME reading by construction rather than by coincidence,
    and it puts the trunk this annotator judged against inside whatever record
    the caller is keeping (see helm/web_land_model.py: a read the body makes and
    the caller's witness cannot see is a silent staleness channel). `None` keeps
    the private dict and every non-projecting caller is untouched.

    Only stable answers are memoized. UNKNOWN is weather — a timeout, vanished
    object, or unreadable ref can clear on the next probe — and caching it would
    turn a transient failure into the projection's permanent truth."""
    from . import vcs
    refs = {} if cache is None else cache
    pins, backends, memo = {}, {}, {}

    def forget_pin_reads(repo_id, trunk=None):
        """Forget only failed pin probes; successful pins stay snapshot-frozen."""
        for name in LOCAL_TRUNK + UPSTREAM_TRUNK:
            projscope.forget(_git_key(
                repo_id, (*_REF_ARGV, name)))
        projscope.forget(_git_key(
            repo_id, ("config", "--get", "remote.origin.url")))
        if trunk:
            # THE EVICTION KEY IS DERIVED FROM THE READ KEY, never spelled a
            # second time: `projscope.forget` is silent on a missing key, so a
            # drifted literal here would evict nothing and say nothing while a
            # failed probe stayed frozen for the whole projection.
            projscope.forget(_peel_key(repo_id, trunk))

    def _pin(repo_id):
        """Pinned trunk object for one repository, resolved once per projection."""
        if not isinstance(repo_id, str) or not repo_id:
            return None
        if repo_id in pins:
            return pins[repo_id]
        try:
            local, upstream = _trunk_refs(repo_id, refs)
            configured = _origin_configured(repo_id)
            trunk = upstream if configured else local \
                if configured is False else None
        except projscope.Expired:
            raise
        except Exception:
            trunk = None
        if not trunk:
            # A failed ref read is weather, not this projection's permanent
            # repository identity. Drop both local caches so a later target can
            # retry; only a successfully resolved object is frozen.
            refs.pop(repo_id, None)
            forget_pin_reads(repo_id)
            return None
        # THROUGH `_resolved_ref`, THE ONE SPELLING — not a fifth peel site.
        # It memoises into `refs`, which is the caller's projection cache when
        # one was handed in, so this annotator's trunk and the row loop's trunk
        # are one resolution rather than two readings of a moving name that
        # happen to agree.
        try:
            sha = (_resolved_ref(repo_id, trunk, refs) or "").lower()
        except projscope.Expired:
            raise
        except Exception:
            sha = ""
        if not _sha(sha):
            refs.pop(repo_id, None)
            refs.pop(("refsha", repo_id, trunk), None)
            forget_pin_reads(repo_id, trunk)
            return None
        pins[repo_id] = sha
        return sha

    def proof_for(owner, carrier):
        """The typed proof for one carrier, on its repository's pinned trunk."""
        crepo = carrier.get("repo_id") if isinstance(carrier, dict) else None
        orepo = owner.get("repo_id") if isinstance(owner, dict) else None
        if not isinstance(crepo, str) or not crepo:
            return PROOF_NO_REPO
        if isinstance(orepo, str) and orepo and orepo != crepo:
            return PROOF_CROSS_REPO
        tip = str(carrier.get("reviewed_tip") or "")
        if not tip:
            return PROOF_NO_TIP
        trunk = _pin(crepo)
        if not trunk:
            return PROOF_NO_PIN
        root = crepo[:-5] if crepo.endswith("/.git") else (gitdir or "")
        if not root:
            return PROOF_NO_REPO
        key = (root, tip, trunk)
        if key in memo:
            return memo[key]
        # THE DURABLE ANSWER SITS ABOVE THE BUDGET DOOR, on `_landing_proof`'s
        # argument: a kept verdict costs no spawn, so charging it against a
        # spend meant to bound git would answer UNKNOWN for a question already
        # settled — and the carriers past the cutoff are exactly the ones the
        # ledger exists to serve. It is keyed on the row's own declared
        # repository rather than on `root`, which is derived and can fall back
        # to the annotator's ambient checkout.
        kept = _kept_carrier_proof(crepo, tip, trunk)
        if kept:
            memo[key] = kept
            return kept
        # Share the projection's derive budget: succession's per-carrier
        # landing proof is the SECOND git storm through board_section (the
        # first being _landing_proof, which round two bounded). Once the
        # projection has spent its budget, degrade to UNKNOWN here too
        # rather than spending unbounded git per carrier — SUCCESSION_UNKNOWN's
        # "landing" reason already names exactly this. UNKNOWN is not memoized
        # below, so a fresh projection re-derives.
        #
        # MEASURED, and it is why the ledger above exists: this derivation is
        # `vcs.landed_state`, 2209 calls at about 48ms across one full
        # projection — 105.8s of a 116.6s pass, against 4.1s for every
        # `landreq._git` spawn put together. The in-projection memo dies with
        # the pass and with every `helm web` restart, so the top CPU consumer
        # on this box paid it again on each rebuild.
        if _derive_expired():
            return PROOF_UNKNOWN
        try:
            be = backends.get(root)
            if be is None:
                be = backends[root] = vcs.backend(root)
            got = be.landed_state(root, tip, trunk)
        except projscope.Expired:
            raise
        except Exception:
            got = None
        answer = {
            vcs.ANCESTOR: PROOF_ANCESTOR,
            vcs.PATCH_EQUIVALENT: PROOF_PATCH_EQUIVALENT,
            vcs.NOT_ANCESTOR: PROOF_ABSENT,
        }.get(got, PROOF_UNKNOWN)
        if answer != PROOF_UNKNOWN:
            memo[key] = answer
            _keep_carrier_proof(crepo, tip, trunk, answer)
        return answer

    # THE PIN TRAVELS WITH THE ORACLE. A caller that wants the CHEAP ancestry
    # answer rather than the full typed proof still has to ask it about THIS
    # projection's trunk -- resolving trunk a second time is exactly the
    # two-readings-of-a-moving-name this closure exists to prevent, and the
    # docstring's promise that one row cannot read ANCESTOR here and UNKNOWN
    # in the next annotator only holds while there is one pin. Exposing it is
    # what lets `_annotate_trunk_containment` stay on the same snapshot
    # without rebuilding one.
    proof_for.pin = _pin
    return proof_for


def _carrier_landing(gitdir=None):
    """Compatibility name for the shared per-projection proof context."""
    return _landing_proofs(gitdir)


def _annotate_contrary_discharge(out, current=None, gitdir=None, reach=None,
                                 landed=None):
    """Stamp contrary discharge from the ALREADY-COMPUTED succession fact.

    Succession and contrary used to spend separate ancestry budgets and could
    publish one row as succession UNKNOWN but contrary-discharge a/b after the
    second pass learned more. One proof pass now owns both fields. Direct test
    callers that have not run it yet are upgraded here exactly once."""
    rows = [l for l in out.values() if l.get("contrary")]
    if not rows:
        return
    if any("succession_state" not in l for l in rows):
        current = current or out
        gitdir = gitdir or os.getcwd()
        reach = set() if reach is None else reach
        _annotate_succession(out, current, reach, gitdir,
                             landed or _carrier_landing(gitdir))
    for lr in rows:
        if confirmation_row(lr):
            lr["contrary_discharge"] = "c"
            continue
        state = lr.get("succession_state")
        carrier = lr.get("succession_carrier") or {}
        if state == SUCCESSION_MOVED:
            lr["contrary_discharge"] = "b" \
                if carrier.get("relation") == "supersedes-ancestry" else "a"
            continue
        root = str(lr.get("chain_root") or "")
        lr["contrary_discharge"] = "unverified" \
            if state == SUCCESSION_UNKNOWN and root \
            and root != dispatches.CHAIN_UNKNOWN else None


def _selected_chain_ids(current, eligible, selector):
    """(raw ids, err) for SELECTOR's complete declared work chain.

    Hydration is narrow; identity and topology are not. Resolve against every
    eligible row first, seed every row with the same effective root, then walk
    supersedes in both directions through the full raw snapshot. Cancelled rows
    remain traversable transit, forks stay visible, and unrelated legacy roots
    or CHAIN_UNKNOWN rows never collapse into one bucket."""
    row, err = dispatches._resolve_row(
        eligible, selector, noun="land request", list_hint="helm lr list", allow_retired=True,
        allow_unknown_kinds=True)
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


def _project_of(path):
    """path -> registry project name, or None. ONE mapper for BOTH sides of
    the scope comparison — the same longest-prefix registry resolution `helm
    store` scopes with ("scoping to project '%s' (from cwd)"), so a row's
    project and the board's project can never be answered by two different
    oracles. Fail-open to None (project_for_cwd's own contract), and None is
    NEVER treated as "mine" — see _mark_foreign_rows.

    THE WRITE DOOR NOW ADMITS ON THIS SAME MAPPER (task/2437), so the body
    lives in `dispatches._project_of` and this is the read side's name for it.
    Two wrappers over one resolver is not two oracles; a second RESOLUTION
    would be, and an admission and a classification that disagreed about one
    repository would file rows the board then hides."""
    return dispatches._project_of(path)


_SCOPE_LENS = threading.local()


@contextlib.contextmanager
def scope_lens(fn):
    """Route `board_scope` through `fn` for the duration of one projection.

    SAME SHAPE AS `dispatches.tier_lens` AND `dispatches.epoch_lens`, and for
    the same reason: `_lr_build` calls `board_scope()` before it touches the
    read-set object, so the body cannot be asked to thread the record down to
    it. The snapshot is already OPEN at that point — `web_cache` enters it
    around the whole build and `_lr_reads()` merely ACCESSES it — so a lens
    installed there is live for that call.

    RESTORES THE PREVIOUS LENS rather than clearing, so composing two
    projections cannot leave the inner resolver installed over the outer's
    remaining rows."""
    prev = getattr(_SCOPE_LENS, "fn", None)
    _SCOPE_LENS.fn = fn
    try:
        yield
    finally:
        _SCOPE_LENS.fn = prev


def board_scope(repo_id=None):
    """{repo_id, project, why} — the identity THIS pipeline projects for.

    THIS IS THE DOOR; `_board_scope_uncached` IS THE BODY. Split for
    `approval_tier`'s reason: the land projection must consume this THROUGH its
    read-set, and the read-set's reader cannot call a function that routes back
    into the read-set.

    ITS PROJECT LAYER MOVES WITH NO LEDGER, TRUNK OR MARKER WRITE. `_project_of`
    resolves through the REGISTRY, so a registry-only remap changes which rows
    read local, foreign and unresolved — and therefore the withheld counts and
    the observation authority on the card — while every other recorded term
    stays byte-identical. Measured by a probe at CL96.

    OUTSIDE A LENS THIS IS EXACTLY WHAT IT WAS: every CLI caller and both
    `landreq` call sites resolve live."""
    lens = getattr(_SCOPE_LENS, "fn", None)
    if lens is not None:
        return lens(repo_id)
    return _board_scope_uncached(repo_id)


def _cli_scope():
    """The scope a CLI caller standing in a directory means — {repo_id, project,
    why}.

    THE DEFAULT WAS THE ONLY HELM-ANCHORED PART OF THE READ SIDE (task/2437).
    `board_scope(repo_id)` already answers correctly for ANY repository —
    measured from another registered project's checkout,
    `board_scope(<that repo's gitdir>)` returned that project while the bare
    call returned HELM, because its default comes from `home_repo_id()`, i.e.
    from where the helm PACKAGE lives. A seat working in another project's
    checkout was therefore shown helm's board with nothing saying so.

    SO THIS IS A DEFAULT, NOT A NEW MECHANISM: it fills in the operand from
    `dispatches.cwd_scope` — the one door `helm dispatch list` and `helm
    dispatch triage` also resolve through, so three CLI surfaces cannot answer
    differently about one directory — and hands it to the same `board_scope`
    every caller already used. Outside every registered project it resolves to
    exactly what the bare call resolved to before.

    THE WEB BOARD DELIBERATELY DOES NOT CONSUME THIS. `web_land_model._lr_repo`
    and the board's own read-set are anchored to `home_repo_id` on purpose (the
    board and the write door may not disagree), and a server process's cwd is
    not an operator's question. The board's scope selector is filed as its own
    lane; see the task named in this commit's body."""
    repo_id, _project, _why = dispatches.cwd_scope()
    return board_scope(repo_id=repo_id)


def _board_scope_uncached(repo_id=None):
    """The live resolution behind `board_scope`.

    TWO LAYERS, EACH FROM ITS ESTABLISHED OWNER, neither invented here:
    - repo identity: `dispatches.home_repo_id()` — the write door's own
      answer, derived from the running package path, NEVER from a rebuildable
      cache (its docstring records why that is a security property). The
      board and the write door already may not disagree (web_land_model._lr_repo);
      this keeps the read scope on the same authority.
    - project identity: `_project_of` over that repo path — the registry's
      longest-prefix resolution, the SAME mapper every row's repo_id goes
      through, so scope and classification agree by construction.

    `why` is set exactly when no repository identity exists; the callers must
    then DISCLOSE it and render the wide board — an unresolvable scope may
    never produce a confident narrower one. The registry being unreadable is
    NOT that case: repo identity still stands, rows of other repos then land
    in the disclosed UNRESOLVED bucket rather than being adopted or hidden."""
    if repo_id is None:
        repo_id, why = dispatches.home_repo_id()
        if not repo_id:
            return {"repo_id": None, "project": None, "why": why}
    return {"repo_id": repo_id, "project": _project_of(str(repo_id)),
            "why": None}


def _mark_foreign_rows(rows, scope):
    """LABEL rows owned by another repository. Never drop one.

    THE OWNER NEVER AUTHORIZED THIS BOARD TRACKING OTHER REPOSITORIES' WORK,
    and it did: measured on the live ledger, 1942 rows for this repo and 15 for
    others, inflating his stalled and contrary counts with work this pipeline
    has no business judging.

    THE FIRST CUT OF THIS FILTERED THEM OUT AND WAS WRONG IN THE WAY THAT
    COSTS MOST. One of those foreign rows carried a LIVE privilege-boundary
    finding on code already on another repository's trunk — a reviewer saying
    authority was derived from caller-supplied values — and it had sat for days
    precisely because it was tracked where nobody could act on it. (The row id
    is deliberately not quoted: it names a DISPATCH row, not a commit, so no
    other clone can dereference it and a citation a reader cannot follow is
    worse than none. The claim here is the SHAPE, not the identifier.) A filter that removes foreign rows removes that one, and every check
    written for the filter passes while it happens: the set was exactly the
    foreign rows, and being exactly right about the wrong shape is still wrong.

    SO COUNTS AND VISIBILITY ARE DIFFERENT CLAIMS AND ARE SCOPED DIFFERENTLY.
    A count is a claim about what THIS pipeline OWES, and foreign rows must not
    enter it. Visibility is a claim about what EXISTS, and a row nobody can see
    is a finding nobody can carry. So the row stays, gains `foreign` and the
    repository that owns it, and every counter reads the marker.

    THREE STATES, AND ABSENCE IS ITS OWN. `repo_id` is written only on a row's
    CREATE event — 71% of ledger lines are lifecycle events that never carry
    it — so a row whose create row cannot be found has NO repo to read. Six
    such rows exist in the live projection, measured, five at verdict and one
    cancelled.

    ABSENCE OF A REPO_ID IS NOT EVIDENCE OF LOCAL ORIGIN. Defaulting it to
    "mine" would silently adopt exactly the rows least entitled to a home, and
    that is this function's own earlier bug inverted: instead of losing foreign
    rows it would claim unknown ones. So absence is UNKNOWN, marked as such,
    excluded from this board's counts for the same reason foreign rows are —
    a count is a claim about what THIS pipeline owes, and an unattributable row
    supports no such claim — and visible for the same reason too.

    Matching this repo: local, unmarked. A DIFFERENT repository, whether or not
    that path still exists: foreign, named, kept. No repo_id at all: unknown,
    kept.

    THE AXIS IS NOW THE PROJECT, NOT ONLY THE REPOSITORY (task/974). A project
    can register several repos, so a sibling repo of the SAME project is LOCAL
    even though its repo_id differs — and a repo of ANOTHER project is foreign
    with that project's NAME on it, which is what the owner's disclosure line
    prints. The mapper is `_project_of` — the registry's own longest-prefix
    resolution, the same one `helm store` scopes with — never a second
    project-identity mechanism. Three-and-a-half states:
      - repo_id == scope repo, or same registered project -> local, unmarked
      - a DIFFERENT project                -> `foreign` + repo + PROJECT name
      - repo_id registered to NO project  -> `project_unresolved`, disclosed —
        NEVER guessed into either side (the UNMARKED pattern: a registry that
        cannot place a row must not adopt it and must not exile it silently)
      - no repo_id at all                 -> `origin_unknown`, as before

    A scope with no repo identity marks NOTHING — the callers disclose WHY and
    render the wide board rather than a confident narrower one.

    THE MARK GOES ON A COPY AND THE ROW IT WAS HANDED IS NEVER WRITTEN, so the
    RETURN carries the verdicts and the argument does not. These rows are a fold
    of the dispatch ledger, and `_ledger_fold` states the contract every reader
    of such a fold inherits: the rows are read-only, and a caller that writes
    must copy before it does. Writing the verdict in place was survivable only
    while every reader paid for its own fold, because then the marks could not
    leave the projection that made them. A fold SHARED between readers is the
    shape a write-maintained snapshot has by definition, and under it one
    reader's project scope silently becomes every reader's: a row would carry
    another board's `foreign` with nothing failing anywhere, because a verdict
    about WHOSE work a row is renders exactly as confidently when it belongs to
    someone else's scope as when it is this board's own. That is the error this
    whole classifier exists to prevent, arriving through the door it marks with.

    COPYING ONLY THE ROWS IT MARKS IS WHY THE IMMUTABILITY IS FREE. Measured on
    the live ledger, 118 of 3397 eligible rows take a mark at all: copying those
    costs 0.36 ms, and copying every eligible row would cost 7.5 ms, against a
    25 s fold and the 153 ms this function already spends on realpath and
    registry resolution. Cost never entered the choice; an enforceable read-only
    contract is the whole of it."""
    if not scope or not scope.get("repo_id"):
        return rows
    mine = os.path.realpath(str(scope["repo_id"]))
    my_project = scope.get("project")
    projects = {}
    out = dict(rows)
    for rid, row in rows.items():
        theirs = str((row or {}).get("repo_id") or "").strip()
        if not theirs:
            out[rid] = dict(row or {}, origin_unknown=True)
            continue
        real = os.path.realpath(theirs)
        if real == mine:
            continue
        if real not in projects:
            projects[real] = _project_of(real)
        their_project = projects[real]
        if their_project is None:
            out[rid] = dict(row, project_unresolved=True, foreign_repo=theirs)
            continue
        if my_project is not None and their_project == my_project:
            continue
        out[rid] = dict(row, foreign=True, foreign_repo=theirs,
                        foreign_project=their_project)
    return out


def project_raw(now=None, selector=None, repo_id=None, scope=None, cache=None):
    """({id: lr}, {id: RAW ledger row}, unavailable) — ONE read, both views.

    `cache` LETS A CALLER EXTEND THE GIT SNAPSHOT PAST THIS CALL, and it exists
    for exactly one shape: a caller whose body embeds git readings taken
    OUTSIDE this function and whose freshness witness was sampled BEFORE it.
    /api/lr is that caller — it fingerprints trunk, then builds `recent_lands`
    off trunk, then projects these rows against trunk, over 22-28s. Passing its
    own per-projection dict in makes `_trunk_refs`/`_resolved_ref` answer this
    projection from the SAME pinned commit the witness recorded, so a trunk
    that moves (or moves and moves back) mid-build cannot produce a body the
    witness never described. Omitted, this is a fresh dict and behaviour is
    exactly as before.

    PROJECT-SCOPED BY DEFAULT (task/974): every projection marks rows against
    `board_scope()` unless the caller passes its own `scope` (rendering
    surfaces do, so their header and their rows come from ONE resolution) or
    a bare `repo_id` (the older repo-level pin, still honored). Marking never
    removes a row — the withholding lives in the classifiers, the disclosure
    in `withheld_split` — so `helm lr show` on a foreign row still answers.

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
    # Read separately they were two instants, and a probe reproduced what fits
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
    # project.
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
    # board. This call sat OUTSIDE the per-row try, so a marker holding
    # `founder: []` raised TypeError out of gate_epoch and took every land loop
    # with it — a projection that refuses one row is the designed worst case; a
    # projection that raises is an outage. gate_epoch validates its own marker
    # now, so this is the second wall, and the only reading of an unreachable
    # boundary that is safe is the one that authorizes nothing.
    try:
        epoch = dispatches.gate_epoch(current, verdicts)
    except projscope.Expired:
        raise
    except Exception:                   # noqa: BLE001 — refuse, never crash
        epoch = dispatches.EPOCH_LOST
    receipts = _receipts_by_tip()       # one write-through index read, folded in
    eligible = {rid: row for rid, row in current.items()
                if not row.get("migration") and row.get("tip")
                and row.get("status") != "cancelled"}
    if scope is None:
        scope = board_scope(repo_id=repo_id)
    # THE MARKED ROWS ARE THE RETURN, NEVER THE ARGUMENT. `_mark_foreign_rows`
    # copies each row it marks, so the scope verdicts live on `eligible` from
    # here down and the fold behind `current` stays the ledger as it was read —
    # which is what lets a later reader share that fold without inheriting this
    # projection's answer to a question only this scope asked.
    eligible = _mark_foreign_rows(eligible, scope)
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
    # A HANDED-IN CACHE IS A HANDED-IN SNAPSHOT, not merely a speed-up: it may
    # already hold this repository's pinned trunk commit, and reusing it is
    # what binds this projection to the instant its caller witnessed.
    cache = {} if cache is None else cache
    out = {}
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
    # (a4784680, "one store read per projection") named what it was leaving:
    # "the remaining 10.1s is git (865 subprocess spawns through
    # _landing_proof) ... left alone deliberately and filed separately." The
    # ledger has since grown from 312 land loops to 1222 and the deferred half
    # is now the whole cost: MEASURED 2026-08-06, 4140 spawns and 627
    # `approval_tier` resolutions over SIXTEEN distinct (recipient, repo) keys,
    # each one walking every fd in /proc and firing a network canary. See
    # helm/projscope.py for why asking one question once is a correctness fix
    # here and not only a faster one.
    with store_load.read_scope(), projscope.scope():
        # ONE EXISTENCE QUESTION PER REPOSITORY, ARMED HERE AND BOUGHT LAZILY.
        # This call ARMS a pending sha list and spawns NOTHING; the first row
        # that actually needs an answer buys the batch through `_objexist_map`.
        # Saying it is asked BEFORE the loop taught eager semantics the code
        # does not have — and a reader who believed it would look for the spawn
        # here, not find it, and conclude the prefetch was dead.
        # Measured on the live ledger: 946 of 1952 spawns in a 248s build were
        # `cat-file -e`, one per row, every one a DISTINCT sha — which is
        # exactly why the projscope memo cannot touch them (it resolves
        # approval_tier 18 times over 18 keys, doing its job perfectly). The
        # cost is the SHAPE, not a missing cache, so the shape changes here:
        # every tip this projection will ask about goes down ONE
        # `cat-file --batch-check` pipe per gitdir, and the row loop reads the
        # answers out of `cache` — the same per-projection dict `_trunk_refs`
        # already uses, rather than a second mechanism.
        #
        # A FAILED BATCH IS NOT A PAGE OF ABSENCES. `_object_exists_batch`
        # returns None on a spawn failure, and `_objexist_map` then caches an
        # EMPTY FAILURE-MARKER MAP under the real key — not nothing. The subtype
        # lets the Web witness retain UNKNOWN; its empty mapping behaviour makes
        # every caller fall through to its own per-sha probe (identical to no
        # prefetch), while storing
        # NOTHING would make the next row re-buy the failing batch, once per
        # row, which is worse than the per-row probe this replaced.
        _prefetch_object_existence(eligible.values(), cache)
        # THE SECOND ARMED BATCH, AND IT IS THE BIGGER ONE. The existence
        # prefetch above collapsed one `cat-file -e` per row; this collapses
        # the PAIR of spawns each row pays at the patch-identity rung — a
        # `git show` to render the tip and a `git patch-id` to hash it.
        # Measured on the live board: 626 of the projection's 663 spawns, the
        # git-per-row shape that keeps the web process' rebuild loop at a full
        # core. Armed here and bought lazily for the same reason, by the same
        # `_will_observe_git` door; see `_prefetch_tip_patch_ids`.
        _prefetch_tip_patch_ids(eligible.values(), cache)
        for rid, row in eligible.items():
            try:
                out[rid] = _lr(row, by_id.get(rid, ()), taken_by_id.get(rid, ()),
                               cache, now, attests[rid], receipts,
                               index=dispatches.verdict_index(verdicts, rid),
                               epoch=epoch, consumed=consumed)
            except projscope.Expired:
                raise
            except Exception as e:
                # A ROW THAT CANNOT BE PROJECTED IS NOT A ROW THAT IS NOT THERE.
                # This used to `continue`, which is a SILENT PARTIAL: the snapshot
                # held the row, the board went out without it, and `unavailable`
                # stayed None — so the card printed one fewer lane and said it had
                # read cleanly. With one row on the ledger that is a board of "0
                # in flight" over a record that holds a land loop, which is the
                # single sentence this surface exists to never say. A probe
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
        # ONE per-repository, pinned-trunk proof context for EVERY annotation.
        # No global reach snapshot and no second oracle may describe a different
        # trunk instant or silently measure another repository's trunk.
        landed = _landing_proofs(cache=cache)
        _annotate_succession(out, current, set(), landed=landed)
        _annotate_contrary_discharge(out)
        _annotate_frontier_debt(out, current, proof=landed)
        # SAME `landed` CLOSURE, deliberately: one pinned trunk per
        # repository for every annotator in this block, so a row cannot
        # read ANCESTOR here and UNKNOWN one call later because origin
        # moved between two reads.
        _annotate_trunk_containment(out, eligible, proof=landed,
                                     cache=cache)
    # THE MARKER TRAVELS WITH THE ROW. `_lr` builds a fresh projection dict, so
    # foreignness computed on the ledger row would be lost exactly where the
    # counters read it — and a predicate that never reaches its consumer is a
    # predicate nobody applies.
    for rid, lr in out.items():
        src = eligible.get(rid) or {}
        if src.get("foreign"):
            lr["foreign"] = True
            lr["foreign_repo"] = src.get("foreign_repo")
            lr["foreign_project"] = src.get("foreign_project")
        elif src.get("project_unresolved"):
            lr["project_unresolved"] = True
            lr["foreign_repo"] = src.get("foreign_repo")
        elif src.get("origin_unknown"):
            lr["origin_unknown"] = True
    return out, current, None


# The EIGHT proof values §2.7 binds, recorded PER ANCESTOR. Two of them relieve
# and six do not, and collapsing the six into one word was the shortfall the
# integrator refused to waive: `cross-repo` and `no-pin` are SEMANTIC states,
# not shades of "unknown". repo_id already carries FOUR distinct values in the
# live ledger (spec §2.6), so a reader who cannot tell "this ancestor is in
# ANOTHER REPOSITORY" from "git could not read it" has lost the one fact that
# decides whether the debt is even ours to bill.
CARRIER_PROOFS = "carrier-proofs.jsonl"   # append-only; see _carrier_proof_ledger
_CARRIER_PROOF_MEMO = {}

# THE SLICE RUNNER'S DATA AUDIT (helm/gateslice.py) reports any module
# data a test unit leaves behind; these names are process-wide by design.
_GATESLICE_MUTABLE = {
    "_ANCESTRY_MEMO": (
        "one entry keyed by the stamp of what it folds; a changed input "
        "misses"),
    "_CARRIER_PROOF_MEMO": (
        "one entry keyed by the stamp of what it folds; a changed input "
        "misses"),
    "_CHAIN_CONTRIB_MEMO": (
        "one entry keyed by the stamp of what it folds; a changed input "
        "misses"),
    "_CONTEST_MEMO": (
        "one entry keyed by the stamp of what it folds; a changed input "
        "misses"),
    "_GATE_INDEX_MEMO": (
        "one entry keyed by the stamp of what it folds; a changed input "
        "misses"),
    "_LAND_PROOF_MEMO": (
        "one entry keyed by the stamp of what it folds; a changed input "
        "misses"),
    "_LEDGER_FOLD_MEMO": (
        "one entry keyed by the stamp of what it folds; a changed input "
        "misses"),
    "_TRUNK_INDEX_MEMO": (
        "one entry keyed by the stamp of what it folds; a changed input "
        "misses"),
}


def carrier_proofs_path():
    return home.global_dir() + "/" + CARRIER_PROOFS


def _carrier_proof_ledger():
    """{(repo_id, tip, trunk): proof} — durable `proof_for` answers.

    A SEPARATE LEDGER FROM `_land_proof_ledger`, AND THE KEYS LOOK IDENTICAL
    ON PURPOSE-LOOKING GROUNDS THAT ARE WRONG. Both are (repository, tip,
    trunk), but they answer DIFFERENT PREDICATES: `_landing_proof` asks
    whether ONE COMMIT's change reached trunk, while `vcs.landed_state` — the
    derivation behind `proof_for` — asks it of a whole CARRIER CHAIN, where a
    single unlanded commit is decisive against the whole. Sharing a file would
    let one question be answered with the other's verdict under a key that
    matched exactly, which is the worst shape a cache can take.

    ALL THREE VERDICTS ARE KEPT, AND ONLY THE POSITIVES CARRY FORWARD. The
    asymmetry is `KEPT_PROOFS`' and it is a fact about implication: present at
    an ancestor implies present at every descendant, and absent at an ancestor
    implies nothing at all. So the exact key answers every verdict, and the
    second index below answers a MOVED trunk from a positive taken against an
    older one — `_kept_carrier_proof` reads the ancestry hop from the durable
    pair ledger and never computes it. `absent` stays a fact about three fixed
    object ids and is served under no other key, exactly as `proof_for`'s own
    in-projection memo treats it.

    WHY A POSITIVE CARRIES, STATED AS THE PROPERTY IT ACTUALLY HAS.
    `vcs.landed_state` asks its question of the range `trunk..tip`. A trunk
    that fast-forwards from `t` to `ref` can only SHRINK that range (every
    commit of `t` is in `ref`), and every commit the shrunken range still
    holds had its patch proved present in `t`'s history, which is a subset of
    `ref`'s. So RELIEF carries: a relieving verdict at `t` relieves at `ref`.
    Its one falsifier is a force-push or history rewrite, which is also what
    stops `t` being an ancestor of `ref`, so the entry simply stops matching.

    THE WORD IS NOT WHAT CARRIES — RELIEF IS — and the gap is one-directional.
    `ancestor` at `t` is `ancestor` at `ref` (reachability only grows), but
    `patch-equivalent` at `t` can be `ancestor` at `ref`, because the move may
    have brought the tip OBJECT itself onto trunk. A carry serves the older,
    weaker word there: still true of the three ids — the patch is in trunk's
    history — and no longer the strongest word git would print. That matters
    because `carrier_discharge_proof` is read as the proof that carried the
    row, so THE GAP IS CLOSED AT THE READERS RATHER THAN HERE: both annotators
    that publish a proof word ask the free per-repo ancestry batch BEFORE this
    ledger is consulted, and take ANCESTOR from it when it answers.
    `_annotate_trunk_containment` always did; `_annotate_frontier_debt`'s
    `best_proof` does it in `_ancestry_word`, which states why the batch and
    not `_ancestry` is the instrument. The entry itself is untouched — it is
    still the relief that removes the spawn — and a reader can no longer be
    served the weaker word while trunk contains the object.

    THE POST-MOVE COST IS WHAT THIS IS FOR. Trunk moves on every land and a
    moved trunk mints new keys for every row; the relieving verdicts are also
    the EXPENSIVE ones, because a relieving answer is the one that runs the
    whole patch-identity ladder rather than exiting at the first unlanded
    commit. Measured over one `project_raw` reading this ledger with the
    current trunk's keys removed — the state one land leaves behind — the 442
    `patch-equivalent` answers cost 2,820 of the pass's 3,242
    `vcs.landed_state` git spawns, against 0 for 467 `ancestor` answers, 404
    for 202 `not-ancestor` and 18 for 1,371 `unknown`.

    THE LIMIT, RESTATED FOR THE CARRY. Repeated passes WITHIN one trunk
    generation were already nearly free; what the carry adds is the FIRST
    pass after a fast-forward, and only for the relieving half. The negative
    and the unknown still retire at every land: carrying `absent` forward is
    a different question that needs a bounded re-check over the new commits
    alone — a change to `vcs.landed_state`'s own predicate and its cherry
    count cross-check — and it stays filed rather than smuggled in here. The
    carry also needs the pair ledger to have DECIDED the hop, so a trunk
    generation that arrives before `_teach_trunk_pairs` has spent its budget
    on that trunk falls back to the live ladder rather than to a guess.

    UNKNOWN IS NEVER WRITTEN. It is weather — a timeout, an unreadable ref, a
    spent budget — and keeping it would turn a transient failure into the
    board's permanent truth. `_landing_proofs` states the same law for the
    memo this backs.

    EVERY KEY MUST BE A FULL OBJECT ID, NEVER A REF NAME, on
    `_land_proof_ledger`'s argument: a name moves, so a proof filed under one
    would be reused after the thing it was proven against had changed. The
    refusal lives at BOTH doors rather than in a caller census.
    """
    path = carrier_proofs_path()
    try:
        key = _ledger_key(path)
    except OSError:
        return {}
    if _CARRIER_PROOF_MEMO.get("key") == key:
        return _CARRIER_PROOF_MEMO["proofs"]
    proofs, by_trunk = {}, {}
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
                if not isinstance(rec, dict):
                    continue
                repo_id, tip = rec.get("repo"), rec.get("tip")
                trunk, proof = rec.get("trunk"), rec.get("proof")
                if not (isinstance(repo_id, str) and repo_id
                        and _sha(tip) and _sha(trunk)):
                    continue
                # A LINE NAMING A VERDICT THIS BUILD DOES NOT KEEP IS DROPPED.
                # The file outlives any one release, so a future word — or a
                # hand-written line — must not become an answer by being
                # present.
                if proof not in KEPT_CARRIER_PROOFS:
                    continue
                proofs[(repo_id, tip, trunk)] = proof
                if proof in _PROOF_RELIEVES:
                    by_trunk.setdefault((repo_id, tip), {})[trunk] = proof
    except (OSError, UnicodeDecodeError):
        # THE DECODE IS INSIDE THE HANDLER THAT FALLS BACK: iterating the file
        # is where UTF-8 is decoded, so one invalid byte raises from the loop
        # and a handler catching only OSError would let it escape past the
        # empty-ledger fallback this reader's contract promises.
        return {}
    _CARRIER_PROOF_MEMO.clear()
    _CARRIER_PROOF_MEMO.update(key=key, proofs=proofs, relieving=by_trunk)
    return proofs


def _relieving_carrier_proofs(repo_id, tip):
    """{trunk: proof} for every RELIEVING verdict kept about this (repository,
    tip), across every trunk one was taken against. Built beside the exact map
    so the two cannot answer from different readings of the file."""
    _carrier_proof_ledger()
    return (_CARRIER_PROOF_MEMO.get("relieving") or {}).get((repo_id, tip))


def _kept_carrier_proof(repo_id, tip, trunk):
    """A durable answer for this question, or None. Spawns nothing.

    THE EXACT KEY FIRST, and it answers every kept verdict including `absent`.

    THEN THE CARRIED-FORWARD HIT, which is `_kept_landing_proof`'s and is
    RELIEVING-ONLY: a verdict taken against an older trunk `t` that is an
    ancestor of `trunk` still holds, because a fast-forward only shrinks the
    range the verdict was proved over. The ancestry hop is READ from the
    durable pair ledger and never computed — a git spawn on this path is the
    one thing this function exists not to take — so an undecided pair simply
    means no carried hit this pass and the live ladder answers as before.
    `_teach_trunk_pairs` is the writer that decides those pairs.

    THE CARRY IS HELD TO BYTE EQUALITY, NOT TO ITS OWN ARGUMENT. Over
    identical fresh copies of a live home — the current trunk's keys removed
    from this ledger, from the landing ledger and from the pair ledger, which
    is the state one land leaves behind — one `project_raw` with the derive
    budget disabled was compared field by field, EVERY field of every row and
    not a chosen few: 3,440 rows, 0 differing, against a base-against-base
    control on two fresh copies that also differs in nothing. The same pass
    fell from 4,389 git spawns to 1,398 and from 1,527 distinct
    `vcs.landed_state` questions to 581."""
    if not (repo_id and _sha(tip) and _sha(trunk)):
        return None
    exact = _carrier_proof_ledger().get((repo_id, tip, trunk))
    if exact:
        return exact
    kept = _relieving_carrier_proofs(repo_id, tip)
    if not kept:
        return None
    pairs = _ancestry_ledger()
    # Appends extend the shared memo in place. Iterate a stable local view;
    # a proof arriving after this snapshot is consumed on the next read.
    for older, proof in tuple(kept.items()):
        if older != trunk and pairs.get((older, trunk)):
            return proof
    return None


def _keep_carrier_proof(repo_id, tip, trunk, proof):
    """Append one kept verdict under full object ids. Fail-soft on the write.

    A ledger helm cannot write costs speed and never correctness, because
    every reader falls back to the live derivation.
    """
    if proof not in KEPT_CARRIER_PROOFS or not repo_id:
        return
    if not (_sha(tip) and _sha(trunk)):
        return
    # A REPOSITORY THIS BOX CANNOT SEE IS NOT AN IDENTITY, and this check
    # costs no spawn on purpose. `_keep_landing_proof` proves both objects are
    # commits because its callers hand it trunk NAMES; here `trunk` is
    # `_pin`'s resolved output and `tip` is the object `landed_state` just
    # answered about, so a second existence probe would buy nothing and would
    # put two `git cat-file` spawns on the write path of every new key.
    #
    # MEASURED WHEN IT DID: the spawning version broke the arms that count
    # git under a spent budget and the one that asserts a single
    # `landed_state` call, because a write-path probe is still a spawn.
    #
    # IT IS ALSO WHAT KEEPS THIS LEDGER OUT OF A MOCKED DERIVATION. An arm
    # that substitutes the backend and hands it a synthetic repository is
    # measuring the CALLER, and a durable write from under it would answer
    # that arm's next question with its own previous answer — measured, six
    # arms at once: a transient-failure control read back the positive it had
    # recorded a moment earlier and called it a live derive.
    if not os.path.isdir(repo_id):
        return
    if _carrier_proof_ledger().get((repo_id, tip, trunk)):
        return                        # already recorded; do not grow the file
    rec = {"repo": repo_id, "tip": tip, "trunk": trunk, "proof": proof}
    # The estate directory is not created by `home.global_dir`, so a first
    # write on a fresh home has nowhere to land. Silently losing every proof
    # there would look exactly like a ledger that works. `_append_kept` owns
    # that, and owns keeping this reader's memo valid across the append.
    _append_kept(carrier_proofs_path(), _CARRIER_PROOF_MEMO,
                 json.dumps(rec, sort_keys=True) + "\n", _carrier_insert(
                     repo_id, tip, trunk, proof))


def _carrier_insert(repo_id, tip, trunk, proof):
    """The insert `_append_kept` applies for one kept carrier verdict.

    BOTH MAPS OR NEITHER. `_append_kept` re-keys the memo to the POST-append
    stat, so an index this does not reach is not repaired by the next read
    either: it is stamped current while missing the line we just wrote, for
    the rest of the process. The cost of that is carries, not answers — the
    reader falls back to the live ladder and derives the same verdict — but a
    memo that is wrong about its own writer is the shape the exact map is
    careful not to have, and the sibling must not be the exception.

    THE RELIEVING INDEX IS ONLY EVER GROWN HERE, never created: an absent one
    means the reader has not built the ledger in this process yet, and
    inventing a partial index under that name would answer a later carry from
    a map holding one line."""
    def insert(proofs):
        proofs[(repo_id, tip, trunk)] = proof
        index = _CARRIER_PROOF_MEMO.get("relieving")
        if proof in _PROOF_RELIEVES and isinstance(index, dict):
            index.setdefault((repo_id, tip), {})[trunk] = proof
    return insert


def _ledger_key(path):
    """The cache identity of a ledger file: WHICH file, and which contents.

    INODE AND DEVICE ARE IN THE KEY, not only size and mtime: a file REPLACED
    by a same-sized write with a preserved mtime is a different file that
    (size, mtime_ns) alone calls identical. That is not exotic here — an atomic rewrite-and-rename is the
    ordinary way to compact or repair an append-only ledger, and it is exactly
    the moment a stale cached map would be trusted.
    """
    st = os.stat(path)
    return (path, st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)


def _append_kept(path, memo, line, insert):
    """Append one ledger line and KEEP THE READ MEMO VALID over our own write.

    THE AMPLIFICATION THIS CLOSES: both kept-proof writers read the WHOLE
    ledger to dedupe, append,
    and the append moves (size, mtime_ns) — so the next lookup reparses the
    entire retained file. Twelve new answers against 50k retained rows read
    109.2 MB and cost 1.428s, against ~0.6ms for a hundred warm hits, and the
    cost grows with every pinned trunk generation rather than only with new
    derives. One cold projection writes about a thousand answers, so it paid
    this a thousand times and the bill read as git.

    WE KNOW EXACTLY WHAT WE WROTE, so the memo does not have to be thrown
    away: insert our own entry and re-key to the post-append stat. ONE stat
    per append, no reparse.

    A SIZE THAT IS NOT OURS MEANS SOMEBODY ELSE WROTE, and then clearing is
    the only correct move — our dict is genuinely missing their line, and
    keeping it would turn a concurrent write into a silent duplicate append
    on the next pass. Fail-soft throughout: a ledger helm cannot write or
    stat costs speed and never correctness, because every reader falls back
    to the live derivation.
    """
    payload = line.encode("utf-8")
    held = memo.get("key")
    try:
        os.makedirs(home.global_dir(), exist_ok=True)
        # THE SNAPSHOT MUST PROVABLY PRECEDE EXACTLY OUR OWN APPEND, which is
        # a stronger condition than "the file grew by our bytes". Requiring the PRE-append identity to equal the one the
        # cached map was built from is what rules out stamping a partial map
        # with somebody else's write, or with a replaced file.
        before = _ledger_key(path) if os.path.exists(path) else None
        torn = False
        if before is not None and before[3]:
            with open(path, "rb") as fh:          # A TORN LAST LINE, sealed.
                fh.seek(-1, os.SEEK_END)          # A crashed writer can leave
                torn = fh.read(1) != b"\n"        # a line with no newline, and
        if torn:                                  # appending would FUSE our
            line = "\n" + line                    # record onto it and lose
            payload = line.encode("utf-8")        # both.
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line)
        after = _ledger_key(path)
    except OSError:
        memo.clear()
        return
    proofs = memo.get("proofs")
    if torn or before is None or held != before \
            or after[:3] != before[:3] \
            or after[3] != before[3] + len(payload) \
            or not isinstance(proofs, dict):
        # Any of: a first write, an interleaved or replacing writer, a torn
        # tail we just repaired, or no map in hand. Our view is incomplete and
        # keeping it would turn somebody else's record into a duplicate append
        # on the next pass. Clearing costs ONE reparse; keeping costs
        # correctness.
        memo.clear()
        return
    insert(proofs)
    memo.update(key=after, proofs=proofs)


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

# The `proof_for` verdicts that are FACTS about three fixed object ids and may
# therefore be kept durably. Spelled once and read by the ledger's reader AND
# its writer, so a fourth word can never be admitted on one side of the seam
# only. UNKNOWN is deliberately absent: see `_carrier_proof_ledger`.
KEPT_CARRIER_PROOFS = (PROOF_ANCESTOR, PROOF_PATCH_EQUIVALENT, PROOF_ABSENT)
# carrier_discharge is a TRI-STATE over those eight. False means PROVEN not on
# trunk; None means the question could not be answered, which is never a "no".
_PROOF_DISCHARGE = {
    PROOF_ANCESTOR: True, PROOF_PATCH_EQUIVALENT: True, PROOF_ABSENT: False,
}

FRONTIER_DEBT_KNOWN = "known"
FRONTIER_DEBT_UNKNOWN = "unknown"

# A carrier RELIEVES only on one of these. `absent` and `unknown` both KEEP the
# debt, and that collapse is load-bearing: landreq's own `_landing_proof` and
# `vcs.landed_state` disagree about which of those two a row is (measured:
# `_landing_proof` answers `absent` for a range whose merges leave `git
# cherry` short, where vcs answers UNKNOWN), so a rule that distinguished them
# would inherit that disagreement. This one cannot.


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


def _unbill_contained(lr):
    """Stop the stall clock on a row whose work is already on trunk.

    ONE FUNCTION FOR BOTH EXITS of the containment walk, because the ancestry
    answer and the patch-identity answer are the same fact arriving by
    different routes and a second copy is how one of them keeps billing.

    `stalled` is an AGE and it reads as a slow reviewer. A row whose change is
    in history has no reviewer who can act and no author who can rebase into
    relevance — whatever is owed on it is owed to the LEDGER, and the mark in
    `_line` says so in words. Leaving the accusation up while adding the
    correction beside it publishes both, and the accusation is the one that
    routes attention. Same reason `owner_gated` and `source_clean_tip` are
    excluded from this flag in `_lr`.

    IT CLEARS THE FLAG AND NOT THE DWELL. The row still renders, still carries
    how long it has sat, and the time it sat is exactly what makes it worth
    finding — suppressing that would hide the row rather than re-address it."""
    lr["stalled"] = False


#: THE STATES A ROW HOLDS BEFORE ANY VERDICT IS RECORDED.
PRE_VERDICT_STATES = ("OPEN", "AWAITING_REVIEW", "AWAITING_BUILD")


def on_main_unverdicted(row):
    """Is this row's WORK on main with NO VERDICT recorded? True only when
    both halves are proven; every other answer is False, never a guess.

    ONE PREDICATE FOR EVERY SURFACE THAT ASKS (task/2381). `helm lr list`'s
    ALREADY ON TRUNK mark and the card's copy of it (the pipeline wall), the
    scheduler's collapsed line (`scheduler.collapse_class`) and the kanban's
    count line (`web_board._kanban_split`, through that same call) all read
    this function, so a row cannot be counted "on main" by one surface and
    listed as a wait by the one beside it — which the owner board did to 8
    of the integrator's 16 listed waits while the kanban folded on the
    containment mark and the scheduler never read it.

    CONTAINMENT ALONE IS NOT THE QUESTION. `_annotate_trunk_containment`
    measures every row whose own git leg came back unobserved, and a
    verdicted row can be one. MEASURED on the live trunk board: seven rows whose work trunk holds by patch identity carried a recorded
    APPROVE, CONCUR or FIX with `observable` False, and the kanban counted
    them — two FIX rows owed by their author among them — as "no verdict
    recorded", while `lr list` printed the same words on each. A recorded
    verdict, or any state a verdict produced, keeps the row out: a row on
    main under a live FIX is a contradiction somebody owes, never ledger
    debris.

    It reads three fields every card has carried since the containment mark
    shipped — `trunk_contains_tip`, `state`, `polarity` — so a projection
    row, a wire card and an older warm body all answer alike."""
    return row.get("trunk_contains_tip") is True \
        and row.get("state") in PRE_VERDICT_STATES \
        and not row.get("polarity")


def _annotate_trunk_containment(out, eligible=None, gitdir=None,
                                proof=None, cache=None):
    """Does trunk ALREADY CONTAIN this row's own tip? Asked of the oracle the
    projection has already built and never asks about the row itself.

    THE DEFECT THIS CLOSES. `_will_observe_git` reads git only for a row whose
    status is verdict/closed, on a premise spelled twice in this file: a row
    with no verdict yet cannot have landed, so "there is nothing that could
    have landed". That is true only while every land goes through helm's
    verdict door, and it is false for any repository helm does not gate. A
    pre-verdict row can therefore pin a tip that is ALREADY an ancestor of
    trunk with zero commits ahead, and nothing on the board says so: it keeps
    rendering as owed, and a whole-suite gate spent on it gates a tree that is
    already in history.

    WHY THIS COSTS ALMOST NOTHING. `_landing_proofs` is already built once per
    projection and already reached for non-terminal rows by
    `_annotate_frontier_debt` -- trunk pinned once per repository, a durable
    kept-proof fast path, an in-projection memo, and the shared derive budget.
    It was only ever asked about a row's CARRIERS. Measured on the live board
    the day this was written, all 15 open rows had `carrier_discharge_proof`
    None because none of them HAS a carrier, so the pinned, repo-correct,
    already-paid-for instrument answered the question for nobody. Sharing the
    same `proof` closure is also what keeps one row from reading ANCESTOR in
    this annotator and UNKNOWN in the next -- the property `_landing_proofs`
    promises and could not deliver to a second caller that built its own.

    THE AUTHORITY GATE IS NOT INHERITED, SO IT IS SPELLED HERE. A row with no
    carrier never reaches `proof` today, so asking for every pre-verdict row
    reaches it for rows that never had a git leg at all. `_landing_proofs` does
    not itself check provenance -- it is reached today only through paths that
    do -- so this annotator applies `_observation_owned`, the STRICT authority
    twin, exactly as `_lr` does. A foreign or project-unresolved row gets no
    git leg here either, and running git inside a repository this board cannot
    call its own stays impossible.

    AND IT ASKS THE LEDGER ROW, NOT ONLY THE PROJECTION. The projection's
    `foreign` / `project_unresolved` markers are copied onto `out` AFTER this
    block runs -- `_lr` builds a fresh dict, so provenance is re-attached in a
    later loop -- which means reading them off `lr` alone tests a flag that is
    not set yet and admits every foreign row. That is not a hypothetical: the
    suite caught it here, on the disclosure listing where foreign rows are the
    ones on the board. `eligible` is the same source that later loop reads, so
    both orderings answer the same way and neither is load-bearing.

    TRI-STATE, OVER THE EXISTING VOCABULARY. `_PROOF_DISCHARGE` already maps
    the eight proof words onto True / False / absent, so this reads it rather
    than spelling a second copy that could admit a ninth word on one side.
    True means PROVEN on trunk -- by ancestry or by patch identity, so a
    cherry-picked land counts. False means PROVEN absent. None means the
    question was not answered and is never a "no".

    IT ANSWERS ONLY FOR ROWS NOBODY ELSE MEASURED. A row whose own leg is
    `observable` already has `landed`/`merged_local` derived from the same
    pinned trunk; re-deriving it here would be a second oracle for one fact.
    """
    for lr in out.values():
        lr.setdefault("trunk_contains_tip", None)
        lr.setdefault("trunk_contains_proof", None)
        lr.setdefault("tip_behind_trunk", None)
    proof = proof or _landing_proofs(gitdir)
    pin = getattr(proof, "pin", None)
    source = eligible or {}
    builds, pins = {}, {}
    for rid, lr in out.items():
        # A terminal row is history and an observable row already has the
        # answer from its own leg; neither is a question this annotator owns.
        if lr.get("terminal") or lr.get("observable"):
            continue
        # BOTH HALVES, because either one alone has a window where it is blind:
        # the projection's markers are attached after this block, and a direct
        # caller may hand rows that carry them already.
        if not (_observation_owned(lr)
                and _observation_owned(source.get(rid) or {})):
            continue
        tip = lr.get("pinned_tip")
        if not _sha(tip):
            continue
        # ANCESTRY FIRST, AND THE REASON IS A MEASUREMENT, NOT A PREFERENCE.
        # This annotator runs at the END of the projection, so the shared
        # derive budget (`_derive_expired`) is normally already spent by
        # everything ahead of it: asking the full typed oracle first returned
        # `unknown` for all 15 rows of the live board, which is a cure that
        # never fires. Ancestry under `projscope` is two memoised listings per
        # gitdir that the projection has usually already bought, costs no
        # per-pair spawn and no derive budget, and answers exactly the question
        # this field NAMES -- does trunk already contain THIS commit.
        #
        # ON THE SAME PIN AS EVERY OTHER ANNOTATOR. The trunk sha comes from
        # the oracle's own `_pin`, never from a second `_trunk_refs` read here.
        trunk = pin(lr.get("repo_id")) if pin else None
        got = _ancestry(lr.get("repo_id"), tip, trunk) if trunk else UNDETERMINED
        # A BUILD ROW'S PINNED TIP IS ITS BASE, NOT ITS WORK. A build sent
        # with `--ref` the trunk itself pins a commit trunk contains the
        # moment it is sent, and asking containment of THAT commit read it
        # "ALREADY ON TRUNK" and stopped its stall clock before anybody had
        # written a line: the owner saw two lanes LANDED as they were sent.
        # The work lives on the build's LANE, so the question goes there
        # (`_annotate_build_lanes`), once per repository. The base distance
        # is still the base's own fact, and it is kept.
        if lr.get("kind") == "build":
            if got == NOT_ANCESTOR and cache is not None:
                lr["tip_behind_trunk"] = _base_behind(
                    lr.get("repo_id"), tip, cache)
            builds.setdefault(lr.get("repo_id"), []).append(lr)
            pins[lr.get("repo_id")] = trunk
            continue
        if got == ANCESTOR:
            lr["trunk_contains_proof"] = PROOF_ANCESTOR
            lr["trunk_contains_tip"] = True
            _unbill_contained(lr)
            continue
        # THE DISTANCE, AND ONLY WHERE THE OBJECT IS PROVEN PRESENT.
        # `base_behind` is stamped by `_lr` on READY rows only, and a
        # pre-verdict row is never `observable`, so the one number that says
        # "this cannot land no matter who reviews it" was missing from exactly
        # the rows a reviewer is being asked to spend a pass on.
        #
        # IT IS A DIFFERENT FIELD, AND THAT IS NOT TIDINESS. `base_behind`'s
        # ABSENCE is load-bearing: it is bound to `observable`, an unobservable
        # READY row is pinned as UNMEASURED rather than zero, and the
        # STALE_BASE_BEHIND rung REFUSES on the number when it is present.
        # Writing a pre-verdict distance into that field would turn "helm
        # never measured this" into "helm measured this and it is fine", and
        # at 450 it would begin refusing rows that are outside that rung's
        # reach. So the pre-verdict distance carries its own name and every
        # existing consumer keeps the contract it was written against.
        #
        # NOT_ANCESTOR IS THE GATE, AND IT IS A COST GATE AS MUCH AS A LOGICAL
        # ONE. `_batched_ancestry` answers NOT_ANCESTOR only for a sha it found
        # in the repository's own object listing, so that word is a PROOF the
        # object is present. UNDETERMINED is the vanished-object case -- and
        # `_base_behind` spawns `rev-list` per row, which on a missing object
        # is exactly the lazy-fetch or stall on a dormant object in a partial
        # or promisor repository that `_will_observe_git` refuses to risk.
        # MEASURED: running it on every row regardless took the projection from
        # seconds to minutes and had to be killed. Gated here it costs one
        # cheap count for rows whose object this repository demonstrably has,
        # and nothing at all for the rows that could hang.
        #
        # ORDER MATTERS: a tip that trunk CONTAINS is also behind it, usually
        # far, so the containment exit above returns before this runs. A
        # distance rendered on already-landed work bills its author for a
        # rebase into a tree that already has the change.
        if got == NOT_ANCESTOR and cache is not None:
            lr["tip_behind_trunk"] = _base_behind(
                lr.get("repo_id"), tip, cache)
        # NOT_ANCESTOR IS NOT AN ANSWER TO "DID THIS LAND". It says this exact
        # sha is not on trunk, which a normal cherry-picked land also satisfies
        # -- so the typed oracle is asked for patch identity before anything is
        # reported as not landed. It sits above its own kept-proof ledger, so a
        # row settled on an earlier pass costs no spawn; past the budget it
        # answers UNKNOWN and this field stays None, which is never a "no".
        #
        # THE ORACLE'S DECLARED PARAMETER SHAPE, not a reconstructed call.
        # `proof_for` reads `repo_id` off the owner and `repo_id` plus
        # `reviewed_tip` off the carrier, and nothing else. The row is its own
        # owner here, so the cross-repo leg cannot fire. `reviewed_tip` is the
        # wrong key to reuse on the row itself -- it means "the tip a reviewer
        # actually read", which is None on a row whose review has not happened,
        # and that is exactly the population this annotator exists for. The
        # arm `test_the_containment_annotator_asks_about_the_rows_own_tip`
        # pins these two keys so a drift in `proof_for` goes red instead of
        # quietly answering no-tip for every row.
        p = proof(lr, {"repo_id": lr.get("repo_id"), "reviewed_tip": tip})
        lr["trunk_contains_proof"] = p
        lr["trunk_contains_tip"] = _PROOF_DISCHARGE.get(p)
        if lr["trunk_contains_tip"]:
            _unbill_contained(lr)
    _annotate_build_lanes(builds, pins)


def _build_lane(lane):
    """The `helm work` lane name a build row's lane label names: the label
    with its `lane/` branch prefix taken off, or None for no label."""
    name = str(lane or "").strip()
    name = name[len("lane/"):] if name.startswith("lane/") else name
    return name or None


def _annotate_build_lanes(builds, pins=None):
    """Is a BUILD's WORK on trunk? Asked of its lane, never of its base.

    `builds` is {repo_id: [build rows]} from `_annotate_trunk_containment`,
    and `pins` its {repo_id: trunk sha}, the oracle's own pin for each.
    THE ANSWER IS `work.lanes_landed`'s, the producer `helm work list` and the
    board's lane column already print, so a lane reads one way on every
    surface: LANDED — the lane authored commits and trunk carries them — is
    True; UNSTARTED (at trunk with nothing authored: the build has not begun)
    and UNLANDED (a kept full verdict) are False, a MEASURED no; GONE and
    UNKNOWN are None, because no lane under that name is not proof the work
    never landed — a builder may have claimed the lane under another name.

    ANCESTRY AND AUTHORSHIP ONLY (`content=False`), because this runs for
    every open build row on every projection: the producer's patch-identity
    leg walks every trunk patch since a stale lane's base, which measured in
    minutes over the live board's build lanes. So a lane tip trunk does not
    hold by object id answers UNKNOWN from the producer — and that is every
    lane this repository lands by cherry-pick, and every retired one: 37 of
    the 92 live build lanes answered it the day this was measured, beside 2
    LANDED.

    PATCH IDENTITY FOR FREE, BEFORE AN UNKNOWN STANDS (`_lane_patch_proof`).
    The durable landing-proof ledger the census already reads keeps the
    patch-identity answer for every tip a review or the census proved, and a
    build lane's tip is usually exactly the tip its review read. Reading it
    spawns nothing and never runs `git cherry`. An UNKNOWN the ledger cannot
    answer stays None: it is not contained, not proven absent, and bills
    nobody for a rebase (`lr list` prints BEHIND only on a proven no).

    A repository whose binding is not a `<checkout>/.git` directory has no
    checkout to hand the producer, and a producer that raises answers
    nothing; both leave every row None, never False."""
    from . import work
    answers = {work.LANE_LANDED: True, work.LANE_UNLANDED: False,
               work.LANE_UNSTARTED: False}
    for gitdir, rows in builds.items():
        bound = str(gitdir or "").rstrip(os.sep)
        root = os.path.dirname(bound) \
            if os.path.basename(bound) == ".git" else None
        lanes = {}
        for lr in rows:
            lane = _build_lane(lr.get("lane"))
            if root and lane:
                lanes.setdefault(lane, []).append(lr)
        if not lanes:
            continue
        try:
            got = work.lanes_landed(root, sorted(lanes), content=False)
        except Exception:               # noqa: BLE001 — unread is None, never no
            continue
        for lane, members in lanes.items():
            verdict = got.get(lane) or {}
            answer = answers.get(verdict.get("state"))
            proof = None if answer is None else verdict.get("proof")
            if verdict.get("state") == work.LANE_UNKNOWN:
                proof = _lane_patch_proof(gitdir, verdict.get("tip"),
                                          (pins or {}).get(gitdir))
                answer = True if proof else None
            for lr in members:
                lr["trunk_contains_tip"] = answer
                lr["trunk_contains_proof"] = proof
                if answer:
                    _unbill_contained(lr)


def _lane_patch_proof(gitdir, tip, pin):
    """PROOF_PATCH_EQUIVALENT when the durable landing-proof ledger holds
    that proof for this lane tip against `pin` (exactly, or carried forward
    over a kept ancestry pair), else None. Spawns nothing.

    PATCH IDENTITY ONLY, AND THE ANCESTOR WORD IS REFUSED ON PURPOSE. The
    producer already asked ancestry and authorship of this tip: a tip it
    found on trunk by object id and still answered UNKNOWN is one whose
    reflog could not say whether the lane authored anything — landed and
    never started look the same there. A kept `ancestor` proof for that tip
    says only what the producer already knew, and taking it as a land is how
    a build sent at trunk read LANDED before anybody wrote a line."""
    return PROOF_PATCH_EQUIVALENT \
        if _kept_landing_proof(gitdir, tip, pin) == PROOF_PATCH_EQUIVALENT \
        else None


def _annotate_frontier_debt(out, current, gitdir=None, proof=None):
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
    `absent` — a claim of PROOF — for a range where merges leave `git cherry`
    unable to account for every commit, and vcs returns UNKNOWN there, which
    is the honest reading. The two also disagreed about a sha whose object is
    not in the repo at all, until the ladder learned what vcs already said: a
    vanished object is not evidence that nothing landed.

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
    proof = proof or _landing_proofs(gitdir)

    def landed(lr, owner=None):
        """Kept as the boolean the walks ask for; the PROOF is the record."""
        return proof(owner if owner is not None else lr, lr) \
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

    pin = getattr(proof, "pin", None)

    def _ancestry_word(owner, carrier):
        """PROOF_ANCESTOR from the FREE per-repo batch, or None for "ask".

        THE GAP THIS CLOSES, and it is one-directional. A kept carrier proof
        is a RELIEF that carries and a WORD that does not: `ancestor` at an
        older trunk is `ancestor` at every descendant, but `patch-equivalent`
        there can be `ancestor` at the descendant, because the move may have
        brought the reviewed OBJECT itself onto trunk (a land that MERGES the
        lane rather than replaying it). Consulting the carry or the oracle
        first lets that older, weaker word shadow a true ancestor on the field
        a reader takes as the proof that discharged the row.
        `_annotate_trunk_containment` has never been reachable by it — it asks
        the same batch first and exits on ANCESTOR — and this is that gate at
        the other door.

        IT IS `_batched_ancestry` AND NOT `_ancestry`, WHICH IS A COST CLAIM.
        `_ancestry` falls back to one `merge-base --is-ancestor` SPAWN PER
        PAIR when the batch cannot answer, and per-carrier spawns are exactly
        the storm the kept-proof ledger exists to remove. The batch is two
        listings per repository that the projection already buys, so this gate
        is paid for; None (no open scope, a short tip, a listing unavailable)
        means the batch CANNOT ANSWER and the carry or the oracle speaks
        unchanged. UNDETERMINED — the vanished-object case — reads the same
        way: never ancestor by default.

        THE PRECONDITIONS ARE `proof_for`'S OWN, IN ITS OWN ORDER, so the gate
        declines wherever the oracle answers with a word about the QUESTION
        rather than about trunk (no-repo, cross-repo, no-tip, no-pin) and
        those words keep reaching the reader unchanged. Matching that order is
        also what keeps the gate free: `pin` is the only leg here that can
        read git, and every carrier that reaches it would have pinned anyway.
        """
        crepo = carrier.get("repo_id") if isinstance(carrier, dict) else None
        orepo = owner.get("repo_id") if isinstance(owner, dict) else None
        if not isinstance(crepo, str) or not crepo:
            return None
        if isinstance(orepo, str) and orepo and orepo != crepo:
            return None
        tip = str(carrier.get("reviewed_tip") or "")
        if not tip or pin is None:
            return None
        trunk = pin(crepo)
        if not trunk:
            return None
        return PROOF_ANCESTOR \
            if _batched_ancestry(crepo, tip, trunk) == ANCESTOR else None

    def best_proof(rid):
        """This row's OWN relief proof, over its carriers, deterministically.

        A relieving carrier wins outright — one proven land discharges the row
        however many other carriers went nowhere. Otherwise the carriers are
        read in sorted id order so two runs over one ledger never disagree.

        ANCESTRY IS ASKED FIRST, PER CARRIER — `_ancestry_word` above: the
        free batch answers before the carry or the oracle, so a carried
        `patch-equivalent` can never shadow a true ancestor at today's trunk.
        """
        cands = sorted(_descend(rid, kids, node,
                                lambda _c, row: _carries(row)))
        owner = out.get(rid) or {}
        seen = []
        for cid in cands:
            carrier = node.get(cid)
            if carrier is None:
                continue
            p = _ancestry_word(owner, carrier) or proof(owner, carrier)
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
        p = best_proof(lr["id"])
        lr["carrier_discharge"] = _PROOF_DISCHARGE.get(p) if p else None
        lr["carrier_discharge_proof"] = p
        if discharged(lr["id"]):
            continue
        for cid in board_heirs(lr["id"]):
            owed.setdefault(cid, []).append((lr["id"], p))
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


def _this_boards_row(lr):
    """Does THIS pipeline owe anything on this row?

    A count is a claim about what this board OWES, and a row the registry
    POSITIVELY places in another project fails it — that is a measured
    contradiction. ABSENCE IS NOT THAT MEASUREMENT: a row with no recorded
    repo (`origin_unknown`) or a repo no project claims
    (`project_unresolved`) answers UNKNOWN, and a board that exiled every
    unknown row would downgrade exactly the legacy rows least able to prove
    themselves (the partial-projection law pins this: a repo binding helm
    cannot read keeps its row on the board). So unknowns STAY — rendered,
    MARKED by `_line`/`card`, counted in `withheld_split`'s disclosed
    UNRESOLVED bucket — while foreign rows leave the default lists with
    their number and the `--all-projects` escape printed where they left.

    VISIBILITY ONLY. This predicate answers what RENDERS; it may never
    authorize a git spawn — that is `_observation_owned`, the STRICT twin
    (a review: reusing this one as git authority let an unresolved row's
    real repository be entered). One question per predicate."""
    return not lr.get("foreign")


def _observation_owned(row):
    """May THIS board run git to observe this row? STRICTLY OWNED.

    NOT the visibility predicate, on the finding of a review (FIX on
    this lane): `_this_boards_row` keeps UNKNOWN-provenance rows VISIBLE
    (`not foreign`), and reusing it as git authority let a
    `project_unresolved` row — a REAL repository path the registry cannot
    assign to any project — reach `_observe`'s git leg, whose
    cannot-resolve-locally path then ran `ls-remote origin` inside that
    repository (`_vanished_proof`) with whatever credentials are ambient.
    Rendering a row is a display choice; running git inside its repository
    is an authority claim, and authority takes the STRICT answer: a row
    carrying a repo this board cannot positively call its own gets NO git
    leg — its git-derived fields render UNKNOWN/unmeasured (`_UNOBSERVED`,
    and `_base_behind` is gated on `observable`, so it inherits the
    refusal) while the row stays visible and marked.

    `origin_unknown` rows pass, and that is not a hole: they carry NO
    repository path, and every git door refuses a falsy or non-string
    gitdir before any spawn (`_git`, `_git_observe`) — there is no
    repository the leg could enter — while their landed receipt wording
    (`payload binding unavailable`, pinned by the repo-less legacy test)
    depends on the flag staying up."""
    return not (row.get("foreign") or row.get("project_unresolved"))


def _loop_rows(lrs, raw, include_landed=False, all_projects=False):
    """Rows for the primary board: carrying chain frontiers by default.

    A superseded predecessor may remain canonically open because no close event
    was minted for it, while an absorbing descendant already owns its debt.
    Showing both as in-flight double-bills one chain. `--all` stays historical
    and preserves every row; the default view folds only validated topology.

    `all_projects=True` is the ESCAPE (task/974): the same list without the
    project-scope withholding, every foreign row still carrying its project
    label so a reader can tell whose it is. It widens ONLY the listing —
    never the git-observation gate (`git=` stays on `_observation_owned`),
    because rendering another project's rows is a display choice and running
    git inside its repository is not.
    """
    if include_landed:
        return sorted(lrs.values(), key=_order)
    kids, node, err = _chain_forest(raw, lrs)
    if err:
        raise _ChainUntrustworthy(err)
    # FOREIGN ROWS LEAVE THE BOARD, AND THE BOARD SAYS SO. This list is what
    # the owner reads as HIS in-flight work, and a row another project
    # positively owns is not that, whatever its state. The count of what
    # left renders beside the list (`withheld_split`) — a number and an
    # escape, never an absence. UNKNOWN-provenance rows stay (see
    # _this_boards_row): absence is not foreignness.
    vals = [lr for lr in lrs.values()
            if not lr["terminal"]
            and (all_projects or _this_boards_row(lr))
            and not _relieved(lr["id"], kids, node)]
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


def open_bucket_rows(lrs):
    """The rows `filed_split` counts as `open` — ONE owner for the population.

    The header renders the frontier split of this bucket and
    `helm lr retire --off-frontier` walks it, so the number the owner reads
    and the number the verb offers to clear are ONE number by construction.
    Two walks is how "in flight" came to name two numbers on one page
    (task/324), and that lesson is younger than this bucket.

    A REVIEWED row is HELD, not open — its obligation is a polarity decision
    nobody has taken, which is a different debt from a lane that vanished —
    and an UNDERIVED row was never measured against trunk at all, so it is not
    classifiable yet. Both are deliberately outside this population and both
    keep their own term in the header."""
    return [lr for lr in lrs.values()
            if not lr["terminal"]
            and lr.get("observe_why") != OBSERVE_UNDERIVED
            and lr["state"] != "REVIEWED"]


def _routed_verdict(lr, verdict, lrs):
    """The placed verdict PLUS the door its polarity chooses.

    THE ROUTE IS PART OF THE CLASSIFICATION, not a second pass the verb runs
    afterwards — because the polarity rung can REFUSE (an unreadable chain is
    not a non-contrary one), and a refusal that only the verb could see would
    put the header's residue and the verb's plan back into disagreement one
    rung lower than the disagreement this whole split exists to end.

    The polarity is the CHAIN's when the row's own field is silent: a row can
    be carrying a contrary declared on a chained round, and routing that as a
    resolution would propose a landing word over debt somebody is owed."""
    polarity, _via, perr = _chain_polarity(lr, lrs)
    if perr:
        return dict(verdict, reason=OFF_FRONTIER_UNCLASSIFIED,
                    rung="polarity", evidence=perr)
    return dict(verdict, polarity=polarity,
                close_reason=off_frontier_route(verdict["reason"], polarity))


def frontier_world(raw):
    """(rows, carriers) — the reads a DOOR'S GATE needs, or None.

    FROM THE PROJECTION'S OWN SNAPSHOT, never a second ledger read. `raw` is
    the canonical instant `project_raw` already returned beside the rows it
    classified, so the question "would this door close this row" is asked
    about the same ledger instant that decided the row is residue at all. A
    second `snapshot_with_verdicts()` here would cost 1.8s on the live board
    AND would let the classification and the gate describe two instants —
    exactly the defect `project_raw`'s own docstring records one layer down.

    `_carriers` is derived ONCE per walk for the same reason the ref table
    is: it is a function of the whole ledger, and the door's gate wants it
    per row."""
    from . import rowworld              # DEFERRED — rowworld imports us.
    if not isinstance(raw, dict) or not raw:
        return None
    return raw, rowworld._carriers(raw)


def off_frontier_closable(lr, verdict, world=None, lrs=None):
    """(True, None) | (False, why) — would the door this row routed to CLOSE
    it now? The authorizing gate's own answer, taken WITHOUT writing.

    THE NUMBER THE OWNER ACTS ON IS THIS ONE, not the classification's. The
    census can prove a row is residue — the lane is gone and git can place
    the tip — and the door it routes to can still refuse, because a close
    demands a PROOF and a placement is not one. MEASURED over the whole
    placed population of a copy of the live ledger: of 316 rows the
    classification placed, `carried`'s authorizing witness affirmed 122 and
    refused 182, so a surface printing 316 as "closable now" was wrong about
    58% of the number it asked the owner to act on. The refusals are not
    noise and they are not a bug in the door: an exact-ancestor tip leaves
    `git cherry`'s range EMPTY, and an empty range affirms every possible
    trunk, so the whole landed-by-ancestry bucket is unprovable by that
    witness and fails closed by construction.

    IT ASKS THE DOOR, IT DOES NOT MODEL THE DOOR. The `carried` and
    `withdrawn` ladders are called in DRY RUN — the same functions `--apply`
    reaches through `close`, with the same evidence — so a rung added to
    either ladder tomorrow moves this number without anybody editing it. A
    preflight that re-derived the gate's conditions would be a second, weaker
    copy of the proof, and the weakest copy would define it.

    IT UNDER-COUNTS AND NEVER OVER-COUNTS, which is the direction a promise
    to the owner must fail in. A door with no preflight here answers FALSE
    with its name, so a route added without one shows up as work the verb
    will not do rather than as a promise it cannot keep.

    THE COST IS MEASURED AND IT IS NOT SMALL. One board read on a copy of the
    live ledger went from 3.1s to 96.3s for 1147 rows. The board absorbs it:
    /api/lr serves stale-while-revalidating against a 600s truth cap sized for
    a ~250s rebuild, and `helm lr list` reads that warm body in milliseconds,
    so only a cold replay pays.

    THE REPLAY WITNESS IS WHERE THE BILL IS, and naming the wrong one sends
    the next reader to a cure that buys nothing. `rowworld._reached_trunk`
    asks `merge-base --is-ancestor` first and returns on it, so its `cherry`
    is cheap and a patch index in front of it changes almost no time.
    Measured over one warm rebuild of this board: `carriage_proof` answered
    227 rows in 32.9s, of which the REPLAY witness (`rowworld._carriage` ->
    `_replay_is_a_noop`) was 28.8s. The bill is one `merge-tree --write-tree`
    per row, one replay of the row's own work onto trunk — which is exactly
    the answer the note above `dispatches._carriage_trunk_sha` refuses to
    store, because git resolves it through merge machinery that moves while
    every object id stands still.

    THAT BILL IS SPLIT ACROSS TWO PHASES AND ONLY HALF OF IT IS THIS
    CENSUS's, which a count taken over a whole projection cannot show. Per
    phase, on one projection over a fresh copy of the live ledger: this census
    under `filed_split` spawned 141 `merge-tree` calls for 138 DISTINCT
    questions, 19.7s; `project_raw` spawned 170 for 84 distinct, 19.7s,
    through `rowworld._carriage_by_row` inside the World build. The two
    question sets are DISJOINT — this census asks about the rows whose lane is
    gone, which is not the population that pass holds — so neither phase
    answers the other's question and a per-walk memo of the relation collapses
    nothing here. `merge-tree` is also absent from `vcs._READ_VERBS`, so no
    projection memo was ever collapsing them.

    AND THERE IS NO ROW TO DROP THE QUESTION FOR, which is the cure a reader
    reaches for first and the one this measurement refuses. Every placed row
    on this board routes to a door that HAS a preflight (227 `carried`, one
    `withdrawn`), and every refusal it returns is the carriage refusal itself
    — so "rows that cannot be closed anyway" is an EMPTY SET at this door
    rather than a cheap filter in front of the witness.

    THE TWO FAMILIES PARTITION BY THE CLASSIFICATION THAT ROUTED THE ROW, so
    the row paying for both is not paying twice for one answer.
    `landed-by-ancestry` leaves `_reached_trunk` an empty range by its own
    short-circuit, so the replay is the only family that can affirm one: 114
    of the 141 payers, and it affirmed exactly one of them.
    `landed-by-patch-id` is what `_reached_trunk` answers — 9 of the 11 rows
    this census offers — and the replay affirmed NONE of its 27 payers.
    Dropping the replay for the ancestry class would therefore take the offer
    away from a row this census offers today, and that class is five sixths of
    the bill.

    NOR DOES THE CHEAP CONTENT TWIN PREDICT THE EXPENSIVE ONE.
    `rowworld._postimages_at_head` costs about 5ms against the replay's 140ms
    and answered False for all 141 payers, the affirming one included: trunk
    had since edited one of that row's three touched paths, and the three-way
    merge still reproduced trunk exactly, which is the carried-THEN-edited
    state the replay exists to see. A filter on the cheap witness would drop
    the only row the expensive one buys.

    SO THE SHAPE TAKEN IS THE ONE `_carriage_trunk_sha`'S NOTE ALREADY NAMES,
    and `frontier_verdicts` is where this walk declares it: the answer
    re-derived under the fold checkpoint's re-verification, keyed on the code's
    own generation and re-verified per repository over every object and ref
    expression the witness read plus the merge machinery's fingerprint
    (`helm/carriageckpt.py` through `helm/foldckpt.py`, whose plan already
    admits exactly these questions). Not a memo of the answer keyed on ids,
    which that note refuses — the trunk OBJECT is in the key, so a land is a
    different question and not a stale entry, and a projection over an
    unchanged repository pays the witness nothing."""
    door = verdict.get("close_reason")
    evidence = _off_frontier_evidence({"close_reason": door}, verdict)
    evidence, err = dispatches._clean(evidence, "close evidence", 256)
    if err:
        return False, err
    if door == "carried":
        out, err = _close_ladder_carried(lr, evidence, None,
                                         verdict.get("trunk"), True, lrs,
                                         world=world)
    elif door == "withdrawn":
        out, err = _close_ladder_withdrawn(lr, evidence, True, lrs)
    else:
        # NOT AN ASSUMPTION ABOUT THE DOOR, A STATEMENT ABOUT THIS FUNCTION.
        # `landed` is the only other routed door and it carries no population
        # at all (a non-contrary row whose tip reached trunk is TERMINAL
        # before this verb sees it, which its own arm asserts). If one ever
        # arrives, it is counted as work the verb will NOT do until somebody
        # wires its ladder here.
        return False, ("--reason %s has no preflight on this census path, so "
                       "this row is not offered as closable" % (door or "none"))
    if err:
        return False, err
    return bool(out), (None if out else "the door answered nothing at all")


def frontier_verdicts(rows, lrs=None, world=None):
    """{row id: off_frontier_reason(...)} — ONE classification, read once.

    THE HEADER AND THE VERB READ THE SAME FUNCTION ON THE SAME WALK, which is
    the whole of this. The first cut had the header ask `lane_branch_presence`
    and the verb ask `off_frontier_reason`, so the strip printed 541
    OFF-FRONTIER "not work owed" and named a verb that reported 286 — the
    header was counting every row whose LANE was gone, including 255 the verb
    refuses to place, and telling the owner they were debris. A number the
    owner acts on and the population the named command acts on must be one
    reading or the line is a promise the verb does not keep.

    THE CACHE IS PER REPOSITORY AND THE ANSWER IS PER ROW. Measured on the
    live board, 1314 of the open rows name ONE repository, so asking git per
    row is 1314 spawns for six distinct answers.

    A repository whose readings could not be taken yields UNCLASSIFIED for
    every row in it — never a retirement reason, which would clear a whole
    repository's rows off one failed spawn.

    `world` ADDS THE DOOR'S OWN ANSWER TO THE SAME WALK, and it is here
    rather than in the verb for the same reason the ROUTE is: a reading only
    one surface takes is a reading the two surfaces will disagree about. With
    it, every placed row carries `closable` — would its door close it NOW —
    and `closable_why` when it would not. Without it the classification is
    exactly what it was, and every caller that prints a `closable` number
    passes one.

    THE WALK IS ONE INSTANT, AND UNTIL THIS IT WAS NOT. `filed_split` runs
    AFTER `project_raw` has returned, so every git question below stood
    OUTSIDE any `projscope.scope()` — and outside a scope both of helm's git
    memos are, by their own contract, byte-identical to no memo at all. So the
    two seams this walk asks through (`landreq._git` and `vcs.GitVcs.run`)
    were inert here, and neither the header nor the verb could tell.
    MEASURED on one cold `helm lr list`, 334 classified rows:
    `rev-parse --verify --quiet <trunk>^{commit}` 339 calls for FOUR distinct
    questions, `show-ref --verify --hash refs/remotes/origin/main` 307 calls
    for ONE, `rev-parse --is-shallow-repository` 265 for ONE — 2258 of 4089
    git spawns were a question this walk had already asked and thrown away.

    IT IS A CORRECTNESS FIX FIRST, exactly as `projscope`'s own docstring
    argues one module over: 334 rows asking "what commit is trunk" 900 times
    across a minute-long walk can be told two different things by a trunk that
    moved, and the header would then publish two instants under one read
    stamp. One scope makes the classification describe the instant the rows
    were projected at.

    A NESTED SCOPE SHARES THE OUTERMOST CACHE AND ONLY THE OUTERMOST CLEARS
    (`projscope.scope`), so a caller that already holds one — the web read
    path does — loses nothing and gains no second lifetime.

    AND THE SCOPE ARMS A BUDGET THAT WAS NEVER IN FORCE HERE, WHICH IS WHY
    THIS WALK SEEDS ITS OWN. `_derive_expired` reads its deadline through
    `projscope.memo`, and that function's own docstring says what happens
    outside a scope: "the memo computes fresh every call, so the deadline is
    always in the future and this is always False". This walk WAS outside a
    scope, so it has never had a derive bound at all — opening one silently
    charged it `_DERIVE_BUDGET_S`, 4.0s, a number sized for a 10s inject turn.
    MEASURED on one snapshot, 1244 rows classified twice: 17 rows flipped from
    `abandoned-unreachable` to `unclassified`, because `_landing_proof`'s
    absence leg answers `unknown` on a spent budget and an unknown never
    places a row. That is the retirable population shrinking and the
    work-owed count growing, from a cure that was supposed to change no
    answer. So the seed is `BOARD_DERIVE_BUDGET_S` — the same number
    `web_land_model` already seeds for the board's own read — which keeps
    today's answer and replaces "unbounded" with a ceiling. THE CEILING IS
    ABOVE THE BOARD'S 30-SECOND REBUILD FLOOR ON PURPOSE: lowering it to the
    floor would change the rendered counts, and what a board should print
    when its own census cannot finish inside the floor is the owner's
    decision, not a side effect of a memo fix. `arm_derive_budget` is
    first-seeder-wins, so a caller that already seeded one — the web path —
    keeps its own."""
    cache = frontier_cache()
    out = {}
    with projscope.scope():
        arm_derive_budget(BOARD_DERIVE_BUDGET_S)
        # THE CENSUS IS A READER, AND THIS IS WHERE IT SAYS SO. The `carried`
        # door's replay witness is the whole of this walk's git bill — 141
        # `merge-tree --write-tree` spawns for 138 distinct questions, 19.7s
        # of one board read — and every one of those answers had been derived
        # by the projection before it against the same three objects. Inside
        # this region `carriageckpt` serves the answer only after it re-asks
        # git every question the witness put to it and re-fingerprints the
        # merge machinery, per repository, in ONE batch-check; a land moves
        # trunk to a different object, which is a different key rather than a
        # stale entry. It is declared HERE and nowhere else on purpose: the
        # close writer and the ladder that authorizes one open no region and
        # derive live, so nothing this store holds can reach a write.
        with carriageckpt.derived():
            for lr in rows:
                # OWNERSHIP IS ASKED BEFORE THE SPAWN, not after it.
                # `for-each-ref` inside another project's repository is an
                # authority claim helm does not hold
                # (`_observation_owned`), and a cache that fills itself first
                # and checks later has already made it.
                owned = _observation_owned(lr)
                table = ref_tables(lr, cache["tables"]) if owned else None
                verdict = off_frontier_reason(lr, table=table, cache=cache)
                placed = verdict["reason"] in OFF_FRONTIER_REASONS
                if placed:
                    verdict = _routed_verdict(lr, verdict, lrs)
                # THE ROUTE RUNG CAN STILL REFUSE — an unreadable chain
                # polarity leaves the row UNCLASSIFIED — so the gate is asked
                # about the routed verdict and never about the pre-route one.
                if world is not None \
                        and verdict["reason"] in OFF_FRONTIER_REASONS:
                    closable, why = off_frontier_closable(
                        lr, verdict, world=world, lrs=lrs)
                    verdict = dict(verdict, closable=closable,
                                   closable_why=why)
                out[lr["id"]] = verdict
    return out


def ref_tables(lr, cache):
    """This row's repository ref table, read at most once per repository.

    `cache` is the caller's own dict, so the lifetime of the reading is the
    caller's walk and never longer — a board read twice in one process asks
    git twice, because between the two reads a lane may have been cut or
    reaped and a cached answer about that is the one thing this probe must
    never give.

    A FAILED read is cached as `(None, err)` exactly like a successful one.
    Re-spawning git per row against a repository that already refused is the
    whole cost this exists to avoid, and repeating the refusal cannot turn
    `unknown` into an answer."""
    gitdir = lr.get("repo_id")
    if not isinstance(gitdir, str) or not gitdir:
        return None, "the row names no repository"
    if gitdir not in cache:
        cache[gitdir] = ref_table(gitdir)
    return cache[gitdir]


def census_verdicts(lrs, raw):
    """{row id: off_frontier_reason(...)} for exactly the rows `filed_split`
    classifies — the open bucket, this board's own rows — with each placed
    row's door asked, on one walk.

    THE BOARD READS THIS ONE WALK THREE TIMES and pays for it once: the
    header's split (`filed_split`), the scheduler's collapsed lines and the
    kanban's, which read each card's `frontier` word. Before, only the split
    kept the answer and the per-row verdicts were thrown away, so the one
    surface that needed them — the owner's list — had no way to say which
    rows are left over without a second walk, and a second walk is a second
    instant free to disagree with the strip beside it."""
    return frontier_verdicts([lr for lr in open_bucket_rows(lrs)
                              if _this_boards_row(lr)], lrs=lrs,
                             world=frontier_world(raw))


def filed_split(lrs, raw, verdicts=None):
    """The all-time FILED population behind the board, partitioned. ONE owner
    for the derivation: /api/lr's card strip and `helm lr list`'s headers both
    render this split, and two walks would let the two surfaces disagree about
    one record (the owner's ask, and a review's finding that the
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
    ref-less legacy rows).

    `underived` IS THE BUCKET THAT MAKES THE OTHERS HONEST, and it is drawn
    out of `open` and `held` rather than added beside them. A non-terminal row
    whose landedness was never DERIVED is not work in progress and not work
    parked -- it is a question nobody asked, and counting it as either makes
    the board report a backlog that is partly its own unfinished reading.
    IT KEYS ON THE CAUSE, NOT ON `observable`, and that distinction is the
    whole correctness of the bucket. `observable` False has four causes and
    only one of them is a failure to finish: helm reads NO trunk for a row
    with no verdict yet (`_will_observe_git`), because nothing could have
    landed, so those rows are unobservable while being ordinary open work.
    Keying on the flag relabels every pre-verdict row as "helm did not look",
    which is the same lie this bucket exists to end, pointing the other way.
    MEASURED on the live ledger the day this was written, the causes separate
    cleanly: 597 underived (REVIEWED 574, CHANGES_REQUESTED 19, READY 4), 6
    not-asked (OPEN 1, AWAITING_REVIEW 3, AWAITING_BUILD 2), 28
    no-trunk-or-tip. Every underived row sits in a post-verdict state where
    landing is possible; every not-asked row is pre-verdict.

    THE COUNT IS AN UPPER BOUND WHILE THIS BUCKET IS NON-EMPTY, in one
    direction only. The proof ledger is POSITIVE-ONLY (see KEPT_PROOFS), so an
    underived row can only turn out to be LANDED, never the reverse -- the
    board over-reports and never under-reports. Reading the same store twice
    shrinks it: measured, eight consecutive cold reads went 604, 519, 472,
    434, 370, 300, 284, 276 land loops while the ledger gained three rows.

    THAT SEQUENCE IS A WITHIN-TRUNK READING, and what makes it carry across a
    land is `_teach_trunk_pairs`. The proof ledger keys on trunk, and
    `_kept_landing_proof` returns a proof taken against an older trunk t
    whenever t is an ancestor of the ref -- correct under ff-only. It READS
    that ancestry hop from the durable pair ledger rather than computing it,
    to keep a git spawn off the free path, and for a long time nothing wrote
    trunk-to-trunk pairs, so the carry-forward could not hit and every
    fast-forward retired the whole warm-up. The writer now teaches those
    pairs where a pass first learns the trunk, under its own budget, so
    coverage is partial on the pass after a land and converges. The header
    still promises only that re-reading lowers the count, never that it
    converges, because a bounded teach is not a guarantee about any one read."""
    filed = {"total": len(raw), "open": 0, "held": 0, "landed": 0,
             "closed": 0, "non_loop": len(raw) - len(lrs), "underived": 0,
             "open_frontier": 0, "off_frontier": 0, "unclassified": 0,
             "closable": 0, "not_closable": 0}
    open_rows = open_bucket_rows(lrs)
    open_ids = {lr["id"] for lr in open_rows}
    # THE CLASSIFICATION IS FOR THE ROWS THIS BOARD'S VERB WILL ACT ON.
    # `helm lr retire --off-frontier` withholds other projects' rows by
    # default (task/974's law, `_this_boards_row`), so classifying them here
    # would put the header back OVER the verb by exactly the rows the verb
    # declines to touch — measured on a copy of the live ledger, 283 residue
    # against the verb's 275. An unclassified foreign row counts as frontier,
    # the safe direction, and the withheld line beside this one is where those
    # rows are disclosed by name and number.
    # AND THE DOOR'S OWN ANSWER RIDES THE SAME WALK. The placed count is not
    # the clearable count — the authorizing gate refuses 58% of this
    # population — so `frontier_world` hands the classification the ledger
    # instant it was already holding, and every placed row comes back carrying
    # whether its door would close it.
    # `verdicts` IS `census_verdicts` of this same projection when the caller
    # already walked it (the web build does, to stamp each card); omitted,
    # the walk is taken here exactly as before.
    if verdicts is None:
        verdicts = census_verdicts(lrs, raw)
    for lr in lrs.values():
        if not lr["terminal"]:
            # ASKED AND COULD NOT FINISH IS NOT OUTSTANDING WORK, and it is
            # the only cause that belongs here. Every layer below reports it
            # honestly: `_landing_proof` answers "unknown" on an expired
            # derive budget and its own comment says "never ABSENT, because
            # running out of time is not evidence the patch is missing";
            # `landed_ever` turns that into None; `_git_observe` returns blind
            # NAMING that exit. The honesty survived five layers and died
            # here, where an unanswered row was counted as open or held and
            # became part of a backlog the fleet then staffed against.
            #
            # OBSERVE_NOT_ASKED IS DELIBERATELY ABSENT from this test. A row
            # with no verdict has nothing that could have landed, so helm
            # never asks -- it is unobservable and it is genuinely open.
            # THE `open` TEST IS `open_bucket_rows`' OWN MEMBERSHIP, not a
            # second copy of its predicate. The verb that offers to clear this
            # bucket walks that function, and two expressions of one rule is
            # how the header and the verb would come to disagree about which
            # rows they are talking about — the exact drift `filed_split` and
            # `inflight_rows` each exist to prevent one bucket over.
            if lr["id"] in open_ids:
                filed["open"] += 1
                # THE OWNER'S NUMBER, SPLIT WHERE IT IS COUNTED. `open` stays
                # exactly what it was — the six-bucket partition still sums to
                # `total`, and every consumer that reads it is untouched — and
                # these two terms partition IT: 1261 rows under one word were
                # read as 1261 queued improvements when most of them were
                # ledger debris whose lane no longer exists (task/2381).
                # THE RESIDUE IS WHAT THE VERB WILL ACT ON, nothing wider.
                # `off_frontier_reason` answers `unclassified` for every
                # reading that did not happen and for every tip it could not
                # place, and `--apply` never touches one — so counting those
                # as residue would print "not work owed" about rows no verb
                # will ever clear. They are counted as FRONTIER and disclosed
                # by name: work owed is the direction that cannot under-report
                # a backlog to the owner.
                # AND THE RESIDUE IS WHAT *THIS BOARD'S* VERB WILL ACT ON.
                # `helm lr retire --off-frontier` withholds other projects'
                # rows by default (task/974's law, `_this_boards_row`), so a
                # foreign row counted here would put the header back over the
                # verb by exactly the rows the verb declines to touch —
                # measured on the live ledger, 283 against 275. A foreign row
                # counts as frontier, the safe direction, and the withheld
                # line beside this one is where it is disclosed by name.
                verdict = verdicts.get(lr["id"]) or {}
                reason = verdict.get("reason")
                if reason in OFF_FRONTIER_REASONS:
                    filed["off_frontier"] += 1
                    # AND THE RESIDUE ITSELF IS TWO POPULATIONS. One number
                    # for "helm placed this row" and "the verb can close it"
                    # was the same conflation one level in: the door's gate
                    # refuses a placed row whose proof it cannot take, and
                    # the owner was reading the placement as the clearance.
                    if verdict.get("closable"):
                        filed["closable"] += 1
                    else:
                        filed["not_closable"] += 1
                else:
                    filed["open_frontier"] += 1
                    if reason == OFF_FRONTIER_UNCLASSIFIED:
                        filed["unclassified"] += 1
            elif lr.get("observe_why") == OBSERVE_UNDERIVED:
                filed["underived"] += 1
            else:
                filed["held"] += 1
        elif lr.get("retired_admin"):
            # AN ADMINISTRATIVE RETIREMENT IS NEVER A LANDING, and the `or`
            # above is exactly where it would have become one: a retired row
            # whose tip Git happens to observe on trunk satisfies
            # `lr["landed"]` and would have been counted into the strip the
            # owner reads as "work that shipped". The retirement makes NO
            # claim about the work — that is its whole definition — so it
            # cannot lend one to a counter. The git observation is NOT
            # discarded: the row keeps `landed` and `land_state`, and
            # `lr show` still reports them; only the CLASSIFICATION of how
            # this row was retired refuses to say "by landing".
            filed["closed"] += 1
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
    to fix it WITHIN one. The predicate is `honored_display`,
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
    # A BUCKET THE BODY DOES NOT CARRY IS SAID, NOT CRASHED AND NOT ZEROED.
    # A WARM body minted by a server older than a bucket has no key for it,
    # and this formatter is on the read path for exactly those: `%(key)d`
    # raised KeyError and took the whole listing down. Rendering 0 would be
    # worse than the crash -- it is a confident claim about a number the
    # sender never sent. The card's JS already answers "?" for the same case
    # (FILED_WAS / fterm), so this is the same law on the python surface.
    def _term(key):
        value = filed.get(key)
        return str(value) if isinstance(value, int) else "?"
    # THE BARE `open` COUNT IS GONE FROM THE HEAD OF THIS LINE (task/2381).
    # It printed one number for two populations: work the owner is owed, and
    # rows whose lane branch no longer exists in any repository — landed under
    # a later tip, or abandoned and reaped. He read the second as the first.
    # The frontier count leads because it is the only one that is work; the
    # residue follows, named, with the verb that clears it, so the number a
    # reader acts on and the command that acts are on one line.
    #
    # A BODY WITH NO FRONTIER TERMS PRINTS THE OLD WORDING, NOT A `?`. This
    # formatter is on the read path for WARM bodies a server older than the
    # split minted, and the same version skew that made `in_flight` render as
    # "? open" one rename ago would make `open` render as "? open on the live
    # frontier" here — which reads as a broken payload and sends the reader to
    # debug the wrong thing. The old server's `open` is a real, honest number;
    # it is only the SPLIT of it that is missing, so the honest rendering for
    # that body is the unsplit sentence, which states exactly what it knows.
    if isinstance(filed.get("open_frontier"), int):
        lead = "%s open on the live frontier" % _term("open_frontier")
        # THE UNPLACEABLE ROWS ARE INSIDE THIS NUMBER AND ARE SAID SO. They
        # are counted as work owed (the safe direction) but they are not
        # ordinary queued work: their lane is gone and helm could not place
        # the tip, so nobody is moving them and no verb will clear them. A
        # count folded in silently would be a backlog the owner cannot act on
        # and cannot see; the parenthetical is the disclosure, and the six
        # buckets still sum to `total` because this is a subset of one of
        # them.
        unclassified = filed.get("unclassified")
        if isinstance(unclassified, int) and unclassified > 0:
            lead += (" (incl. %d unclassified — lane gone, tip not placeable, "
                     "counted as work owed)" % unclassified)
    else:
        lead = "%s open" % _term("open")
    return ("filed %s all-time · %s · %s held · "
            "%s underived · %s landed · %s closed · %s non-loop%s"
            % (_term("total"), lead, _term("held"),
               _term("underived"), _term("landed"), _term("closed"),
               _term("non_loop"), off_frontier_line(filed)))


def off_frontier_line(filed):
    """The residue term, or NOTHING when there is none to report.

    SILENT ON ZERO AND SILENT ON ABSENT, for two different reasons that land
    on the same output. A board with no residue should not carry a term about
    debris it does not have; and a WARM body minted by a server older than
    this split has no key at all, where naming a `?` residue and a command to
    clear it would send a reader after rows nobody counted.

    TWO NUMBERS, BECAUSE THE RESIDUE IS TWO POPULATIONS. "Placed" is the
    classification's answer — the lane is gone and git can say where the work
    went — and it is NOT the same fact as "the close ladder will take it".
    MEASURED over 316 placed rows on a copy of the live ledger: the `carried`
    door's authorizing witness affirmed 122 and refused 182 (every
    landed-by-ancestry row among them, because an exact-ancestor tip leaves
    `git cherry`'s range empty and an empty range affirms every possible
    trunk). One number covering both facts is wrong about 58% of the
    population it calls clearable. The owner reads totals,
    so both totals are HERE rather than one line deeper in a verb he has to
    run — and the per-row refusal stays in the verb, which is where a reason
    that differs per row belongs.

    A BODY THAT CARRIES NO SPLIT MAKES NO CLEARANCE CLAIM AT ALL. The same
    version-skew law the rest of this strip follows: an older server sends
    the residue and no `closable` key, and inventing a zero there would say
    "nothing can be cleared" about a board nobody measured. It names the
    census verb instead, which is true whatever the split turns out to be."""
    residue = filed.get("off_frontier")
    if not isinstance(residue, int) or residue <= 0:
        return ""
    # THE COUNT IS THE VERB'S OWN. `frontier_verdicts` classified these rows
    # and only the rows it PLACED are counted here, so the population the
    # owner reads is the population the census walks — not a wider "the lane
    # is gone" set the verb would then decline to touch.
    head = (" · %d OFF-FRONTIER (lane gone AND the work placed — not work "
            "owed)" % residue)
    closable, refused = filed.get("closable"), filed.get("not_closable")
    if not isinstance(closable, int) or not isinstance(refused, int):
        return head + " · helm lr retire --off-frontier censuses them"
    if closable:
        head += (" · %d closable now: helm lr retire --off-frontier --apply "
                 "closes these %d" % (closable, closable))
    if refused:
        # NO APOSTROPHE IN THIS SENTENCE, and that is a constraint rather than
        # a style: the card renders the identical string through `esc()`, so a
        # `'` becomes `&#39;` and the two surfaces stop being byte-identical —
        # which is the parity arm's whole subject.
        head += (" · %d placed but NOT closable yet — the close ladder "
                 "cannot take the witness these rows need (helm lr retire "
                 "--off-frontier names the refusal per row)" % refused)
    return head


def withheld_split(lrs, scope):
    """The project-scope DISCLOSURE record, ONE owner for the derivation —
    `helm lr list`'s header and the card's strip (/api/lr `withheld`) both
    render this, for the same reason `filed_split` has one owner: two walks
    would let two surfaces disagree about one record.

    NO SILENT TRUNCATION (task/974): the default board withholds rows owned
    by other projects, and this is where the owner learns THAT it did, HOW
    MANY left, WHOSE they are, and the escape that shows them. Counted over
    the marks `_mark_foreign_rows` stamped on this same projection — never a
    second classification — so the count and the withholding cannot drift.
    Foreign rows are named per project; rows whose project cannot be resolved
    (no repo recorded, or a repo the registry cannot place) are the
    UNRESOLVED bucket, disclosed, never guessed into either side."""
    out = {"scope": (scope or {}).get("project"),
           "scope_repo": (scope or {}).get("repo_id"),
           "scope_why": (scope or {}).get("why"),
           "foreign": 0, "unresolved": 0, "by_project": {}}
    for lr in lrs.values():
        if lr.get("foreign"):
            out["foreign"] += 1
            name = str(lr.get("foreign_project")
                       or lr.get("foreign_repo") or "?")
            out["by_project"][name] = out["by_project"].get(name, 0) + 1
        elif lr.get("origin_unknown") or lr.get("project_unresolved"):
            out["unresolved"] += 1
    return out


def withheld_line(withheld, all_projects=False):
    """The disclosure string, one shape on every surface (filed_line's twin).

    Three honest postures, never a silent one:
    - scope resolved: the scope name, the withheld numbers per project, the
      unresolved bucket, and the escape;
    - `--all-projects`: the same numbers, now SHOWN and labeled, said so;
    - scope UNRESOLVED: says WHY and that the board is wide — a projection
      that cannot resolve scope says so rather than rendering a confident
      narrower board."""
    if not isinstance(withheld, dict):
        return ("project scope UNKNOWN — this body carries no withheld "
                "reading (an older `helm web` built it)")
    if not withheld.get("scope_repo"):
        return ("project scope UNRESOLVED (%s) — showing EVERY project's "
                "rows" % (withheld.get("scope_why")
                          or "no repository identity"))
    who = withheld.get("scope") or (
        "repository %s (registered to no project)" % withheld["scope_repo"])
    n, m = withheld.get("foreign") or 0, withheld.get("unresolved") or 0
    verb = "shown labeled" if all_projects else "withheld"
    parts = ["project scope: %s%s" % (who,
                                      " — ALL PROJECTS" if all_projects else "")]
    if n:
        by = ", ".join("%s %d" % (k, v) for k, v in
                       sorted((withheld.get("by_project") or {}).items()))
        parts.append("%d row%s from other projects %s%s"
                     % (n, "s"[:n != 1], verb, " (%s)" % by if by else ""))
    else:
        parts.append("0 rows from other projects")
    if m:
        # UNRESOLVED rows are SHOWN, marked — absence of provenance is not
        # foreignness (see _this_boards_row) — so this term is a census of
        # marks on the board above, not of rows removed from it.
        parts.append("%d row%s of UNRESOLVED project shown, marked"
                     % (m, "s"[:m != 1]))
    if n and not all_projects:
        parts.append("--all-projects shows them")
    return " · ".join(parts)


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

    A refutation of 5554ad1e by topology rather than by reading: asking only
    for the PROJECTED STATE to be LANDED/SUPERSEDED is not enough, because a
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
    if kid.get("retired_admin"):
        # ADMINISTRATIVE RETIREMENT CARRIES NOTHING FORWARD, exactly like
        # ABANDONED and for the same reason: it is helm giving up on READING
        # the row, never the row handing its work to a successor. Stated
        # rather than left to fall out of the state tuple below, because a
        # later edit to `_DEBT_ABSORBING` must not be able to quietly make a
        # retirement discharge its parent's debt. The git legs above still
        # run first and still win: if the change is OBSERVED on trunk, that
        # is a measurement and it absorbs, whatever the row's terminal says.
        return False
    # Rows that reached a terminal STATE without a recorded close reason —
    # legacy shapes the ledger never retro-fits — still read honestly here.
    return kid["state"] in _DEBT_ABSORBING




class _ChainUntrustworthy(Exception):
    """The supersedes graph cannot be trusted, so NO classifier answer is
    honest. Carried as an exception rather than a per-row flag because the
    surface is all-or-nothing: handing a caller some rows AND an unavailable
    notice is two answers to one question."""


def _effective_root(row):
    """This endpoint's PROVEN chain identity on an edge, or None.

    THE TWO ENDS ARE NOT SYMMETRIC, and treating them alike is the regression
    a review caught in 4d55b7c: I refused any edge whose parent lacked a root,
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
    The asymmetry a review named is REAL, but it is not a property of the
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
    nearest unresolved ancestor keep its debt.

    A CONTRARY ROW TAKES ONLY LANDED RELIEF. `contrary` is not debt, it is an
    ALARM: changes were requested and the change reached trunk ANYWAY. A live
    successor absorbs ordinary debt because somebody is demonstrably holding
    the work — but nothing about holding work un-lands what already landed, so
    the ordinary rule suppresses a standing safety alarm in exchange for an
    intention. Only a descendant that actually LANDED resolves the condition
    the alarm is about, so only that relieves it.

    THE GAP WAS LATENT AND IS NOW UNIVERSAL. Any hand-dispatched successor to a
    contrary row already erased its alarm from `lr list` and the owner board
    (`--all` and `show` kept it, which is exactly how it stayed unnoticed).
    Minting the author's obligation on every FIX makes that successor exist
    every time, so the every-time behaviour is what forced the reading. Same
    shape as the ABANDONED carve-out in `_successor_owning`: absorbing is a
    property of what the successor DID, never of it merely existing."""
    own = node.get(rid)
    landed_relief_only = bool(own is not None and own.get("contrary"))
    seen, stack = set(), list(kids.get(rid, ()))
    while stack:
        cid = stack.pop()
        if cid in seen:
            continue
        seen.add(cid)
        kid = node.get(cid)
        if _carries(kid) and not (landed_relief_only and not _landed_relief(kid)):
            return True
        stack.extend(kids.get(cid, ()))
    return False


def _landed_relief(lr):
    """Did this descendant actually reach trunk? The only relief a CONTRARY
    alarm accepts. Reads the recorded closure the same way `_absorbs_debt`
    does — the fact, never a projection of it — plus the git-observed
    `landed` flag, because a contrary alarm is itself a git observation and
    must be answerable by one."""
    if lr is None:
        return False
    return bool(lr.get("landed") or lr.get("closed_by_landing")
                or lr.get("close_reason") == "landed"
                or lr.get("state") == "LANDED")


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
    # refs/heads/lane/ + the PREFIX-STRIPPED lane (#142 r2,
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
    rc, head, _e = be.text(repo_dir, *_REF_ARGV, rb, timeout=5)
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
    argued with it — a review called that contradictory and was right. When
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
        # THE SILENT BRANCH IS WHERE THE ORPHANED BUILD ROW LIVES (task/430,
        # owner-greenlit). `_offer_landing_state` returns
        # UNKNOWN for a BUILD row without an explicit reviewed_tip — correctly,
        # because a build ref is a BASE and 48 of 52 live ones are already
        # ancestors of trunk. But UNKNOWN is exactly the state a row lands in
        # when its work reached trunk under a DIFFERENT LANE LABEL, and this
        # return is why such a row bills a named seat for days of delay on a
        # job that is done. Two live specimens on 2026-08-08.
        found = offchain_landing(store_row)
        return _offchain_line(*found, rid=str(store_row.get("id") or ""))\
            if found else ""
    # THE VERB COMES FROM THE STATE, exactly as the ROLE does. OWED_BY already
    # keys builder/reviewer/integrator off `state`; saying "not building" at an
    # AWAITING_REVIEW row names an activity nobody was doing, which is a second
    # false clause bolted onto the correction for the first. A review caught it.
    verb = {"AWAITING_BUILD": ", not building",
            "AWAITING_REVIEW": ", not reviewing"}.get(str(state or ""), "")
    return ("ALREADY ON TRUNK at %s — needs CLOSING%s; owed by NOBODY"
            % (str(trunk)[:12], verb))


# A LANE LABEL IS A FREE STRING AND THIS SEARCHES TRUNK WITH IT, so it is
# bounded on both ends before any git call: too short and it matches half the
# history, too long and it is not a lane. Anchored to the lane vocabulary
# helm actually mints (lowercase words joined by dashes).
_LANE_CITABLE = re.compile(r"\A[a-z0-9][a-z0-9-]{9,79}\Z")


def _row_instant(row):
    """A row's creation instant as an epoch, or None if it cannot be read.

    The cross-row ordering `_lane_recurs_later` needs. `seq` cannot serve:
    it counts a SINGLE row's lifecycle events, so two rows created a week
    apart both start at 0.

    THE CANONICAL VALIDATOR IS THE AUTHORITY, NOT A LOCAL PARSE (review
    blocker 2). This stripped whitespace and accepted stamps that
    `dispatches._valid_ts` REJECTS — so a row the ledger considers
    unstamped got a confident instant here, and this predicate's False
    AUTHORIZES A TERMINAL. Two readers of one field must not disagree about
    which values are real; the one that can end a row defers to the one that
    admits it."""
    from . import dispatches
    stamp = (row or {}).get("ts")
    if not dispatches._valid_ts(stamp):
        return None
    try:
        return calendar.timegm(time.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ"))
    except (ValueError, TypeError):
        return None


def _lane_recurs_later(store_row):
    """Did this row's lane label appear in ANY later dispatch? -> True/False/None.

    None is UNREADABLE, never False. Criterion (b) of task/430: a lane that
    RECURS is a live chain and its row is owed to somebody; a lane that never
    appears again is the renamed-continuation case the dispatch verb's own
    warning names — "a renamed continuation is invisible to every same-lane
    rule".
    """
    # SEQ IS A PER-ROW LIFECYCLE COUNTER, NOT A CROSS-ROW CLOCK (review
    # blocker 2, reproduced). This compared `r["seq"] > seq` across DISTINCT
    # rows — and two recurring rows commonly BOTH sit at seq 0, so "did the
    # lane appear again LATER" read False and admitted the terminal proposal
    # on a live chain. The ordering that spans rows is the TIMESTAMP the
    # ledger stamps on every event; an unparseable one is UNREADABLE, never
    # "not later", because this predicate's False is what authorizes a
    # terminal.
    lane = str(store_row.get("lane") or "").strip()
    mine = _row_instant(store_row)
    if not lane or mine is None:
        return None
    try:
        from . import dispatches
        snap, unavailable = dispatches.snapshot()
        if unavailable:
            return None
        # SCAN EVERY SIBLING BEFORE ANSWERING (a review's open uncertainty:
        # "should ONE malformed timestamp silence a lane's recurrence rung
        # forever?"). Answer: NO — and the early `return None` did worse than
        # silence it, it made the verdict depend on DICT ORDER. Measured on
        # one fixture, an undatable sibling and a genuinely-later one:
        #     undatable first  -> None
        #     undatable second -> True
        # Same rows, same lane, two answers. So the loop now COLLECTS and
        # ranks the evidence afterwards.
        unreadable = False
        for r in snap.values():
            if str(r.get("lane") or "").strip() != lane:
                continue
            if str(r.get("id") or "") == str(store_row.get("id") or ""):
                continue
            theirs = _row_instant(r)
            if theirs is None:
                unreadable = True      # an undatable sibling is unreadable
                continue
            if theirs > mine:
                # A MEASURED RECURRENCE OUTRANKS AN UNREADABLE ONE. A sibling
                # we CAN date and that IS later proves the chain is live, and
                # no unreadable row can take that proof away — an unreadable
                # sibling is missing evidence, never evidence against.
                return True
            if theirs == mine:
                # EQUAL STAMPS CANNOT ORDER (review blocker 2). The ledger
                # stamps at SECOND resolution, so two rows filed in the same
                # second are genuinely unordered — and "not later" is not the
                # honest reading of that, because False here AUTHORIZES A
                # TERMINAL on a possibly-live chain. It is UNKNOWN for the
                # same reason an unparseable stamp is: the predicate cannot
                # see which came first. It does NOT outrank a later sibling
                # found elsewhere in the scan, so it only sets the flag.
                unreadable = True
        # Nothing later was proved. An unreadable sibling means we cannot say
        # NO — and False is the answer that authorizes a terminal.
        return None if unreadable else False
    except Exception:                              # noqa: BLE001
        return None


def _first_token_bounded(listing, label):
    """The newest commit whose message cites `label` as a WHOLE dashed token.
    -> sha or None

    `git log --fixed-strings --grep` IS A SUBSTRING MATCH (review blocker
    3, reproduced): `widget-lane-x` matches `widget-lane-x-extra`, and a
    walked STEM matches every unrelated label that starts with it. Git's grep
    has no dashed-token boundary, and `\b` will not supply one because a
    hyphen IS a word boundary to every regex flavour — so the boundary has to
    be applied HERE, to the message git handed back.

    A CITATION ENDS AT A CHARACTER THAT CANNOT CONTINUE A LANE LABEL. Lane
    labels are `[a-z0-9-]`, so a match is whole exactly when neither
    neighbour is one of those. That is the same dash-join rule the docs
    correction on this lane describes; until now the docs described a guard
    the implementation did not have.

    THE FAKES PROVED A STRONGER ORACLE THAN PRODUCTION, which is why no arm
    caught this: the test doubles matched by exact set membership, so they
    implemented the correct behaviour and the suite verified the FIXTURE."""
    for chunk in (listing or "").split("\x1f"):
        sha, sep, body = chunk.partition("\x1e")
        if not sep:
            continue
        sha = sha.strip().lower()
        if sha and _cites_whole_token(body, label):
            return sha
    return None


def _cites_whole_token(text, label):
    """Does `text` name `label` as a complete dashed token?"""
    if not label:
        return False
    start = 0
    while True:
        at = (text or "").find(label, start)
        if at < 0:
            return False
        before = text[at - 1] if at else ""
        after = text[at + len(label):at + len(label) + 1]
        if not _LANE_CHAR.match(before or " ") \
                and not _LANE_CHAR.match(after or " "):
            return True
        start = at + 1


# ANY ADJACENT ALPHANUMERIC ENDS THE CLAIM, not only a lowercase one
# (review blocker 3, reproduced against real git: `widget-lane-x` matched
# `widget-lane-xExtra` and the post-filter passed it as EXACT). A lane label
# is lowercase by convention, but the BOUNDARY is about what could CONTINUE a
# token in the message git handed back — and a message is arbitrary text, not
# a label. `\w` covers Unicode letters and digits under Python 3's default,
# so a Cyrillic or full-width suffix cannot slip past either.
_LANE_CHAR = re.compile(r"[\w-]", re.UNICODE)


def offchain_landing(store_row, gitdir=None, trunk=None):
    """"may have landed off-chain" for a build row whose lane never recurs.

    task/430, owner-greenlit 2026-08-08. THE ROW'S OWN REF CANNOT SEE A
    SUCCESSOR THAT LANDED UNDER ANOTHER LANE LABEL, so every landedness check
    keyed on that ref answers UNKNOWN and the surface bills a named seat for
    delay on finished work. Two live specimens the same day: one build row
    whose work landed as a differently-named lane, one whose cure arrived in
    somebody else's carrier.

    A HIT IS SOUND, A MISS IS UNKNOWN, AND THAT ASYMMETRY IS THE WHOLE
    CONTRACT. Trunk citing the lane verbatim is positive evidence the work
    reached trunk; trunk NOT citing it proves nothing at all — a fold that
    never named the lane, a squashed subject, a reworded message all produce
    the same silence as work that genuinely never landed. So this renders a
    MARKER on a hit and NOTHING on a miss, and it never prints "not landed".
    (The same law governs patch-id, which the owner's constraint names: it
    hashes CONTEXT LINES, so trunk drift changes the id and a miss is again
    UNKNOWN. Citation is the cheaper probe with the identical asymmetry.)

    IT CITES THE ROW ID BESIDE THE SHA, per the owner's second constraint: a
    lane sha is rebasable and dangles, and this rung exists because of orphan
    shas. The row id is the durable handle a reader can still resolve after
    the branch is gone.
    """
    lane = str(store_row.get("lane") or "").strip()
    rid = str(store_row.get("id") or "")
    if not rid or not _LANE_CITABLE.match(lane):
        return None
    if str(store_row.get("kind") or "") != "build":
        return None
    if _lane_recurs_later(store_row) is not False:
        # RECURS or UNREADABLE both stay silent. A live chain is owed to
        # someone, and an unreadable ledger is not evidence of a dead lane.
        return None
    # THE CALLER'S TRUNK IS THE PROBE'S TRUNK (a review's blocker 1,
    # reproduced). This resolved its OWN repo and its OWN local ref while its
    # caller had already derived both — so `stalebot.classify_dispatch` could
    # judge landedness against origin/main and then take carrier evidence
    # from a LOCAL main that nobody else can see. A local-only citation mints
    # a false supersede proposal; an origin-only carrier is missed entirely.
    # Two authorities for one question is the split; the parameters close it,
    # and the fallback stays only for callers that genuinely have neither.
    try:
        if not gitdir:
            gitdir, err = _close_repo(store_row, None)
            if err or not gitdir:
                return None
        ref = trunk or _resolve_ref(gitdir, LOCAL_TRUNK) or LOCAL_TRUNK
        p = _git(gitdir, "log", "--format=%H%x1e%B%x1f", "--fixed-strings",
                 "--grep=%s" % lane, "-n", "40", ref)
    except Exception:                              # noqa: BLE001
        return None
    if p is None or p.returncode != 0:
        return None
    sha = _first_token_bounded(p.stdout, lane)
    if sha:
        return (sha, lane, True)
    # THE SPEC'S CRITERION MISSED ITS OWN MOTIVATING SPECIMEN, measured before
    # this branch existed. Row 652d9794's lane label is
    # "parked-dispatch-rebind-orphaned-tip" and trunk cites
    # "dispatch-rebind-orphaned-tip" — ZERO hits on the label, ONE on the stem.
    # The ORPHAN-DISPOSITION SWEEP RENAMED THE LABEL when it parked the row, so
    # the detector for "a renamed continuation is invisible to every same-lane
    # rule" was itself defeated by a rename helm performs on its own rows.
    # `_strip_lane_prefix` does not help: it strips FAMILY prefixes (lane/,
    # rescue/, wt/), and this one is a word segment.
    #
    # SO IT WALKS LEADING SEGMENTS OFF, and says which form matched — a stem
    # hit is weaker evidence than a label hit and must not be reported as the
    # same thing.
    #
    # THE SPECIFICITY GUARD IS THE DASH-JOINED FORM, NOT THE LENGTH FLOOR, and
    # a wording of this comment credited the length floor instead (review
    # 463ae38a attacked the stated reason rather than the behaviour,
    # which is the only way this class is ever caught: mutating a comment is a
    # no-op, so no mutation test can find it). `--fixed-strings` matches the
    # DASH-JOINED stem, and commit prose writes "docs and tests" with spaces,
    # so a stem like `docs-and-tests` cannot hit by coincidence — a match
    # requires a message containing the exact dashed token, which IS citation
    # evidence. Measured: `about-work`, `name-about-work` and `docs-and-tests`
    # all CLEAR the 10-character floor and all return ZERO on the real trunk
    # log. The floor bounds the walk; the dash-join is what makes a hit mean
    # something. A future author who reads "the floor is the guard" would
    # happily lower it and quietly widen the search.
    stem = lane
    while "-" in stem:
        stem = stem.split("-", 1)[1]
        if not _LANE_CITABLE.match(stem):
            break
        try:
            # THE STEM BRANCH NEEDS THE BOUNDARY MORE THAN THE LABEL ONE
            # DOES, not less: a stem is a PREFIX by construction, so a raw
            # substring match hits every unrelated label that begins with it.
            q = _git(gitdir, "log", "--format=%H%x1e%B%x1f", "--fixed-strings",
                     "--grep=%s" % stem, "-n", "40", ref)
        except Exception:                          # noqa: BLE001
            return None
        if q is None or q.returncode != 0:
            return None
        hit = _first_token_bounded(q.stdout, stem)
        if hit:
            return (hit, stem, False)
    return None                                      # MISS -> UNKNOWN -> silence


def _offchain_line(sha, matched, exact, rid):
    return ("MAY HAVE LANDED OFF-CHAIN — trunk commit %s cites %s %r while no "
            "later dispatch does; check row %s before billing anyone"
            % (sha[:12], "lane" if exact else "lane STEM", matched, rid[:12]))


def _stalled_rows(lrs, raw=None, all_projects=False):
    kids, node, err = _chain_forest(raw if raw is not None else {}, lrs)
    if err:
        raise _ChainUntrustworthy(err)
    # SAME RULE AS THE PRIMARY BOARD: a stalled count is a claim that THIS
    # pipeline is behind on something. A row it cannot land, judge or chase is
    # not its stall to answer for. `all_projects` is the same escape the
    # primary board carries — listing only, labels intact.
    out = [lr for lr in lrs.values()
           if lr["stalled"] and not lr["terminal"]
           and (all_projects or _this_boards_row(lr))
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
    pre-rebase reviewed tip. That change is reachable as 7526ff8, "gate: the
    epoch anchors the founding EVENT, because a work-item id comes back".

    POLARITY LAUNDERING, measured at gate 5554ad1e: with
    `kid.get("polarity")` tested for TRUTH, a same-tip child carrying a FIX and a
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
        kid = node.get(cid)           # a dead end (review blocker 1).
        if (kid is not None
                and kid.get("polarity") == "approve" and kid.get("gate")
                and not kid.get("ungated")
                and kid.get("reviewed_tip")
                and kid["reviewed_tip"] == lr.get("reviewed_tip")):
            return kid
        stack.extend(kids.get(cid, ()))
    return None


def _hold_kind(polarity, tier_kind):
    """The classification itself, from the two facts it actually decides on.

    ONE TABLE, TWO SHAPES. The lr projection spells the lifecycle `state` and
    pre-materialises `tier_kind`; the LOCKED WRITER holds the raw dispatch row,
    which spells the lifecycle `status` and carries the immutable verdict tier
    evidence instead of a materialised word. Restating the polarity rule for
    the second shape is how two answers drift apart while both keep reading
    authoritative, so each caller adapts its own row and this decides."""
    if polarity == "concur":
        return "advisory"
    if polarity == "approve":
        return "pre-tier" if tier_kind == dispatches.TIER_PRE_TIER \
            else "authorization-held"
    return "unbillable"


def review_hold_kind(lr):
    """Authorization/dwell classification, independent of projection health."""
    if lr.get("state") != "REVIEWED":
        return "unbillable"
    return _hold_kind(lr.get("polarity"), lr.get("tier_kind"))


def recorded_hold_kind(row):
    """`review_hold_kind`'s answer for a RAW dispatch row, from the row's OWN
    IMMUTABLE RECORD — the single derivation both close doors bind to.

    THE WRITER STAMPS THIS AND THE REPLAY RE-DERIVES IT. The ladder measured a
    projection outside the lock, so the writer re-derives under the lock and
    records ITS answer — the law `discharged` states about its tier, "THE TIER
    IS THE LOCK'S, NEVER THE CALLER'S". Replay then asks this same function of
    the standing state and requires the capture to EQUAL it, because a recorded
    word that names an allowed kind while the row's own evidence says otherwise
    is a close the writer would refuse replaying as a terminal — the two doors
    disagreeing about one row, which is the split the arm exists to close.

    PRE-TIER IS ABSENT TIER FIELDS ON A PRE-V4 VERDICT — BOTH CLAUSES, and the
    version one is not decoration. `approval_tier_for_verdict` answers PRE-TIER
    on that absence only when `verdict_version` is not 4; a v4 verdict lacking
    its required record-time tier proof is DAMAGED, because that writer was
    obliged to stamp one and a record missing what its own version promises is
    a contradiction rather than a historical gap. The reducer RETAINS such a
    row — `_apply` accepts a v4 verdict event, persists `verdict_version=4`,
    and permits the author and tier fields to be wholly absent — so this is a
    standing state an expiry can meet, not a shape only a forger can build.
    Reading presence alone would call it pre-tier and admit a missing-proof v4
    APPROVE through a door built for readable historical ones.

    ASKING IT THIS WAY IS ALSO WHY NOTHING HERE READS POLICY. Both clauses are
    recorded fields, decided before any branch of the canonical resolver loads
    policy history, and every branch that does load it has the tier fields and
    is therefore not pre-tier. `ok`, `none` and every DAMAGED reading refuse
    here alike, so the only bits this door needs are the ones that cannot move.
    Replay consequently touches no mutable policy in any branch, which is
    `subsumed`'s law satisfied structurally rather than by assertion.

    (AND IT RETURNS ONE WORD. `approval_tier_for_verdict` returns the PAIR
    (state, why) — `_tier_unknown` mints exactly that — and passing the pair to
    `tier_unknown_kind`, which compares `str(tier_state)` to "unknown", would
    make every approve read authorization-held including a genuine historical
    pre-tier one. A suite whose real-writer positives are all CONCUR never
    reaches this line at all, so the approve leg needs a positive of its own:
    `tests.test_lr_expired.ExpiredPreTierApproveTest`.)"""
    if not row.get("verdict_ref"):
        return "unbillable"
    polarity = row.get("polarity")
    tier_kind = None
    if polarity == "approve" \
            and row.get("verdict_version") != 4 \
            and not any(key in row
                        for key in dispatches.VERDICT_TIER_FIELDS):
        tier_kind = dispatches.TIER_PRE_TIER
    return _hold_kind(polarity, tier_kind)


#: The two hold kinds that AUTHORIZE NOTHING. `review_hold_kind` already draws
#: this line for billing — advisory is CONCUR, which is absent from every
#: global close allowlist by design, and pre-tier is an APPROVE carrying no
#: record-time authorization-tier evidence. `authorization-held` is the third
#: and is EXCLUDED: that row's verdict can still authorize a land, so its debt
#: is real and expiring it would discard an approval somebody can act on.
NONAUTHORIZING_HOLDS = ("advisory", "pre-tier")

EXPIRE_ADMIT = "ADMIT"
EXPIRE_REFUSE = "REFUSE"
EXPIRE_UNMEASURED = "UNMEASURED"


def _lane_tips_with_work(gitdir, trunk):
    """{tip sha: [lane ref]} for lane branches carrying COMMITS OF THEIR OWN.

    AN EMPTY LANE MUST NOT PROTECT A ROW, and that is not hypothetical: 49 of
    274 lane branches measured on this repo sit AT or BEHIND trunk with no
    commits of their own, because claiming a lane cuts a branch at trunk and
    writes nothing. Keying the guard on "is a lane tip" would let every fresh
    claim shield any row reviewed at that commit, and the guard would get
    weaker each time somebody claimed a lane — a protection that grows as the
    fleet works is the wrong direction for a door that closes work.

    So the question is whether the lane has WORK, asked as `rev-list --count
    trunk..tip`. A lane at trunk answers 0 and protects nothing; a lane with
    commits answers non-zero and its tip is off limits.

    (None, why) when the walk cannot be trusted — never an empty mapping,
    because "no lane holds this tip" and "I could not read the lanes" must not
    share a value on the guard side of a close.
    """
    # `_git` HANDS BACK A CompletedProcess, NOT A STRING, and a first cut read
    # it as one: `str(out).splitlines()` stringifies the whole repr into a
    # SINGLE line, so the parse produced one bogus sha, `rev-list` answered
    # non-zero on the garbage, and the walk reported exactly one lane with
    # work instead of 225. It did not raise and it did not return empty — it
    # returned a plausible number, which is why the census looked finished.
    # Read `.stdout`, and check `returncode` first, like every sibling here.
    out = _git(gitdir, "for-each-ref",
               "--format=%(objectname) %(refname:short)", "refs/heads/lane/")
    if out is None or getattr(out, "returncode", 1) != 0:
        return None, "the lane refs could not be read"
    tips = {}
    for line in (out.stdout or "").splitlines():
        sha, _, ref = line.partition(" ")
        sha, ref = sha.strip(), ref.strip()
        if not sha or not ref:
            continue
        ahead = _git(gitdir, "rev-list", "--count", "%s..%s" % (trunk, sha))
        if ahead is None or getattr(ahead, "returncode", 1) != 0:
            return None, ("lane %s could not be measured against trunk, so "
                          "whether it carries work is UNKNOWN" % ref)
        if (ahead.stdout or "").strip() != "0":
            tips.setdefault(sha, []).append(ref)
    return tips, None


def expired_verdict(lr, lane_tips=None, lane_err=None):
    """(state, reason) — may this row be closed as an EXPIRED review?

    THE ROW IS NOT WRONG AND NOBODY IS LATE. This door exists for a review
    that recorded a verdict which AUTHORIZES NOTHING against work that never
    reached trunk: the reviewer did their job, the author moved on, and the
    row can be closed by no other door — `landed` needs it on trunk,
    `superseded` needs a successor, `withdrawn` needs a proof of absence the
    row does not carry, and `resolved` needs its own tip landed. Measured on
    the live board: 32 of 35 open rows are in this class, 11 to 43 days old,
    and the board cannot reach zero while they sit there.

    THREE ANSWERS, NEVER TWO. UNMEASURED is its own state and is NOT a
    refusal-with-a-reason — a caller batching over the projection must be able
    to report "could not tell" separately from "measured and no", because a
    door that closes work may not let an unreadable input decay into an
    absent one. Nine rows answer UNMEASURED today.
    """
    kind = review_hold_kind(lr)
    if kind not in NONAUTHORIZING_HOLDS:
        return EXPIRE_REFUSE, (
            "%s is %s — expired closes a verdict that AUTHORIZES NOTHING, and "
            "this one%s" % (lr.get("id"), kind,
                            " can still authorize a land"
                            if kind == "authorization-held"
                            else " is not a recorded review at all"))
    land = lr.get("land_state")
    if land in ("LANDED", "MERGED_LOCAL"):
        return EXPIRE_REFUSE, (
            "%s's reviewed tip is %s — the work REACHED the trunk, so this is "
            "a landed close, never an expiry" % (lr.get("id"), land))
    if land != "ABSENT":
        # NOT_CLAIMED and UNKNOWN both mean the landing question was not
        # answered. `land_state` is UNKNOWN when the row is abandoned or not
        # observable, and neither is proof the work is absent.
        return EXPIRE_UNMEASURED, (
            "%s's land state is %s — expiry needs a MEASURED absence from "
            "trunk, and this row never produced one%s"
            % (lr.get("id"), land, _unmeasured_cause(lr)))
    tip = str(lr.get("reviewed_tip") or "")
    if not tip:
        return EXPIRE_UNMEASURED, (
            "%s records no reviewed tip, so there is nothing to measure "
            "against trunk" % lr.get("id"))
    if lane_tips is None:
        return EXPIRE_UNMEASURED, (
            "%s cannot be adjudicated because the lane refs are unreadable "
            "(%s) — a live lane's tip must never be closed as expired"
            % (lr.get("id"), lane_err or "reason unrecorded"))
    if tip in lane_tips:
        return EXPIRE_REFUSE, (
            "%s's reviewed tip is the tip of %s, a lane carrying commits of "
            "its own — that work is live and expiry is for a review nobody "
            "can act on" % (lr.get("id"), ", ".join(lane_tips[tip])))
    return EXPIRE_ADMIT, (
        "%s recorded a %s verdict that authorizes nothing, and its reviewed "
        "tip %s is absent from trunk and is no live lane's tip"
        % (lr.get("id"), kind, tip[:12]))


def _unmeasured_cause(lr):
    """The clause AFTER an UNMEASURED expiry: what is KNOWN about why the
    projection could not measure this row, read off `observe_why`, and which
    door that cause belongs to.

    THE BARE REFUSAL SENT THE INTEGRATOR TO CUT THE WRONG LAYER. "this row
    never produced one" reads as a defect in the ROW, and it was read as "the
    derivation cannot see a lane whose refs are gone" — a derivation cut was
    briefed on that premise, and the derivation never reads lane refs.

    AND THEN THE FIRST CLAUSE NAMED A CAUSE THE VALUE DOES NOT CARRY. Each of
    these three values is ONE label over SEVERAL worlds:

      * `no-trunk-or-tip` is written when this repository names no recognized
        trunk ref OR when the reviewed tip is not a commit in its object
        store (`_git_observe`'s single `not (local_ref or up_ref) or not
        _commit_exists_cached` exit). A pruned tip is one of those worlds; a
        tip whose object is PRESENT leaves a trunk this repository does not
        name and a commit peel that did not succeed undistinguished, because
        the peel projection answers False on ANY failed git call, a spawn
        error included;
      * `underived` follows ANY landing leg that came back None — an expired
        derive budget is one producer of that, and so is a `_git` that did not
        run, a `cherry` that exited non-zero, and a patch index that could not
        be built (`_landing_proof`'s "unknown" into `landed_ever`);
      * `not-asked` is `_UNOBSERVED`'s value for every row `_will_observe_git`
        declines: no tip, an observation this board does not own (a foreign or
        unresolved project), or a row not closed yet. A row carrying a live
        nonretired CONCUR verdict reaches it whenever its project cannot be
        resolved, so `OBSERVE_REASON`'s default wording for the value — "no
        verdict yet" — is false about exactly that row.

    So the clause states the DISJUNCTION and names the probe together with the
    part of it that probe decides, and it relays the producer's recorded reason as the producer's rather
    than asserting it. A cause may be named here only when something recorded
    it; inferring one from the label is the defect this docstring is about.
    """
    why = lr.get("observe_why")
    tip = str(lr.get("reviewed_tip") or "")[:12]
    if why == OBSERVE_NO_TRUNK:
        return (" (the projection recorded `%s` for %s, which is one value "
                "over two worlds and does not say which: either this "
                "repository names no recognized trunk ref to compare "
                "against, or the reviewed tip did not peel to a commit in its "
                "object store. `cat-file -t %s` in the bound repository "
                "separates an ABSENT object from a PRESENT one and nothing "
                "more. A tip that is GONE has no patch to compare against "
                "trunk, so its absence is unmeasurable by any reader and a "
                "destroyed substrate is `close --reason stranded`'s question; "
                "a tip that IS an object leaves the question OPEN between a "
                "trunk this repository does not name and a commit peel that "
                "did not succeed, because the projection answers this value "
                "on any failed git call, a spawn error included, and no close "
                "reason follows from that)"
                % (OBSERVE_NO_TRUNK, tip, tip))
    if why == OBSERVE_UNDERIVED:
        return (" (the derivation of this row did not COMPLETE — a landing "
                "leg answered unknown rather than reached or absent, and `%s` "
                "is the one value for every producer of that: an expired "
                "derive budget, a git call that did not run, a `cherry` that "
                "exited non-zero, a patch index that could not be built. The "
                "row records no cause, so none may be named here. Projecting "
                "the row alone re-attempts the derivation, which is an "
                "attempt and not a promise: a leg that failed for a reason "
                "other than the budget may fail the same way, and a "
                "projection after its cause clears can read reached or "
                "absent; nothing here promises either)" % OBSERVE_UNDERIVED)
    if why == OBSERVE_NOT_ASKED:
        return (" (helm never asked git about this row: `_will_observe_git` "
                "declines a row with no tip, a row whose observation this "
                "board does not own — a foreign or unresolved project — and "
                "a row that is not closed yet, and `%s` does not say which. "
                "The projection's recorded reason for that value reads %r, "
                "which is the mapping's default wording and NOT a reading of "
                "this row's verdict)"
                % (OBSERVE_NOT_ASKED, observe_reason(why)))
    return ""


def expired_measured(lr, gitdir, pinned, lane_tips=None, lane_err=None):
    """(state, why) with the absence RE-MEASURED against `pinned` — the
    projection's answer AND git's, never git's instead of the row's.

    THE ROW'S `land_state` IS A CACHED PROJECTION AND THIS DOOR WRITES. The
    census reads the warm body so it can report the board an operator is
    looking at; the warm body can predate a merge or a fetch with no ledger
    write of its own, so a row that says ABSENT there may be on trunk by the
    time anything closes. Closing on that cached absence records the CURRENT
    trunk sha beside an OLD measurement — a proof that reads sound and is not.
    So the batch previews from the cache and the DOOR re-derives.

    AND THE EMPTY-LANE GUARD CANNOT COVER FOR IT — it makes the hole worse.
    That guard refuses a tip that is some live lane's, but a lane whose work
    has LANDED has no commits of its own any more, so it stops being a lane
    with work at exactly the moment its row must not close. The two guards
    fail in the same direction on the same row, which is why the landing
    question has to be asked directly rather than inferred from the lane.

    `landed_ever` is the tri-state, and UNKNOWN STAYS OPEN: True refuses
    because the work reached trunk, None is UNMEASURED because a door that
    closes work may not treat an unreadable landing as an absent one, and only
    a measured False proceeds.
    """
    # THE CACHED READING IS ASKED FIRST AND IS NEVER OVERRIDDEN — this is a
    # CONJUNCTION, not a replacement. Substituting a live ABSENT for a
    # projection that says LANDED would let the two sources disagree and pick
    # the one that closes, and a door that closes work takes the other side of
    # that: two instruments disagreeing about whether work reached trunk is a
    # reason to leave the row open, never a reason to expire it.
    state, why = expired_verdict(lr, lane_tips, lane_err)
    if state != EXPIRE_ADMIT:
        return state, why
    reviewed = str(lr.get("reviewed_tip") or "")
    try:
        landed = landed_ever(gitdir, reviewed, pinned)
    except Exception as exc:                 # noqa: BLE001
        return EXPIRE_UNMEASURED, (
            "%s's reviewed tip could not be measured against the pinned trunk "
            "(%s), so its absence is UNKNOWN" % (lr.get("id"), exc))
    if landed is True:
        return EXPIRE_REFUSE, (
            "%s's reviewed tip %s IS on trunk at %s, measured now rather than "
            "read from the projection — the work REACHED the trunk, so this is "
            "a landed close, never an expiry"
            % (lr.get("id"), reviewed[:12], pinned[:12]))
    if landed is None:
        return EXPIRE_UNMEASURED, (
            "%s's reviewed tip %s could not be proven absent from trunk at %s "
            "— expiry needs a MEASURED absence and fails closed without one"
            % (lr.get("id"), reviewed[:12], pinned[:12]))
    return EXPIRE_ADMIT, why


def expired_census(lrs, gitdir, trunk):
    """Counts and per-row verdicts over the WHOLE live projection, computed
    before anything closes.

    `gitdir` IS A GIT DIRECTORY, NOT A WORKING TREE, and the distinction bit
    me on the first probe of this pair: `_git` takes `--git-dir` while
    `_close_repo` takes the worktree and RETURNS the git dir, so the door and
    this census want opposite spellings of the same repository. Handing this
    one a worktree path makes every `_git` call fail and the whole census
    answers UNMEASURED — which is the safe direction, but silently, and a
    census that reports nothing measurable is indistinguishable from a board
    with nothing on it.

    THE CENSUS IS THE FIRST ARTIFACT, NOT A REPORT ON THE ACT. A door that
    closes rows in bulk is judged by the population it would touch, and that
    population has to be readable BEFORE a single write — so this runs the
    same predicate the door runs, over every row, and returns the buckets.
    Running it after would only describe what already happened.
    """
    lane_tips, lane_err = _lane_tips_with_work(gitdir, trunk)
    out = {"admit": [], "refuse": [], "unmeasured": [],
           "lane_refs_unreadable": lane_err}
    bucket = {EXPIRE_ADMIT: "admit", EXPIRE_REFUSE: "refuse",
              EXPIRE_UNMEASURED: "unmeasured"}
    for lr in lrs or ():
        if lr.get("close_reason"):
            continue
        state, why = expired_verdict(lr, lane_tips, lane_err)
        out[bucket[state]].append(
            {"id": lr.get("id"), "lane": lr.get("lane"),
             "kind": review_hold_kind(lr), "dwell_s": lr.get("dwell_s"),
             "land_state": lr.get("land_state"), "why": why})
    return out


def _held_reason(lr):
    if review_hold_kind(lr) == "advisory":
        return "advisory review recorded; CONCUR does not authorize landing"
    return str(lr.get("ungated") or "no reason recorded").split(" — ")[0]


def _unmeasurable_rows(lrs, raw=None, all_projects=False):
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
        # SAME SCOPE AS THE BOARD IT RENDERS BESIDE (task/974). This list was
        # the one classifier without the rule, so a foreign REVIEWED row kept
        # a lane on the owner's card that neither loops nor stalls would own.
        if not (all_projects or _this_boards_row(lr)):
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
                # obligation (review blocker 2).
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
        lrs, rid, noun="land request", list_hint="helm lr list", allow_retired=True,
        allow_unknown_kinds=True)


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
    MISSED their own lanes while gate/argv-body-guard-r4 matched. A probe
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
        q = _git(gitdir, *_REF_ARGV, ref + "^{commit}")
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
        p = _git(gitdir, *_REF_ARGV, ref)
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


# ---------------------------------------------------------------------------
# ADMINISTRATIVE RETIREMENT — `helm lr retire`
#
# THE DEFECT THIS SECTION ENDS: the ledger could not be cleared, because
# every terminal helm owned before this one asserts something about the
# WORK — it landed, it was superseded, it was withdrawn, its evidence was
# destroyed — and each is fail-closed on a proof. That is right, and it has one
# consequence nobody designed: a row whose PROOF CHAIN IS UNREACHABLE can reach
# no terminal at all. A reviewer seat that no longer exists cannot be stamped;
# a succession carrier whose commit was pruned cannot be read; an author seat
# renamed three weeks ago cannot answer. Those rows are immortal, and they bill
# the stalled / contrary / unmeasurable counts forever. Rows accumulate at
# that age for exactly this reason and no other verb on the board can touch
# them.
#
# WHAT RETIREMENT CLAIMS, AND WHAT IT REFUSES TO CLAIM. It says one thing:
# helm MEASURED this row's proof chain unreachable, so nobody can act on it and
# no counter should keep charging for it. It never says the work landed, never
# says it did not, never says the verdict was right or wrong. That is why it is
# its own event kind rather than a `close --reason`: every close reason is a
# claim about the change, and putting "we could not reach this" into that
# vocabulary would launder a measurement of OUR blindness into a statement
# about THEIR work.
#
# THE LAW THAT KEEPS IT HONEST is the one this repo already runs on every other
# irreversible door: refuse on the MEASURED CONTRADICTION, never on absence.
# A reachable proof chain refuses. An UNMEASURABLE proof chain also refuses —
# an unreadable roster, an unlistable process table, an ambiguous seat, a
# transient tier read. Only a positive finding of permanent unreachability
# authorizes the write, and the writer (`dispatches._record_retire_proven`)
# re-runs the finding under the ledger lock rather than trusting the one this
# door saw.
#
# THE DISCRIMINATION THAT MATTERS MOST is parked-versus-walled. A seat whose
# upstream is rate-limited is exactly as unable to stamp a tier as a seat that
# was deleted, and the two look identical in the tier vocabulary — both read
# `TIER_DARK`. They are opposite facts: the walled seat comes back and its row
# must go the normal path; the deleted seat never does. `seat_reach` is the
# instrument that separates them, and it separates them on evidence the tier
# never touches — a current roster session, and a live pane declaring the seat
# by name. Either one is enough to refuse.

RETIRE_REASONS = dispatches.RETIRE_REASONS

#: The TWO CALL SHAPES a retirement can arrive through, and the ONLY axis the
#: belt exemption is keyed on beside the reason. They are values rather than a
#: boolean so a call site reads as a declaration of what the operator did.
RETIRE_CALL_SINGLE_ID = "single-id"   # `helm lr retire <id> --reason CODE`
RETIRE_CALL_SWEEP = "sweep"           # `helm lr retire --sweep`, unattended

#: The reasons ELIGIBLE for the belt exemption — eligible, never exempt on
#: their own. ONE MEMBER, and it joins by argument rather than by convenience,
#: the same contract `_TERMINAL_OVERRIDES_VERDICT` states for its own register.
#:
#: A REASON-KEYED EXEMPTION IS WHAT LEAKED, and that is the correction worth
#: recording. The first cut of this register was consulted by reason ALONE at
#: `_measure_retire`, which is the common wrapper the single-id door AND the
#: unattended sweep both call. Its argument for containment was that the sweep
#: pre-skips a live-owner row — but that pre-skip (`_retire_owner_is_live`)
#: asks only about the ONE OWING seat, while the belt requires EVERY party
#: (custodian, sender, recipient) to be positively absent. A row whose owing
#: seat is absent and whose SENDER is live therefore passed the pre-skip, hit
#: the exempt wrapper, and was retired automatically with a live party on it.
#: The containment was asserted in a docstring and implemented nowhere.
#:
#: SO THE EXEMPTION IS A PROPERTY OF THE CALL SHAPE. It applies only at
#: `RETIRE_CALL_SINGLE_ID` — one named row, one explicit integrator act,
#: `--reason` typed out — and NEVER at `RETIRE_CALL_SWEEP`, where every reason
#: including this one runs under the full all-parties belt and is refused while
#: any party is live. An unattended pass may not spend a row a human never
#: named.
#:
#: WHY THE SINGLE-ID DOOR MAY SET THE BELT ASIDE AT ALL. The belt is a PROXY
#: for a question this reason measures DIRECTLY. Its own law is "a chain is
#: unreachable only when NO party can act", and it answers that by asking
#: whether any party is breathing — because for every other reason the finding
#: is about something a live party could still restore: a repository can be
#: re-cloned, a succession carrier re-tipped, a seat re-rostered.
#: `reviewed-object-destroyed` measures "can anyone act" at the OBLIGATION
#: instead: the normal path for the row is adjudicating its reviewed commit,
#: that commit is proven absent from the row's own repository by a repository
#: that just proved it can read a known-good object, and no recorded rewrite
#: translates it to a live one. No living seat can re-read, re-attest or land
#: an object that does not exist, so a live party is not evidence that the
#: normal path is open. A proxy cannot outrank a direct measurement of the same
#: question — but a HUMAN has to be the one making that call, which is exactly
#: what the call-shape key encodes, and the door says so in its output.
_RETIRE_BELT_EXEMPT = ("reviewed-object-destroyed",)

#: Appended to the measurement the single-id door records, so the set-aside is
#: on the permanent record rather than only in this source file. Its clause is
#: the ONE register member's argument — a second member owes its own sentence
#: here, which is another reason joining the register is not free.
_BELT_SET_ASIDE = (" — LIVENESS BELT SET ASIDE for this ONE row by an explicit "
                   "single-id `helm lr retire <id> --reason %s`: no live party "
                   "can re-read or land an object measured absent. The "
                   "unattended sweep runs this reason under the full belt.")

#: The reasons whose single-id door asks the belt about FEWER parties, and
#: which roles it stops asking about. This is not `_RETIRE_BELT_EXEMPT`: that
#: register sets the whole belt aside, and this one keeps the belt running for
#: every party that can still move the row.
#:
#: THE BELT'S QUESTION IS "CAN ANY PARTY ACT", AND A REVIEWER WHO HAS RECORDED
#: A FIX VERDICT HAS NO MOVE LEFT ON THAT ROW. Its obligation was the review
#: and the verdict discharged it; the next move is the author's cure. A live
#: reviewer is therefore not evidence that the row's normal path is open, and
#: counting it makes an absent author's owed cure unretirable for as long as
#: the reviewer keeps working anywhere in the fleet. The author and any
#: custodian still owe a move, so the belt still asks about them.
#:
#: KEYED ON THE CALL SHAPE TOO, the same containment `_RETIRE_BELT_EXEMPT`
#: argues: only `RETIRE_CALL_SINGLE_ID` may set a party aside, and the
#: unattended sweep runs every reason under the full all-parties belt. The row
#: shape is re-checked by `_retire_discharged_roles` rather than trusted from
#: the reason, so a reason that admitted the wrong row still cannot widen the
#: set-aside past a recorded FIX verdict.
_RETIRE_BELT_DISCHARGED = {"author-absent-lane-idle": ("recipient",)}

#: Appended to the measurement for each party the belt did not ask about, so
#: the record states who was set aside and why.
_BELT_DISCHARGED = (" — @%s (%s) not asked by the liveness belt: its FIX "
                    "verdict at %s discharged its move on this row")


def _retire_discharged_roles(reason, row, call_shape):
    """The roles the belt skips for this reason, this row and this call shape.

    Empty unless the operator named one row, the reason is in
    `_RETIRE_BELT_DISCHARGED`, and the RAW row carries a recorded FIX verdict
    with the reviewed commit it was recorded on. A custodian is never in the
    returned set: `_retire_all_parties` names a custodian first, so a reviewer
    who also holds custody is asked about as the custodian it is.
    """
    if call_shape != RETIRE_CALL_SINGLE_ID:
        return ()
    roles = _RETIRE_BELT_DISCHARGED.get(reason, ())
    if not roles or row.get("status") != "verdict" \
            or dispatches._replay_polarity(row.get("polarity")) != "fix" \
            or not str(row.get("reviewed_tip") or "").strip():
        return ()
    return tuple(roles)


def _measure_retire(reason, row, ctx, call_shape=RETIRE_CALL_SWEEP):
    """(measurement, refusal) — THE ONE PATH every retire reason is invoked
    through, with the shared liveness belt applied EXACTLY ONCE.

    THE BELT GUARDS THE ADMIT, NOT THE ENTRY, and that ordering is the whole
    design. Run first, it SHADOWS every reason-specific diagnosis: a walled
    reviewer stops being reported as WALLED and becomes a generic "a party is
    reachable" — true, and strictly less useful to whoever reads the refusal.
    So the reason answers its own question first; the belt runs only where
    that reason would otherwise say YES.

    It also ends a placement inconsistency that was invisible while each
    reason carried its own copy: two called the belt at their ENTRY and two at
    their ADMIT, so two reasons shadowed themselves and two did not. One path
    cannot drift from itself, and a reason added later inherits the belt
    without having to remember it.

    `call_shape` IS WHAT THE OPERATOR DID, and it is the second half of the
    one exemption's key: a reason in `_RETIRE_BELT_EXEMPT` sets the belt aside
    only under `RETIRE_CALL_SINGLE_ID`. IT DEFAULTS TO THE BELTED POLE on
    purpose — a caller that does not declare a shape is treated as unattended,
    so forgetting to pass it can only ever over-refuse, never spend a row.
    Read `_RETIRE_BELT_EXEMPT` before adding to either half.
    """
    measure = RETIRE_MEASUREMENTS[reason]
    measurement, refusal = measure(row, ctx)
    if refusal:
        return None, refusal
    if call_shape == RETIRE_CALL_SINGLE_ID and reason in _RETIRE_BELT_EXEMPT:
        # THE SET-ASIDE IS DISCLOSED WHERE IT IS SPENT. This text rides into
        # `retire_measurement`, so `lr show`, the dry-run plan and the ledger
        # all carry the sentence — a terminal that skipped a shared guard may
        # not be indistinguishable from one that passed it.
        return "%s%s" % (measurement, _BELT_SET_ASIDE % reason), None
    discharged = _retire_discharged_roles(reason, row, call_shape)
    refusal = _retire_live_guard(row, ctx, discharged=discharged)
    if refusal:
        return None, refusal
    for role, seat in _retire_all_parties(row):
        if role in discharged:
            measurement += _BELT_DISCHARGED % (
                seat, role, str(row.get("reviewed_tip") or "")[:12])
    return measurement, None


def _retire_owner_is_live(owing, reach):
    """Is this row's owing seat MEASURABLY reachable right now.

    The skip this serves used to consult a five-name tuple of "live TLAs" — a
    RIVAL STATIC TRUTH about who is alive, stale the moment a seat is added,
    renamed or re-homed (helm seat names have expired more than once). A new
    seat absent from the roll had its rows swept; a departed seat present in
    it blocked sweeping forever.

    IT IS NOT ENOUGH TO ASK THE ROSTER EITHER, and that is the correction
    worth recording: "on the roster" is far broader than "is a live TLA", so
    deriving the set from roster keys made the sweep skip every row owed by
    ANY rostered seat and clear nothing. The honest question was never
    membership, it is REACHABILITY — which `seat_reach` already answers, from
    the bundle this action measured once.

    FAILS CLOSED: anything but a measured LIVE (including UNKNOWN) is treated
    as not-skippable here, and the per-row belt then refuses it anyway on the
    same reading. A sweep that cannot tell must not silently clear.
    """
    if not owing:
        return False
    state, _detail = seat_reach(owing, **(reach or {}))
    return state == SEAT_LIVE
RETIRE_SWEEP_DEFAULT_DAYS = 14

SEAT_ABSENT = "ABSENT"      # POSITIVELY gone, by ONE of two proofs: rostered
                            # and the canonical projection says absent, or
                            # UNROSTERED and missing from every instrument that
                            # can see a seat (`_unrostered_absence`). BOTH
                            # proofs then clear the SAME duration rung
                            # (`_absence_is_durable`) — an instantaneous miss
                            # dates nothing, and this is THE ONLY STATE THAT
                            # PERMITS A RETIREMENT, so "we could not tell" and
                            # "quiet since breakfast" can never spend a row.
SEAT_LIVE = "LIVE"          # reachable: a roster session, or a pane declaring it
SEAT_DARK = "DARK"          # rostered, no current session, no live pane
SEAT_UNNAMED = "UNNAMED"    # resolves cleanly and matches NO roster row
SEAT_UNKNOWN = "UNKNOWN"    # an instrument could not answer — fail closed

_UNPROBED = object()


# The projection answers one of four words and they mean three different
# things to this verb. `quiet` is "seated, idle a while" — a seat with a pane,
# between turns — so it is LIVE, not gone; reading only `fresh` collapsed a
# four-word answer into a boolean and made IDLE indistinguishable from GONE.
_PRESENCE_LIVE_WORDS = frozenset(("fresh", "quiet"))
_PRESENCE_ABSENT_WORDS = frozenset(("absent",))


#: WHY `_presence_state` ANSWERED WHAT IT DID, as a third reading beside the
#: state, because THREE DIFFERENT WORLDS COLLAPSE INTO ONE `SEAT_UNKNOWN` and
#: only one of them is a MISS. A caller that has to tell them apart was reading
#: the refusal PROSE to do it, which is a measurement bound to a sentence.
PRESENCE_UNREADABLE = "unreadable"      # no projection was read at all
PRESENCE_UNATTRIBUTED = "unattributed"  # a row IS this name; it could not be read
PRESENCE_CLASSIFIED = "classified"      # a row IS this name and was classified
PRESENCE_MISS = "miss"                  # readable, describes rows, none is this


def _presence_state(name, rows=_UNPROBED):
    """(state, detail, reason) from the canonical presence projection —
    SEAT_LIVE, SEAT_ABSENT, or SEAT_UNKNOWN, with the `PRESENCE_*` word for WHY.

    THE REASON IS NOT THE STATE. An unreadable projection, a projection that
    describes this name and cannot attribute its beat, and a projection that
    describes the fleet and never mentions this name are three different facts
    that all answer UNKNOWN, and a caller deciding whether the estate was
    positively described has to have them separated at the producer.

    ONLY A POSITIVE `absent` PERMITS ANYTHING. Every other outcome — the
    projection cannot be read, the projection has no row for this seat, the
    row's identity is UNVERIFIED, or it carries a word this function does not
    classify — answers UNKNOWN, and UNKNOWN refuses.

    A PROJECTION MISS IS NOT ABSENCE, and that is the whole point: the
    projection emits one row per ROSTER seat, so a seat with no roster row is
    simply not described by it. An unrostered but very-much-alive native seat
    is indistinguishable, to this instrument, from one that never existed.
    Treating that silence as "gone" would retire live work; treating it as
    UNKNOWN costs a refusal that a human or phase 2 can revisit. A refused
    row waits. A wrongly-retired row is a corruption.
    """
    if rows is _UNPROBED:
        try:
            rows = _reach_probe_presence()
        except Exception:                   # noqa: BLE001
            return SEAT_UNKNOWN, ("the presence projection could not be read, "
                                  "so @%s was NOT measured" % name), \
                PRESENCE_UNREADABLE
    if rows is None:
        return SEAT_UNKNOWN, ("the presence projection is unavailable, so "
                              "@%s was NOT measured" % name), \
            PRESENCE_UNREADABLE
    want = str(name or "").casefold()
    for row in rows or ():
        if not isinstance(row, dict):
            continue
        if str(row.get("seat") or "").casefold() != want:
            continue
        if row.get("unverified") or row.get("unverified_session"):
            return SEAT_UNKNOWN, ("@%s's presence beat cannot be attributed "
                                  "to it, so it was NOT measured" % name), \
                PRESENCE_UNATTRIBUTED
        word = str(row.get("presence") or "").strip().lower()
        runtime = row.get("runtime") if isinstance(row.get("runtime"), dict) else {}
        if word in _PRESENCE_LIVE_WORDS:
            return SEAT_LIVE, ("presence projection reports @%s %s (%s/%s)"
                               % (name, word.upper(),
                                  runtime.get("family") or "?",
                                  runtime.get("backend") or "?")), \
                PRESENCE_CLASSIFIED
        if word in _PRESENCE_ABSENT_WORDS:
            return SEAT_ABSENT, ("presence projection reports @%s ABSENT — no "
                                 "recent beat" % name), PRESENCE_CLASSIFIED
        # A WORD THIS DOOR DOES NOT CLASSIFY IS STILL A ROW ABOUT THIS NAME, so
        # the reason is UNATTRIBUTED and not a miss: the projection DID describe
        # the subject, and only the reading of it failed.
        return SEAT_UNKNOWN, ("presence projection reports @%s as %r, a word "
                              "this door does not classify, so it was NOT "
                              "measured" % (name, word)), PRESENCE_UNATTRIBUTED
    return SEAT_UNKNOWN, ("the presence projection names no row for @%s — it "
                          "describes one row per ROSTER seat, so this is a "
                          "MISS and not a measurement of absence" % name), \
        PRESENCE_MISS


#: How long a name must be silent in EVERY instrument that can see a seat
#: before an UNROSTERED one is called gone. The window is the whole safety
#: margin of `_unrostered_absence`: roster absence is a TIME-VARYING predicate
#: (four names moved absent-to-present in thirteen minutes while task/2333 was
#: measured), so a point-in-time miss proves nothing and a week of measured
#: silence is what makes the miss mean something.
SEAT_SILENCE_DAYS = 7

def _seat_activity():
    """THE LEDGER'S ACTIVITY READING, AND THIS MODULE COMPUTES NO PART OF IT.

    `dispatches.seat_activity` is the whole instrument: the validated act per
    seat, the newest act naming a seat that the projection could NOT attribute,
    and the index's own currency — all of it derived by the fold that validates
    those acts, at the module that writes them.

    THREE ROUNDS OF ONE FINDING ARE WHY THERE IS NOTHING LEFT HERE. This reader
    kept a bespoke index over RAW events, and every question it had to answer
    locally it answered wrong in the direction that spends somebody's rows:
    which kinds record a hand (a table of three, which had never heard of an
    administrative retirement), which binding to trust (an envelope read raw,
    skipping the validator), and which identity to validate a verdict against
    (taken from the verdict's own `recipient` field, so an event supplied the
    join its own author was then checked against). A fourth local answer was
    waiting: an act the walk could not attribute simply vanished, so recent
    unattributable activity on a seat's own obligation read as that seat's
    silence — and silence is what spends a row.

    Named and resolved through module globals at call time, like its sibling
    probes, so an arm installing a double is seen.
    """
    return dispatches.seat_activity()



def _seat_silence_days(stamp, now=None):
    """Whole days between `stamp` and now, or None when it cannot be read.

    UNREADABLE IS NONE, never 0 and never a large number: a stamp this cannot
    parse has measured nothing, and 0 would read as "just now" while a large
    number would authorize a terminal. `_epoch` exists for exactly this
    question and `dispatches._valid_ts` is the admission authority it defers to
    — the ruling on `_created_epoch` is verbatim this case ("two readers of one
    field must not disagree about which values are real; the one that can end a
    row defers to the one that admits it"), and this reading can end a row."""
    if not dispatches._valid_ts(stamp):
        return None
    when = _epoch(stamp)
    if when is None:
        return None
    return int(((now if now is not None else time.time()) - when) // 86400)


def _absence_is_durable(name, activity, now=None):
    """(ok, detail) — is this seat's measured absence OLD ENOUGH to spend a row?

    ONE RUNG, BOTH PERMIT PATHS, and it is here because the first cut of phase
    2 put it on only one of them. `SEAT_SILENCE_DAYS` states the governing fact
    — roster absence is a TIME-VARYING predicate, four names moved
    absent-to-present in thirteen minutes while task/2333 was measured — and
    that fact is about the PROJECTION, so it indicts the ROSTERED permit path
    exactly as hard as the unrostered one. The rostered path's four clauses are
    all positive and all instantaneous: a roster row exists, the projection says
    ABSENT, no current session, no pane. Not one of them dates the absence, and
    SEAT_ABSENT is the only state that permits a retirement.

    MEASURED ON THE LIVE BOARD, which is why this is a rung and not a tidy-up:
    one seat that was ROSTERED, standing CURRENT, and had authored a ledger
    event 3 DAYS earlier read SEAT_ABSENT on 73 OPEN ROWS — every one of them
    retirable through `author-unresolvable` while that seat was still acting. It
    was the ONLY rostered-and-current seat reading ABSENT. The eight seats the
    door actually exists for (91 rows) are all past the window and not one of
    them changes answer, so this narrows the permit to exactly the population
    that was wrong. THE PER-SEAT DAY COUNTS ARE DELIBERATELY NOT WRITTEN HERE:
    they were, and they kept steering after they expired — three of the eight
    had been credited with newer work by the time the reader was rebuilt, so a
    reader checking the prose against the board found it contradicted on
    three of those seats by a fortnight apiece while the CLAIM (all
    eight outside a 7-day window) stayed true. The claim is the durable part;
    the numbers belong to whoever re-measures, and `helm lr retire <id> --reason
    author-unresolvable --dry-run` prints them for one row on demand.

    IT NEVER TOUCHES LIVE, ONLY THE PERMIT. One seat on that board is silent 32
    days and reads LIVE off a current roster session; a rung that could
    downgrade that would be reading silence AS absence, which is the defect
    rather than the cure — long silence is normal for a seat doing one long
    thing, and a proxy family can go a month without authoring a row. Both
    callers reach this only AFTER every liveness exit has been taken, so a
    positive liveness proof always outranks the ledger.

    UNREADABLE, UNDATED AND NEVER-SEEN ALL REFUSE. `err` or a missing index is
    the tri-state UNKNOWN. A name the ledger never recorded as an ACTOR cannot
    be spoken about at all (a typo is not a departure). A stale INDEX — newest
    authorship anywhere older than the window — means the ledger stopped being
    written, and a fleet must not be expired by its own instrument going quiet.

    AND SO DOES A RECENT ACT NOBODY COULD ATTRIBUTE. The silence is measured
    against the newest act the ledger knows about this name in EITHER store, not
    only the newest one it could credit. An act on this seat's own obligation
    that the projection refused, or a verdict it accepted while recording no
    author at all, MIGHT be this seat's — and an index that only counts what it
    can credit reads that as silence, which is a permit. So less-credited
    activity is not automatically the safe direction: the omission made recent
    unattributable work AUTHORIZE the terminal it should have blocked. Outside
    the window it changes nothing (a seat silent for the window under both
    readings is silent), and that is the whole live footprint of this rung, as
    measured over the live ledger's 14577 events: of 36 seats the open board
    names, 4 carry an unresolved act newer than their newest credited one and
    every one of those is more than a fortnight old, so not one answer moves.
    Re-measure with `helm lr retire <id> --reason author-unresolvable
    --dry-run`, which prints the reading for one row.
    """
    if activity is None:
        return False, ("the ledger's authorship index was not measured, so "
                       "@%s's absence has no measured DURATION and an "
                       "undated absence never spends a row" % name)
    validated, unresolved, newest, err = activity
    if err or validated is None:
        return False, ("%s — @%s's silence was NOT measured"
                       % (err or "the activity reading is unreadable", name))
    key = str(name or "").strip().lstrip("@").casefold()
    mine = validated.get(key)
    if not mine:
        return False, ("the dispatch ledger records @%s as the author of "
                       "nothing, ever — a name helm never saw act is not a "
                       "name it can prove departed" % name)
    # THE NEWEST ACT OF EITHER KIND is what the silence is measured against; a
    # doubt OLDER than the credited act adds nothing, because the credited one
    # is already the more recent thing known.
    doubt = (unresolved or {}).get(key)
    latest = doubt if doubt and doubt > mine else mine
    quiet_days = _seat_silence_days(latest, now)
    if quiet_days is None:
        if latest == dispatches.UNDATED_ACT:
            return False, ("an act on @%s's own obligation carries no readable "
                           "timestamp AND no hand this ledger can bind, so it "
                           "can be placed neither in time nor with anybody "
                           "else and this name's silence was NOT measured"
                           % name)
        return False, ("@%s's newest ledger act carries an unreadable "
                       "timestamp (%r), so its silence was NOT measured"
                       % (name, latest))
    fresh_days = _seat_silence_days(newest, now)
    if fresh_days is None or fresh_days >= SEAT_SILENCE_DAYS:
        return False, ("the dispatch ledger's own newest authored event is %s "
                       "— the index is not current, so nobody's silence in it "
                       "means anything"
                       % ("unreadable" if fresh_days is None
                          else "%dd old" % fresh_days))
    if quiet_days < SEAT_SILENCE_DAYS and latest is not mine:
        # SAY WHAT THE LEDGER PROVES AND NOT ONE WORD MORE. Claiming @name
        # AUTHORED this event would be the very attribution the fold refused to
        # make; what is measured is that something happened on its obligation
        # and nobody can say whose hand it was.
        return False, ("an event on @%s's own obligation %dd ago — inside the "
                       "%dd silence window — records no hand this ledger can "
                       "bind, so it MAY be @%s's own act and the silence "
                       "around it is NOT a measured departure"
                       % (name, quiet_days, SEAT_SILENCE_DAYS, name))
    if quiet_days < SEAT_SILENCE_DAYS:
        return False, ("@%s authored a ledger event %dd ago — under the %dd "
                       "silence window, so it is ACTING, and a projection that "
                       "cannot see it right now is a point-in-time miss and "
                       "not a departure" % (name, quiet_days,
                                            SEAT_SILENCE_DAYS))
    if latest is not mine:
        return True, ("its last authored ledger event is %s, the newest act on "
                      "its obligations this ledger could not attribute is %s "
                      "(%dd, window %dd), and the index's newest authorship is "
                      "%dd old"
                      % (mine, latest, quiet_days, SEAT_SILENCE_DAYS,
                         fresh_days))
    return True, ("its last authored ledger event is %s (%dd, window %dd) in "
                  "an index whose newest authorship is %dd old"
                  % (mine, quiet_days, SEAT_SILENCE_DAYS, fresh_days))


#: THE ORDER `_unrostered_absence` MAY ANSWER IN, top first, as data: (exit id,
#: the state that exit answers, what it takes to REACH it). Read by
#: `_unrostered_exit`, so no rung types its own state and the declared order and
#: the code cannot drift apart.
#:
#: EVERY UNKNOWN EXIT PRECEDES THE ONE ADMIT. That is the invariant, and it is
#: the one an expanding instrument breaks first: each new fact this function
#: learned to read was added as a rung, and the two PROJECTION rungs were added
#: LAST while belonging FIRST — so a projection that explicitly described the
#: subject and could not attribute it was overtaken by rungs about the fleet,
#: the panes and the ledger, and the function admitted on an estate it had never
#: positively read. An instrument that acquires a rung acquires an ORDERING
#: question, and a list is the only place to answer it once.
_UNROSTERED_EXITS = (
    ("projection-unreadable", SEAT_UNKNOWN,
     "no projection was read at all, so its silence about this name is the "
     "absence of an instrument and not a measurement"),
    ("projection-describes-the-subject", SEAT_UNKNOWN,
     "the projection HAS a row for this name and could not classify it — an "
     "unattributed beat, or a word this door does not know. Fact 1 below is "
     "that NO ROW IN IT IS THIS NAME, so a described subject is not a miss, and "
     "an estate that names the subject has not been read as silent about it"),
    ("projection-describes-no-live-seat", SEAT_UNKNOWN,
     "the projection describes no LIVE seat anywhere, so it is not currently "
     "describing this fleet and absence from it measures nothing"),
    ("pane-census-names-nobody", SEAT_UNKNOWN,
     "the pane census names no seat at all, so it cannot show that this name "
     "in particular is missing"),
    ("absence-has-no-measured-duration", SEAT_UNKNOWN,
     "the ledger's activity reading could not date this name's silence, or "
     "dated it INSIDE the window, or is itself not current, or carries a "
     "recent act on this name's own obligation that it could not attribute to "
     "anybody — which MAY be this name's and so is not silence"),
    ("absent", SEAT_ABSENT,
     "every instrument that can see an unrostered seat ran, each one describes "
     "a fleet this name is not in, and its silence is a measured week old"),
)

_UNROSTERED_EXIT_STATE = {exit_id: state
                          for exit_id, state, _why in _UNROSTERED_EXITS}


def _unrostered_exit(exit_id, detail):
    """(state, detail) for ONE DECLARED EXIT of `_unrostered_absence`.

    The state comes from `_UNROSTERED_EXITS` and is never typed at the rung, so
    the declared order and the code answer with one voice; a rung naming an exit
    nobody declared raises here instead of inventing a seventh answer.
    """
    return _UNROSTERED_EXIT_STATE[exit_id], detail


def _unrostered_absence(name, presence, agents, activity, presence_reason,
                        now=None, presence_detail=""):
    """(state, detail) — PHASE 2: is a name with no roster row positively gone?

    THE HOLE THIS FILLS IS NAMED IN `_presence_state` AND IN `seat_reach`
    ("Proving that seat gone is phase 2's job"). `author-unresolvable` once
    admitted on SEAT_UNNAMED — no roster row — and that was correctly killed,
    because every roster-keyed instrument is blind to an unrostered seat and
    reading a blind instrument's silence as absence is how a live seat loses
    its work. The answer was NOT to widen the old rung: it is to find evidence
    that is not roster-keyed. Measured on the live board, six of twelve stalled
    rows were doorless for exactly this reason — each owed by a name no roster
    row answers to — with every existing reason refusing UNMEASURED.

    FOUR POSITIVE FACTS, in the shape the rostered permit path already uses,
    and each one is an instrument that RAN. `_UNROSTERED_EXITS` is the ORDER
    they are asked in and the states they answer:
      1. the presence projection was read, describes at least one LIVE seat —
         the pipeline works right now — and NO ROW IN IT IS THIS NAME. All three
         clauses are rungs: `presence_reason` is the producer's own word for
         which of them holds, because "the projection does not describe this
         name" and "the projection describes it and could not read it" are
         opposite facts that answer with the same state;
      2. the pane census walked /proc and found at least one pane declaring a
         seat in its own environ, and none of them declares this name;
      3. the ledger's authorship index was read, HAS a reading for this name
         (a name helm never recorded as an actor is a name this cannot speak
         about — a typo, not a departure), and that reading is at least
         SEAT_SILENCE_DAYS old;
      4. that index is CURRENT — its newest authorship anywhere is inside the
         window — so a ledger that simply stopped being written cannot expire
         a fleet.

    THE RESIDUAL, NAMED RATHER THAN LAUNDERED: a seat that never heartbeats
    (so it is on no roster), never carries HELM_CHAT_NAME in a pane environ,
    and neither sends a dispatch nor answers a review for a week is invisible
    to every instrument helm has, for ANY name, rostered or not. This door
    cannot distinguish that seat from a departed one and does not claim to.
    What bounds the cost is the terminal it feeds: `retire` is administrative,
    it asserts nothing about the work, and it touches no ref, no worktree and
    no commit — the lane stays exactly where it is and `helm seat reassign`
    still moves the bytes.
    """
    # THE PROJECTION'S OWN MISS SENTENCE LEADS EVERY ANSWER THIS FUNCTION
    # GIVES, admissions included. It is fact 1 of the four, it is the sentence
    # `_presence_state` already wrote for exactly this case, and restating it in
    # this function's words would be a second spelling of another instrument's
    # measurement. It also preserves a contract the OLD ordering delivered by
    # accident: before phase 2 an unrostered name fell through to the
    # projection-UNKNOWN branch, so the caller read the projection's wording.
    # Putting the roster question in front of that branch silently dropped it —
    # the STATE stayed the same word while the refusal stopped saying WHICH
    # instrument missed, which is the whole content of a tri-state answer. It is
    # built here, before the first rung, so no branch can forget it.
    miss = (presence_detail or "").strip()
    lead = "@%s has no roster row" % name
    if miss:
        lead += " and %s" % miss
    if presence_reason == PRESENCE_UNREADABLE:
        return _unrostered_exit(
            "projection-unreadable",
            "%s — and with no projection read at all, the rungs below are "
            "reading an instrument that never answered" % lead)
    if presence_reason == PRESENCE_UNATTRIBUTED:
        return _unrostered_exit(
            "projection-describes-the-subject",
            "%s — so this name was NOT missed by the projection, it was "
            "DESCRIBED by it and could not be read, and an estate that names "
            "the subject has measured no silence about it" % lead)
    live_seats = sum(1 for row in (presence or ())
                     if isinstance(row, dict)
                     and str(row.get("presence") or "").strip().lower()
                     in _PRESENCE_LIVE_WORDS)
    if not live_seats:
        return _unrostered_exit(
            "projection-describes-no-live-seat",
            "%s, and the projection reports NO live seat at all — it is not "
            "currently describing this fleet, so absence from it measures "
            "nothing" % lead)
    declaring = len((agents or {}).get("by_seat") or {})
    if not declaring:
        return _unrostered_exit(
            "pane-census-names-nobody",
            "%s, and the pane census names NO seat at all, so it cannot show "
            "that this name in particular is missing" % lead)
    durable, why = _absence_is_durable(name, activity, now)
    if not durable:
        return _unrostered_exit("absence-has-no-measured-duration",
                                "%s, but %s" % (lead, why))
    return _unrostered_exit(
        "absent",
        "%s; the projection describes %d live seat(s) and none is it, %d "
        "pane(s) declare a seat and none is it, and %s"
        % (lead, live_seats, declaring, why))


def seat_reach(seat, roster=None, roster_failed=None, agents=_UNPROBED,
               presence=_UNPROBED, activity=_UNPROBED):
    """(state, detail) — the ONE reachability instrument every retire reason
    consults. LIVE / DARK / UNNAMED / UNKNOWN.

    THE PANE IS ASKED BEFORE THE ROSTER, and that order is the cure for the
    failure this whole verb is built around. The roster is written by seats
    that heartbeat; a seat whose harness is walled upstream can stop
    heartbeating while its pane is very much alive and its operator very much
    waiting. Reading the roster first and stopping there would call that seat
    gone. A pane declaring the seat by name in its own environ is independent
    evidence the roster never touched (store: liveness-is-environ-not-cwd),
    and it is decisive for LIVE.

    UNKNOWN IS A REAL ANSWER AND EVERY BLIND PATH LANDS ON IT: an unreadable
    roster, an EMPTY roster (a whole fleet that is down is not a fleet of
    deleted seats), an unlistable process table, a name that does not resolve
    cleanly, and a name matching more than one roster row. Absence of an
    instrument is never evidence that a seat is gone.

    A NAME WITH NO ROSTER ROW IS NOW ASKED, NOT WAVED THROUGH AS UNKNOWN —
    `_unrostered_absence` is phase 2 and it carries the whole contract,
    including what it still refuses and the residual it cannot see.

    `roster` / `roster_failed` / `agents` / `presence` / `activity` let a sweep
    read every instrument ONCE and pass the same reading to every row, so a
    hundred classifications cannot straddle a roster rewrite. Omitted, each
    call measures for itself, and `activity` is the one instrument measured
    LAZILY — only the unrostered path reads it — so a bundle-less caller that
    never meets an unrostered name pays nothing for the ledger. A caller that
    classifies more than one row passes `_reach_bundle`, where it joins the
    other three as a named probe and is read at most once (a per-row read is
    the defect the bundle exists to prevent: agent_index measured 0.677s and
    the naive loop put three of them inside a lock).
    """
    from . import beacons, seats, seats_common
    name = str(seat or "").strip().lstrip("@")
    if not name:
        return SEAT_UNNAMED, "the row names no seat in this role"
    if roster is None:
        roster, roster_failed = seats.roster_checked()
    if roster_failed:
        return SEAT_UNKNOWN, ("the roster is unreadable, so @%s's "
                              "reachability was NOT measured" % name)
    if not roster:
        return SEAT_UNKNOWN, ("the roster is EMPTY — a fleet that is entirely "
                              "down cannot be told from a fleet of deleted "
                              "seats, so @%s was NOT measured" % name)
    canonical, err = seats._resolve_against(name, roster)
    if err:
        # Positive alias evidence lives here too: a name helm knows belongs to
        # some OTHER seat resolves with an error, and treating that as
        # "nobody" would retire a row whose owner is alive under a new label.
        return SEAT_UNKNOWN, ("@%s does not resolve cleanly (%s), so its "
                              "reachability was NOT measured" % (name, err))
    # THE CANONICAL PROJECTION IS ASKED FIRST, and that ordering is the point.
    #
    # The pane rung below names a seat from HELM_CHAT_NAME in a process
    # environ, and a claude-native seat's processes do not carry it — such a
    # seat must prefix each helm invocation with it for exactly that reason —
    # so that rung is STRUCTURALLY BLIND to a whole family. It also fails closed
    # to UNKNOWN when the process table cannot be listed. Asking it first let
    # a proxy-only instrument's failure decide a question the canonical
    # projection could already answer, which is the difference between adding
    # a source and making one canonical.
    #
    # MONOTONIC TOWARD REFUSAL: this rung can only turn a would-be
    # UNNAMED/DARK into LIVE, never the reverse, so it cannot newly admit a
    # retirement. UNVERIFIED presence is NOT live — evidence helm has not
    # confirmed may not authorize an irreversible retirement — and absence of
    # an answer is never an answer.
    # THE ROWS ARE RESOLVED HERE, not inside `_presence_state`, because the
    # unrostered rung below needs the PROJECTION ITSELF (how many seats it
    # calls live right now) and not only its answer about one name. A probe
    # that raises is None, which every reader downstream treats as UNKNOWN.
    if presence is _UNPROBED:
        try:
            presence = _reach_probe_presence()
        except Exception:                   # noqa: BLE001 — see _reach_bundle
            presence = None
    presence_state, presence_detail, presence_reason = _presence_state(
        name, presence)
    if presence_state == SEAT_LIVE:
        return SEAT_LIVE, presence_detail
    if agents is _UNPROBED:
        agents = beacons.agent_index()
    if agents is None:
        return SEAT_UNKNOWN, ("the process table could not be listed, so no "
                              "pane could be named for @%s" % name)
    pids = agents["by_seat"].get(name.casefold()) or []
    if pids:
        return SEAT_LIVE, ("live pane pid %s declares seat @%s in its own "
                           "environ" % (pids[0], name))
    # THE ROSTER QUESTION IS ASKED BEFORE THE PROJECTION'S SILENCE IS READ,
    # and the order is the whole of phase 2. A name with NO roster row reaches
    # the projection-miss branch below — that is where three
    # seats died UNMEASURED on six stalled rows — and the branch is
    # right about a ROSTERED seat whose projection row could not be read while
    # being the wrong question for a name the projection never described.
    hits = [key for key in roster if seats.recipient_matches(key, canonical)]
    if len(hits) > 1:
        return SEAT_UNKNOWN, ("@%s matches %d roster rows — an ambiguous name "
                              "was NOT measured" % (name, len(hits)))
    if not hits:
        # UNNAMED IS STILL NOT A PERMIT STATE. "No roster row" is not proof a
        # seat is gone and nothing here reads it that way: the miss is one of
        # four facts `_unrostered_absence` requires, and the one that makes it
        # mean anything is a WEEK of measured silence in the one index that is
        # not roster-keyed. An instrument that could not look still answers
        # UNKNOWN there, so the branch this replaces remains reachable for
        # every case it was actually right about.
        if activity is _UNPROBED:
            activity = _seat_activity()
        return _unrostered_absence(name, presence, agents, activity,
                                   presence_reason,
                                   presence_detail=presence_detail)
    if presence_state == SEAT_UNKNOWN:
        # THE PROJECTION COULD NOT MEASURE THIS SEAT and no pane spoke for it
        # either. The roster legs below can only say whether a ROW exists, and
        # a row is not a measurement of liveness — reading DARK here would
        # dress "we could not tell" up as "we looked and it is quiet".
        return SEAT_UNKNOWN, presence_detail
    row = roster[hits[0]] if isinstance(roster.get(hits[0]), dict) else {}
    session = str(row.get("session") or "").strip()
    # `hits[0]` is a roster KEY and `session` a roster VALUE — both unvalidated
    # at the join seam, and both are about to become the retire refusal a human
    # reads in a terminal. Launder at THIS emission site (seats_common law), so
    # a hostile seat name cannot reshape the operator's screen by way of a land
    # request nobody could retire.
    hit = seats_common._seat_label(hits[0])
    if session:
        return SEAT_LIVE, ("roster row @%s holds current session %s"
                           % (hit, seats_common._seat_label(session[:8])))
    if presence_state == SEAT_ABSENT:
        # THE ROSTERED PERMIT PATH, and every clause is positive: the seat HAS a
        # roster row (so the projection describes it), the projection says
        # ABSENT, the row carries no current session, and no pane declares it.
        # Four positive facts, not the absence of a contrary one.
        #
        # AND NOT ONE OF THEM DATES THE ABSENCE, which is the fifth clause this
        # path owed and did not have. All four are read at one instant, the
        # projection they read is a TIME-VARYING predicate by its own
        # measurement (`SEAT_SILENCE_DAYS`, and `owed_seat_standing` states the
        # same fact for the board), and SEAT_ABSENT is the ONLY state that
        # permits a retirement. So a seat between sessions presented exactly as
        # a departed one: one rostered, standing-current seat whose last
        # authored event was 3 DAYS old reached here on 73 open rows.
        # The duration rung is shared verbatim with the unrostered path so the
        # two routes to SEAT_ABSENT cannot mean two different things.
        if activity is _UNPROBED:
            activity = _seat_activity()
        durable, why = _absence_is_durable(name, activity)
        if not durable:
            return SEAT_UNKNOWN, ("roster row @%s carries no current session "
                                  "and the projection reports it absent, but "
                                  "%s" % (hit, why))
        return SEAT_ABSENT, ("roster row @%s carries no current session, no "
                             "pane declares it, %s, and %s"
                             % (hit, presence_detail, why))
    return SEAT_DARK, ("roster row @%s carries no current session and no live "
                       "pane declares it, but %s — DARK is not a measurement "
                       "of absence" % (hit, presence_detail))


# The stage a RAW ledger row is owed from, and the raw field naming the seat
# that owes it. DERIVED from the projection's own tables (`VERDICT_STATE`,
# `OWED_BY`) rather than transcribed beside them, so a polarity or a role
# added there cannot be honoured in one place and forgotten here.
_RETIRE_ROLE_FIELD = {"author": "sender", "reviewer": "recipient",
                      "builder": "recipient"}


def _retire_stage(row):
    """The stage word for a RAW ledger row, on raw evidence only.

    DELIBERATELY NARROWER THAN THE PROJECTION, in the direction that retires
    FEWER rows. `_lr` rewrites a HELD approve's state to REVIEWED (the
    `ungated` clause), which would make the reviewer the owing seat; this
    keeps it READY, whose role is `lander` and carries no seat, so the live
    guard is a no-op there and the tier reason — which measures the reviewer
    itself, properly — is the only door that can retire such a row. A rule
    that cannot see a projection must not guess at one.

    It cannot BE the projection: this runs inside the ledger lock, where
    replaying the ledger to project a row would deadlock on the file it is
    holding.
    """
    status = str(row.get("status") or "")
    if status == "verdict":
        polarity = dispatches._replay_polarity(row.get("polarity"))
        return VERDICT_STATE.get(polarity, "REVIEWED")
    if status in ("open", "held"):
        if row.get("delivery") == "observed":
            return "AWAITING_BUILD" if row.get("kind") == "build" \
                else "AWAITING_REVIEW"
        return "OPEN"
    return status.upper()


def _retire_owing_seat(row):
    """(role, seat) this RAW row is owed by — seat None when nobody nameable.

    REVIEWED is the one role assignment worth stating out loud, because the
    projection words it "unknown (declared verdict held)" and prints no seat.
    That is right for a board: nobody is LATE. It is wrong for this verb,
    whose question is "who could still move this row", and for a verdict held
    by policy the answer is exactly one seat — the reviewer, who is the only
    party who can re-verdict it. So the role resolves to `reviewer` here and
    the measurement text says which rule assigned it.
    """
    stage = _retire_stage(row)
    role = "reviewer" if stage == "REVIEWED" else OWED_BY.get(stage, "unknown")
    field = _RETIRE_ROLE_FIELD.get(role)
    seat = str(row.get(field) or "").strip() if field else ""
    return role, (seat or None)


def _retire_all_parties(row):
    """[(role, seat)] — EVERY seat that could still act on this row, deduped.

    THE OWING ROLE IS A SUBSET, NOT THE SET. `_retire_owing_seat` answers "who
    is LATE", which is a board question and remains right for the reasons that
    ask it. This answers "who could still MOVE this", which is the retirement
    question, and the two differ exactly where a row's role names no field —
    an OPEN row is owed by the integrator, who is on no row, while its sender
    and recipient are both sitting right there and both able to act.

    ORDER IS SENDER FIRST because a pending dispatch's issuer owns the
    delivery and chase leg, so it is the party most likely to be
    live and the cheapest refusal to reach.

    CUSTODY IS READ HERE, NOT DEFERRED — a known future authorization hole is
    not discharged by naming it. Listing only sender and recipient and
    writing the gap into a docstring means — a row whose custody was MOVED away from its sender would
    have its current custodian unconsulted, which UNDER-refuses, which is the
    dangerous direction.

    `custodian` IS A DURABLE FIELD, and that is what separates reading it from
    the rejected cure. `_RETIRE_ROLE_FIELD["integrator"]` was
    refused because NO field named the integrator anywhere — inventing one
    would have resolved to nothing forever. `custodian` is written by
    `dispatches.mark_custody` and carried in the ledger; only its READER
    (`dispatches.custodian_of`) is unlanded, on lane/seat-reassign-one-door.
    So this reads the field through a local helper with custodian_of's exact
    contract — the recorded custodian, else the sender — and a row that has
    never had custody moved answers `sender`, which is the same answer as
    before. Correct-by-construction the moment custody starts moving, and
    identical until then.

    ORDER IS CUSTODY, SENDER, RECIPIENT: the party that owes the delivery and
    chase leg first, since it is the one most likely to still be acting.
    """
    out, seen = [], set()
    custodian = str(row.get("custodian") or "").strip()
    if custodian:
        seen.add(custodian)
        out.append(("custodian", custodian))
    for role, field in (("sender", "sender"), ("recipient", "recipient")):
        seat = str(row.get(field) or "").strip()
        if seat and seat not in seen:
            seen.add(seat)
            out.append((role, seat))
    return out


def _reach_probe_presence():
    """The presence projection, read through the package attribute at CALL
    time so an arm patching `seats_report.presence_report` is seen."""
    from . import seats_report
    return seats_report.presence_report()


def _reach_probe_roster():
    from . import seats
    return seats.roster_checked()


def _reach_probe_agents():
    from . import beacons
    return beacons.agent_index()


def _reach_probe_activity():
    """The ledger's activity reading, as the 4-tuple `_absence_is_durable`
    reads. Named in `_REACH_PROBES` like its siblings so ONE action measures it
    at most once, and resolved through module globals at call time so an arm
    patching `_seat_activity` is seen."""
    return _seat_activity()


# THE INSTRUMENTS AS DATA. (key, probe, value-if-the-probe-raises.)
# This was three hand-written try blocks of one shape, and the third was added
# BESIDE the other two rather than joining them — which is how a new
# instrument escaped both the once-per-action contract and the test guarding
# it. A fourth joins this tuple and is measured once, like the others.
# Probes are named, NOT referenced. A tuple holding function OBJECTS would
# bind at import and an arm patching the module attribute would never be seen
# — the double would be installed and the live instrument would run anyway.
# The name is resolved through module globals at CALL time, which is the same
# late-binding seam the rest of this module relies on to stay testable.
_REACH_PROBES = (("roster", "_reach_probe_roster", ({}, True)),
                 ("agents", "_reach_probe_agents", None),
                 ("presence", "_reach_probe_presence", None),
                 ("activity", "_reach_probe_activity", None))


def _reach_bundle(reach=None):
    """The canonical liveness reading for ONE action.

    Every instrument is measured AT MOST ONCE and reused by every reason,
    every party on the row, and the shared belt. An instrument already in
    `reach` is never re-measured — that is what lets a sweep read the fleet
    once and hand the same reading to a hundred classifications, so they
    cannot straddle a roster rewrite.

    A probe that raises stores its failure value rather than propagating:
    absence of an answer is never an answer, and every reader downstream
    treats these as UNKNOWN and REFUSES rather than admitting a retirement.
    """
    out = dict(reach or {})
    for key, probe_name, on_failure in _REACH_PROBES:
        if key in out:
            continue
        try:
            out[key] = globals()[probe_name]()
        except Exception:                   # noqa: BLE001 — see docstring
            out[key] = on_failure
    roster = out.get("roster")
    if isinstance(roster, tuple):    # roster_checked answers (rows, failed)
        out["roster"], failed = roster
        out.setdefault("roster_failed", failed)
    out.setdefault("roster_failed", False)
    return out


def _retire_live_guard(row, ctx, discharged=()):
    """The refusal every reason shares — bar the one argued exemption in
    `_RETIRE_BELT_EXEMPT`, AND ONLY AT THE SINGLE-ID DOOR: the owing seat must
    not be reachable.

    A live seat — including one whose upstream is walled — can take the normal
    path, and the normal path is always better than an administrative
    terminal: it produces a claim about the work. So a reachable owner refuses
    even when the reason's own measurement would pass, and an UNMEASURABLE
    owner refuses too.

    THE EXEMPTION IS THIS RULE'S OWN LAW, NOT A HOLE IN IT. Liveness is how
    this door answers "can any party act"; a reason that measures the OBLIGATION
    itself unreachable — a reviewed object proven absent from its own
    repository — has answered that question directly, and a live party is then
    not evidence that the normal path is open. But that substitution is a
    JUDGEMENT, so only a human making it on one named row may spend it: the
    exemption is keyed on the CALL SHAPE as well as the reason, and the
    unattended sweep runs every reason — that one included — through this
    guard. Read `_RETIRE_BELT_EXEMPT` before adding to either half.

    EVERY PARTY, NOT THE OWING ONE — it is the whole reason this loop
    exists. This guard used to ask `_retire_owing_seat`,
    which resolves a role and then a single field. An OPEN row's role is
    `integrator`, which names no field, so the seat came back None and this
    guard returned None — and its own docstring says so, which is how the
    behaviour reads as intentional. It IS intentional that an OPEN row has no
    OWING seat. It was never intentional that a row with a live sender AND a
    live recipient could retire.

    Measured against the real `retire()` door: status=open,
    delivery=pending, sender live, recipient live, repo_id absent ->
    would_append=True. Traced: `_measure_repo_unreadable` calls this guard
    (which passed), then admits on `if not gitdir` without ever asking who is
    alive. Two of the four reason classifiers never consult liveness at all,
    so curing that one would leave `_measure_succession_unreadable` holding
    the identical hole and any fifth reason inheriting it by default. The
    rule belongs at the ONE door all four already call.

    A chain is unreachable only when NO party can act. This door retires the
    UNREACHABLE, never the merely-orphaned-on-one-side.
    """
    # ONE REACH BUNDLE, REUSED (measured). Widening this guard from
    # one owing seat to every party multiplied its cost by the number of
    # parties: with an empty `reach`, each `seat_reach` re-reads BOTH
    # instruments, and `beacons.agent_index` WALKS /proc — under the dispatch
    # write lock. One retirement called roster_checked and agent_index three
    # times, once per party.
    #
    # THAT WALK IS MEASURED AT 0.677s ON A BUILD NODE, and the naive loop put
    # three of them inside a lock. The all-parties rule is right and its naive
    # loop is not: correctness at the door does not license paying for the
    # door once per name.
    #
    # An explicitly supplied `reach` still wins — a sweep that already holds a
    # measurement passes it and this measures nothing.
    # LOCAL IMPORT, the idiom this file already uses for both of these — they
    # are NOT module-level here, so a bare `seats` / `beacons` would NameError.
    # It is also the right seam: reading the package attribute at CALL time is
    # what lets the arms patch `roster_checked` / `agent_index` and be seen.
    reach = _reach_bundle(ctx.get("reach"))
    # STORED BACK, not just copied. The author and tier reasons run their own
    # belt after this one, and a bundle that lived only in this frame made
    # them re-measure every instrument from scratch — the once-per-action
    # contract held inside this function and nowhere else.
    if isinstance(ctx, dict):
        ctx["reach"] = reach
    # EVERY PARTY MUST BE POSITIVELY ABSENT. The belt permits on ONE state and
    # refuses on every other, rather than refusing on a list of bad states —
    # the difference matters because a state nobody enumerated (a new one, or
    # one this door forgot) then defaults to REFUSING instead of to admitting.
    # "We could not tell" can never spend a row.
    # `discharged` names roles `_retire_discharged_roles` proved have no move
    # left on this row; every other party is still asked.
    for role, seat in _retire_all_parties(row):
        if role in discharged:
            continue
        state, detail = seat_reach(seat, **reach)
        if state == SEAT_ABSENT:
            continue
        if state == SEAT_LIVE:
            return ("%s names @%s (%s), which is REACHABLE — %s. Retirement "
                    "is for a proof chain measured PERMANENTLY unreachable; a "
                    "live party, including one temporarily walled upstream, "
                    "goes the normal path" % (row["id"], seat, role, detail))
        return ("%s names @%s (%s), which is NOT PROVEN GONE (%s) — %s. "
                "Retirement permits only a party measured positively absent; "
                "an unmeasured chain refuses and waits, because a refused row "
                "can be revisited and a wrongly-retired one is a corruption"
                % (row["id"], seat, role, state, detail))
    return None


def _measure_repo_unreadable(row, ctx):
    """(measurement, refusal) — Git for this row's own repository cannot be
    read, measured by asking Git.

    THE READ IS THE MEASUREMENT: `git rev-parse --git-dir` against the
    recorded common-dir. A zero exit is the contradiction and refuses. Only
    TWO shapes authorize: a Git that RAN and answered non-zero, and a row
    that never recorded a repository at all — the second because `repo_id` is
    immutable on an append-only row, so an absent binding is absent forever
    and no Git question about it can ever be asked.

    A GIT THAT DID NOT RUN REFUSES, and that is not a nicety. `_git` answers
    None for an unspawnable Git AND for a five-second TIMEOUT, and a timeout
    is a loaded box, not a destroyed repository. Reading it as unreadable
    would let a busy moment retire live work — absence of an answer is never
    an answer (store: missing-evidence-is-not-evidence-against).
    """
    gitdir = str(row.get("repo_id") or "").strip()
    if not gitdir:
        return ("the row records no repository binding at all, so no Git "
                "question about it can ever be asked"), None
    proc = _git(gitdir, "rev-parse", "--git-dir")
    if proc is None:
        return None, ("git did not RUN against %s (unspawnable, or it timed "
                      "out) — %s's repository was NOT measured, and an "
                      "unanswered question is not a destroyed repository"
                      % (gitdir, row["id"]))
    if proc.returncode == 0:
        return None, ("git ANSWERS for %s (rev-parse --git-dir -> %s) — the "
                      "repository is READABLE, so %s's proofs can still be "
                      "re-run and it goes the normal path"
                      % (gitdir, (proc.stdout or "").strip()[:80], row["id"]))
    return ("git rev-parse --git-dir in %s exited %d (%s)"
            % (gitdir, proc.returncode,
               (proc.stderr or "").strip()[:160] or "no stderr")), None


def _measure_succession_unreadable(row, ctx):
    """(measurement, refusal) — the declared succession carrier's proof cannot
    be read, measured by attempting the read.

    THE CARRIER IS RESOLVED AGAINST THE RAW LEDGER, never against a
    projection. A projection is scoped and hydrated; a carrier missing from
    ONE is an artifact of the filter, not a fact about the world, and calling
    that unreadable would retire every row whose ledger could not be read —
    on a board of any size. Absence from the ledger ITSELF is a different and
    genuine finding, and it is reported as its own shape.

    The proof is the carrier's reviewed commit. `git rev-parse --verify` on it
    answering zero is the contradiction: the chain is reachable and the
    normal succession path applies.
    """
    carrier_id = str(row.get("supersedes") or "").strip()
    if not carrier_id:
        return None, ("%s declares no succession carrier, so there is no "
                      "succession proof to be unreadable — this reason "
                      "speaks only about a row that points at one"
                      % row["id"])
    carrier = ctx["snapshot"].get(carrier_id)
    if carrier is None:
        return ("the declared succession carrier %s is absent from the "
                "dispatch ledger itself — the pointer resolves to nothing"
                % carrier_id[:12]), None
    gitdir = str(carrier.get("repo_id") or "").strip()
    tip = str(carrier.get("reviewed_tip") or carrier.get("tip") or "").strip()
    if not gitdir or not tip:
        return ("succession carrier %s records %s, so its proof cannot be "
                "read" % (carrier_id[:12],
                          "no repository" if not gitdir
                          else "no reviewed tip")), None
    proc = _git(gitdir, *_REF_ARGV, tip + "^{commit}")
    if proc is None:
        # SAME LAW AS THE REPO PROBE: `_git` answers None for a timeout as
        # well as for an unspawnable Git, and a loaded box is not a pruned
        # commit. Only a Git that RAN and said no is the measurement.
        return None, ("git did not RUN against %s (unspawnable, or it timed "
                      "out) — the succession carrier's proof was NOT "
                      "measured for %s" % (gitdir, row["id"]))
    if proc.returncode == 0:
        return None, ("the succession carrier's proof READS — %s resolves in "
                      "%s — so %s's chain is reachable and goes the normal "
                      "succession path" % (tip[:12], gitdir, row["id"]))
    return ("succession carrier %s: git ran and cannot read its reviewed "
            "commit %s in %s (rev-parse exited %d)"
            % (carrier_id[:12], tip[:12], gitdir, proc.returncode)), None


def _measure_reviewed_object_destroyed(row, ctx):
    """(measurement, refusal) — the row's OWN reviewed proof object no longer
    exists, measured three independent ways, no one of which is sufficient.

    THE POPULATION, measured on the live board: eight rows whose reviewed tip
    is a dangling object, of which five have no terminal at all. Nothing can
    be re-adjudicated over a commit that is gone — the reviewer cannot re-read
    what it attested, the author cannot re-offer it, `close` has no proof to
    walk and `expired` reads land_state UNKNOWN — so the row bills the
    stalled / pre-tier / advisory counters forever.

    WHY THIS IS A RETIREMENT AND NOT `close --reason stranded` OR `lr
    abandon`, which both already speak about destroyed substrate: those two
    claim the WORK is gone, and both therefore veto on a surviving
    lane-family ref. That veto is right for them and it is exactly what
    leaves this population doorless — all five rows carry one
    (refs/heads/prerebase/gate-epoch, refs/heads/docref-review,
    refs/heads/lane/owner-read-shape and two refs/helm-retired lanes).
    Retirement claims nothing about the work, so a surviving ref cannot
    contradict it: the object is gone, therefore that ref is not the reviewed
    work, and the row's PROOF is unreachable whatever the ref holds. The ref
    names are DISCLOSED in the measurement instead of blocking it, so a
    reader is told where to look for work this terminal does not write off.

    THE THREE LEGS ARE NOT ONE FACT SAID THREE TIMES.
      1. the id is one `helm lr refs` audits (`_audited_commit_ids`, the
         audit's own predicate) — without it the retirement would cite a
         corroborating surface that says nothing about this row. That
         predicate carries a declared completeness remainder (a sha256 commit
         id is outside `_SHA_RE`, so this leg over-refuses it); read it
         there;
      2. the object is MISSING from the row's own repository, in a repository
         that first proves it can read a known-good object. That positive
         control is `stranded`'s mass-termination guard and it is not
         optional: a repo that cannot read anything is not a repo whose
         "missing" means anything;
      3. the recorded rewrite sidecar does not translate it to a live object.
         A translated tip is a live identity, not a destroyed one, and the
         refusal hands over the translated sha so the row can be adjudicated
         through it.

    EVERY UNKNOWN REFUSES, at every leg: a Git that did not run, a rc outside
    {0,1}, an unreadable sidecar, an unreadable ref table. A destroyed object
    is proven by Git ANSWERING that it is missing, never by silence.
    """
    tip = str(row.get("reviewed_tip") or "").strip()
    if not tip:
        return None, ("%s binds no reviewed commit, so it has no reviewed "
                      "proof object to be destroyed — that row's obligation "
                      "is still the review" % row["id"])
    # LEG 1 — the AUDIT JOIN, first because it is free and diagnostic.
    audited = dict(_audited_commit_ids(row))
    if audited.get("reviewed_tip") != tip:
        return None, ("measurement 1 of 3 (audit) FAILED: %s's reviewed tip "
                      "%r is not a commit id `helm lr refs` audits, so the "
                      "audit can never report it unresolvable and this "
                      "reason would cite a corroboration nobody can find"
                      % (row["id"], tip[:64]))
    gitdir = str(row.get("repo_id") or "").strip()
    if not gitdir:
        # `repo-unreadable` MEASURES THIS BINDING, AND IT IS BELTED. This
        # reason sets the liveness belt aside for a single named row;
        # `repo-unreadable` never does, so on the same row it refuses while
        # any party is live or unmeasured and admits only once every party
        # reads ABSENT. Measured both ways on one row; an unconditional
        # "wants --reason repo-unreadable" is false for the first.
        return None, ("%s records no repository binding, so no Git question "
                      "about its reviewed object can be asked. `--reason "
                      "repo-unreadable` measures exactly that missing "
                      "binding, but it runs the full liveness belt, which "
                      "this reason sets aside: it admits only once every "
                      "party on the row reads ABSENT, and refuses while one "
                      "is live or unmeasured" % row["id"])
    # LEG 2 — POSITIVE CONTROL, then the object itself. Same instrument for
    # both, so the control is a known-good input to the exact probe whose
    # answer authorizes the write.
    head = _git(gitdir, "rev-parse", "HEAD")
    if head is None or head.returncode != 0 or not (head.stdout or "").strip():
        return None, ("measurement 2 of 3 (object) NOT MADE: the repository "
                      "at %s could not resolve its own HEAD, so nothing it "
                      "calls missing means anything" % gitdir)
    control = head.stdout.strip()
    if _object_exists(gitdir, control) is not True:
        return None, ("measurement 2 of 3 (object) NOT MADE: the repository "
                      "at %s cannot read its own HEAD object %s — refusing "
                      "to call anything destroyed in a repository that "
                      "cannot read a known-good one" % (gitdir, control[:12]))
    present = _object_exists(gitdir, tip)
    if present is True:
        return None, ("measurement 2 of 3 (object) FAILED: the reviewed "
                      "object %s IS still present in %s — %s is adjudicable "
                      "and goes the normal path"
                      % (tip[:12], gitdir, row["id"]))
    if present is None:
        return None, ("measurement 2 of 3 (object) NOT MADE: Git could not "
                      "determine whether %s exists in %s — retirement is "
                      "FAIL-CLOSED on an unknown object state"
                      % (tip[:12], gitdir))
    # LEG 3 — TRANSLATION. A recorded rewrite is a live identity.
    table, terr = _ref_translations_checked()
    if terr:
        return None, ("measurement 3 of 3 (translation) NOT MADE: the "
                      "ref-translation sidecar is unreadable (%s)" % terr)
    translated = table.get(tip)
    if translated:
        alive = _object_exists(gitdir, translated)
        if alive is True:
            return None, ("measurement 3 of 3 (translation) FAILED: %s "
                          "translates to the LIVE object %s (recorded "
                          "rewrite) — adjudicate %s through that identity, "
                          "its proof is not destroyed"
                          % (tip[:12], translated[:12], row["id"]))
        if alive is None:
            return None, ("measurement 3 of 3 (translation) NOT MADE: Git "
                          "could not determine whether the translated object "
                          "%s exists in %s" % (translated[:12], gitdir))
    # DISCLOSURE — never a veto, and an unreadable ref table still refuses:
    # the measurement PROMISES to name surviving refs, and one that could not
    # look would record that promise over an unread table.
    matches, ferr = _lane_family_refs(gitdir, row.get("lane"),
                                      row.get("branch"))
    if ferr:
        return None, ("%s — the surviving-ref disclosure is part of this "
                      "measurement, so an unreadable ref table refuses"
                      % ferr)
    named = ", ".join(ref[:80] for ref, _obj in (matches or [])[:3])
    if len(matches or []) > 3:
        named += " (+%d more)" % (len(matches) - 3)
    return ("reviewed object %s is MISSING in %s (cat-file -e; control: HEAD "
            "%s reads), %s, and it is a commit id `helm lr refs` audits at "
            "field reviewed_tip — surviving lane-family refs: %s"
            % (tip[:12], gitdir, control[:12],
               "no rewrite translation is recorded for it" if not translated
               else "its recorded translation %s is missing too"
                    % translated[:12],
               named or "none")), None


def _measure_author_unresolvable(row, ctx):
    """(measurement, refusal) — the seat this row is owed by resolves to no
    roster row, measured against the roster and the live pane census.

    DARK IS NOT UNNAMED, and conflating them is the failure this verb must
    never make: a rostered seat with no current session is idle, and an idle
    seat is woken, not written off. Only a name that resolves cleanly and
    matches NOTHING — no roster row, no pane declaring it — is unresolvable.
    """
    # THE SHARED BELT FIRST. The all-parties guard was wired into
    # repo/succession only; _measure_author_unresolvable and
    # _measure_tier_unevaluable_parked bypassed it. That falsifies
    # the claim that the guard sits at "the ONE door
    # all four classifiers already call". Only TWO called it. These two ask
    # `seat_reach` themselves, about the OWING seat, which is their own
    # reason's question and is not the belt: a row whose owing seat is
    # genuinely unresolvable can still have a LIVE recipient or custodian, and
    # nothing here was asking.
    role, seat = _retire_owing_seat(row)
    if not seat:
        return None, ("%s is owed by %s, which names no seat — there is no "
                      "owing seat to be unresolvable. That row wants a "
                      "different reason" % (row["id"], role))
    state, detail = seat_reach(seat, **ctx["reach"])
    if state == SEAT_ABSENT:
        # POSITIVELY ABSENT IS WHAT "UNRESOLVABLE" MEANS NOW. This admitted on
        # SEAT_UNNAMED — "resolves cleanly, matches no roster row" — which read
        # the ABSENCE of a roster row as proof the seat was gone. Every helm
        # liveness instrument is keyed on the roster, so a seat with no row is
        # described by none of them: that is UNMEASURABLE, not absent, and
        # admitting on it is exactly how an unrostered live seat loses its
        # work. The reason keeps its name and question — is the owing seat
        # gone — and now requires a positive answer to it.
        #
        # THE SHARED BELT GUARDS THE ADMIT, NOT THE ENTRY. Calling it FIRST
        # closed the hole and SHADOWED every reason-specific refusal — a
        # walled reviewer stopped being reported as WALLED and became a
        # generic "a party is reachable", which is true and less useful.
        # A refusal this reason can DIAGNOSE beats a refusal the belt can
        # only ASSERT, so the belt runs exactly where this reason would
        # otherwise say yes.
        return ("owing seat @%s (role %s, from stage %s): %s"
                % (seat, role, _retire_stage(row), detail)), None
    if state == SEAT_DARK:
        return None, ("@%s IS on the roster — %s. An idle seat is woken, not "
                      "written off; %s goes the normal path"
                      % (seat, detail, row["id"]))
    if state == SEAT_UNKNOWN:
        return None, ("@%s could not be measured — %s. This reason needs the "
                      "owing seat proven GONE, and an instrument that could "
                      "not look has not answered; %s waits rather than being "
                      "written off on silence" % (seat, detail, row["id"]))
    if state == SEAT_LIVE:
        return None, ("@%s is REACHABLE — %s — so %s is a live obligation, "
                      "not cruft" % (seat, detail, row["id"]))
    return None, ("@%s's reachability was NOT measured — %s. Retirement "
                  "refuses on an unmeasured chain, never on absence"
                  % (seat, detail))




#: Characters the record reserves, below the writer's measurement cap, for the
#: belt's disclosure of each party it set aside. The retained branch and tip
#: lead the measurement and are never trimmed; the author's reachability
#: sentence is the long, re-derivable part and is trimmed to what remains,
#: because a record over the writer's cap is refused outright.
_ABSENT_AUTHOR_BELT_RESERVE = 200


def _measure_author_absent_lane_idle(row, ctx):
    """(measurement, refusal) — a FIX-verdicted row whose AUTHOR is absent from
    every roster surface helm has, whose lane has not moved for the silence
    window, and whose chain has nothing left on the board.

    THE POPULATION: rows whose reviewer recorded a FIX, whose author then left
    the fleet, and whose lane branch still holds the unlanded work. Every other
    door refuses them. `dispatch cancel` refuses a verdicted row, `close
    --reason withdrawn` refuses content it cannot call absent, and
    `author-unresolvable` runs the all-parties belt, which a reviewer still
    working elsewhere in the fleet fails forever — although that reviewer's
    move on the row was spent the moment it recorded the verdict. The belt half
    of that is `_RETIRE_BELT_DISCHARGED`; this function is the measurement half.

    FIVE MEASUREMENTS, each refusing on its own contradiction or on UNKNOWN:
      1. the row's owing role is `author` (a recorded FIX verdict);
      2. the author reads SEAT_ABSENT through `seat_reach` — roster, rename
         aliases and alias evidence, the presence projection, the pane census,
         and a ledger silence of SEAT_SILENCE_DAYS — AND `seat_lineage` over
         the same roster reading answers ORPHANED: no roster row answers to
         the name and no renamed row carries it. A rename is the author still
         present under another name, so the cure is that seat's move. And the
         SPAWN REGISTER holds no record for it: a record helm's own register
         still holds is presence to helm, whatever config survives beside it.
         The register is read through `orcaadopt.spawn_register_census`, over
         the domain `registered_seats` walks, and any record or directory it
         names unreadable makes the author's presence UNKNOWN;
      3. no same-chain successor is still on the board, and every node the
         walk crosses has a readable chain identity (`_chain_unfinished`);
      4. the row's bound lane branch (`ref_branch`, never a name guessed from
         the lane label) exists and its tip was committed at least
         SEAT_SILENCE_DAYS ago. The window is the author-silence window on
         purpose: one rule says how long a quiet author is a departed one, and
         the ledger and the lane are two witnesses to the same silence;
      5. neither the lane tip nor the reviewed tip LANDED on the authoritative
         trunk, read through `_landing_proof`: ancestry, then patch-id, because
         lands here are routinely cherry-picked and an ancestry-only reading
         calls a cherry-picked land unlanded. Only `absent` on an object that
         exists admits. Either tip on trunk refuses naming both readings
         and no command, because retiring would hide a claim about the work
         behind a terminal that makes none, and no close door admits a raw
         FIX row on that fact alone.

    THE RECORD NAMES WHAT IS RETAINED. Retirement touches no ref and no
    worktree, so the branch and its full tip lead the measurement: a reader of
    the terminal is told where the work still is, and `helm work gc` keeps an
    unlanded branch for triage.
    """
    rid = row["id"]
    role, author = _retire_owing_seat(row)
    if role != "author" or not author:
        return None, ("%s is owed by %s from stage %s — this reason retires "
                      "only an owed cure, a row whose recorded FIX verdict "
                      "hands the next move to its author"
                      % (rid, role, _retire_stage(row)))
    reviewed = str(row.get("reviewed_tip") or "").strip()
    if not reviewed:
        return None, ("%s records no reviewed commit, so its FIX verdict "
                      "binds nothing a cure could answer" % rid)
    reach = _reach_bundle(ctx.get("reach"))
    ctx["reach"] = reach
    state, detail = seat_reach(author, **reach)
    if state == SEAT_LIVE:
        return None, ("author @%s is REACHABLE — %s. The cure is its move: "
                      "`helm dispatch triage %s` shows it the FIX"
                      % (author, detail, rid[:12]))
    if state != SEAT_ABSENT:
        return None, ("author @%s is %s, not measured ABSENT — %s. Re-measure "
                      "with `helm lr retire %s --reason author-absent-lane-idle "
                      "--dry-run` once every roster surface answers"
                      % (author, state, detail, rid[:12]))
    from . import seats_lineage
    heir, lineage, why = seats_lineage._seat_lineage_uncached(
        author, (reach.get("roster"), reach.get("roster_failed")))
    # NO REFUSAL BELOW NAMES `helm seat reassign`: it moves open and held rows
    # only, and every row this reason reads carries a verdict.
    if lineage == seats_lineage.SEAT_RENAMED:
        return None, ("author @%s is PRESENT under another name — %s. The "
                      "cure is @%s's move: `helm dispatch triage %s` shows it "
                      "the FIX" % (author, why, heir, rid[:12]))
    if lineage == seats_lineage.SEAT_CURRENT:
        return None, ("author @%s still has a roster row (lineage %s: %s), so "
                      "it is not absent from every roster surface and this "
                      "reason does not apply to %s. No helm verb removes a "
                      "roster row; `helm seat where %s` reads what that row "
                      "still answers, and the row stays the author's owed cure"
                      % (author, lineage, why, rid[:12], author))
    if lineage != seats_lineage.SEAT_ORPHANED:
        return None, ("author @%s's roster lineage is %s (%s), so its absence "
                      "from every roster surface was NOT measured. Re-measure "
                      "with `helm lr retire %s --reason author-absent-lane-idle "
                      "--dry-run` once the roster reads"
                      % (author, lineage, why, rid[:12]))
    # THE CHECKED REGISTER, NOT THE MINTED-PROXY RESOLVER. `orcaadopt.
    # helm_spawned` omits a spawn.json it could not read or parse, and skips
    # one whose family or instance config.yaml is gone, so its silence about
    # the author is not a measurement of absence. The census reads every
    # register record `registered_seats` can see; any place it names
    # unreadable could hold the author's, so presence is UNKNOWN, here and in
    # the locked re-measure, which runs this same function.
    from . import orcaadopt, seats_common
    try:
        records, unreadable = orcaadopt.spawn_register_census()
    except Exception as exc:                 # noqa: BLE001 — UNKNOWN refuses
        unreadable = [("the seats root", "%s: %s" % (type(exc).__name__, exc))]
    if unreadable:
        path, why = unreadable[0]
        return None, ("helm's spawn register could not be read at %s (%s%s), "
                      "so whether its register still holds author @%s is "
                      "UNKNOWN. Re-measure with `helm lr retire %s --reason "
                      "author-absent-lane-idle --dry-run` once it reads"
                      % (path, why, "; %d more unreadable" % (len(unreadable) - 1)
                         if len(unreadable) > 1 else "", author, rid[:12]))
    wanted = seats_common._seat_key(author)
    for name, path, rec in sorted(records, key=lambda r: r[1]):
        names = {name} | {str(rec.get(k) or "") for k in ("seat", "identity")}
        if wanted in {seats_common._seat_key(n) for n in names if n}:
            # NO COMMAND: whether that seat can come back depends on its
            # session, identity, endpoint and harness, none measured here.
            return None, ("author @%s has a spawn register record at %s, so "
                          "helm's own register still accounts for it and it "
                          "is PRESENT to helm, not absent. This reason "
                          "retires only an author no register record names"
                          % (author, path))
    snapshot = ctx.get("snapshot")
    if not isinstance(snapshot, dict):
        return None, ("the dispatch ledger snapshot was not read, so %s's "
                      "successor chain was NOT measured" % rid)
    unfinished, unresolved = _chain_unfinished(row, snapshot)
    if unresolved:
        node, why = unresolved[0]
        return None, ("%s's successor chain is UNRESOLVED at %s (%s): replay "
                      "cannot say whether that node continues this work, so "
                      "what lies under it was NOT measured terminal"
                      % (rid, str(node.get("id") or "?")[:12], why))
    if unfinished:
        kid = unfinished[0]
        return None, ("%s's chain continues at %s (%s%s), which is not "
                      "terminal — that row carries the obligation; "
                      "`helm lr show %s` names its next move"
                      % (rid, kid["id"][:12], kid.get("status") or "?",
                         " %s" % kid["polarity"] if kid.get("polarity") else "",
                         kid["id"][:12]))
    gitdir = str(row.get("repo_id") or "").strip()
    if not gitdir:
        # THIS REASON'S OWN POPULATION IS WHERE THE REDIRECT FAILS. It sets a
        # live reviewer aside (`_RETIRE_BELT_DISCHARGED`); `repo-unreadable`
        # asks the belt about every party, so over the very row this reason
        # exists for — a live reviewer, an absent author — it refuses. It
        # admits the same row once every party reads ABSENT, and the sentence
        # says so rather than promising it.
        return None, ("%s records no repository binding, so its lane cannot "
                      "be measured. `--reason repo-unreadable` measures "
                      "exactly that missing binding, but its liveness belt "
                      "asks about EVERY party, including the reviewer this "
                      "reason sets aside: it admits only once every party on "
                      "the row reads ABSENT, and refuses while one is live or "
                      "unmeasured" % rid)
    branch = str(row.get("ref_branch") or "").strip()
    if not branch.startswith("refs/heads/"):
        return None, ("%s bound no lane branch when it was dispatched "
                      "(ref_branch %r), so there is no lane to measure idle and "
                      "nothing this record could name as retained; helm never "
                      "guesses a branch from the lane label"
                      % (rid, branch or None))
    listed = _git(gitdir, "for-each-ref",
                  "--format=%(refname) %(objectname) %(committerdate:unix)",
                  branch)
    if listed is None or listed.returncode != 0:
        return None, ("Git could not list %s in %s, so the lane was NOT "
                      "measured" % (branch, gitdir))
    hit = [line.split() for line in (listed.stdout or "").splitlines()
           if line.split()[:1] == [branch]]
    if not hit:
        return None, ("lane branch %s no longer exists in %s — nothing is "
                      "retained, so this is not the exit; `helm lr retire %s "
                      "--reason reviewed-object-destroyed --dry-run` measures "
                      "a row whose reviewed object went with it"
                      % (branch, gitdir, rid[:12]))
    if len(hit[0]) != 3 or not hit[0][2].isdigit():
        return None, ("lane branch %s answered an unreadable tip or commit "
                      "time (%r), so its idleness was NOT measured"
                      % (branch, " ".join(hit[0])))
    lane_tip, committed = hit[0][1], int(hit[0][2])
    idle = int((time.time() - committed) // 86400)
    if idle < SEAT_SILENCE_DAYS:
        return None, ("lane %s tip %s was committed %dd ago — inside the %dd "
                      "silence window, so the cure may still be moving. "
                      "Re-measure after %s with `helm lr retire %s --reason "
                      "author-absent-lane-idle --dry-run`"
                      % (branch, lane_tip[:12], idle, SEAT_SILENCE_DAYS,
                         time.strftime("%Y-%m-%dT%H:%MZ", time.gmtime(
                             committed + SEAT_SILENCE_DAYS * 86400)),
                         rid[:12]))
    trunk, pinned, _target, terr = _trio_trunk(gitdir)
    if terr:
        return None, ("the authoritative trunk of %s is unreadable (%s), so "
                      "whether the lane landed was NOT measured"
                      % (gitdir, terr))
    tips = [(label, sha, _landing_proof(gitdir, sha, pinned))
            for label, sha in (("lane tip", lane_tip),
                               ("reviewed tip", reviewed))]
    # ONE REFUSAL FOR EITHER TIP ON TRUNK, naming both readings, so a landed
    # lane never hides a reviewed tip on trunk and the reverse. IT NAMES NO
    # COMMAND. `close --reason landed` never admits this row:
    # `_close_ladder_landed` refuses a row whose own polarity is FIX as
    # CONTRARY, and `_chain_polarity` returns the row's own FIX before it
    # reads any later verdict, so a later APPROVE does not change it.
    # `--reason superseded` admits only with a later same-chain APPROVE of a
    # landed tip, from the author or a proven handoff, which this measurement
    # does not read. A command named here could refuse the row it names.
    if any(proof in KEPT_PROOFS for _label, _sha, proof in tips):
        return None, ("%s: this work reached %s, so this reason does not "
                      "apply. No command is named here: `close --reason "
                      "landed` refuses a row whose own verdict is FIX, as "
                      "CONTRARY, and `close --reason superseded` needs a "
                      "later APPROVE on the same chain, which this reason "
                      "does not measure"
                      % ("; ".join("the %s %s reads %s on %s"
                                   % (label, sha[:12], proof, trunk)
                                   for label, sha, proof in tips),
                         trunk))
    for label, sha, proof in tips:
        # ABSENT IS ADMITTED ONLY ON AN OBJECT THAT EXISTS. A destroyed object
        # is a different reason, and the landing ladder answers `unknown` for
        # one; ancestry is asked as well so the admission never rests on the
        # ladder alone — it reads a missing object UNDETERMINED, so both must
        # agree before admitting.
        if proof != "absent" or _ancestry(gitdir, sha, pinned) != NOT_ANCESTOR:
            return None, ("Git could not determine whether the %s %s is on "
                          "%s, so this row was NOT measured unlanded; a "
                          "reviewed object that is gone wants `helm lr retire "
                          "%s --reason reviewed-object-destroyed --dry-run`"
                          % (label, sha[:12], trunk, rid[:12]))
    head = ("RETAINS %s at %s (last commit %dd ago, window %dd; lane tip and "
            "reviewed tip %s both unlanded on %s by ancestry and patch-id; "
            "`helm work gc` keeps an unlanded "
            "branch) — chain has no successor still on the board — author "
            "@%s absent from every roster surface (lineage %s): "
            % (branch, lane_tip, idle, SEAT_SILENCE_DAYS, reviewed[:12],
               trunk, author, lineage))
    room = dispatches._RETIRE_MEASUREMENT_CAP - _ABSENT_AUTHOR_BELT_RESERVE \
        - len(head)
    if len(detail) > room:
        detail = detail[:max(room - 3, 0)] + "..."
    return head + detail, None


def _measure_tier_unevaluable_parked(row, ctx):
    """(measurement, refusal) — an APPROVE exists, its reviewer's approval
    tier is measurably DARK, and that reviewer seat is parked: no current
    roster session and no live pane.

    BOTH HALVES ARE MEASURED HERE, and the second is what makes the first
    safe. `TIER_DARK` means only "no proxywatch pass ever stamped a runtime
    proof for this seat", and that sentence is equally true of a deleted seat
    and of a seated one whose upstream is rate-limited — helm's own
    `tier_unknown_cure` says so in as many words ("both live shapes end
    here"). Retiring on the tier alone would write off every walled seat's
    reviews. So the tier answers WHY the row cannot be evaluated and
    `seat_reach` answers whether that is permanent.

    A DARK approval tier does not imply an absent reviewer: rows routinely
    carry a dark tier while their reviewer is plainly reachable, holding a
    current session or a declaring pane. Every such row refuses here,
    correctly, and that refusal is the feature.
    """
    polarity = dispatches._replay_polarity(row.get("polarity"))
    if polarity != "approve":
        return None, ("%s carries polarity %s — this reason speaks only "
                      "about a recorded APPROVE whose authorization tier "
                      "cannot be evaluated"
                      % (row["id"], polarity or "UNDECLARED"))
    if row.get("status") != "verdict":
        return None, ("%s has no recorded verdict, so there is no approve "
                      "whose tier could be unevaluable" % row["id"])
    state, why = dispatches.approval_tier_for_verdict(row)
    kind = dispatches.tier_unknown_kind(state)
    if state in ("ok", "none"):
        # "none" is NOT an unknown: it is helm answering that no approval-tier
        # policy is declared at all, which the land gate reads as permission.
        # Treating it as unevaluable would retire every row on an estate that
        # simply never declared a tier.
        return None, ("%s's approval tier EVALUATES (%s) — the approve is "
                      "authorized and the row can land; nothing is "
                      "unreachable here" % (row["id"], state))
    if state == "outside":
        return None, ("%s's reviewer is MEASURED outside the approval tier "
                      "(%s) — that is a finding about the review, not an "
                      "unreachable proof chain; it wants a re-review"
                      % (row["id"], why or "no reason recorded"))
    if kind != dispatches.TIER_DARK:
        return None, ("%s's tier is unevaluable as %s, not dark (%s) — %s"
                      % (row["id"], kind or "unclassified",
                         why or "no reason recorded",
                         tier_unknown_cure(kind)))
    reviewer = str(row.get("recipient") or "").strip()
    seat_state, detail = seat_reach(reviewer, **ctx["reach"])
    if seat_state == SEAT_LIVE:
        return None, ("%s's reviewer @%s is REACHABLE — %s. A dark tier on a "
                      "live seat is a WALLED upstream, not a parked seat: it "
                      "clears when a proxywatch pass stamps the seat, so the "
                      "row goes the normal path"
                      % (row["id"], reviewer, detail))
    if seat_state == SEAT_UNKNOWN:
        return None, ("%s's reviewer @%s could not be measured — %s. "
                      "Retirement refuses on an unmeasured chain"
                      % (row["id"], reviewer, detail))
    # THE SHARED BELT GUARDS THE ADMIT, same placement as its sibling. This
    # reason has already refused every case it can DIAGNOSE — wrong polarity,
    # no verdict, tier evaluates, reviewer outside, tier not dark, reviewer
    # reachable or unmeasurable — each with a sentence naming what it found.
    # Running the belt at entry would shadow all of them with a generic "a
    # party is reachable", which is true and strictly less useful to whoever
    # reads the refusal. So it runs here, on the one path that would say yes.
    return ("approve tier DARK (%s); reviewer @%s is %s: %s"
            % (why or "no reason recorded", reviewer, seat_state, detail)), None


# Ordered MOST-FOUNDATIONAL FIRST, and the order is a claim: a row whose own
# repository cannot be read has no other proof worth attempting, and a row
# whose succession pointer is dead is unreachable regardless of who owes it.
# A sweep reports the FIRST reason that measures, and the refusals of the
# others beside it, so a reader can see the whole classification rather than
# the winner alone.
RETIRE_MEASUREMENTS = {
    "repo-unreadable": _measure_repo_unreadable,
    "succession-unreadable": _measure_succession_unreadable,
    # AFTER the two pointer reasons and BEFORE the two seat reasons: a
    # destroyed proof object outranks any question about who owes the row,
    # and it is inserted rather than placed at either end so no existing
    # pairwise precedence moves (a sweep reports the FIRST reason that
    # measures, so a reorder would silently re-classify historical rows).
    "reviewed-object-destroyed": _measure_reviewed_object_destroyed,
    "tier-unevaluable-parked": _measure_tier_unevaluable_parked,
    "author-unresolvable": _measure_author_unresolvable,
    # LAST, so no existing pairwise precedence moves. Under the sweep's full
    # belt it admits only rows `author-unresolvable` already admitted, so it
    # never wins a sweep; its door is the single-id call, where
    # `_RETIRE_BELT_DISCHARGED` stops the belt asking a discharged reviewer.
    "author-absent-lane-idle": _measure_author_absent_lane_idle,
}
RETIRE_SWEEP_ORDER = ("repo-unreadable", "succession-unreadable",
                      "reviewed-object-destroyed",
                      "tier-unevaluable-parked", "author-unresolvable",
                      "author-absent-lane-idle")


def _retire_context(snapshot=None, reach=None):
    """The read-once instrument bundle every measurement in ONE run shares.

    A sweep classifies dozens of rows; measuring the roster and the process
    table per row would let a hundred classifications straddle a roster
    rewrite and disagree about the same seat. One reading, threaded through.
    """
    if snapshot is None:
        # THE SWEEP'S OWN READ, NOT A LENIENT ONE OF THIS DOOR'S. The sweep
        # hands in its strict `project_raw` snapshot; the single-ID path read
        # `dispatches.snapshot()`, which SKIPS a complete corrupt line and
        # measured silence over a record with a hole in it. `retire_read` is
        # that one door and argues the strictness.
        snapshot, unavailable = dispatches.retire_read()
        if unavailable:
            return None, "dispatch ledger unavailable: %s" % unavailable
    if reach is None:
        # THE SAME CONSTRUCTOR THE DOOR USES. This built its own TWO-instrument
        # dict, so a sweep carried no presence reading at all and every row
        # measured the projection for itself — the once-per-action contract
        # broken on the one path that classifies dozens of rows.
        reach = _reach_bundle()
    return {"snapshot": snapshot, "reach": reach}, None


def _retire_locked_ctx(snapshot):
    """The context the LOCKED re-measure runs under: the ledger being held,
    and a FRESH reachability reading.

    THE DOOR OWN CONTEXT IS DELIBERATELY NOT REUSED. A sweep reads the roster
    and the process table ONCE so a hundred classifications cannot straddle a
    roster rewrite — which is right for CLASSIFYING and wrong for WRITING: by
    the time the thirtieth row is appended that reading is minutes old, and a
    seat that came back in between would be written off on a measurement
    taken before it did. Measuring later only shrinks that window; measuring
    AT THE MOMENT OF ACTION closes it, and the moment of action is inside
    this lock. The cost is one roster read and one process-table walk per
    retirement, held under the ledger lock — deliberately paid, on the same
    reasoning that puts abandon Git probes there.

    An empty `reach` is the instruction to measure: `seat_reach` treats
    roster=None as "read it yourself".
    """
    return {"snapshot": snapshot, "reach": {}}


def _retire_seat():
    """The acting seat, from this process's own environment, read through
    home.chat_name — THE validated ingestion seam.

    Reading HELM_CHAT_NAME raw here would let a control-char / bidi seat name
    become the AUTHOR of a retire ledger row, which is precisely the sink the
    cross-module display-launder tripwire exists to close. A hostile name
    RAISES (home.SeatNameError) rather than retiring a land request under a
    laundered identity; unset still returns None, so the caller's own "needs
    an acting seat" refusal stays the message a human sees.
    """
    return home.chat_name()


def retire(rid, reason, note=None, seat=None, dry_run=False, ctx=None,
           preflight=None, call_shape=RETIRE_CALL_SINGLE_ID):
    """(result, err) — administratively retire ONE land request.

    The measurement runs TWICE by design: once here, so a refusal is reported
    without touching the ledger and a `--dry-run` can rehearse the whole
    classification; and once inside `_record_retire_proven`'s lock, which is
    the run that authorizes the append. Only the second is recorded. The pair
    is not redundant — this one exists to give a human a readable refusal, and
    that one exists because the world can change between a listing and a
    write.

    `call_shape` DEFAULTS TO SINGLE-ID BECAUSE THAT IS WHO CALLS THIS BY NAME:
    the CLI's `retire <id> --reason CODE`, one explicit act on one named row.
    The sweep is the only other caller and it passes `RETIRE_CALL_SWEEP`, which
    is carried into BOTH measurements — the readable one here and the locked
    one that authorizes the append — so an unattended pass can never reach the
    belt exemption through this door either.
    """
    reason = str(reason or "").strip()
    if reason not in RETIRE_REASONS:
        return None, ("retire --reason must be one of %s"
                      % "|".join(RETIRE_REASONS))
    seat = seat or _retire_seat()
    if not seat:
        return None, ("retire needs an acting seat — set HELM_CHAT_NAME or "
                      "pass --seat; an administrative terminal with no actor "
                      "on it is an unauditable write")
    if ctx is None:
        ctx, err = _retire_context()
        if err:
            return None, err
    row, err = dispatches._resolve_row(
        ctx["snapshot"], rid, noun="land request", list_hint="helm lr list", allow_retired=True)
    if err:
        return None, err
    if row.get("retired_admin"):
        if row.get("retire_reason") == reason:
            return dict(row), None
        return None, ("%s is already retired under --reason %s"
                      % (row["id"], row["retire_reason"]))
    terminal = dispatches._close_retired_by(row)
    if terminal:
        return None, ("%s is already retired by %s — retirement is for rows "
                      "nothing else could close" % (row["id"], terminal))
    if row.get("status") not in dispatches._RETIRABLE_STATUSES:
        return None, ("%s is %s, which bills nothing — retire clears a LIVE "
                      "obligation" % (row["id"],
                                      row.get("status") or "in an unknown state"))
    if preflight is None:
        measurement, refusal = _measure_retire(reason, row, ctx,
                                               call_shape=call_shape)
        if refusal:
            return None, refusal
    else:
        # A CALLER THAT ALREADY MEASURED AGAINST THIS EXACT ctx HANDS ITS
        # RESULT IN. The sweep preflights every row to build its plan, and
        # re-running the identical measurement here made it THREE per row —
        # preflight, door, locked — of which only the LOCKED one is load
        # bearing (it must resolve against the ledger the lock is holding,
        # not the one read seconds earlier). The door measurement is the
        # redundant middle, and this drops exactly that one.
        measurement = preflight
    if dry_run:
        return {"dry_run": True, "id": row["id"], "reason": reason,
                "measurement": measurement, "seat": seat, "note": note,
                "would_append": True}, None
    # THE LOCKED RUN GETS THE LOCKED SNAPSHOT. Its measurement must resolve a
    # succession carrier against the ledger it is holding, not against the one
    # this door read seconds ago — a carrier retipped in between would be
    # judged on a row nobody is looking at any more.
    out, err = dispatches._record_retire_proven(
        row["id"], reason, seat, note,
        lambda fresh, current: _measure_retire(
            reason, fresh, _retire_locked_ctx(current),
            call_shape=call_shape))
    if err:
        return None, err
    # THE RAW APPLIED ROW, deliberately, not `get()`. A projection replays the
    # whole ledger and runs git per row (measured at seconds per row), and a
    # sweep calls this once per retirement — so the terminal a caller is
    # handed is the state machine's own output for the event just written,
    # which carries every retire_* field a surface needs.
    return out, None


def _retire_billing_sets(lrs, raw, all_projects=False):
    """({class: {id}}, err) — the sweep's population, taken FROM THE COUNTERS.

    THIS IS NOT A RE-DERIVATION, and the difference is the whole point. A
    first cut of this asked each projected row for `lr["stalled"]` and
    `lr["state"] == "REVIEWED"` directly, which sounds like the same question
    and is not: `_stalled_rows` additionally drops rows RELIEVED by a chain
    successor and rows belonging to another project, and `_unmeasurable_rows`
    drops the ones a same-tip descendant already measured. A re-derived
    version considers far more rows than the board's own counters charge
    for — it would retire chain-folded predecessors nobody was billed for,
    and other projects' rows this board does not own.

    So the sets come from the exact functions that print the numbers. A row
    can only be swept if a counter the owner reads is charging for it, and a
    bucket added to the board later is a visible omission here rather than a
    silent over-reach.
    """
    try:
        stalled = {lr["id"] for lr in _stalled_rows(lrs, raw, all_projects)}
        unmeasurable = {lr["id"]
                        for lr, _why in _unmeasurable_rows(lrs, raw,
                                                           all_projects)}
    except _ChainUntrustworthy as e:
        return None, str(e)
    # The CONTRARY banner's own predicate, one place over (`_line`, `card`,
    # the console badge): a live contradiction that succession has not
    # honored. `honored_display` is the display quiet-down and is exactly
    # what the board reads, so a row the board renders as quiet is not
    # charging and is not swept.
    contrary = {lr["id"] for lr in lrs.values()
                if lr.get("contrary") and not lr["terminal"]
                and not honored_display(lr)
                and (all_projects or _this_boards_row(lr))}
    return {"stalled": stalled, "unmeasurable": unmeasurable,
            "contrary": contrary}, None


def retire_sweep(older_than_days=RETIRE_SWEEP_DEFAULT_DAYS, dry_run=True,
                 seat=None, note=None, now=None, all_projects=False):
    """(report, unavailable) — classify every currently-billing anomaly row
    older than N days, retire the ones that classify, name the ones that do
    not and why.

    THE REFUSALS ARE THE PRODUCT, as much as the retirements. A sweep that
    printed only what it cleared would be indistinguishable from a sweep whose
    measurements were broken, and the population it must NOT touch — live
    seats, walled upstreams, readable chains — is exactly the population a
    careless version of this verb would destroy. So every considered row
    appears in the report with either the reason that carried it or every
    reason's refusal.
    """
    seat = seat or _retire_seat()
    if not dry_run and not seat:
        return None, ("retire --sweep needs an acting seat — set "
                      "HELM_CHAT_NAME or pass --seat")
    try:
        days = float(older_than_days)
    except (TypeError, ValueError):
        return None, "retire --older-than needs a number of days"
    if days < 0:
        return None, "retire --older-than cannot be negative"
    cutoff_s = days * 86400.0
    lrs, raw, unavailable = project_raw(now)
    if unavailable:
        return None, unavailable
    sets, err = _retire_billing_sets(lrs, raw, all_projects)
    if err:
        return None, err
    ctx, err = _retire_context(snapshot=raw)
    if err:
        return None, err
    billing = set().union(*sets.values())
    report = {"older_than_days": days, "dry_run": bool(dry_run),
              "seat": seat, "all_projects": bool(all_projects),
              "considered": 0, "billing": len(billing),
              "skipped_live_tla": [], "plan": [], "refused": [],
              "retired": [], "errors": [],
              "by_class": {name: len(ids) for name, ids in sets.items()}}
    for lr in sorted(lrs.values(), key=lambda r: -r.get("dwell_s", 0)):
        if lr["id"] not in billing:
            continue
        # A row can charge more than one counter (a stalled contrary is both);
        # the label names every bucket it is billing, so the report cannot
        # under-state what clearing it relieves.
        klass = "+".join(name for name in sorted(sets)
                         if lr["id"] in sets[name])
        # A DWELL THAT WAS NEVER MEASURED IS NOT AN AGE. `dwell_known` False
        # means the row's entry instant is unreadable or impossible, and a
        # row that renders "0m" forever would otherwise be swept the moment
        # the bar was lowered, or never — either way on a number nothing
        # measured.
        if not lr.get("dwell_known") or lr.get("dwell_s", 0) < cutoff_s:
            continue
        report["considered"] += 1
        row = raw.get(lr["id"])
        if row is None:
            report["errors"].append(
                {"id": lr["id"], "err": "no raw ledger row for this "
                                        "projection — nothing measured"})
            continue
        role, owing = _retire_owing_seat(row)
        if _retire_owner_is_live(owing, (ctx or {}).get("reach")):
            report["skipped_live_tla"].append(
                {"id": lr["id"], "lane": lr.get("lane"), "class": klass,
                 "role": role, "seat": owing, "dwell_s": lr.get("dwell_s")})
            continue
        refusals, carried = [], None
        for reason in RETIRE_SWEEP_ORDER:
            # EVERY REASON UNDER THE FULL BELT HERE, the exempt one included.
            # The skip above asks only about the ONE OWING seat; the belt
            # requires EVERY party absent. A row whose owing seat is gone and
            # whose sender is live reaches this loop, and an unattended pass
            # may not spend it — the single-id door is where a human takes
            # that responsibility by name.
            measurement, refusal = _measure_retire(
                reason, row, ctx, call_shape=RETIRE_CALL_SWEEP)
            if refusal:
                refusals.append({"reason": reason, "refusal": refusal})
                continue
            carried = (reason, measurement)
            break
        if carried is None:
            report["refused"].append(
                {"id": lr["id"], "lane": lr.get("lane"), "class": klass,
                 "state": lr.get("state"), "dwell_s": lr.get("dwell_s"),
                 "refusals": refusals})
            continue
        reason, measurement = carried
        entry = {"id": lr["id"], "lane": lr.get("lane"), "class": klass,
                 "state": lr.get("state"), "dwell_s": lr.get("dwell_s"),
                 "reason": reason, "measurement": measurement,
                 "also_refused": refusals}
        report["plan"].append(entry)
        if dry_run:
            continue
        out, err = retire(lr["id"], reason, note=note, seat=seat, ctx=ctx,
                          preflight=measurement,
                          call_shape=RETIRE_CALL_SWEEP)
        if err:
            report["errors"].append(
                {"id": lr["id"], "reason": reason, "err": err})
            continue
        report["retired"].append(dict(entry, retire_ts=out.get("retire_ts")))
    report["would_clear"] = len(report["plan"])
    return report, None


def off_frontier_census(apply_it=False, all_projects=False, lrs=None,
                        raw=None):
    """(report, err) — the off-frontier residue, and optionally its clearance.

    `lrs`/`raw` ARE A PROJECTION THE CALLER ALREADY READ, and handing one in
    is how the header and this verb become one reading rather than two that
    agree by luck. MEASURED before they could: one process, one copy of the
    live ledger, `filed_split` read 304 placed rows and this census read 316
    on the very next line — the same classifier over two projections, because
    `open_bucket_rows` membership moves with the wall-clock derive budget each
    projection gets, and this function opened its own. The twelve disputed
    rows were all abandoned-unreachable; the classifier itself agreed row for
    row on the population the two reads shared. Omitted, this opens its own
    projection exactly as before — two CLI invocations are still two reads,
    and nothing here can make a later read equal an earlier one; what this
    removes is a second read inside ONE answer.

    DRY BY DEFAULT AND THE CENSUS IS THE DEFAULT OUTPUT, the shape
    `helm lr expired` and `helm lr retire --sweep` already use and for the
    same reason: an operator who cannot see WHICH rows would close cannot
    tell a safe run from a surprising one, and this population is over a
    thousand rows nobody has looked at.

    THE POPULATION IS THE HEADER'S OWN. `open_bucket_rows` is the single
    owner of "what `filed_split` counts as open", so the residue this verb
    offers to clear and the residue the header reports are one number by
    construction rather than by agreement.

    THE CENSUS AND THE CLEARANCE RUN THE SAME PREDICATE BY CALL —
    `off_frontier_reason` answers for both — so the preview cannot promise a
    population the act then disagrees with. And UNCLASSIFIED survives to the
    output as its own bucket: "could not tell" must be countable separately
    from "measured and no", because it is the only bucket whose rows are
    never touched by `--apply`.

    `--apply` PROPOSES AND THE EXISTING LADDER DECIDES. This verb mints no
    close vocabulary: it hands each row the reason its measurement AND its
    verdict polarity support (`off_frontier_route`) and reports whatever
    `close` answers, in `close`'s words.

    THE ROUTE IS TWO FACTS, and reading only the first is what made the first
    cut close nothing at all. It proposed `landed` for every placed row on a
    board that is 295 of 301 FIX, and met the same correct refusal every time
    — a contrary on trunk is contrary DEBT, and a lane going away answers no
    finding. The doors that DO admit this population were already in the
    registry: `carried` for a contrary whose work reached trunk (its own
    docstring names "a FIX-verdicted row whose work landed anyway" as the
    class it exists for), `withdrawn` for a tip that is on no ref and not on
    trunk. `stranded` is gone from the table: it demands a PRUNED object while
    `off_frontier_reason` proves the object PRESENT before it will answer
    `abandoned-unreachable`, so that route was refused by construction.

    `superseded` IS NOT ROUTED TO, AND THAT IS A MEASUREMENT RATHER THAN AN
    OMISSION. It is the door a reader reaches for first — `landed`'s own
    refusal lists it first among the doors for contrary debt — but its ladder
    needs a later APPROVE VERDICT ROW on the same work chain that clears the
    approval-authority rung, not merely a later tip. Asked twice against the
    live ledger, once with each chain-derived successor tip, it refused every
    sampled row with the same sentence: no later approve binds the superseding
    tip. Routing there would have been a second door that cannot open, which
    is exactly the defect `stranded` was.

    THE REFUSALS ARE STILL THE PRODUCT. MEASURED against the live ledger,
    `carried`'s witness cannot be taken for an EXACT-ancestor tip — the
    replay range is empty — so that bucket comes back refused with the
    instrument's own words. Those rows are the population task/2334 calls
    doorless, now enumerated per row with the measurement that puts each one
    there.

    AND THE REFUSAL IS MEASURED BEFORE THE OWNER IS OFFERED THE ROW, not
    after. `off_frontier_closable` asks each door, dry, on the same walk that
    classified the row, so `plan` is what the classification found and
    `closable` is what the ladder will take — two numbers, never one word for
    both. `--apply` then acts on exactly the rows that walk admitted and
    reports the others as REFUSED in the ladder's own sentence, which is the
    sentence the preflight already read: attempting a close whose gate has
    just refused, in the same process, against the same ledger instant, would
    re-derive every witness to be told the same thing twice.
    """
    if lrs is None or raw is None:
        lrs, raw, unavailable = project_raw()
        if unavailable:
            return None, unavailable
    rows = [lr for lr in open_bucket_rows(lrs)
            if all_projects or _this_boards_row(lr)]
    report = {"dry_run": not apply_it, "all_projects": bool(all_projects),
              "considered": len(rows), "on_frontier": 0, "off_frontier": 0,
              "unclassified": 0, "by_reason": {}, "plan": [],
              "unclassified_rows": [], "closed": [], "refused": [],
              "closable": 0, "not_closable": 0, "not_closable_rows": []}
    verdicts = frontier_verdicts(rows, lrs=lrs, world=frontier_world(raw))
    for lr in rows:
        verdict = verdicts[lr["id"]]
        reason = verdict["reason"]
        if reason == ON_FRONTIER:
            report["on_frontier"] += 1
            continue
        entry = {"id": lr["id"], "lane": lr.get("lane"),
                 "state": lr.get("state"), "dwell_s": lr.get("dwell_s"),
                 "reason": reason, "rung": verdict.get("rung"),
                 "evidence": verdict["evidence"]}
        if reason == OFF_FRONTIER_UNCLASSIFIED:
            report["unclassified"] += 1
            report["unclassified_rows"].append(entry)
            continue
        # THE ROUTE CAME WITH THE CLASSIFICATION, and deliberately: the
        # polarity rung can refuse (an unreadable chain is not a non-contrary
        # one), and a row it refuses must land in the SAME bucket for the
        # header as for this verb. Re-deriving it here is how the two would
        # drift again one rung below where they last did.
        report["off_frontier"] += 1
        report["by_reason"][reason] = report["by_reason"].get(reason, 0) + 1
        entry["polarity"] = verdict.get("polarity")
        entry["close_reason"] = verdict["close_reason"]
        entry["closable"] = bool(verdict.get("closable"))
        report["plan"].append(entry)
        # THE DOOR ALREADY ANSWERED, ON THIS WALK. A row its gate refuses is
        # counted apart and reported with the ladder's own refusal — the same
        # sentence a close would print, read before the owner is told the row
        # is clearable rather than after he runs the verb.
        if not entry["closable"]:
            report["not_closable"] += 1
            refusal = dict(entry, refusal=verdict.get("closable_why"))
            report["not_closable_rows"].append(refusal)
            if apply_it:
                report["refused"].append(refusal)
            continue
        report["closable"] += 1
        if not apply_it:
            continue
        # `repo` IS NOT PASSED, and that is the row's own binding winning.
        # `_close_repo` returns the stored `repo_id` when no override arrives;
        # handing it that same value back routes it through the OVERRIDE arm,
        # which resolves the path as a WORKING TREE — and a row's binding is a
        # git common-dir. The close would then refuse "not a readable Git
        # working tree", a spurious answer that hides whatever the ladder was
        # really going to say.
        #
        # THE DELIVERY DECLARATION RIDES ONLY `landed`, and `close` refuses it
        # on every other reason — so it is passed exactly where the ladder
        # demands one and nowhere else. CLI-class is the only class a sweep
        # can honestly declare: it says helm's own code runs fresh per
        # invocation, and the ladder still measures the guard rail and refuses
        # if THIS checkout is running pre-land rules.
        out, err = close(lr["id"], entry["close_reason"],
                         evidence=_off_frontier_evidence(entry, verdict),
                         trunk=verdict.get("trunk"),
                         live=entry["close_reason"] == "landed")
        if err:
            report["refused"].append(dict(entry, refusal=err))
            continue
        report["closed"].append(dict(entry, closed_ts=(out or {}).get("close_ts")))
    return report, None


def _off_frontier_evidence(entry, verdict):
    """The measurement, plus the half the chosen door asks for in prose.

    `carried` and `withdrawn` each REQUIRE evidence and each say what it is
    for: carried wants the reason no discharge was ever recorded (the half
    git cannot supply), withdrawn wants the attestation that this debt will
    not land. The measurement alone answers neither, and a sweep that handed
    both doors the same bare sentence would be filing prose that does not
    address the door it opened. Clipped by `dispatches._clean` at the writer;
    the measurement leads so a truncation costs the commentary, never the
    proof."""
    clause = {
        "carried": (" — the lane was reaped with no discharge recorded, so "
                    "nothing in the ledger says the work arrived"),
        "withdrawn": (" — the lane was reaped and the work is on no ref, so "
                      "this debt will not land"),
    }.get(entry.get("close_reason"), "")
    return verdict["evidence"] + clause


def annotate_delivered_report(rid, artifact_ref, report_ref, evidence):
    """Append the explicit correction for one historical cancelled BUILD."""
    row, err = dispatches.record_delivered_report_correction(
        rid, artifact_ref, report_ref, evidence)
    if err:
        return None, err
    return get(row["id"])




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

# ADDING A REASON HERE IS FOURTEEN REGISTRATIONS, AND THIS IS THE ONLY PLACE
# THEY ARE LISTED TOGETHER. The tree records this class in three separate comments —
# `dispatches.CLOSE_REASONS` calls the writer/CLI split "the trap", the
# `chain-proof` entry in `_CLOSE_STATE_FIELDS` calls itself "the seventh
# registration point", and `test_every_writer_reason_has_a_TERMINAL_STATE`
# calls `rowstate._CLOSE_TERMINAL` "the register nobody pinned" — each naming
# the ONE it was bitten by. Nobody wrote the list, so every author rediscovers
# the next entry the expensive way. It is written here because this is the
# tuple a new reason starts in:
#
#   1. this tuple                        the CLI offers it
#   2. dispatches.CLOSE_REASONS          the writer ACCEPTS it (else refused)
#   3. dispatches._CLOSE_POLARITY        which verdict polarities may use it
#   4. dispatches._CLOSE_STATE_FIELDS    which proof keys PERSIST (else silent)
#   5. rowstate._CLOSE_TERMINAL          the word an operator READS
#   6. cli._VERB_HELP["lr"]              the help text
#   7. USAGE (this module)               the usage string
#   8. docs/VERBS.md                     the synopsis AND the prose COUNT
#
# AND THE SIX THE LIST ITSELF OMITTED, measured by walking the newest reason
# end to end rather than by remembering it. The list said EIGHT while a reason
# registered in all eight still could not be CALLED — `close_routes` is a raw
# dict index, so the ninth entry is a KeyError on the real invocation and the
# comment that exists to stop the expensive rediscovery was itself a way to
# have it:
#
#   9. landreq_close.close_routes        the ladder is REACHABLE (else KeyError)
#  10. landreq_close._close_ladder_<r>   the ladder EXISTS
#  11. _OWNER_NAMES["landreq_close"]     the tail binding re-exports the name
#  12. dispatches_close._close_event_error  a per-reason arm (else the chain's
#                                        default ACCEPT admits a proofless event)
#  13. dispatches_close._close_idempotent   a retry identity (else an honest
#                                        retry takes the retired-once refusal)
#  14. dispatches._apply                 ONLY if the reason may close a row
#                                        that never got a verdict; a
#                                        verdict-only reason inherits the
#                                        general arm, and getting this wrong
#                                        makes the writer report success over
#                                        a row that stayed open
#
# CONDITIONAL ON WHAT THE REASON CLAIMS, not on taste: REPO_TRUNK_REASONS (it
# measures in a named repository), dispatches.CLOSE_EXACT_PROOF_MODE (it
# records exactly one witness), _REF_FIELDS (it persists a commit id),
# `_record_close_proven`'s keyword list (it persists a field no reason
# persisted before), and the per-reason flag gates in `landreq_cli._cmd_close`
# (it takes a flag of its own).
#
# THEY FAIL IN THREE DIFFERENT REGISTERS, which is why one green run proves
# little: 1/2/6/7/8 have parity arms that name them in a single failure;
# 5 does NOT refuse at all, it falls past the table and the row renders as
# some other word; 3, 4, 12, 13 and 14 RAISE, DROP or SILENTLY ACCEPT at the
# write and are unreachable from a DRY RUN — so a reason can be documented,
# spelled correctly everywhere and green across dozens of arms while
# persisting nothing. WRITE ONE ARM THAT DRIVES THE REAL WRITE PATH and all
# five announce themselves immediately.
#
# 3 AND 5 ARE POLICY, NOT BOOKKEEPING. Read their reasoning before adding a
# value: `_CLOSE_POLARITY` carries the law that concur authorizes nothing,
# with an arm computing the admitting set from the live map so a later
# addition cannot slip in; `_TERMINAL_OVERRIDES_VERDICT` is one entry long and
# says other reasons join by argument. Joining either is a contract change for
# every member, never a registration.
CLOSE_CLI_REASONS = ("landed", "superseded", "withdrawn", "out-of-scope",
                     "stranded", "subsumed", "delivered-report", "discharged",
                     "resolved", "carried", "chain-proof", "expired",
                     "endorsement-moot")

#: The reasons whose proof is computed IN a named repository against ITS trunk,
#: so `--repo/--trunk` are the honest way to name them. ONE tuple, because the
#: refusal message is built from it: a hand-written list beside a membership
#: test is a second spelling, and it had already gone stale on `chain-proof`.
REPO_TRUNK_REASONS = ("landed", "stranded", "subsumed", "carried",
                      "chain-proof", "expired", "endorsement-moot")

#: Close reasons whose TERMINAL decides the rendered state even for a
#: FIX/SUPERSEDE row, overriding "verdict intent remains the lifecycle state".
#: NARROW ON PURPOSE. That rule is deliberate for `withdrawn` and the discharge
#: doors and is pinned by their own arms — my first cut applied the register to
#: every reason and flipped two of them, which is a contract change wearing a
#: bug fix. `chain-proof` belongs here because its terminal is the ONLY thing
#: that decides its state: the row's own verdict says CHANGES_REQUESTED while
#: rowstate says SUPERSEDED, and one row rendering two words is how a burn-down
#: counts what an operator cannot see. Other reasons join by argument, not by
#: accident.
_TERMINAL_OVERRIDES_VERDICT = ("chain-proof",)

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
    if lr.get("retired_admin"):
        return "retire --reason %s" % lr.get("retire_reason")
    if lr.get("verdict_retracted"):
        return "retract (its %s verdict was taken back)" % str(
            lr.get("retracted_polarity") or "undeclared").upper()
    if lr.get("close_reason"):
        return "close --reason %s" % lr["close_reason"]
    return None


def _retired_refusal(lr):
    return ("%s is already retired by %s; a row is retired once — refusing a "
            "different closure" % (lr["id"], _retired_by(lr)))


def _resolve_row_for_diagnosis(current, rid, noun="land request",
                               list_hint="helm lr list"):
    """(row, err) — resolve for a door that must DIAGNOSE a terminal row.

    THE ONE PLACE IN THIS MODULE PERMITTED TO SEE A RETIRED ROW ON A CLOSE
    PATH, and it never hands one back. A retired row is consumed here into
    `(None, <specific terminal refusal>)`, so no caller can receive it as
    something actionable — the exemption is an EARNED PROPERTY of this
    function, not a flag five callers each carry and could each misuse.

    Why it exists at all: the shared terminality rung can only say a row "was
    administratively retired". These doors can say `retire --reason
    author-unresolvable` — the exact terminal verb and its registered reason —
    and a refusal a caller can DIAGNOSE beats one the belt can only ASSERT.

    The refusal names the bounded terminal verb, the REGISTERED reason and the
    timestamp. It never echoes the retire note or measurement, it performs no
    append and no other side effect, and it cannot fall through: a retired row
    leaves here as an error every time.
    """
    row, err = dispatches._resolve_row(current, rid, noun=noun,
                                       list_hint=list_hint,
                                       allow_retired=True)
    if err:
        return None, err
    if row.get("retired_admin"):
        when = str(row.get("retire_ts") or "").strip() or "an unrecorded time"
        return None, ("%s — %s at %s" % (_retired_refusal(row),
                                         _retired_by(row), when))
    return row, None


def _retirement_stands(lr, lrs=None, repo=None):
    """The retired-once refusal iff the standing retirement still BLOCKS a
    different closure — None when a discharge ladder may proceed past it.

    THE ONE CARVE-OUT IS THE CONTRADICTED WITHDRAWAL (the lifecycle clause in
    `_lr`): a withdrawal's premise — the change stayed off trunk — is
    FALSIFIABLE, and once the FIX/SUPERSEDE-verdicted change is proven on
    trunk the projection re-exposes the row as CONTRARY, owed by the
    integrator, prescribing the confirmation-round protocol. Every discharge
    door then refused at this very rung, so the designed debt had no verb
    (measured live 2026-08-11 on an operator row; the id is deliberately not
    quoted — it names a DISPATCH row no other clone could dereference, and
    the claim is the shape).

    THE CONTRADICTION IS RE-DERIVED HERE, never read from a projection field
    (a projection field is stale by exactly one append): a withdrawn-class
    retirement with NOTHING ELSE standing; a FIX/SUPERSEDE polarity, own or
    chain-declared (`_chain_polarity`, the walk every polarity gate lends);
    and the reviewed change historically ON the pinned trunk (`landed_ever`
    — ancestry or patch identity, the same fact the projection's `_landed`
    legs fold — against the discharge trio's pin, stricter than the
    read-side projection by the trio's own law). FAIL-CLOSED THROUGHOUT: any
    other standing retirement, a silent or conflicting polarity, an
    unresolvable repository or trunk, or an UNKNOWN land state keeps the
    retirement standing — only the measured contradiction opens the door,
    absence of proof never does. Which LATER rung then admits or refuses the
    row stays each ladder's own question; this helper only stops the
    withdraw from answering it first.

    RETURNS (refusal, contradiction). When the carve-out opens the door,
    refusal is None and `contradiction` carries the GIT LEGS of the
    re-derivation — the `landed_ever` leg used and the pinned trunk it was
    measured on — for the discharging close event
    (`dispatches._CONTRADICTION_PROOF_FIELDS`). The STATE legs (retirement
    class, polarity, chain provenance, same-code set) are deliberately NOT
    captured here: `dispatches._rebind_contradiction` re-derives them under
    the ledger lock at the moment of write, because a chain verdict can land
    between this read and that lock and the event must record the truth it
    bound, never the truth this door happened to see (the
    measured-early-used-late law). Every refusing path returns
    (refusal, None); an unretired row returns (None, None)."""
    if not _retired_by(lr):
        return None, None
    refusal = _retired_refusal(lr)
    if not (lr.get("withdrawn") or lr.get("close_reason") == "withdrawn"):
        return refusal, None
    if lr.get("discharged") or lr.get("closed_by_landing") \
            or lr.get("abandoned") \
            or lr.get("close_reason") not in (None, "withdrawn"):
        return refusal, None
    # ONE derivation rule for the STATE legs — the same whole-object binder
    # the locked writer runs (`dispatches._rebind_contradiction`), so the
    # admission, every door rung downstream, and the write can never act on
    # different polarities (the old shape admitted a
    # chain-declared contradiction here and a per-case `polarity` read
    # re-refused it one rung later). It refuses own-APPROVE, a conflicted
    # or silent or approve-only chain, and an unreadable sidecar — the
    # admission semantics `_chain_polarity` carried, one owner now.
    capture = {}
    if dispatches._rebind_contradiction(capture, lr, lrs) is not None:
        return refusal, None
    reviewed = str(lr.get("reviewed_tip") or "")
    gitdir, err = _close_repo(lr, repo)
    if err:
        return refusal, None
    _ref, pinned, _target, err = _trio_trunk(gitdir)
    if err:
        return refusal, None
    # The TYPED leg, because the capture is the proof: `landed_ever` folds
    # ancestor and patch-equivalent into one True, and an event that cannot
    # say WHICH leg carried it records a measurement nobody can re-run.
    leg = _landing_proof(gitdir, reviewed, pinned)
    if leg not in ("ancestor", "patch-equivalent"):
        return refusal, None
    capture.update(contradiction_land_leg=leg,
                   contradiction_trunk_ref=_ref,
                   contradiction_trunk_sha=pinned)
    return None, capture


def _pin_ref(gitdir, ref):
    """(full sha, err) — ONE resolution of `ref` at ladder entry (D7). Every
    rung below runs against the pinned sha, so a trunk that moves mid-ladder
    cannot make two rungs answer about two different trunks; the recorded pin
    is what makes a proof that went stale-but-was-true auditable."""
    p = _git(gitdir, *_REF_ARGV, ref + "^{commit}")
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




def _trunk_aliases(stored):
    aliases = {stored}
    stored = str(stored or "")
    if stored.startswith("refs/heads/"):
        aliases.add(stored[len("refs/heads/"):])
    if stored.startswith("refs/remotes/"):
        aliases.add(stored[len("refs/remotes/"):])
    return aliases




def _rehearsed(row, fields):
    """(rehearsal, None) — the ladder's own preview fields OVER the writer's
    candidate event.

    task/2857 moved the rehearsal INSIDE the writer, so every ladder now calls
    `dispatches._record_close_proven` even for a dry run and gets back the
    exact event that just passed every row-state check the writer runs (on an
    unlocked snapshot, because a rehearsal appends nothing).
    This merges that with what only the ladder can know — the fan-out preview,
    the hold kind, the overridden reviewer — and the LADDER WINS on any key
    both spell, so no rehearsal field any caller reads changes value.

    AN ALREADY-CLOSED ROW COMES BACK AS THE STANDING ROW, with no `dry_run`
    marker, and passes through untouched. That is the idempotent answer, the
    one `_close_result` turns into "ALREADY closed — no append needed", and
    stamping a rehearsal onto it would report `would_append` for a row that
    needs no append.
    """
    if not isinstance(row, dict) or not row.get("dry_run"):
        return row, None
    return dict(row, **fields), None






def credit_line(credited):
    """The AUTHORS line a landed close prints, or None when there is nothing
    to say.

    A SEAM RATHER THAN AN INLINE FORMAT, because the thing worth pinning is the
    THRESHOLD: one author prints NOTHING. The ordinary lane has one author, and
    a line on every close is a line the reader learns to skip — which would
    make the several-author case, the only one this exists for, invisible
    exactly where it matters.
    """
    names = [name for name in (credited or ())
             if isinstance(name, str) and name.strip()]
    if len(names) < 2:
        return None
    return ("  AUTHORS %s — this lane carried several authors and every one "
            "the chain records is credited" % ", ".join(names))










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


def _same_code_tips(lr):
    """({the reviewed tip and its recorded rewrite translation}, err) — the
    SAME-CODE identity set every chain declaration is matched against.
    Extracted from `_chain_polarity` so the write-time rebind
    (`dispatches._rebind_contradiction`) and the polarity gates walk ONE
    rule; the unreadable-sidecar refusal is verbatim the gate's."""
    reviewed = str(lr.get("reviewed_tip") or "")
    if not reviewed:
        return None, "the row has no reviewed tip to name its code"
    table, terr = _ref_translations_checked()
    if terr:
        return None, (
            "the ref-translation sidecar is unreadable (%s) — the chain's "
            "declared polarity is UNKNOWN, and a polarity gate never consults "
            "a chain it could not fully read" % terr)
    same = {reviewed}
    new = table.get(reviewed)
    if new:
        if not _sha(new):
            full = _git(lr.get("repo_id"), *_REF_ARGV,
                        new + "^{commit}")
            if full is not None and full.returncode == 0 \
                    and full.stdout.strip():
                new = full.stdout.strip().lower()
        same.add(new)
    return same, None




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


def _remember_landing(gitdir, reviewed, pinned, stored_pid=None,
                      carriers=()):
    """Store the (patch-id -> trunk sha) this land just PROVED.

    THE OWNER'S BAR: a landing is a fact proved ONCE when the train lands and
    STORED, so no row is ever "unprovable" on a read. This is the write half.

    It writes into the SAME durable map `_rescue_cache_path` already owns —
    that cache was already keyed on an immutable patch-id and already argued
    for outliving any worktree. What it lacked was a FEED INDEPENDENT OF THE
    CAPPED SCAN: its only writer took `sha = index.get(pid)`, so it could
    never learn a landing the 600-commit window could not already see, and it
    held ten entries while a thousand landed rows read unknown.

    THE PATCH-ID IS DERIVED FROM THIS TIP BY GIT, NOW. Never the receipt's
    stored one — that is correlation evidence, and a hand-built receipt
    carrying a dead tip plus any unrelated live patch-id once claimed its own
    landing. Same rule the read path states at the index consult.

    A failure here is silent BY DESIGN and the cost is the status quo: a cold
    store is slow, never wrong. This must never be able to fail a land.

    WHAT THIS FEED STILL DOES NOT LEARN, stated rather than discovered later:
    a PATCH-EQUIVALENT landing whose carrier no caller hands us. The rung that
    proved it knows which trunk commit is equivalent; it does not return it,
    so there is no candidate here that satisfies the reader and NOTHING is
    written. That is the correct outcome — an absent entry costs one
    re-derivation, a wrong one costs a permanent refusal — and it is not a
    hole in the store: `backfill_landing_store` resolves exactly this case
    through its uncapped index, off the read path.
    """
    try:
        if not pinned:
            return
        # BOTH KEYS, AND THE SECOND ONE IS THE WHOLE POINT. The reader keys on
        # the RECEIPT'S STORED patch-id (`row.get("patch_id")` at the rescue
        # call), not on one derived now, and the two disagree on most rows —
        # so a feed keyed only on the live id misses the reader's key.
        #
        # THE VALUE IS NOT `pinned`. Filing both keys against the new trunk
        # head is what poisoned this store: the head carries the live id at
        # best and neither id at worst, so the reader refused its own feed's
        # entries. Each key is resolved to a sha that actually ANSWERS for it,
        # and a key with no such sha is not written at all — an absent entry
        # costs a re-derivation, a wrong one costs a permanent refusal.
        pids = {p for p in (_tip_patch_id(gitdir, reviewed),
                            str(stored_pid or "").strip().lower() or None) if p}
        if not pids:
            return
        # CARRIERS LEAD, AND THAT ORDER IS THE POINT. On the ancestry rung
        # `reviewed` is itself on trunk and answers for its own id; on the
        # translated and content rungs it is NOT, and the sha that answers is
        # the one that rung just found. Passing them in is what lets the two
        # expensive rungs feed a store the cheap one used to feed alone.
        candidates = tuple(carriers) + (reviewed, pinned)
        entries = {}
        for pid in pids:
            sha = _correlated_sha(gitdir, pid, candidates, pinned)
            if sha is not None:
                entries[pid] = sha
        _merge_rescue_entries(gitdir, entries, pinned)
    except Exception:                          # noqa: BLE001
        pass


BACKFILL_BUDGET_S = 20.0
BACKFILL_SCAN_CAP = 100000   # off the read path: look at the whole trunk


def _readable_correlation(gitdir, pid, sha):
    """Would the READER accept this entry? Its contract, not a copy of it.

    The reader re-proves TWO legs on a cached hit, and the second is that the
    target still CARRIES the patch-id it was cached under. An entry whose
    target does not is written, correct, and permanently unreadable — the
    store fills up and never answers, which is this row's whole defect.
    MEASURED on the first backfill: 8 of 96 entries were dead on arrival.

    A WRITER'S ADMISSION RULE MUST MATCH ITS READER'S. Ancestry is checked by
    the reader at read time against a trunk that moves, so it is deliberately
    NOT checked here; content identity is immutable and is.

    TRI-STATE: True / False / None, and NONE IS NOT FALSE. `_patch_id` is
    fail-open and answers None for TWO different facts — a spawn that failed
    or timed out, and a range with no diff in it — so None here means the
    question was not decided, never that the entry is dead. False demotes a
    held entry and licenses a rewrite; reaching it from an unreadable hash
    replaces a live entry on work that never ran.

    THE TWO CAUSES OF None ARE SEPARATED, through the availability-aware
    hash face this reader was named as the consumer of. EMPTY_RANGE is git
    having LOOKED: the target holds no diff, so it carries no patch-id at
    all, so it cannot be carrying the non-empty one this entry was cached
    under -- a MEASURED False, and the rewrite it licenses is earned.
    UNMEASURED is git not having answered, and stays None.

    AN UNRECOGNISED STATE IS UNMEASURED, not a comparison. The fallthrough
    is deliberately the preserving one: a state added later that this reader
    has never seen must not reach `got == pid` with a None `got` and demote a
    live entry on a question it could not read.
    """
    state, got = _measured_patch_id(gitdir, sha)
    if state == HASHED:
        return got == pid
    if state == EMPTY_RANGE:
        return False
    return None


def backfill_landing_store(gitdir, trunk, budget_s=BACKFILL_BUDGET_S,
                           receipts=None):
    """Seed the landing store from receipts that landed BEFORE the feed
    existed. -> (stored, examined, incomplete), or None if the ledger is
    UNREADABLE — which is a different fact from a ledger with no rows.

    INCOMPLETE MEANS THIS RUN DID NOT DECIDE EVERYTHING IT WAS ASKED. The flag
    was first written for a cut-short history walk and named for that cause,
    which was too narrow twice over: an unreadable trunk pin already returns
    it having walked NOTHING, and a receipt whose ancestry or content git
    could not read is undecided without any walk at all. There is ONE fact
    here and several causes of it — a spent budget, a capped or failed index,
    a pin that could not be taken, a hash that could not be read — because the
    caller acts on exactly one distinction: `incomplete=False` is the sentence
    "there is no work left", and a caller acts on it by never asking again.

    EXAMINED COUNTS WORK, NOT DOUBT. A receipt whose held entry could not be
    re-checked is skipped before it is counted, so a run can honestly report
    examined=0 while reporting incomplete=True. The two answer different
    questions and neither substitutes for the other.

    THE WRITE HALF ALONE DOES NOT MOVE THE OWNER'S NUMBER. `_remember_landing`
    only learns from lands that happen after it, so every historical row stays
    unprovable on a read until something teaches the store about it. This is
    that something, and it runs OFF THE READ PATH — which is the one place the
    600-commit scan is the right tool rather than the defect.

    ONE SCAN, THEN DICT HITS. The naive shape re-derives per receipt and pays
    the whole walk each time; this builds the index once and looks each
    receipt up in it, which is the same trade `_stored_patch_index` documents.

    A CAPPED INDEX STILL BACKFILLS WHAT IT CAN SEE. A breached cap is not a
    failure here: every receipt inside the window gets stored permanently, and
    the ones outside it stay exactly as unprovable as they already were. The
    store only ever grows more truthful.
    """
    import time as _time
    deadline = _time.monotonic() + max(0.0, float(budget_s))
    rows = receipts if receipts is not None else _landing_receipts()
    if rows is None:
        # UNREADABLE IS NOT EMPTY. Returning the no-work triple here would
        # tell the caller the history is finished when nothing was read.
        return None
    if not rows:
        return 0, 0, False
    cache = pk.read_json(_rescue_cache_path(), {}) or {}
    prefix = "%s\t" % str(gitdir or "")
    pending = [r for r in rows if str(r.get("patch_id") or "").strip()]
    if not pending:
        return 0, len(rows), False
    rv = _git(gitdir, *_REF_ARGV, str(trunk or ""))
    pinned = (rv.stdout or "").strip() if rv and rv.returncode == 0 else ""
    if not pinned:
        # A PIN THAT COULD NOT BE TAKEN IS NOT A FINISHED HISTORY. Every receipt
        # below is judged against this trunk, so without it NOTHING was
        # decided — and `incomplete=False` is the sentence "there is no work
        # left", which a caller acts on by never asking again.
        return 0, len(rows), True
    examined = 0
    incomplete = False
    index = None
    entries = {}
    for r in pending:
        if _time.monotonic() >= deadline:
            incomplete = True
            break
        pid = str(r["patch_id"]).strip().lower()
        # A KEY THAT IS PRESENT IS NOT A KEY THAT ANSWERS, and the old filter
        # skipped every present key. That made the store self-sealing: one bad
        # entry — and the land feed used to write them — was never revisited,
        # so the row it covered stayed unprovable forever while this function
        # reported nothing left to do. A held entry the READER would accept is
        # still a skip; one it would refuse is re-derived here.
        # The re-check is a git call per row, so it lives INSIDE the budgeted
        # loop. Hoisting it into the pending filter, where it reads more
        # naturally, would put an unbounded scan in front of the deadline.
        held = cache.get(prefix + pid)
        if isinstance(held, str) and held:
            # `is not False` for the same reason as the write door: TRUE
            # means the reader still accepts this entry and NONE means we
            # could not ask, and only a MEASURED refusal justifies spending
            # the walk below to replace it.
            #
            # THE SKIP IS RIGHT FOR BOTH AND THE SILENCE IS RIGHT FOR ONE.
            # TRUE is a settled receipt: the store already answers for it and
            # there is nothing left to do. NONE is the same skip reached for
            # the opposite reason — the question was never answered — and it
            # lands EARLIER than `examined`, so a run that decided nothing
            # reported (0, 0, False): looked at nothing, finished.
            held_answer = _answers_for(gitdir, pid, held, pinned)
            if held_answer is not False:
                if held_answer is None:
                    incomplete = True
                continue
        examined += 1
        tip = str(r.get("reviewed_tip") or "").strip()
        sha = None
        # ANCESTRY FIRST, AND IT IS THE RUNG THAT MATTERS HERE. It is
        # unbounded: a tip 760 commits back is as provable as one at depth 3,
        # and it needs no index and no patch-id at all. MEASURED on this
        # estate: 0 of 154 stored patch-ids hit the capped index, while 6 of 8
        # sampled receipts are plain ANCESTORS of trunk — including one at
        # depth 107, INSIDE the window, whose stored id still missed. The
        # window was never the only reason; the stored id simply is not the
        # landed commit's id. A backfill keyed on the capped index inherits
        # both faults and stores nothing, which is what the first cut did.
        anc = _ancestry(gitdir, tip, pinned) if tip else NOT_ANCESTOR
        if anc == UNDETERMINED:
            # ANCESTRY UNKNOWN IS AN UNANSWERED QUESTION, AND THE FALLBACK IS
            # STILL VALID. Two facts that look like one and are not. The
            # index below does not depend on ancestry at all -- it asks
            # whether trunk carries this id -- so a COMPLETE index that
            # misses is real evidence and must keep flowing. What must not
            # happen is the run reporting a finished history: the first rung
            # was never answered for this receipt, so even a clean miss below
            # leaves something this run did not decide.
            #
            # Recorded BEFORE the fallback rather than instead of it. The
            # earlier shape had no third branch at all: UNDETERMINED simply
            # was not ANCESTOR, so it fell through silently and one eligible
            # receipt with a readable pin and an unreadable ancestry probe
            # returned (0, 1, False) -- looked at one, finished.
            incomplete = True
        if anc == ANCESTOR:
            # ANCESTRY IS THE FIRST LEG, NOT THE ONLY ONE. A tip that is on
            # trunk still has to CARRY the receipt's stored patch-id, and it
            # very often does not: a rebase or cherry-pick lands different
            # bytes under the same review, so the tip hashes to Q while the
            # receipt's key is P. Selecting it anyway hands `_merge_rescue_
            # entries` an entry its admission check refuses, and the receipt
            # is then dropped with NO attempt at the other carrier the index
            # may hold for P — a proof lost to the rung that was supposed to
            # find it fastest.
            carries = _readable_correlation(gitdir, pid, tip)
            if carries is True:
                sha = tip
            elif carries is None:
                # UNKNOWN, so decide nothing. Falling through to the index on
                # an unread hash would let a MISS there stand as absence for a
                # receipt nothing actually asked about, and storing the tip
                # would write on a question git did not answer.
                #
                # DECIDING NOTHING IS NOT THE SAME AS HAVING NOTHING TO
                # DECIDE, and this `continue` used to say the second. The
                # receipt is eligible, the pin is readable, the tip is an
                # ancestor, and the one question that would settle it came
                # back unanswerable — a run that ends here has work left and
                # must say so, or the caller never returns to it.
                incomplete = True
                continue
            # carries is False: the tip is on trunk and is not the carrier.
            # The index is exactly the fallback for that case.
        if sha is None:
            if index is None:
                # UNCAPPED HERE, AND ONLY HERE. The read path's 600 exists to
                # bound a hot call; this one runs once, off that path, and the
                # cap is precisely what left landings unprovable whose only
                # barrier was the window.
                #
                # THE BUDGET GOES IN WITH IT. This walk is the single most
                # expensive thing the function does, and a deadline consulted
                # only between receipts cannot bound it — the overrun happens
                # entirely inside one call. A build cut short comes back
                # marked capped, and a capped index makes this run INCOMPLETE:
                # a partial index cannot distinguish "not on trunk" from "not
                # looked at yet", and reporting incomplete=False after an
                # overrun would tell the caller the history is finished.
                index, cut = _stored_patch_index(
                    gitdir, trunk, cap=BACKFILL_SCAN_CAP, deadline=deadline
                )
                if index is None:
                    # THE WALK FAILED, WHICH IS NOT AN EMPTY WALK, and `or {}`
                    # made those one value. `_stored_patch_index` answers None
                    # when rev-list itself could not run — an unreadable or
                    # absent trunk, a broken object store — and returns
                    # capped=False with it, because there is no partial index
                    # to describe. Folding that into `{}` gives every lookup
                    # below a clean MISS: nothing is stored, and the run
                    # reports incomplete=False, which tells the caller the
                    # history is FINISHED after proving nothing at all.
                    incomplete = True
                    index = {}
                elif cut:
                    incomplete = True
            sha = index.get(pid)
        if sha:
            entries[pid] = sha
    # THE MERGE INHERITS THE SAME DEADLINE. Its admission checks are git
    # calls, one per entry, so handing it an unbounded run after this loop
    # stopped on the budget is the leak with the budget's name on it.
    # AND THE MERGE'S OWN UNDECIDED QUESTIONS COME BACK THROUGH THE SAME
    # FLAG. `_merge_rescue_entries` re-asks the reader's contract per entry
    # and can fail to get an answer there too; OR-ing is what keeps one
    # unanswerable question anywhere in the run from being reported as a
    # finished history.
    stored, merge_incomplete = _merge_rescue_entries(
        gitdir, entries, pinned, deadline
    )
    return stored, examined, incomplete or merge_incomplete


def _landing_receipts():
    """Every land receipt on this estate, newest last, or None if UNREADABLE.

    NONE IS NOT AN EMPTY LIST HERE. An unreadable ledger and a ledger with no
    rows produce the same `stored=0, examined=0` from the backfill, and the
    caller then reports a finished history when it has read nothing at all.
    The two are different facts and only one of them means there is no work.

    A TORN TAIL AND A CORRUPT ROW ARE ALSO DIFFERENT FACTS, and the line
    between them is the TERMINATOR. An append-only jsonl legitimately ends
    mid-line: a writer was interrupted, the bytes after the final newline are
    not yet a row, and dropping them is exactly the durability boundary every
    other reader of this format applies. A COMPLETE line — one with a newline
    after it — that does not parse is different in kind: it was fully
    written, so its malformation is CORRUPTION, and skipping it hands the
    caller a list that silently omits a receipt while looking whole. That is
    the shape a backfill turns into "0 stored, nothing left to do".

    Skipping only when EVERY row is unusable is not enough: a ledger with one
    corrupt interior row among healthy ones returns the healthy ones, and the
    receipt in the corrupt row stays unprovable forever with nothing
    reporting it.
    """
    try:
        with open(receipts_path(), "rb") as fh:
            data = fh.read()
    except OSError:
        return None
    if not data:
        return []
    lines = data.split(b"\n")
    lines.pop()                # the unterminated tail is not a line
    out = []
    for raw in lines:
        if not raw.strip():
            continue
        try:
            row = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            # THE DEFECT WAS CATCH PLACEMENT, NOT THE HIERARCHY. A
            # `UnicodeDecodeError` IS a `ValueError` subclass, always, so the
            # second name here catches the first and is redundant as a matter
            # of types. It is written out because this reader decodes BYTES
            # EXPLICITLY rather than handing str to json: the decode is now a
            # step of its own, and naming its failure keeps the reason a
            # non-UTF-8 ledger is UNREADABLE from reading like an accident of
            # inheritance. The decode sits INSIDE this try, which is the
            # whole of the fix -- a decode performed while iterating the
            # text, before the parse and outside anything catching it, would
            # propagate rather than mark the ledger unreadable.
            return None
        if not isinstance(row, dict):
            return None        # a complete line that is not a row is corrupt
        out.append(row)
    return out








def _same_tip_siblings(lr, reviewed):
    """Every OTHER not-yet-retired dispatch row bound to the IDENTICAL tip.

    THE OWNER'S QUESTION, verbatim (2026-08-12): "even if new reviews create
    new rows, when something lands they should all close, no?" He was reading
    the pipeline view, where ONE piece of work rendered as three separate live
    rows at three different ages. Measured that morning: 46 live rows, 34
    distinct lanes — 12 rows of pure double-count, and the board's "41 in
    flight" was being read as 41 units of work.

    THE DISCRIMINATOR IS THE BOUND COMMIT, NOT THE LANE. A lane is a LABEL:
    free text, renamable, and two unrelated efforts may reuse one. Closing by
    lane would close genuinely separate work, and helm's own chain law
    (`dispatches.CHAIN_REQUIRED`) says so in as many words. Two rows that
    name the SAME 40-hex commit are not similar work, they ARE the same
    content — a second cross-family review leg, a re-ask after a compaction,
    an integrator's duplicate. When that commit lands, every one of them has
    been discharged by identity.

    REPO-SCOPED, because the same sha in two repositories is a coincidence,
    not a relation. Retired rows are skipped: a row is retired once.
    """
    tip = str(reviewed or "").strip().lower()
    if not dispatches._FULL_TIP.fullmatch(tip):
        return [], None
    current, unavailable = dispatches.snapshot()
    if unavailable:
        return [], unavailable
    mine = str(lr.get("id") or "")
    repo_id = str(lr.get("repo_id") or "")
    out = []
    for rid in sorted(current):
        row = current.get(rid)
        if not isinstance(row, dict) or rid == mine:
            continue
        if str(row.get("repo_id") or "") != repo_id:
            continue
        if dispatches.bound_tip(row) != tip:
            continue
        if dispatches._close_retired_by(row):
            continue          # a row is retired once
        out.append(row)
    return out, None


# A FIX or SUPERSEDE verdict on the same tip is DEBT, NOT DUPLICATION, and it
# is the one thing this sweep must never touch. `_close_ladder_landed` already
# refuses that polarity for the row it was called on — its reviewed tip on
# trunk is a CONTRARY, not a resolution — so closing a sibling that carries it
# would launder through the fan-out exactly what the front door refuses.
_SIBLING_DEBT_POLARITIES = ("fix", "supersede")


def _sweep_preview(lr, reviewed, compose_manifest=None, compose_gate=None,
                   trunk=None, live=False, needs_restart=None):
    """(would-close ids, would-stay-open ids) — the rehearsal's two lists.

    THE PREVIEW MUST SPLIT THE SET THE WAY THE WRITE DOES. The selector returns
    every peer bound to the tip, debt included, because the debt refusal is the
    WRITE side's business — so a preview built straight off the selector
    promises to close rows it will then refuse, which is a rehearsal of a
    different action. Same split, one source, so the two cannot drift.
    """
    closed, refused = _close_same_tip_siblings(
        lr, reviewed, trunk, live, needs_restart,
        compose_manifest=compose_manifest, compose_gate=compose_gate, dry_run=True)
    return [p["id"] for p in closed], [rid for rid, _why in refused]






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
            # NOT STRANDED'S QUESTION: the translated object is LIVE, and
            # `stranded` refuses a tip whose recorded translation is a live
            # object. Measured on this shape, every door asked refused it —
            # `stranded` on the live translation, `landed` because the
            # translation is not on trunk — so no door is offered.
            return None, ("translated object %s is preserved under no "
                          "archive/rescue ref, so this door cannot adjudicate "
                          "it; it is still a LIVE object, so stranded refuses "
                          "it too. It is adjudicated through that translated "
                          "identity, and no door is promised here"
                          % translated[:12])
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
            # THIS DOOR TAKES ANY POLARITY, `landed` DOES NOT. Measured on this
            # shape: `landed` follows the translation and admits an undeclared
            # verdict, and refuses a FIX as a contrary. So `landed` is offered
            # for the verdicts it takes and nothing is promised for the rest.
            return None, ("the translated change itself IS on trunk — that "
                          "is a landed outcome, not a supersession: `--reason "
                          "landed` follows the same translation and admits an "
                          "APPROVE or an undeclared verdict; it refuses a FIX "
                          "or SUPERSEDE as a contrary, and no door is promised "
                          "here for one")
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
        # THE CONTENT RULE, TRANSLATED (the FIX on r1; the
        # relation direction below is the r2 FIX — "carries" means the
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
        proof_mode=proof_mode, translated_tip=translated, dry_run=dry_run)
    if err:
        return None, err
    if dry_run:
        return _rehearsed(row, {
            "dry_run": True, "id": lr["id"], "reason": "superseded",
            "superseding_tip": superseding,
            "superseding_id": approved[0][1]["id"],
            "contrary_state": "none",
            "proof_mode": proof_mode,
            "translated_tip": translated,
            "preserved_ref": preserved_ref,
            "closing_trunk_ref": trunk_ref,
            "closing_trunk_sha": pinned})
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
























#: What an attestation must carry to admit an UNMEASURABLE tier. The attester
#: is the DISCHARGING SEAT — the integrator or the row's issuer — never the
#: human owner: an outcome the owner has already locked must not be re-gated on
#: him, and a door that needs the owner is a door that does not open at 4am.
CHAIN_ATTEST_FIELDS = ("v", "attester", "evidence", "ts")
CHAIN_ATTEST_ALGORITHM = "chain-authority-v1"


def _acting_seat():
    """This process's DECLARED seat, or None — `seats.own_name`, the same
    resolver `board._acting_seat` uses.

    IDENTITY IS RESOLVED, NEVER TYPED. An attestation names who admitted an
    unmeasurable tier, so letting the caller pass that name as a string would
    make the accountable half of the record the one part a forger controls.
    """
    try:
        from . import seats
        return seats.own_name() or None
    except Exception:                    # noqa: BLE001 — unknown, not a crash
        return None






def _approval_policy_readable(repo_id=None):
    """Can the approval-tier prior ITSELF be read? -> bool.

    The discriminator between an UNKNOWN that names a gap in one ROW and an
    UNKNOWN that names a broken WORLD. Mirrors `_approval_tier_uncached`'s own
    first two steps rather than parsing its sentence, because a refusal routed
    on prose drifts the moment the prose is improved. An ABSENT policy reads
    TRUE: there is nothing to be outside of, which is a real answer.
    """
    from . import store
    try:
        from .inject._ledger import project_for_cwd
        project = project_for_cwd(repo_id if repo_id is not None
                                  else os.getcwd())
    except Exception:                   # noqa: BLE001 — unknown project scope
        project = None
    try:
        policy, why = store.load_certain_policy("approval-tier",
                                                project=project)
        if not why:
            return True
        return not store.policy_declared("approval-tier", project=project)
    except Exception:                   # noqa: BLE001 — unreadable is FALSE
        return False












def board_section(now=None):
    """The data a console renders as a 'land loops + where they're stalling'
    panel. The console owns the actual anchor; this exposes the rows in a
    stable, brief-shaped section dict (title + loops + the stalled subset).

    `undecided` COUNTS THE PROJECTED POPULATION, NOT THE TWO LISTS BESIDE IT,
    and that is the whole reason it is computed here rather than by a caller.
    `loops` and `stalled` are two predicates over the same rows, so they
    OVERLAP — a stalled row is a non-terminal loop past its threshold — and a
    count summed across them bills one row twice. They also both WITHHOLD:
    a foreign row, a terminal row and a relieved row each leave, so a row
    whose landing could not be established can be absent from both lists while
    still being a row the reader failed to decide. Counting over `lrs`, which
    is keyed by id and holds every projected row, answers the question a
    caller actually has — how much of what I read is undetermined — and cannot
    double-bill, because a dict has one entry per row.

    IT IS SCOPED THE WAY THE LISTS ARE, AND ONLY THAT WAY. `project_raw`
    MARKS foreign rows rather than dropping them, so `lrs` carries other
    projects' work; a count over all of it would report this board as
    uncertain about rows it does not owe. `_this_boards_row` is the same
    visibility predicate the two lists use, so what is excluded here is
    excluded there — and it is the VISIBILITY twin, never `_observation_owned`,
    because this is a rendering decision and authorizes no git. An
    unknown-provenance row therefore STAYS COUNTED, which is the safe
    direction: it is a row this board may owe and cannot prove it does not.

    What it does NOT apply is the loop/stall classification, which is the
    filter the count has to see past — those two predicates overlap and each
    withhold, so a row that is undecided can be absent from both lists.

    It is None, never 0, when the projection is unavailable. Zero is a
    measurement meaning "nothing was undecided"; a refused projection decided
    nothing at all, and the two must not share a spelling."""
    lrs, raw, unavailable = project_raw(now)
    if unavailable:
        return {"title": "LAND LOOPS", "unavailable": unavailable,
                "loops": [], "stalled": [], "undecided": None}
    undecided = sum(1 for lr in lrs.values()
                    if _this_boards_row(lr)
                    and str(lr.get("land_state") or "").upper() == "UNKNOWN")
    try:
        loops_, stalled_ = _loop_rows(lrs, raw), _stalled_rows(lrs, raw)
    except _ChainUntrustworthy as e:
        # the OWNER CONSOLE is exactly where a silently-partial fold does the
        # most damage: it reads as a clean board. The population WAS projected
        # before the fold refused, so the count stands and rides out with the
        # refusal rather than reverting to None.
        return {"title": "LAND LOOPS", "unavailable": str(e),
                "loops": [], "stalled": [], "undecided": undecided}
    return {"title": "LAND LOOPS", "unavailable": None,
            "loops": [card(lr) for lr in loops_],
            "stalled": [card(lr) for lr in (stalled_ or [])],
            "undecided": undecided}


# ------------------------------------------------------------------------ CLI

USAGE = ("usage: helm lr list [--all] [--all-projects] [--json] [--cold] "
         "| show <id> [--json] |"
         "stalls [--json] | "
         "foldcheck <tip> [--gate gate:TOKEN] [--repo PATH] [--no-fetch] | "
         "legacy-completion-hints [--json] | "
         "land <id> [--json] | compose <id> [<id>...] [--trunk REF] "
         "[--repo PATH] [--bounded-concur] [--dry-run] [--json] | close <id> --reason "
         "landed|superseded|withdrawn|out-of-scope|stranded|subsumed|"
         "delivered-report|discharged|resolved|carried|chain-proof|expired|endorsement-moot "
         "[--evidence LINE] [--artifact-ref REF] "
         "[--report-ref CHAT_REF] [--tip FULL_SHA] [--repo PATH] [--trunk REF] "
         "[--live | --needs-restart WHAT] "
         "[--compose-manifest PATH --compose-gate gate:ID] [--dry-run] [--json] | "
         "annotate-delivered-report <id> --artifact-ref REF "
         "--report-ref CHAT_REF --evidence LINE [--json] | discharge <id> "
         "<full-superseding-tip> <evidence...> [--json] | withdraw <id> "
         "<evidence...> [--json] | abandon <id> --reason TEXT "
         "[--repo PATH] [--json] | retire <id> --reason "
         "tier-unevaluable-parked|succession-unreadable|author-unresolvable|"
         "repo-unreadable|reviewed-object-destroyed|author-absent-lane-idle "
         "[--note TEXT] [--seat NAME] [--dry-run] [--json] | "
         "retire --sweep [--older-than Nd] [--dry-run] [--all-projects] "
         "[--seat NAME] [--note TEXT] [--json] | "
         "retire --off-frontier [--apply] [--all-projects] [--json] | "
         "close-landed <id> --trunk REF "
         "[--repo PATH] [--json] | refs [--repo PATH] [--json] | "
         "migrate --commit-map PATH [--repo PATH] [--apply] [--json]\n"
         "  list rides the warm projection when a local `helm web` is fresh "
         "(--json included; --cold forces the replay); the same data is "
         "GET /api/lr on that instance, ~26ms warm")


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
    if lr.get("retired_admin"):
        return "RETIRED"
    if lr.get("withdrawn") and not lr.get("withdraw_contradicted"):
        return "WITHDRAWN"
    if lr.get("discharged"):
        return "DISCHARGED"
    return None


def withdrawal_hand(lr):
    """WHOSE withdrawal this was, read for the REVIEWER (task/2619).

    THE OUTSIDER'S READING IS THE ONE THIS ANSWERS. A FIX verdict retired
    `withdrawn` says the reviewed change is provably absent from trunk and
    nothing more, so the reviewer who wrote the finding meets a row that ended
    without a cure and without a successor — which reads, from outside, as an
    author who ignored the verdict. It is the opposite fact in the case this
    door exists for: the author ACCEPTED the finding in full and deleted the
    artifact, and the evidence line beside this one says so in their words.

    So the hand is named. The writer already records it — `withdrawing_seat`
    is stamped by `_close_ladder_withdrawn` from the acting process, and
    `close_actor` is the universal stamp the close lock applies — and NEITHER
    was ever rendered, so a reader of the page could not tell the author's own
    agreement from a third party retiring somebody else's row.

    AND THE DOOR TAKES ANY SEAT, DELIBERATELY. `_close_ladder_withdrawn` binds
    no author and its own comment says why: the rows it exists for include
    older verdicts recorded before author binding was stable, and the
    integrator carrying out a verdict is a legitimate closer. That design is
    kept — a guard is not added here — and the consequence is DISCLOSED
    instead: when the closing seat is not the row's author, the line says so
    rather than letting the page imply an agreement nobody recorded.

    Never raises and never guesses: an unrecorded hand is named unrecorded."""
    seat = str(lr.get("withdrawing_seat") or lr.get("close_actor") or "").strip()
    author = str(lr.get("author") or "").strip()
    if not seat:
        return ("by an UNRECORDED seat — this close predates the actor stamp, "
                "so the page cannot say whose withdrawal it was")
    if author and seat == author:
        return ("by the AUTHOR %s — the verdict was ACCEPTED, not ignored; "
                "the evidence is their attestation" % seat)
    return ("by %s, who is NOT the author (%s) — the author's own agreement "
            "is not recorded on this row" % (seat, author or "unrecorded"))


# TWO VALUES ONLY — see the classifier: `discharged` was a lifecycle label in an
# evidence field, and the `discharged` row field already names that path.
_CONTRARY_PROVENANCE_WORDS = {
    "observed": "observed on trunk",
    "recorded": "recorded when closed",
}


def contrary_fact(lr):
    """The physical fact behind a contrary alarm.

    ONE DOOR FOR ALL THREE RENDERERS — `lr list`, the detail block, and the web
    card — because this pair of surfaces has drifted before and each one's own
    comment already promises it prints "the same words" as the other. A rule
    living in three places is a rule that holds in two.

    "STATE UNREAD" RATHER THAN A GUESS, and this is a behaviour FIX. Both
    terminal renderers read `"LANDED" if state == "landed" else "MERGED_LOCAL"`,
    so a row whose state was neither asserted MERGED_LOCAL — a physical claim
    nobody measured. The web card already said "STATE UNREAD" for that case, so
    the two surfaces described such a row differently, in exactly the way this
    lane exists to end. I could NOT construct the case from a supported verb and
    my ledger sweep for one returned zero WITHOUT a must-hit, so treat it as
    unreached rather than unreachable: the cure is that an unmeasured state
    stops claiming a measured one, which is right whether or not it occurs.
    """
    state = lr.get("contrary_state")
    return "LANDED" if state == "landed" \
        else "MERGED_LOCAL" if state == "merged-local" else "STATE UNREAD"


def contrary_provenance_clause(lr):
    """HOW the contrary fact is known, as a TRAILING clause or "".

    A TRAILING CLAUSE RATHER THAN PART OF THE FACT, and that is a correction
    measured rather than reasoned. The first version spliced the provenance into
    `contrary_fact`, which turned "MERGED_LOCAL on local trunk" into
    "MERGED_LOCAL (observed on trunk) on local trunk" and reddened TEN arms
    across four classes — every one of them asserting a contiguous phrase this
    lane had no business splitting. The fact is the fact; provenance annotates
    the whole claim, so it goes at the end where it cannot break an adjacency.

    RENDERED ONLY WHERE THE ALARM IS. Every caller sits inside an
    `if lr.get("contrary")` gate, and that gate is the contract: `contrary_state`
    is populated on rows whose `contrary` is FALSE — a routine approve-then-land
    row reads contrary=False, contrary_state="landed" — because `contrary`
    requires a fix/supersede polarity and the state has a fourth branch that
    does not. So the state, and now its provenance, is meaningful ONLY where
    contrary is true. Every consumer already gated that way; nothing said so.
    """
    word = _CONTRARY_PROVENANCE_WORDS.get(lr.get("contrary_provenance"))
    return " [%s]" % word if word else ""


def honored_display(lr):
    """THE one display predicate for a contrary whose verdict was HONORED
    through succession (`contrary_discharge` "a"/"b") or which IS the
    succession instrument itself ("c", a confirmation round — rendered as a
    quiet "confirmation", never an alarm) — every python surface that quiets
    an alarm for these rows asks THIS, and web_ui.html's `lrHonored` is its
    JS twin (the parity tests pin them equal row for row).

    It exists because the first cut answered the question inline per surface
    and the surfaces immediately disagreed (dispatch 97d8899a): an
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
    role, seat, _state, _why = owed_seat_standing(lr)
    return role, seat




def owed_seat_standing(lr):
    """(role, seat, state, why) — the display pair plus what that NAME means.

    THE ROW OUTLIVES THE SEAT IT NAMES, and until now the board could not say
    so. `author` and `reviewer` hold a seat name as a plain string, every
    consumer compares holders by exact canonical-token equality, and nothing
    on the rename or exit paths ever rewrote them — so a row addressed to a
    name nobody answers to renders identically to a row somebody owes work on
    today. Measured on the live board: 36 role-slots across seven such names,
    reading as ordinary STALLED debt owed by a builder.

    A RENAME IS ANSWERED, NOT MARKED. Where the lineage `rename_seat` writes
    resolves the old name to a successor, THAT is who holds the ball and the
    pair says so — the row needed no rewrite, the answer was on the roster the
    whole time. Only a name with no row and no lineage is ORPHANED.

    IT IS A RENDERING AND IT EXPIRES NOTHING. Roster absence is transient —
    four names moved absent-to-present in thirteen minutes during task/2333's
    census — so this states a fact about right now and leaves `owed_by`,
    `stalled`, the deadline and every enforcement reader untouched, the same
    contract `honored_display` states one function up. The cure for an orphan
    is `helm seat reassign`, which moves the BYTES; a board that expired rows
    on this predicate would eventually expire a live seat's work while it was
    between sessions.

    UNKNOWN RENDERS NOTHING. An unreadable roster and ambiguous lineage both
    answer UNKNOWN, and the caller stays silent rather than banner a row it
    could not measure.
    """
    if honored_display(lr):
        return "nobody", None, None, None
    if lr.get("owner_gated"):
        return "owner", None, None, None
    if lr.get("contrary"):
        return "integrator", None, None, None
    role = lr.get("owed_by") or "unknown"
    seat = lr.get(_OWED_SEAT_FIELD.get(role, ""))
    if not seat:
        # ROLES WITH NO SINGLE HOLDER ASK NO SEAT QUESTION. integrator, nobody
        # and unknown render unchanged for the reason `_owed_by_whom` already
        # states: inventing a name for them is the same defect pointing the
        # other way, and an ORPHANED banner on one would be exactly that.
        return role, seat, None, None
    try:
        from .seats_common import _seat_label
        from .seats_lineage import (SEAT_CURRENT, SEAT_RENAMED,
                                    seat_lineage)
        name, state, why = seat_lineage(seat)
    except Exception as exc:                 # noqa: BLE001 — a board that
        return role, seat, None, str(exc)    # cannot ask renders as before
    if state == SEAT_RENAMED:
        return role, name, state, why
    if state == SEAT_CURRENT:
        # THE ROSTER'S OWN SPELLING, not the row's. They differ only in case
        # and decoration, and `recipient_matches` already decided they are the
        # same seat — printing the live key keeps the board and `helm chat
        # seats` answering with one string.
        return role, name, state, why
    # THE ORPHANED NAME IS LAUNDERED BEFORE IT IS PRINTED, and only this
    # branch needs it. The other two return a name the ROSTER supplied, which
    # the roster's own doors already validated; this one returns a string an
    # `author`/`reviewer` field has carried since whenever the row was
    # written, and it is about to be put in a terminal listing and on the
    # wire. `_seat_label` is the identity for every legitimate seat name and
    # only rewrites the ones that could move a cursor.
    return role, _seat_label(seat), state, why


# ── THE REVIEWER PICKER'S MISSING WORD ───────────────────────────────────────
# THE OWNER'S RULING: "the reviewer picker (a dark family is skipped with the
# reason printed, never silently)." There is no picker FUNCTION to fix
# — `git grep -rn -i least.loaded` over helm/ returns nothing — the pick is a
# human integrator reading `helm lr list` and choosing a seat off it. THAT is
# the picker, which makes this listing the load-bearing place for the word: a
# reviewer whose vendor is dark was invisible at the exact moment a row was
# being routed to it, and the row it already holds looked like an ordinary
# stall.
#
# ONE PERSISTED READ FOR THE WHOLE LISTING, memoised per seat. This renderer
# already spends a merge-base three-dot per row; what it must not do is add a
# file read per row, and it must not read the record at module scope either —
# a memo outside a call is a durable cross-call channel serving a stale answer
# to the one surface whose job is to be current. So the lookup is built per
# listing and dies with it.
def _avail_walled(rec):
    """Is this record the predicate's measured wall? ASKED OF the module that
    owns the word — the same reader the roster and the fleet table use — so the
    board cannot drift from the vocabulary when it gains an entry."""
    try:
        from . import seat_usability
        return seat_usability.availability_walled(rec)
    except Exception:                       # noqa: BLE001
        return False


def _availability_lookup():
    """(seat -> availability record or None) — one snapshot read, memoised.

    THE LAUNCH METADATA COMES WITH IT. A row owed by a `pi-codex` seat is owed
    by a seat billed to family `codex`, and the seat NAME cannot say so — a
    lookup that hands the predicate a name alone answers NO_VENDOR for exactly
    the custom-runtime seats whose wall this board exists to explain. The roster
    is asked ONCE per listing, beside the one snapshot read, and dies with it.
    """
    try:
        from . import proxywatch, seat_usability
        up, err = proxywatch.upstream_snapshot()
        runtimes = seat_usability.roster_runtimes()
    except Exception as e:                  # noqa: BLE001 — the board never
        why = ("the availability reader failed (%s: %s)"                # dies
               % (e.__class__.__name__, e))
        return lambda _seat: None if not _seat else {
            "state": "UNKNOWN", "family": None, "text": "UNKNOWN — " + why}
    cache = {}

    def look(seat):
        if not seat:
            return None
        if seat not in cache:
            rt, ver = runtimes.get(str(seat)) or (None, False)
            try:
                cache[seat] = seat_usability.availability(
                    str(seat), upstream=up, upstream_error=err,
                    runtime=rt, runtime_verified=ver)
            except Exception:               # noqa: BLE001
                cache[seat] = None
        return cache[seat]
    return look






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
# EVERY RECORDED COMMIT ID, because `lr refs` exists to prove that a proof can
# still be RE-VERIFIED — and a recorded id this register omits is one nobody
# will ever be told went unresolvable. `carried_base` and `closing_trunk_sha`
# are recorded commit ids on the carried/chain-proof terminals and were absent
# here, so the two doors whose whole premise is a PRUNED object had the least
# audited references in the file.
_REF_FIELDS = ("ref", "tip", "reviewed_tip", "original_tip", "superseding_tip",
               "landing_trunk_sha", "retarget_from", "retarget_to",
               "review_sha", "trunk_sha", "carried_base", "closing_trunk_sha")
_SHA_RE = re.compile(r"\A[0-9a-f]{7,40}\Z")
_FULL_SHA_RE = re.compile(r"\A(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")


def _audited_commit_ids(row):
    """[(field, sha)] — every commit id on ONE row that `helm lr refs` audits.

    THE AUDIT'S OWN PREDICATE, factored rather than restated. `dangling_refs`
    walks both ledgers through this, and the `reviewed-object-destroyed`
    retirement measures its own row through it: that reason's third leg
    CLAIMS the audit reports this id unresolvable, and a claim about another
    surface has to be made with that surface's rule. A field this does not
    yield is invisible to `lr refs`, so a retirement citing the audit over it
    would cite a corroboration the operator cannot find — and the shape
    clause is not decoration, because the audit skips a value that is not
    sha-shaped and would report nothing at all about it.

    KNOWN COMPLETENESS REMAINDER — THE SHA-256 CEILING. `_SHA_RE` tops out at
    40 hex characters, so a commit id from a sha256-objectformat repository
    (64 hex, which `_FULL_SHA_RE` does accept) is not yielded here, is not
    walked by `dangling_refs`, and cannot satisfy the destroyed-object
    retirement's first leg. The consequence is an over-refusal, never a wrong
    write: such a row refuses at "measurement 1 of 3 (audit)" and stays open.
    Widening it is one regex and a re-measure of what else reads `_SHA_RE`;
    it is unfixed here because no sha256 repository is on the board to
    measure the change against, and a widening nothing exercises is a second
    unmeasured claim rather than a cure."""
    return [(field, row.get(field)) for field in _REF_FIELDS
            if isinstance(row.get(field), str) and _SHA_RE.match(row[field])]


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
        for field, sha in _audited_commit_ids(lr):
            seen.setdefault(sha, []).append(("lr", lr.get("id"), field))
    # BOTH ledgers hand back {id: row}, so iterate values — walking the mapping
    # itself yields id STRINGS, which silently have no fields to inspect and
    # would have reported a clean audit over nothing.
    drows = dispatches.rows()
    for d in (drows.values() if isinstance(drows, dict) else drows):
        for field, sha in _audited_commit_ids(d):
            seen.setdefault(sha, []).append(("dispatch", d.get("id"), field))
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




def _warm_port():
    """The port a local `helm web` would be listening on.

    NO NEW STATE FILE ON PURPOSE. A pidfile or registry entry naming the live
    port is one more thing that can outlive its process and be believed —
    `nothing-stale-ever` applied to the discovery itself. `helm web` binds
    127.0.0.1 on web_server.DEFAULT_PORT unless `--port` overrides, so the
    env override and that constant are the whole truth, and a wrong guess
    costs one refused connection and a cold fallback."""
    raw = (os.environ.get("HELM_WEB_PORT") or "").strip()
    if raw.isdigit():
        return int(raw)
    from . import web_server
    return web_server.DEFAULT_PORT


# The cold-replay backstop: a warm projection older than this is refused and
# replayed even when input-freshness says it is CURRENT. PINNED, not derived:
# this was `_LR_HARD_TTL_S * 30`, which read as "an hour" only by arithmetic
# accident (120 * 30) — that web constant is a serve-stale DISPLAY budget
# that moved 120->600 for measured reasons of its own (see its derivation in
# web_land_model), and the raise silently widened this refusal 1h->5h.
# MEASURED as a real defect (review, dispatch ee48cea6, exact-tip probe):
# age=7200/ledger_age=8200 rendered WARM — a two-hour-stale projection
# bypassing cold replay under a contract that promises one genuinely quiet
# hour. The two thresholds guard DIFFERENT claims — "a KNOWN past may stand
# in while a rebuild runs" vs "a missed ledger write is likelier than a
# quiet hour" — so this one is pinned to the hour its contract names and
# moves only when THAT contract does.
_WARM_QUIET_HOUR_S = 3600


def warm_lr_body(timeout=2.0):
    """The WARM land projection from a local `helm web`, or (None, why).

    -> (body, None) on a usable answer; (None, reason) otherwise. The reason
    is for the operator, never swallowed: an accelerator that silently does
    nothing is indistinguishable from one that is broken.

    WHY A SHORT TIMEOUT RATHER THAN A GENEROUS ONE: measured 2026-08-07 on the
    live instance, a COLD cache answers this endpoint in 18.6 SECONDS (the
    projection runs 22-28s) while a warm one answers in 3 ms. There is no
    middle ground to wait for — either the projection is already built or we
    are about to pay the cold price twice, once here and once in the fallback.
    So this asks briefly and gives up cheaply.

    EVERY REFUSAL PATH FALLS BACK, per the row: no process, an `unavailable`
    body, a cold-start `warming` body, an answer older than the model's own
    truth cap, or any exception at all. The warm path is an accelerator and
    may never become a new way for `lr list` to fail."""
    # THE INSTANCE SERVES ITS OWN HELM HOME, NOT OURS — checked FIRST, before
    # any socket, because it is the cheapest and the most important of these.
    # `helm web` resolved its ledger from ITS environment when it started, so
    # a CLI running under an overridden home is asking about a DIFFERENT
    # ledger, and the warm answer would be about someone else's data —
    # confidently, and in exactly the right shape, which is the worst way to
    # be wrong.
    #
    # SURFACED BY THE GATE, NOT BY DESIGN, and that is the honest record: the
    # suite sets HELM_HOME to a temp dir, and this path answered from the REAL
    # ledger. It also means a test could reach a live server, so its result
    # depended on whether one happened to be running on the box.
    for var in ("HELM_HOME", "MELD_HOME"):
        if (os.environ.get(var) or "").strip():
            # QUIET: not a fault, just no fast path here. A reason printed
            # on every invocation of the ordinary case is noise, and the
            # header already distinguishes a warm answer by saying WARM.
            return None, None
    import urllib.request
    url = "http://127.0.0.1:%d/api/lr" % _warm_port()
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            body = json.load(resp)
    except Exception as e:
        # QUIET for the same reason: most boxes run no web surface at all.
        return None, None
    if not isinstance(body, dict):
        return None, "warm projection was not an object"
    # A COLD START ANSWERS `warming`, and that is a real answer meaning "no
    # rows yet" — rendering it would print an empty board over a full ledger.
    if body.get("warming"):
        return None, "warm projection is still building"
    if body.get("unavailable"):
        return None, "warm projection says: %s" % body["unavailable"]
    if not isinstance(body.get("loops"), list):
        return None, "warm projection carries no loops"
    age = body.get("read_age_s")
    if not isinstance(age, int):
        return None, "warm projection did not state its age"
    led = body.get("ledger_age_s")
    if not isinstance(led, int):
        return None, "warm projection did not state its ledger age"
    # FRESHNESS IS ABOUT THE INPUT, NOT THE CLOCK — and my first cut got this
    # wrong in a way only measurement showed. I gated on wall age against the
    # model's _LR_HARD_TTL_S (120s at the time; 600 since), which reads
    # sensibly and is the wrong
    # QUESTION: measured 2026-08-07 against the live instance, an idle helm
    # web serves a projection that ages monotonically (199s -> 247s over 48s)
    # because nothing has written to the ledger, so a wall-clock gate refused
    # every read and the accelerator never fired at all.
    #
    # A projection is USABLE when it was computed AFTER the newest ledger
    # write, however long ago that was. `ledger_age_s >= read_age_s` says
    # exactly that: the newest input is older than the projection, so the
    # projection saw it. A four-minute-old answer over a four-hour-old ledger
    # is not stale, it is CURRENT — and a sixty-second-old answer over a
    # ledger written ten seconds ago is stale despite being younger.
    #
    # This is the same discipline as naming the ref AND its sha in a gate
    # verdict: bind the freshness claim to what was actually compared.
    if led < age:
        return None, ("the ledger changed %ds ago, after this projection was "
                      "computed %ds ago" % (led, age))
    # ABSOLUTE BACKSTOP, deliberately generous and deliberately NOT the
    # freshness test: it only catches a projection so old that a missed ledger
    # write is more likely than a genuinely quiet hour. Pinned to that hour —
    # see _WARM_QUIET_HOUR_S for why it no longer rides the web display cap.
    if age > _WARM_QUIET_HOUR_S:
        return None, "warm projection is %ds old — replaying to be sure" % age
    return body, None




#: The `helm lr` verbs whose WHOLE body is a read, and therefore the only ones
#: `cmd_lr` may put a memo scope around. Membership is the safety boundary, so
#: it is a set of names rather than a property anyone has to re-derive: a verb
#: that writes must never appear here, and one that is added must be read end
#: to end first.
#:
#: ONLY THE VERB THAT WAS MEASURED IS IN IT. `show`, `stalls` and `refs` read
#: too and would very likely gain; none of them has been measured, and an
#: unmeasured membership in a set whose contents decide whether a write can be
#: answered from an older question is exactly the kind of guess this set exists
#: to make impossible.
_SCOPED_READ_VERBS = frozenset({"list"})






def _retire_days(value):
    """(days, err) for an `--older-than` operand — `14d`, `14`, or `0.5d`."""
    text = str(value or "").strip().lower()
    if text.endswith("d"):
        text = text[:-1]
    try:
        days = float(text)
    except ValueError:
        return None, ("--older-than takes a number of days (e.g. 14d), not %r"
                      % str(value))
    if days < 0:
        return None, "--older-than cannot be negative"
    return days, None
























_CQ_ESC = {b"a": b"\a", b"b": b"\b", b"f": b"\f", b"n": b"\n", b"r": b"\r",
           b"t": b"\t", b"v": b"\v", b"\\": b"\\", b'"': b'"'}


def _cunquote(tok):
    """One git C-quoted token -> (decoded bytes, remainder after the close
    quote), or None when the quoting is malformed. Mirrors quote.c's
    unquote_c_style: the named single-char escapes plus octal digits."""
    if not tok.startswith(b'"'):
        return None
    out, i = bytearray(), 1
    while i < len(tok):
        c = tok[i:i + 1]
        if c == b'"':
            return bytes(out), tok[i + 1:]
        if c != b"\\":
            out += c
            i += 1
            continue
        esc = tok[i + 1:i + 2]
        if esc in _CQ_ESC:
            out += _CQ_ESC[esc]
            i += 2
            continue
        j = i + 1
        while j < min(i + 4, len(tok)) and tok[j:j + 1].isdigit():
            j += 1
        if j == i + 1:
            return None
        try:
            out.append(int(tok[i + 1:j], 8))
        except ValueError:
            return None
        i = j
    return None


def _diff_header_path(rest):
    """The b-side path (canonical decoded bytes) of a `diff --git <a> <b>`
    header, or None when the header alone cannot answer.

    This is the digest's per-file path AUTHORITY — the one line every diff
    shape carries, including the three that have no ---/+++ at all
    (mode-only, pure rename, binary; the r5 false-carry classes each
    hashed under `?` for exactly that gap). git C-quotes any path that
    needs it, and a quoted token can neither contain a bare `"` nor appear
    inside an unquoted one, so the first ` "` in the header is ALWAYS the
    b-token boundary — one find covers both-quoted and mixed forms. The
    unquoted SAME-path form (`a/P b/P` — every non-rename diff) splits at
    a length-determined offset, spaces and all. The remaining shapes (an
    unquoted RENAME's two different paths; a quoted-a/unquoted-b rename)
    are ambiguous by construction and answer None — their `rename from`/
    `rename to` lines carry the exact paths and are hashed as META text."""
    def strip(tok):
        return tok[2:] if tok[:2] in (b"a/", b"b/") else tok
    cut = rest.find(b' "')
    if cut != -1:
        got = _cunquote(rest[cut + 1:])
        return strip(got[0]) if got and not got[1] else None
    if len(rest) >= 5 and (len(rest) - 5) % 2 == 0:
        half = (len(rest) - 5) // 2
        p2 = rest[5 + half:]
        if rest[:2] == b"a/" and rest[2 + half:5 + half] == b" b/" \
                and rest[2:2 + half] == p2:
            return p2
    return None


_NO_NEWLINE = b"\\ No newline at end of file"

# A REGULAR unified-hunk header — `@@ -a[,b] +c[,d] @@` — and ONLY that.
# `startswith(b"@@")` also matches the combined-diff `@@@` header and any
# malformed `@@` line, both of which this parser has no scan rules for; the
# strict grammar makes those unmeasurable instead of guessing the state
# (task/789, second stop). FULL-MATCH, not prefix: `.match` anchors
# the start only, so `@@ -1 +1 @@oops` would pass a prefix grammar — the
# closing `@@` must be followed by end-of-line OR a section-context
# separator (a space, then the free-text function context). `\Z` binds the
# whole line.
_REGULAR_HUNK = re.compile(
    rb"\A@@ -\d+(,\d+)? \+\d+(,\d+)? @@(?: .*)?\Z")

# Only a BODY line can be the line a no-newline marker annotates. Headers,
# index lines, hunk headers and every META form are structural: they describe
# the file, not its text, so a marker can never belong to one.
#
# `--- a/f.txt` and `+++ b/f.txt` begin with `-` and `+` and are NOT payload,
# which is the trap this predicate exists to make unhittable — testing
# `ln[:1] in (b"+", b"-")` against a raw stream classifies both headers as
# changed content.
_DIFF_STRUCTURAL = (b"diff --git ", b"index ", b"--- ", b"+++ ", b"@@",
                    b"Binary files", b"GIT binary patch",
                    b"deleted file mode", b"new file mode", b"old mode",
                    b"new mode", b"similarity index", b"rename from",
                    b"rename to")


def _nl_predecessor(ln, in_hunk):
    """The raw line a following no-newline marker would annotate, or None.

    ONE predicate, consulted ONCE per line at the top of the scan, because
    the invariant is IMMEDIATE predecessor and an earlier shape let a branch
    keep a stale one: each `continue` that forgot `prev = None` -- the
    Binary/META pair -- made `+payload` -> `old mode 100644` -> marker hash
    the marker as the author's own content when it belongs to nobody.

    HEADER-VS-PAYLOAD CANNOT BE DECIDED FROM THE PREFIX ALONE, which a
    first cure got wrong in the other direction. Inside a hunk, REMOVING a
    file line whose text is `-- final` renders as `--- final`, and ADDING
    `++ final` renders as `+++ final` -- byte-identical to the file headers.
    Reading those as structural called a real payload marker an orphan and
    made the whole identity unmeasurable. Same bytes, opposite meanings,
    separated only by parser STATE:

      OUTSIDE a hunk  `--- a/f.txt` / `+++ b/f.txt` are the file headers.
      INSIDE a hunk   every line carries a leading +, - or space, so the
                      FIRST BYTE alone classifies and nothing else can
                      collide: `diff --git`, `index`, `Binary files` and
                      the mode/rename family all begin with a letter.
    """
    if in_hunk:
        return ln if ln[:1] in (b"+", b"-", b" ") else None
    if ln.startswith(_DIFF_STRUCTURAL):
        return None
    return ln if ln[:1] in (b"+", b"-", b" ") else None


def _commit_content_identity(root, sha):
    """Content identity for ONE commit as (patch_id, payload_digest,
    newline_fingerprint) — the RICH OWNER, uniformly three fields.

    patch_id is git's own per-commit `show | patch-id --stable` — the same
    instrument `git cherry` uses — so mode changes on different paths,
    deletion variants, renames and binary edits all discriminate (a review's
    r3 collision family, probed pairwise: mode-f != mode-g, del-f !=
    del-empty, bin1 != bin2). It is the PRIMARY answer.

    The digest is the secondary, and it exists because patch-id hashes
    CONTEXT lines: the same changed text picked onto a tree whose
    neighbourhood moved earns a different patch-id (the batch-2
    moved-target eviction — an innocent lane accused by an instrument that
    could not tell its content from its context). The digest covers the
    per-file payload with the hunk-header line-number fields and the
    text-file blob-addressing lines stripped: every +/- content line
    hashed under ITS OWN file's path, every metadata line (mode, deletion,
    rename, binary-ness) likewise. It stays stable across exactly the
    context shift patch-id flinches at, while still binding path, payload
    and patch shape.

    THE PATH IS BOUND FOR EVERY DIFF SHAPE, quoted or not (the r5
    false-carry classes, each reproduced through a CLEAN cherry-pick or
    probed pairwise before this cure). The `diff --git` header is the
    per-file authority (`_diff_header_path` — C-quoted forms decode, the
    unquoted same-path form length-splits); ---/+++ only refine when the
    header could not answer (the unquoted-rename case). Pre-cure, path came
    from ---/+++ alone, so a mode-only chmod hashed under `?` and a
    rename-remapped chmod false-carried; a quoted deletion's `--- "a/.."`
    failed the a/-prefix test and two different quoted deletions collided.

    A BINARY EDIT'S DIGEST BINDS ITS CONTENT, not just path and
    binary-ness: `git show` without --binary emits no payload, so the
    `index` line's blob oids ARE the content identity — recorded FULL
    (--full-index) because abbreviation width moves with the odb, and the
    two sides of a carry comparison are computed at different instants.
    Pre-cure, divergent binary bytes at one path shared a digest and a
    union-merged blob false-carried. core.quotepath is pinned because
    rename/Binary META lines hash the RENDERED path text, and the rendering
    must not follow user config (these ids cross repos in manifests).

    CARRY = (patch-ids equal OR digests equal) AND newline fingerprints
    equal. patch-id alone evicts the innocent; digest alone would forgive a
    hunk-positioned change that patch-id catches; and because the first two
    are an OR, a payload-newline change whose patch-id agrees would carry
    even as the digest disagrees — so the third axis is an AND veto, not a
    third option. True empty commits ("EMPTY" on all three axes) are
    comparable, never None — compose treats None as REFUSAL, never
    agreement.
    """
    be = vcs.backend(root)
    import hashlib
    rc, raw, _err = be.run(root, "-c", "diff.noprefix=false",
                           "-c", "core.quotepath=true",
                           "show", "--format=", "--full-index",
                           "--no-renames", sha)
    if rc != 0:
        return None
    body = raw if isinstance(raw, bytes) else raw.encode("utf-8", "replace")
    if not body.strip():
        # EMPTY is a REAL identity on all three axes, never a 2-field
        # short-form: the consumer compares the fingerprint unconditionally,
        # and a shape that varies with emptiness is the fail-open a review
        # named. A true empty commit has no newline change to bind.
        return ("EMPTY", "EMPTY", "EMPTY")
    rc, out, _err = be.text(root, "patch-id", "--stable", stdin=body)
    pid = out.split()[0] if rc == 0 and out.split() else None
    if not pid:
        return None                      # a diff git itself cannot hash
    # The secondary: sha256 over (path, +/-/meta line) pairs, headers and
    # context dropped — the payload axis that survives a context shift.
    parts = []
    nl_marks = []                # the changed-line newline fingerprint stream
    path = None
    idx = None
    prev = None                  # the raw diff line the next marker annotates
    in_hunk = False              # `--- `/`+++ ` mean different things inside
    for ln in body.split(b"\n"):
        if ln != _NO_NEWLINE:
            # THE ONE PLACE THE PREDECESSOR IS ASSIGNED, and it runs BEFORE
            # any branch below can `continue` past it. That ordering is the
            # whole cure: with the assignment inside the branches, two of
            # them (Binary and the mode/rename META family) returned early
            # and left a stale `prev`, so a marker after a META line
            # inherited an earlier `+` and hashed as the author's content.
            # Here a branch CANNOT preserve state, however it exits.
            #
            # `in_hunk` is read BEFORE this line can change it, which is the
            # correct instant: a `@@` line is not itself payload, and a
            # `diff --git` line ends the previous file's hunk rather than
            # belonging to it.
            prev = _nl_predecessor(ln, in_hunk)
        if ln.startswith(b"diff --git "):
            path = _diff_header_path(ln[11:])   # per-file state, not per-diff
            idx = None
            in_hunk = False             # a new file: headers are headers again
            continue
        if ln.startswith(b"@@"):
            # Unambiguous even mid-file: inside a hunk EVERY payload line
            # carries a leading +, - or space, so a bare `@@` is always the
            # next hunk header and never content. STRICT GRAMMAR (per
            # task/789 second stop): only a REGULAR unified-hunk header
            # (`@@ ...@@`) opens a hunk. A combined-diff `@@@` or a malformed
            # `@@` is not a shape this parser has rules for, so it makes the
            # whole identity unmeasurable rather than guessing the scan state.
            if not _REGULAR_HUNK.match(ln):
                return None
            in_hunk = True       # a marker never spans a hunk boundary
            continue
        if in_hunk:
            # INSIDE A HUNK THE FIRST BYTE CLASSIFIES, and nothing else can
            # collide (a review's second stop, measured): every structural
            # marker (`diff --git`, `index`, `Binary files`, the mode/rename
            # family) begins with a LETTER, so it can only appear OUTSIDE a
            # hunk. A payload line whose text renders as a header — removing
            # `-- final` shows as `--- final`, adding `++ final` as
            # `+++ final` — must reach the payload append HERE, before any
            # header branch below can swallow it. The pre-cure order read
            # `+++ ` / `--- ` / `index ` / META unconditionally, so an
            # in-hunk `--- alpha` / `+++ alpha` was treated as a file header
            # and never reached `parts`: two commits adding the same path
            # with payloads `++ alpha` vs `++ bravo` shared ONE digest — a
            # live false-carry under Compose's patch-OR-digest. Measured on
            # this box: digests equal, patch-ids distinct.
            if ln[:1] in (b"+", b"-"):
                parts.append((path or b"?") + b"\x00" + ln)
                continue
            if ln == _NO_NEWLINE or ln == b"":
                # The marker and the terminal split artifact fall THROUGH to
                # their handlers below (the marker must reach the marker
                # branch; the empty line is the stream's end).
                pass
            elif ln[:1] == b" ":
                # ORDINARY IN-HUNK CONTEXT: classified, carries no change, and
                # must NOT reach the closed-world refusal below — `prev` was
                # already assigned at the top of the loop, so a following
                # no-newline marker still finds it. Continue, never append.
                continue
            else:
                # A line inside a hunk that is neither payload nor context
                # nor the marker is a shape this parser has no rule for —
                # unmeasurable, never optimistically dropped.
                return None
        # Below here `in_hunk` is False: the block above already classified
        # every in-hunk line (payload +/- and context ` ` continue; only the
        # marker and the terminal empty line fall through; any other in-hunk
        # byte returned None), so these header/META branches can only be
        # reached OUTSIDE a hunk, where they are structural.
        if ln.startswith(b"index "):
            idx = ln                    # full oids — the --full-index pin
            continue
        if ln.startswith(b"+++ "):
            tgt = ln[4:]
            if path is None and tgt != b"/dev/null":
                path = tgt[2:] if tgt.startswith(b"b/") else tgt
            continue
        if ln.startswith(b"--- "):
            srcf = ln[4:]
            if path is None and srcf.startswith(b"a/"):
                path = srcf[2:]
            continue
        if ln.startswith((b"Binary files", b"GIT binary patch")):
            parts.append(b"META\x00" + (path or b"?") + b"\x00" + ln)
            parts.append(b"BIN\x00" + (path or b"?") + b"\x00"
                         + (idx or b"?"))
            continue
        if ln.startswith((b"deleted file mode", b"new file mode",
                          b"old mode", b"new mode", b"similarity index",
                          b"rename from", b"rename to",
                          b"copy from", b"copy to")):
            parts.append(b"META\x00" + (path or b"?") + b"\x00" + ln)
            continue
        if not in_hunk and ln[:1] in (b"+", b"-", b" "):
            # OUTSIDE A HUNK a raw payload OR CONTEXT line is NOT A VALID
            # SHAPE (row 1160 blocker #2, and a review's exact-tip
            # extension to space). The in-hunk block above already appended
            # every legitimate +/- payload and `continue`d, so reaching here
            # with a leading +/- means the line followed recognized
            # headers/META with NO `@@` hunk opening — a `+payload` with no
            # hunk around it. Minting an identity from that pretends a diff
            # was parsed when none was: measured, a mock body with valid
            # headers and no hunk returned a real digest for
            # `+payload-without-any-hunk`. A bare context line outside a hunk
            # is the same shape — it reached the end of the loop with no hunk
            # ever opened, so it is not context OF anything. Unmeasurable,
            # never appended. (An in-hunk context/marker line falls through
            # here with in_hunk True and is NOT refused — it reaches the
            # marker test below.)
            return None
        if ln == _NO_NEWLINE:
            # THE MARKER BELONGS TO THE LINE BEFORE IT, NOT TO THE FILE
            # (a review ruling, task/789). It is CONTENT when it annotates a
            # +/- PAYLOAD line — the commit itself changed whether the file
            # ends in a newline — and CONTEXT when it annotates an unchanged
            # line, which is trunk's formatting showing through a pick the
            # author had no part in.
            #
            # THE FIRST CURE GUESSED FROM FILE POSITION and was wrong in a way
            # that mattered: it bound every marker, so a clean pick whose lane
            # edited a file's MIDDLE while TRUNK stripped that file's ending
            # got a different digest for a change the lane never made. That
            # narrows the context-shift rescue from two axes to one — the
            # batch-2 eviction class, pointed at a new input.
            #
            # An ORPHANED marker (no classifiable predecessor) is MALFORMED
            # and makes the whole identity unmeasurable, never optimistically
            # one side or the other: compose reads None as REFUSAL, which is
            # the safe direction for a token nobody can attribute.
            if prev is None:
                return None
            if prev[:1] in (b"+", b"-"):
                token = b"NL\x00" + (path or b"?") + b"\x00" + prev[:1]
                parts.append(token)
                nl_marks.append(token)   # ONE classified stream, two readers
            # a marker consumes its predecessor: the next one must find its
            # own or be an orphan
            prev = None
            continue
        if ln == b"":
            continue                 # the terminal split artifact, not a line
        # CLOSED WORLD (the integrator's terminal-round ruling, task/789):
        # every line above either matched an enumerated shape and
        # `continue`d, or returned None. A line that reaches here is a shape
        # this parser has NO rule for — a valid-but-unenumerated structural
        # line git emitted (a future header, a metadata family nobody
        # listed), and the whole identity is unmeasurable BY CONSTRUCTION
        # rather than falling through into a digest that never saw it. Six
        # per-case findings on this parser are the signal to invert the
        # default, and this is the inversion: the next unknown shape is a
        # pass (unmeasurable), not a defect.
        return None
    digest = hashlib.sha256(b"\n".join(parts)).hexdigest() if parts else None
    # THE THIRD AXIS: the changed-line newline fingerprint, hashed from the
    # ONE classified `nl_marks` stream. The digest folds the NL tokens into
    # `parts`, so it catches a newline change only when patch-id ALSO
    # flinches — but Compose admits on patch-equal OR digest-equal, so a
    # payload-newline change whose patch-id agrees would carry even as the
    # digest disagrees (measured: two commits differing only by terminal
    # newline give patch_equal=True, digest_equal=False, and the OR admitted
    # it). The fingerprint is bound separately so Compose can AND it. None
    # exactly when the digest is, so the owner is uniformly 3-field.
    nl_fp = (hashlib.sha256(b"\n".join(nl_marks)).hexdigest()
             if digest is not None else None)
    # AXIS TOTALITY (the integrator's terminal-round ruling): a rich
    # identity is FULLY POPULATED on every axis or None entire. A truthy
    # tuple with None axes — a nonempty diff git could patch-id but whose
    # payload stream classified to nothing — is unrepresentable, because a
    # downstream `new_id[2] != orig_id[2]` on two Nones reads equal and a
    # carry gets authorized on emptiness (measured by a probe: a fabricated
    # unknown-metadata stream returned truthy (pid, None, None)). Compose
    # reads None as cannot-authorize-carry, the safe direction.
    if pid is None or digest is None or nl_fp is None:
        return None
    return (pid, digest, nl_fp)


CONTENT_EQUIVALENT = "content-equivalent"


CONTENT_ALGORITHM = "content-equivalent-v1"


def _commit_witness(root, sha):
    """{commit, parent, tree} for one commit, or None — the immutable half."""
    be = vcs.backend(root)
    rc, out, _err = be.text(root, "rev-list", "--parents", "-n", "1", sha)
    if rc != 0 or not out.split():
        return None
    fields = out.split()
    rc, tree, _err = be.text(root, "rev-parse", sha + "^{tree}")
    if rc != 0 or not tree.strip():
        return None
    return {"commit": fields[0], "parents": fields[1:], "tree": tree.strip()}


def _content_equivalent_witness(root, reviewed, carrier, trunk_ref, pinned,
                                identity, matches):
    """The IMMUTABLE record of a content-equivalent proof, or None.

    WHY A WITNESS AND NOT JUST A CARRIER SHA: this rung answers by SEARCHING
    trunk, and trunk MOVES. A replay six months from now that re-ran the
    search would walk a different history and could legitimately reach a
    different answer — more candidates, a revert-and-reapply since, a rewrite
    — so a proof that can only be re-derived by re-searching is not immutable.
    Everything needed to re-check the CLAIM without re-running the SEARCH is
    recorded here: both commits with their parents and trees, the pinned trunk
    the search ran against, the algorithm version, the content fingerprint
    that matched, and HOW MANY candidates matched it.

    `matches` is recorded even though the rung refuses anything but 1, because
    a stored 1 is what lets replay assert uniqueness was CHECKED rather than
    assumed — and if the rung's policy ever loosened, an old proof would still
    say what it was granted under.
    """
    src = _commit_witness(root, reviewed)
    car = _commit_witness(root, carrier)
    if not src or not car:
        return None
    return {"v": 1, "algorithm": CONTENT_ALGORITHM,
            "trunk_ref": trunk_ref, "trunk": pinned,
            "source": src, "carrier": car,
            "payload_digest": identity[1], "newline_fingerprint": identity[2],
            "matches": matches}


def _content_application_equivalent(root, source, carrier):
    """(True | False | None, why) — does source's delta produce carrier?

    Content fingerprints narrow the candidate set, but cannot distinguish two
    identical replacements at different occurrences. This applies the source's
    single-parent delta to the candidate's parent in a temporary object store
    and compares the resulting tree with the candidate tree. No worktree, ref,
    index, or persistent object is changed.
    """
    src = _commit_witness(root, source)
    car = _commit_witness(root, carrier)
    if not src or not car:
        return None, "source or carrier commit is unreadable"
    if len(src["parents"]) > 1 or len(car["parents"]) > 1:
        return False, "application-equivalent content requires one delta per commit"
    be = vcs.backend(root)
    rc, patch, _err = be.run(
        root, "diff-tree", "--root", "--no-commit-id", "-r", "-p",
        "--binary", "--full-index", "--no-renames", "--unified=0", source)
    if rc != 0 or not patch:
        return None, "the source delta is unreadable"
    rc, objects, _err = be.text(root, "rev-parse", "--git-path", "objects")
    if rc != 0 or not objects:
        return None, "repository object directory is unreadable"
    objects = objects if os.path.isabs(objects) else os.path.abspath(
        os.path.join(root, objects))
    with tempfile.TemporaryDirectory(prefix="helm-content-apply-") as tmp:
        scratch = os.path.join(tmp, "objects")
        os.mkdir(scratch)
        env = {"GIT_INDEX_FILE": os.path.join(tmp, "index"),
               "GIT_OBJECT_DIRECTORY": scratch,
               "GIT_ALTERNATE_OBJECT_DIRECTORIES": objects}
        empty = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
        if not car["parents"]:
            rc, made, _err = be.text(
                root, "hash-object", "-t", "tree", "-w", "--stdin",
                env=env, stdin=b"")
            if rc != 0 or made != empty:
                return None, "could not materialize the empty carrier base"
        carrier_base = car["parents"][0] if car["parents"] else empty
        rc, _out, err = be.text(root, "read-tree", carrier_base, env=env)
        if rc != 0:
            return None, "the carrier base is unreadable (%s)" % (err or "git failed")
        rc, _out, err = be.text(
            root, "apply", "--cached", "--unidiff-zero", "--whitespace=nowarn",
            env=env, stdin=patch)
        if rc == 1:
            return False, "the source delta does not apply at the carrier site"
        if rc != 0:
            return None, "could not apply the source delta (%s)" % (err or "git failed")
        rc, tree, err = be.text(root, "write-tree", env=env)
    if rc != 0:
        return None, "application replay produced no tree (%s)" % (err or "git failed")
    tree = tree.strip()
    if not _sha(tree):
        return None, "application replay did not produce a tree"
    if tree != car["tree"]:
        return False, "same content is not application-equivalent at this site"
    return True, None


def _content_equivalent_replay(root, witness):
    """(True | False | None, why) — re-check a recorded proof. NO TRUNK WALK.

    THE WHOLE POINT OF THE WITNESS. The rung ANSWERS by searching trunk, and
    trunk moves; a replay that re-ran the search would ask a different question
    every time it ran. This re-derives the identity of the two RECORDED commits
    and compares them to the RECORDED fingerprints, so the answer is a property
    of the objects the proof named and not of today's history.

    None is UNREADABLE and never False: a witness whose commits this clone
    cannot resolve says the measurement failed here, not that the proof was
    wrong. A close ladder that read those the same way would retroactively
    void every proof written on a machine that later pruned an object.
    """
    if not isinstance(witness, dict) or witness.get("v") != 1:
        return None, "witness is not a v1 record"
    if witness.get("algorithm") != CONTENT_ALGORITHM:
        return None, ("witness was written by %r, not %r"
                      % (witness.get("algorithm"), CONTENT_ALGORITHM))
    if witness.get("matches") != 1:
        return False, ("witness records %r matches — a proof granted on a "
                       "non-unique carrier is not one"
                       % (witness.get("matches"),))
    for side in ("source", "carrier"):
        rec = witness.get(side)
        if not isinstance(rec, dict) or not rec.get("commit"):
            return None, "witness has no %s commit" % side
        live = _commit_witness(root, rec["commit"])
        if not live:
            return None, ("%s commit %s does not resolve here"
                          % (side, str(rec["commit"])[:12]))
        if live["tree"] != rec.get("tree") or live["parents"] != rec.get("parents"):
            return False, ("%s commit %s no longer has its recorded tree or "
                           "parents — history was rewritten under this proof"
                           % (side, str(rec["commit"])[:12]))
        ident = _commit_content_identity(root, rec["commit"])
        if not ident or ident[1] in (None, "EMPTY"):
            return None, ("%s commit %s has no readable content identity"
                          % (side, str(rec["commit"])[:12]))
        if ident[1] != witness.get("payload_digest") or \
                ident[2] != witness.get("newline_fingerprint"):
            return False, ("%s commit %s does not carry the recorded content "
                           "fingerprint" % (side, str(rec["commit"])[:12]))
    trunk = witness.get("trunk")
    if not _sha(trunk):
        return None, "witness has no pinned trunk commit"
    carrier = witness["carrier"]["commit"]
    rc, _out, err = vcs.backend(root).text(
        root, "merge-base", "--is-ancestor", carrier, trunk)
    if rc == 1:
        return False, ("carrier commit %s is not reachable from the pinned trunk %s"
                       % (carrier[:12], trunk[:12]))
    if rc != 0:
        return None, ("carrier membership in the pinned trunk is unreadable (%s)"
                      % (err or "git failed"))
    equivalent, why = _content_application_equivalent(
        root, witness["source"]["commit"], carrier)
    if equivalent is not True:
        return equivalent, why
    return True, None


def _commit_paths(root, sha):
    """The b-side path set one commit touches, or None if unreadable.

    Read with `-z` and `--no-renames` for the same reason the identity is:
    git QUOTES non-ASCII paths under some core.quotepath settings and not
    others, so a quoted read would make the candidate walk depend on the
    READER's configuration. This only NARROWS a candidate list — the identity
    comparison decides — but a narrowing that silently drops a path drops the
    carrier with it, so it gets the byte-exact door too.
    """
    be = vcs.backend(root)
    rc, raw, _err = be.run(root, "-c", "diff.noprefix=false",
                           "-c", "core.quotepath=false",
                           "diff-tree", "--root", "--no-commit-id", "-r",
                           "--raw", "-z", "--no-renames", sha)
    if rc != 0:
        return None
    body = raw if isinstance(raw, bytes) else raw.encode("utf-8", "replace")
    parts = body.split(b"\0")
    out, i = [], 0
    while i < len(parts):
        head = parts[i]
        if not head:
            i += 1
            continue
        if not head.startswith(b":") or i + 1 >= len(parts):
            return None
        out.append(os.fsdecode(parts[i + 1]))
        i += 2
    return out or None


def _content_equivalent_carrier(root, tip, trunk_ref, trunk_sha=None):
    """(carrier sha, why-not) — the ONE trunk commit carrying this tip's change.

    THE FOURTH RUNG, and deliberately the weakest mechanical one:

        ancestry > patch-identity > recorded translation > CONTENT-EQUIVALENT
                 > independent confirmation

    It exists because Git can place 969 of 1167 reviewed tips on trunk and
    cannot place 198 (measured 2026-08-09: ancestor 505, patch-equivalent 464,
    absent 196, unknown 2). For land request db87bcd4 the reviewed tip and the
    trunk commit 98582d17 differ by exactly 3 of 98 CONTEXT lines and by
    nothing else — same added bytes, same removed bytes, same paths — and
    `git cherry` calls that absent. Three context lines were keeping a landed
    row billing a named seat.

    IT CONSUMES THE DIGEST AXIS ONLY. patch-identity already owns the stronger
    tier and is checked BEFORE this one, so reusing it here would be a second
    admission door for a question already answered; it is kept as a CONTROL
    (a patch-identity match means the caller should never have reached this
    rung) and never as an alternate way in. The newline fingerprint is a VETO
    ANDed on top, not a third option: two commits that disagree about whether
    a file ends in a newline are two changes, and Compose learned that the
    expensive way (task/789).

    BOTH SIDES MUST SATISFY THE PROOF DOMAIN. A carrier outside it is as
    unusable as a source outside it, and asking only about the source is how a
    binary or a merge sneaks in from the trunk side.

    A SHARED KEY IS UNKNOWN, NEVER A PICK. Two trunk commits carrying the same
    change — revert-then-reapply is the ordinary way — give no basis in content
    for choosing, and choosing anyway is the failure this rung exists to avoid.
    Measured on this trunk: 2744 commits, 2353 distinct keys, 3 shared (0.13%).

    UNIQUENESS IS SCOPED TO THE PINNED TRUNK, never the object database: a repo
    holds unreferenced objects and other lanes' tips, and an answer that
    changes when somebody fetches is not a proof.
    """
    be = vcs.backend(root)
    # PARENT COUNT FIRST, BEFORE ANY IDENTITY READ (a review ruling). A merge
    # has no single delta — it depends which parent you diff against — and
    # `git show` emits nothing for one, so the identity falls through to the
    # EMPTY sentinel. Catching merges THERE would work by accident and would
    # bucket every merge with every genuinely-empty commit, which is a
    # collision class, not a refusal. Ask the structural question first and
    # say which one it was.
    rc, out, _err = be.text(root, "rev-list", "--parents", "-n", "1", tip)
    if rc != 0 or not out.split():
        return None, "the source commit does not resolve"
    if len(out.split()) > 2:
        return None, "a merge has no single delta to compare"
    source = _commit_content_identity(root, tip)
    if not source or source[1] in (None, "EMPTY"):
        return None, "the source commit has no readable content identity"
    pinned = trunk_sha
    if not pinned:
        rc, out, _err = be.text(root, "rev-parse", trunk_ref)
        if rc != 0 or not out.strip():
            return None, "trunk ref %s does not resolve" % trunk_ref
        pinned = out.strip()
    # NARROW THE WALK BY THE SOURCE'S OWN PATHS, never the answer: a carrier
    # must touch every path the source touches, so a commit touching none of
    # them cannot be one. `rev-list -- <paths>` returns commits touching ANY,
    # a strict SUPERSET of the eligible set, and the identity comparison stays
    # the only thing that decides. Measured: 37s unnarrowed vs 4s narrowed on
    # db87bcd4's eight files, same carrier both ways.
    paths = _commit_paths(root, tip)
    if paths is None:
        return None, "the source commit's path set is unreadable"
    literal = [":(literal)" + path for path in paths]
    # --full-history OR THE CENSUS IS NOT A CENSUS. A path-limited rev-list
    # applies DEFAULT HISTORY SIMPLIFICATION at merges: when a merge is
    # TREESAME to one parent for these paths, git prunes the OTHER side
    # entirely. A content-equivalent carrier living on that pruned branch is
    # reachable from the pin and invisible here — and both outcomes are then
    # wrong in opposite directions. With one surviving hit the uniqueness
    # check below GRANTS uniqueness it has not established; with none it
    # reports a clean MISS. Either retires a row on an incomplete carrier
    # census, which is the one thing this whole rung exists to prevent.
    rc, out, _err = be.text(root, "rev-list", "--full-history", pinned,
                            "--", *literal)
    if rc != 0:
        return None, "could not walk %s for candidates" % trunk_ref
    hits, unreadable, displaced = [], [], []
    for sha in out.split():
        other = _commit_content_identity(root, sha)
        if not other:
            rec = _commit_witness(root, sha)
            if rec and len(rec["parents"]) > 1:
                continue                 # a merge has no single candidate delta
            unreadable.append(sha)
            continue
        if other[1] == "EMPTY":
            continue                     # outside a non-empty source's domain
        if other[1] is None:
            unreadable.append(sha)
            continue
        if other[1] != source[1] or other[2] != source[2]:
            continue
        equivalent, _why = _content_application_equivalent(root, tip, sha)
        if equivalent is None:
            unreadable.append(sha)
        elif equivalent:
            hits.append(sha)
            if len(hits) > 1:
                break                    # non-uniqueness is now irrevocable
        else:
            displaced.append(sha)
    if len(hits) > 1:
        return None, ("%d trunk commits carry application-equivalent content "
                      "(%s) — content cannot choose between them"
                      % (len(hits), ", ".join(h[:12] for h in hits[:3])))
    if unreadable:
        return None, ("the candidate census is unreadable at %s — content "
                      "cannot prove uniqueness" % ", ".join(
                          sha[:12] for sha in unreadable[:3]))
    if not hits:
        if displaced:
            return None, ("matching payload was not application-equivalent at "
                          "the recorded site")
        return None, None               # a complete, clean miss
    return hits[0], None


def _commit_content_id(root, sha):
    """The historical 2-tuple adapter: (patch_id, payload_digest).

    The RICH owner is `_commit_content_identity`, which adds the newline
    fingerprint as a third axis. This adapter preserves the two-field shape
    for callers that predate it; Compose consumes the rich owner directly so
    its newline veto is never bypassed through the adapter. Returns None
    exactly when the owner does."""
    identity = _commit_content_identity(root, sha)
    if identity is None:
        return None
    return identity[:2]


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


# ---------------------------------------------------------------------------
# THE OWNER-NAMES TABLE: what the satellites own and this module re-exports
# ---------------------------------------------------------------------------
# EVERY NAME BELOW IS STILL IMPORTABLE FROM `landreq` EXACTLY AS BEFORE, which
# is what makes the split behaviour-neutral for callers: `cli.py` resolves its
# verbs by string, and roughly six hundred arms across twenty test modules
# patch attributes on THIS module and then drive them.
#
# ONE LITERAL, AND IT DRIVES THE BINDING. Each satellite's publish loop reads
# THIS tuple, so the declaration and the runtime binding cannot drift -- there
# is no second list to forget. It is also what the retired-name rung reads: a
# name removed from this file is not retired when this literal hands it to a
# satellite that actually defines it, and without the declaration that rung
# refuses, because a split that forgot to republish leaves every
# `landreq.NAME` consumer dangling and a focused diff cannot see it.
#
# IT IMPORTS THE MODULES, NEVER THE NAMES. Importing names here would deadlock
# the reverse order against satellites that import this module eagerly; a
# module object exists in `sys.modules` from the first line of its execution,
# so binding through the module succeeds whichever one the process reaches
# first.
_OWNER_NAMES = (
    ("landreq_cli", (
        "_card_standing", "_cmd_abandon", "_cmd_annotate_delivered_report",
        "_cmd_close", "_cmd_close_landed", "_cmd_compose",
        "_cmd_discharge", "_cmd_expired", "_cmd_land",
        "_cmd_legacy_completion_hints", "_cmd_lr", "_cmd_retire",
        "_cmd_retire_inner", "_cmd_withdraw", "_line", "_print_loop_list",
        "_print_off_frontier", "_print_retire_sweep", "_render_show",
        "card", "cmd_lr"
    )),
    ("landreq_close", (
        "_chain_authority", "_chain_authority_anchor", "_chain_children",
        "_chain_end", "_chain_forest", "_chain_key", "_chain_polarity",
        "_chain_repo", "_chain_root_index", "_chain_unfinished",
        "_chain_write_authority", "_close_expired_one", "_close_label",
        "_close_ladder_build_landed", "_close_ladder_carried",
        "_close_ladder_chain_proof", "_close_ladder_delivered_report",
        "_close_ladder_discharged", "_close_ladder_endorsement_moot",
        "_close_ladder_expired",
        "_close_ladder_landed", "_close_ladder_resolved",
        "_close_ladder_stranded", "_close_ladder_subsumed",
        "_close_ladder_superseded", "_close_ladder_withdrawn",
        "_close_landed_content", "_close_landed_proof",
        "_close_out_of_scope", "_close_repo", "_close_result",
        "_close_same_tip_siblings", "_close_trunk",
        "_landed_head_predecessors", "chain_attestation",
        "chain_contributor_index", "chain_contributors", "chain_credits",
        "chain_fork_census", "chain_frontier_error", "chain_key",
        "chain_path_intact", "close", "close_landed", "close_routes"
    )),
)

from . import landreq_cli            # noqa: E402  (tail binding)
from . import landreq_close          # noqa: E402  (tail binding)
