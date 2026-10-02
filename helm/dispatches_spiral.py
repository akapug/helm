"""`helm.dispatches_spiral` -- IS ONE LANE SPIRALLING, and what ends it.

ONE QUESTION. Every name here reads the dispatch ledger for serialized
rounds of review on one lane -- counted as distinct reviewed tips per sender
and lane -- and answers whether the next move is another round or a MELD:
`review_spiral` for the Stop rung, `chain_rounds` for one chain, and the
fold, the prescription and the conversation-is-over test they share. Nothing
here writes.

MEASURED AT THE CUT, NOTHING LEFT IN THE LEDGER CALLS IN: the remaining
`dispatches` module names none of these definitions. Their readers -- the
spiral Stop rung, the stop-facts resident, `spiral_findings` and the review
door -- spell them `dispatches.review_spiral` and `dispatches.chain_rounds`,
which still answer. That is what makes this a leaf to cut.

THE CODE MOVED WHOLE, AND EVERY LEDGER NAME IS SPELLED `dispatches.NAME` AT
ITS CALL SITE -- 57 reads of 30 distinct names; no other byte of a moved line
changed. A `from` import, or a bare global left behind, binds the object ONCE
at import, so an arm that patches an attribute on `dispatches` and then drives
this code would reach the original and measure nothing, while every structural
guard stayed green. The module spelling keeps the lookup at CALL TIME, exactly
as a bare global did inside the ledger. Names this file OWNS are spelled that
way too, because they are published back onto `dispatches` and the suites
patch them there.

THE ONE EXCEPTION IS A READ THAT RUNS AT IMPORT, and it is the faithful
spelling rather than a gap: the default window `hours=SPIRAL_WINDOW_H` on
three readers, and `SPIRAL_ADVISORY_PRESCRIPTIONS`, which is built from
`SPIRAL_UNREAD`. Such a read binds once in either spelling, as it did in the
ledger, and at that moment only this module's own global exists -- the publish
loop at the bottom has not run yet, so `dispatches.NAME` there would raise.

ONE NAME MOVED UNRESOLVED, AND ITS OWN LANE RESOLVED IT (task/3412).
`_reader_clean` read `seats`, which the ledger never binds at module scope, so
the call raised inside its own `try` and the answer was False; the move kept
that as `dispatches.seats`, which names nothing either. It now imports `seats`
at call time, the way the other satellites reach a sibling module, so the
count above is 56 reads of 29 names today.

THE CYCLE IS BROKEN THE WAY THE LEDGER'S OTHER SATELLITES BREAK IT: this
module imports `dispatches` EAGERLY, and `dispatches` imports this one at the
END of its own body, after every name it needs exists. A module object is in
`sys.modules` from the first line of its execution, so either import order
resolves.
"""
from . import dispatches
from . import spiral_findings


# ---------------------------------------------------------------------------
# review spiral — serialized rounds on ONE lane, where a MELD is the cure
# ---------------------------------------------------------------------------

# THE INCIDENT (2026-07-29/30). helm's typed store already holds the rule. The
# `review-begins-with-cat-file` heuristic says, verbatim: "at TWO rounds the
# cure is a MELD, never round three. Live cost of getting this wrong: ~6 async
# rounds on one small lane, 2026-07-29." That entry FIRED in the integrator's
# injected context on EVERY TURN of the session in which he then ran six
# serialized rounds on one lane, until the owner asked why a meld had not
# ended it before round 6 — after which ONE meld exchange closed
# all three remaining questions.
#
# So this is not a knowledge gap and another store entry cannot fix it. Owner,
# same night: "we still fail to reach for them automatically. maybe stophooks
# that recognize situations where they would be handy?" A rule that fires and
# is not followed needs a GATE, not a louder rule (premise
# enforce-not-advise-for-repeated-behavior).
#
# THE SIGNAL: DISTINCT REVIEWED TIPS PER LANE, NOT DISPATCH COUNT.
# Counting review dispatches per lane is the AVAILABLE signal; counting the
# distinct TIPS those dispatches bound is the RIGHT one, and the live ledger
# says so out loud. Measured over the real ledger (1041 events, 202 review
# rows, 130 sender/lane pairs), lane `stop-candidate-seat-scope` carries two
# review dispatches from `claude` at the SAME tip ad7f2b1ac951, 38 seconds
# apart, to gemini and to ds4pro. That is a deliberate cross-family fan-out —
# helm's OWN law (heuristic 14: cross-family refutation, not same-family
# refinement) — and it is the healthiest move a dispatcher makes. Raw dispatch
# count calls it two rounds and would gate the one behaviour we want more of.
# Distinct tips calls it one round, correctly: A ROUND IS A NEW TIP, because a
# round happens when the author changed the code and re-submitted. The ledger
# records the tip natively (`_resolve_tip` resolves it at write time), so this
# is measured, not inferred — unlike the "-r2 means review" lane-name heuristic
# that misclassified 5 of 17 and is the reason `kind` exists at all.
#
# THE READER IS `snapshot()`, NOT RAW EVENTS — the deliberate inverse of
# `mix()` above, for a stated reason. `mix` asks "what was SENT in a window"
# and so reads sent events. This asks "what obligations exist on this lane and
# in what STATE", which is precisely snapshot's question: it dedupes by id,
# validates identity, and carries the terminal status this needs to drop
# CANCELLED rounds. A cancelled dispatch is a withdrawn round — it never
# consumed a reviewer round-trip — and counting it inflates a healthy lane
# toward the threshold on work that did not happen. Measured: dropping
# cancelled rows halves the firing population (10 lanes -> 5 of 122) while both
# real incidents survive at their FULL round counts, 8 and 7.
SPIRAL_WINDOW_H = 12
# Measured distinct-tip distribution over those 122 sender/lane review pairs:
#   1 round 104 | 2 rounds 13 | 3 rounds 2 | 4 rounds 1 | 7 rounds 1 | 8 rounds 1
# TWO is the store's stated CURE point, not its failure point, and 2 rounds is
# ordinary: one round of findings, a fix, a re-review, done. Gating there would
# stop ~13% of all review lanes, and this very file already carries the lesson
# that a rung which blocks everybody is switched off within a day (see the
# built-but-not-wired latch in seats.py). So TWO WARNS.
# THREE is the round the rule forbids by name. Blocking there fires on 5 of 122
# pairs (4%) and catches BOTH measured incidents — `gate-mints-its-own-evidence`
# (the integrator -> codex, 7 tips in 1h58m) and
# `resolve-matches-session-not-just-env` (claude -> codex, 8 tips in 6h46m).
# The block lands the moment round three has been dispatched and the seat tries
# to go idle waiting for its verdict, which is exactly where the owner
# interrupted by hand: it converts round three into the LAST async round
# instead of the third of six.
SPIRAL_MELD_ROUNDS = 2
SPIRAL_BLOCK_ROUNDS = 3

# A CHAIN THAT ENDED IS NOT A SPIRAL — it is a conversation that CONVERGED.
# Measured false positive, 2026-07-31: the guard fired on `land-pipeline-card`
# at 4 rounds. That chain's rounds read fix, fix, fix, fix, fix, fix, APPROVE —
# It was approved and it LANDED at e5e4cad before the guard ever spoke. The
# rounds were real; the spiral was over. Counting rounds answers "how much
# ping-pong has there been", but the guard's actual question is "is there a
# ping-pong I can still interrupt", and only the LAST round's polarity answers
# that one. Same narrower-neighbour defect this file already documents twice.
#
# `cancelled` is dropped earlier as a WITHDRAWN round; these are the polarities
# that END one. `approve` ends the work; `supersede` hands it to a new chain
# root, which then counts on its own from one. A `fix` verdict is NOT terminal
# and must keep counting — a chain sitting at round three with findings
# outstanding is precisely the live spiral about to become round four, and that
# is the one the block exists to catch.
#
# ONE RULE DOES THIS, NOT TWO. The obvious first fix — skip a chain whose own
# last round carries a terminal polarity — was written, and MUTATION TESTING
# KILLED IT: reverting it left the suite green, because the spent-prefix rule
# below already rejects every input it rejected. A chain that ends in its own
# approve has last-round-ts == that approve's ts, so `<=` catches it too. Two
# checks refusing the same input measure NEITHER, since reverting either leaves
# the other refusing; the redundant one was deleted rather than kept for
# comfort. If you are tempted to re-add it, the surviving rule is strictly
# weaker and strictly sufficient.
#
# FAIL-OPEN IN THE RIGHT DIRECTION. The cost of the miss and the cost of the
# false fire are not symmetric: a missed spiral wastes reviewer round-trips,
# while a false block stops a seat from going idle over work that already
# shipped — and this file already carries the lesson that a rung which fires on
# healthy behaviour is switched off within a day (the built-but-not-wired latch
# in seats.py). A guard that gates converged work teaches the fleet to ignore it.
SPIRAL_TERMINAL_POLARITIES = ("approve", "supersede")

# A SOURCE-CLEAN HOLD ENDS A ROUND THE WAY AN APPROVE DOES (task/3072). It is
# the reviewer's positive answer in the one shape the door lets a reviewer
# give it: the source read found nothing, and an approve must bind a
# whole-suite token only the land gate mints, so the verdict door itself sends
# a clean reader to `hold --source-clean TIP`. Reading the hold as "no answer"
# made the rung fire on a chain whose newest event WAS the clean read —
# measured on a four-row chain whose last row was held SOURCE-CLEAN and still
# printed "round 4". It joins `settled` below, not the polarity tuple: a hold
# is not a verdict polarity and must never enter an authorization set.
#
# THE CLOSING CURE SEND IS NOT A ROUND (task/3072, task/3713 D2).
# PATCH names the reviewer's committed cure with `--patch-tip`; MELD-DIFF
# records the diff for the author to apply, and its first advancing direct
# successor names that cure tip. Both closing re-sends ask for confirmation,
# not another cure cycle. A FIX read at either tip restores the real round.
# The tip still supplies newest peer, lane and landing probe.


def _is_fix(row):
    return row.get("status") == "verdict" \
        and str(row.get("polarity") or "").casefold() == "fix"


def _adopted_patch_tips(observations, pending=None):
    """Closing re-sends of PATCH or author-applied MELD-DIFF cures.

    `observations` is the fold's per-chain map (tip -> rows from any sender).
    PATCH names the reviewer's committed cure tip explicitly. MELD-DIFF names
    no tip: only the first advancing, direct supersedes child of its FIX can
    be the author's cure. `pending` is the door's unsent child of that FIX.
    A FIX read AT either adopted tip restores it as a real round."""
    out = dispatches._adopted_diff_tips(observations, pending)
    for rows in (observations or {}).values():
        for row in rows:
            if dispatches._is_fix(row):
                patch = str(row.get("patch_tip") or "").lower()
                if patch:
                    out.add(patch)
    return {tip for tip in out
            if not any(dispatches._is_fix(r) for r in (observations or {}).get(tip, ()))}


def _adopted_diff_tips(observations, pending=None):
    """Advancing, direct author re-sends of a MELD-DIFF reader's FIX.

    Unlike PATCH, the reader names no committed tip. The author must first
    send a DIFFERENT tip directly superseding the particular FIX row. A later
    seat may carry the author's work: `sender` can change on a continuation,
    so it is NOT proof of who applied the diff. Chain/repo identity, direct
    parenthood, and the child opener's checked application proof are required.
    Until then the finding is still a
    round; if the cure itself draws FIX, that read becomes a real round. The
    optional unsent row gives `chain_rounds` the stop rung's post-append view.
    """
    obs = observations or {}
    rows = [r for group in obs.values() for r in group]
    parents = {str(r.get("id") or ""): r for r in rows if dispatches._is_fix(r)
               and r.get("review_mode") == "MELD-DIFF"
               and dispatches._has_diff_handoff(r)
               and not r.get("patch_tip")}
    out, seen = set(), set()
    for row in rows + ([pending] if pending else []):
        parent = parents.get(str(row.get("supersedes") or ""))
        tip = str(row.get("tip") or "").lower()
        # The writer mints application proof only after seeing the parent's
        # FIX. Equal second-resolution stamps cannot overturn that ordering;
        # without proof, ties still fail closed. Pending sends use the door's
        # current instant.
        sent = dispatches.instant_epoch(row.get("ts"))
        fixed = dispatches.instant_epoch(parent.get("verdict_ts")) if parent else None
        if not parent or parent["id"] in seen or not tip \
                or sent is None or fixed is None or sent < fixed \
                or (sent == fixed and not dispatches._has_applied_diff(row, parent)) \
                or tip == str(parent.get("tip") or "").lower() \
                or row.get("chain_root") != parent.get("chain_root") \
                or row.get("repo_id") != parent.get("repo_id") \
                or obs.get(tip, (row,))[0] is not row:
            continue
        # Only the FIRST new tip from this FIX can be its closing re-send;
        # sending another distinct tip beside it opens an ordinary round.
        seen.add(parent["id"])
        if dispatches._has_applied_diff(row, parent) \
                and not any(dispatches._is_fix(r) for r in obs.get(tip, ())):
            out.add(tip)
    return out

# WHICH PRESCRIPTIONS ADVISE RATHER THAN BLOCK, read by the stop rung so the
# two surfaces cannot disagree about which one walls a seat. FINISH is a
# convergence the typed counts proved. UNDER-ARMED is NOT here (T3): every
# round finding a defect the earlier arms could not see is the chain that
# most needs the BAR — the closed harms and falsifier classes the next read
# may use — and a paired A/B measured the bar meld paying off on exactly that
# shape, so it blocks with a bar-meld invite instead of advising. UNREAD is a chain with enough dispatches for a block
# and too few recorded reads for rounds (task/2682): a meld would spend the
# one reader who finally arrived on a conversation with nothing to converge.
# MELD, the spiral itself, is absent here and blocks.
SPIRAL_UNREAD = "UNREAD"
SPIRAL_ADVISORY_PRESCRIPTIONS = ("FINISH", SPIRAL_UNREAD)


def _reader_clean(row):
    """Is this row held SOURCE-CLEAN by its own READER (the recipient)?

    A source-clean claim is the reviewer's answer. The hold door stamps who
    held (`hold_actor`, task/3053) and refuses a clean claim from anyone but
    the recipient; the FOLD binds it too, because a forged event, or a hold
    written before the stamp, reaches the fold without passing that door. A
    claim held by anyone else — the author on its own open row, above all —
    answers nothing, and one that names no hand cannot be shown to be the
    reader's, so it answers nothing either. Fail closed: a clean claim
    SUPPRESSES the rung, so an unproven one must not.

    THE READER IS WHOEVER THE DOOR'S OWN COMPARATOR SAYS IT IS (task/3412):
    `seats.recipient_matches`, the one the hold door refuses a stranger
    with. It was read through a name no module binds, inside an
    `except Exception`, so the call raised whenever it was reached and an
    alias the door admits read as a stranger's hand, silently. No handler
    guards it now: the comparator is total over two strings, so the only
    thing a handler here ever caught was a binding like that one. A failure
    propagates to its callers' own wrappers, and each one is heard: the stop
    rung records the swallow, the stop-facts resident reports the rounds as
    UNKNOWN, and the dispatch door prints that it could not read the chain.
    None of them reads a broken comparator as "not the reader".

    FOR A ROW THE FOLD WROTE THE TWO ANSWERS AGREE: the fold keeps a
    `hold_actor` only when it is a seat token and stores the recipient
    canonical, and for two tokens the comparator IS casefold equality. The
    second answer decides a row that did not come through the fold."""
    if not dispatches._clean_tip_of(row):
        return False
    actor = str(row.get("hold_actor") or "").strip()
    recipient = str(row.get("recipient") or "").strip()
    if not actor or not recipient:
        return False
    if actor.casefold() == recipient.casefold():
        return True
    from . import seats
    return seats.recipient_matches(actor, recipient)


def _answered(rows):
    """Did anyone ANSWER the round these rows carry?

    A verdict of any polarity, a source-clean hold by the row's reader, or a
    model run's advisory read is an answer. An open, held, discharged or
    cancelled row with none of those is a dispatch nobody read."""
    return any(r.get("status") == "verdict" or r.get("verdict_ts")
               or dispatches._reader_clean(r) or r.get("advisory_reads")
               for r in rows or ())


def _round_view(bucket, pending=None):
    """({tip: order} counted as ROUNDS, unanswered-tip count).

    A round is a new tip the author asked someone to judge, and two kinds of
    tip are not one:
      * an adopted PATCH or author-applied MELD-DIFF cure tip confirms a cure
        already judged (task/3072, task/3713 D2);
      * a tip nobody answered, once a newer tip replaced it, was never read
        (task/2682): the author rebased, or the reader was walled, and a meld
        has no finding to converge. Its count is returned so the rung can say
        the missing thing is a reader instead of prescribing a meld.
    The NEWEST tip always counts: it is the round in flight. `order` is the
    tip's first dispatch in LEDGER APPEND ORDER (see `_spiral_fold`), so
    exactly one tip is newest; two sends in one second are two ordered
    rounds, never a tie that one reader breaks one way and another the
    other."""
    adopted = dispatches._adopted_patch_tips(bucket.get("observations"), pending)
    tips = {tip: order for tip, order in bucket["tips"].items()
            if tip.lower() not in adopted}
    latest = max(tips.values()) if tips else None
    observations = bucket.get("observations") or {}
    counted = {tip: order for tip, order in tips.items()
               if order == latest or dispatches._answered(observations.get(tip))}
    return counted, len(tips) - len(counted)


def _unread_evidence(dispatched, read):
    """The UNREAD advisory's sentence: what was measured, and the fix.

    Zero recorded reads means there is no reviewer yet (task/2682). Telling
    the author to record a verdict assumes someone already read the tip and
    left it out of the ledger, so that sentence is only for a chain that has
    at least one recorded read.
    """
    if not read:
        return ("%d dispatches and ZERO reads; the reviewer is the missing "
                "thing, go find one" % dispatched)
    return ("%d dispatches at distinct tips, %d with a recorded read (a "
            "verdict, a source-clean hold or an advisory read), so there is "
            "no recorded finding to converge. The fix: the reader records "
            "each read on its row with `helm dispatch verdict <row> <tip> "
            "...`; an answer given only in chat does not count. Once the "
            "reads are recorded the chain reads MELD or FINISH as usual"
            % (dispatched, read))


def _patch_tips_of(observations):
    """Every patch tip a FIX on the chain named: the chain's tip lineage a
    meld's agreed TIP may point into, beside the dispatched tips."""
    return {str(r.get("patch_tip")).lower()
            for rows in (observations or {}).values() for r in rows
            if dispatches._is_fix(r) and r.get("patch_tip")}


def _answered_reading(bucket, current):
    """(prescription, evidence) over the chain's ANSWERED rounds only.

    THE ONE READING THE STOP RUNG AND THE SEND DOOR BOTH GIVE. The rung runs
    after a send, with the newest round in flight and no verdict on it; the
    door runs before the send, when that round does not exist yet. Read over
    every counted round, the rung saw an in-flight round as a missing
    observation and answered MELD on a chain whose recorded counts converged,
    while the door, reading the same chain, answered FINISH. The finding
    trajectory is a fact about reads that happened, so both surfaces read
    exactly those. The round COUNT still includes the round in flight."""
    obs = bucket.get("observations") or {}
    answered = {t: w for t, w in bucket["tips"].items() if dispatches._answered(obs.get(t))}
    if len(answered) < dispatches.SPIRAL_MELD_ROUNDS:
        return "MELD", ("finding trajectory UNKNOWN: %d answered round(s), "
                        "fewer than %d" % (len(answered), dispatches.SPIRAL_MELD_ROUNDS))
    view = dict(bucket, tips=answered)
    prescription, evidence = dispatches._spiral_prescription(view, current)
    # ROUNDS ARE COUNTED BY THE CALLER; WHAT THE ROUNDS FOUND IS COUNTED HERE.
    # Three rounds re-litigating one finding read identically to three rounds
    # each finding a defect the last one's arms could not see. Only the first
    # reading is a spiral. The sibling can turn MELD into UNDER-ARMED and can
    # do nothing else — see its docstring for the direction of doubt.
    return spiral_findings.sharpen(view, len(answered), dispatches.SPIRAL_BLOCK_ROUNDS,
                                   prescription, evidence)


def _sender_strings(now=None, hours=SPIRAL_WINDOW_H, snap=None):
    """Every sender string the ledger actually recorded in the window.

    The spiral gate keys on this rather than trusting that a seat's resolved
    display name is the string it writes under — they diverge, silently, and
    the divergence exempts the seat instead of failing it.

    `snap` is the caller's ALREADY-READ state. `review_spiral` reads the
    ledger and then called this, which read it AGAIN — two ~190ms folds of
    the same 1,577-event file, both on the Stop path, for one answer. The
    parameter is optional so every other caller is unchanged."""
    if isinstance(snap, dict):
        rows = snap
    else:
        try:
            rows, _err = dispatches.snapshot()
        except Exception:
            return set()
    # snapshot() returns a DICT KEYED BY ID, not a list. Iterating it directly
    # walks the id STRINGS, every isinstance(row, dict) is False, and the set
    # comes back empty — which made this helper report EVERY seat as unmatched
    # on its first cut, reproducing the exact blindness it exists to remove.
    out = set()
    for r in (rows or {}).values():
        if isinstance(r, dict) and r.get("sender"):
            out.add(str(r["sender"]).casefold())
    return out


def _spiral_prescription(bucket, current):
    """Choose from typed observations on this already-folded chain, not prose.

    Fan-out is one round, never a sum of possibly overlapping findings. Every
    reviewer must agree; absent/contradictory observations buy no exemption.
    The canonical parent walk permits intervening build rows and lane renames.
    """
    counts, previous = [], None
    ordered = sorted(bucket["tips"], key=bucket["tips"].get)
    if len(set(bucket["tips"].values())) != len(ordered):
        return "MELD", "finding trajectory UNKNOWN: ambiguous round order"
    for tip in ordered:
        rows = bucket["observations"][tip]
        count, polarity = rows[0].get("finding_count"), rows[0].get("polarity")
        for row in rows:
            if row.get("status") != "verdict" or row.get("reviewed_tip") != tip \
                    or count is None or dispatches._finding_error(
                        row.get("finding_count"), row.get("prior_relation")) \
                    or row.get("finding_count") != count \
                    or row.get("polarity") != polarity \
                    or polarity not in ("fix", "concur"):
                return "MELD", "finding trajectory UNKNOWN: missing, malformed or conflicting verdict observations"
            if previous is not None and not any(
                    row.get("chain_root") not in (None, dispatches.CHAIN_UNKNOWN)
                    and row.get("chain_root") == parent.get("chain_root")
                    and row.get("repo_id")
                    and row.get("repo_id") == parent.get("repo_id")
                    and dispatches._chain_reaches(row, parent["id"], current,
                                       review_predecessor_tip=parent["tip"])[0] is True
                    for parent in previous):
                return "MELD", "finding trajectory UNKNOWN: prior review is not proven on the supersedes chain"
        counts.append(count)
        previous = rows
    slope = " -> ".join(map(str, counts))
    if not all(a > b for a, b in zip(counts, counts[1:])):
        return "MELD", "findings %s are not strictly falling" % slope
    relations = [row.get("prior_relation") for row in previous]
    if not counts[-1] or any(r not in dispatches.PRIOR_RELATIONS for r in relations) \
            or len(set(relations)) != 1:
        return "MELD", "findings %s; newest all-findings prior-cure relation UNKNOWN (zero is not a regression)" % slope
    if relations[0] != "regression-of-cure":
        return "MELD", "findings %s; newest findings are %s, not regressions of the prior cure" % (slope, relations[0])
    return "FINISH", "convergence: findings %s; all newest findings are regressions of the prior cure" % slope


def _spiral_fold(current, cutoff, want):
    """(chains, observations, settled, live_chains) — the ONE fold both
    `review_spiral` and the dispatch door read (`chain_rounds`), so the two
    cannot disagree about how many rounds a chain has. `chains` holds only
    `want`'s rows; the other three are facts about the WORK, sender-blind."""
    import calendar
    import time
    chains = {}                 # chain id (or "lane:<name>" for legacy) -> rounds
    observations = {}           # verdicts are work facts, across all senders
    settled = {}                # lane -> newest terminal decision's ORDER
    open_rows = {}              # chain -> its OPEN rows; carriage judged below
    # THE ROUND ORDER IS THE LEDGER'S APPEND ORDER. The snapshot is keyed by
    # id in first-appearance order, which is the order the dispatch rows were
    # appended; a row's `ts` has one-second resolution, so two sends in one
    # second tied and each reader broke the tie its own way (the door
    # synthesized "now + 1" for its unsent row; the rung saw two equal
    # stamps and counted both). The position is the one key both share.
    for order, r in enumerate((current or {}).values()):
        # `kind == "review"` EXPLICITLY. UNKNOWN is a value, not a default (the
        # law `mix` was built on): a row that predates the field is not a review
        # round, it is a row we cannot classify, and inventing rounds out of it
        # would put a made-up number behind a hard block.
        if r.get("kind") != "review":
            continue
        # THE SENDER FILTER USED TO SIT HERE, above `settled`, and that one line
        # of placement was the whole bug — see where it moved to, below.
        if r.get("status") == "cancelled":
            continue
        lane = str(r.get("lane") or "")
        tip = str(r.get("tip") or "")
        peer = str(r.get("recipient") or "")
        if not lane or not tip or not peer:
            continue      # nothing to name in the cure command -> not a finding
        try:
            when = calendar.timegm(time.strptime(str(r.get("ts") or ""),
                                                 "%Y-%m-%dT%H:%M:%SZ"))
        except (ValueError, TypeError):
            continue      # an unparseable stamp is not evidence of age
        if when < cutoff:
            continue
        chain = r.get("chain_root")     # already replayed; see `_replay_chain`
        if chain == dispatches.CHAIN_UNKNOWN:
            continue      # a corrupt chain is not evidence of a round, and it
                          # must not be merged into a real one either
        # A LEGACY row has no chain, and inventing one would be the lane-name
        # heuristic all over again. It keeps EXACTLY today's behaviour — keyed by
        # its lane, in a namespace no chain id can collide with — so history
        # neither loses its rounds nor contaminates a chained one.
        pol = str(r.get("polarity") or "").casefold()
        if pol in dispatches.SPIRAL_TERMINAL_POLARITIES or dispatches._reader_clean(r):
            # Newest decision per LANE, tracked across every bucket. See the
            # spent-prefix note under the reduce below for why the lane, and not
            # the chain, is the right key for this one fact. Keyed by the lane
            # STEM (#142): a decision recorded under either spelling of the
            # family settles both.
            if order > settled.get(dispatches._lane_stem(lane), -1):
                settled[dispatches._lane_stem(lane)] = order
        # TWO QUESTIONS, TWO SCOPES — and one filter used to answer both.
        # Counting ROUNDS is PER-SENDER: the guard bills the seat that is
        # stopping, and a seat is never gated for someone else's spiral. So the
        # filter belongs HERE, gating the chain bookkeeping below and nothing
        # above it. Recognising a DECISION is SENDER-BLIND: a verdict is a fact
        # about the WORK, not about who dispatched the round that carried it.
        #
        # With the filter above `settled`, a verdict only counted if the seat
        # being billed had dispatched it — so HANDING A LANE ON, the healthy
        # move, froze your own round count at its high-water mark forever:
        # nothing you dispatched could ever close it again. Live cost: three FIX
        # rounds from the integrator, gemini takes the lane over and APPROVEs
        # at 07:25, and the guard blocked a seat whose lane had been finished
        # for an hour. Rows with no recorded sender still settle, which is the
        # fail-open direction here — an unattributable approve is still an
        # approve, and reading it SUPPRESSES a warning rather than raising one.
        key = chain or ("lane:" + dispatches._lane_stem(lane))
        # AN OPEN ROW IS THE ONE UNAMBIGUOUS SIGN A CONVERSATION IS STILL RUNNING,
        # and like `settled` it is SENDER-BLIND: somebody is waiting on a verdict
        # in this chain whoever dispatched the round that asked for it. Read here,
        # above the sender filter, for the same reason `settled` is — a fact about
        # the WORK belongs to the work. Carriage is judged after the loop.
        if r.get("status") == "open":
            open_rows.setdefault(key, []).append(r)
        observations.setdefault(key, {}).setdefault(tip, []).append(r)
        if str(r.get("sender") or "").casefold() != want:
            continue
        b = chains.setdefault(key, {"tips": {}, "ts": {}, "last": None,
                                    "last_order": -1, "who": set(),
                                    "observations": observations[key]})
        b["tips"].setdefault(tip, order)
        b["ts"].setdefault(tip, when)
        b["who"].add(peer)
        if order > b["last_order"]:
            b["last_order"] = order
            b["last"] = (when, peer, lane,
                         str(r.get("polarity") or "").casefold())
            # THE TIP AND ITS REPOSITORY TRAVEL TOGETHER OR NEITHER IS USABLE.
            # A sha is only an identity inside the repository that holds it, and
            # this ledger is global; asking the wrong repo about a real sha is
            # how a probe answers truthfully about something else entirely.
            b["last_tip"] = tip
            b["repo_id"] = r.get("repo_id")
    # AN OPEN ROW A SUCCESSOR CARRIES IS NOBODY WAITING (task/3086). A round
    # replaced by `--supersedes` before anyone answered stays OPEN forever, and
    # `status == open` read it as a live conversation, so a landed chain kept
    # prescribing a MELD over rows a FIX or a source-clean HOLD carried.
    # `carrier` walks the successor SET, so a dead successor leaves the row
    # owed. The index costs ~100 ms on the live ledger and this runs per seat,
    # so it is built only when a chain at the rung's threshold has an open row.
    spiralling = [k for k, b in chains.items()
                  if k in open_rows and len(b["tips"]) >= dispatches.SPIRAL_MELD_ROUNDS]
    live_chains = set()
    if spiralling:
        index = dispatches._successor_index(current)
        cycles = dispatches._cycle_components(index)
        live_chains = {k for k in spiralling
                       if any(dispatches.carrier(r, current, index, cycles) is None
                              for r in open_rows[k])}
    return chains, observations, settled, live_chains


def review_spiral(sender, hours=SPIRAL_WINDOW_H, now=None, snap=None):
    """(info | None, err) — the worst eligible review chain `sender` is running.
    Blocking MELD chains outrank FINISH advisories; within that tier choose
    the most DISTINCT tips inside the window, then the newest round.
    Nothing below SPIRAL_MELD_ROUNDS is reported.

    info = {chain, lane, rounds, peer, recipients, span_h, since_h,
            prescription, finding_evidence}. The last two are typed-observation
    advice, never approval authority. `span_h` is first tip to newest tip;
    `since_h` is first tip to `now`, the window "did this seat meld DURING
    this spiral" asks about, so it does not shrink as the spiral ages. `peer` is the
    recipient of the most recent round — the seat you are ping-ponging with, and
    so the seat to invite into the meld. `lane` is the MOST RECENT round's
    label, because that is what the seat currently calls this work.

    THE CHAIN, NOT THE LANE STRING. Grouping by lane was wrong in BOTH
    directions at once, which is why neither half could be patched alone:

      OVERCOUNT — two unrelated pieces of work reusing one lane name merged into
      a single fake spiral, and a finished spiral kept counting because the name
      stayed in the window.
      UNDERCOUNT — the real incident. `gate-mints-its-own-evidence` landed and
      `gate-epoch-is-append-order` opened immediately to close a hole in it:
      round 10 of the same work under a new name, and every same-lane rule read
      it as round 1.

    Chain keying fixes both from one relation: a renamed continuation carries
    its parent's root and keeps counting; a reused name roots a new chain and
    starts over.

    ONE CHAIN, NOT ALL OF THEM: two chains at two rounds each is a healthy
    night, and a rung that listed every one would be the wall this prevents.

    SAME SENDER: the guard bills the seat that is stopping, and a seat is never
    gated for someone else's spiral. Rows whose sender was never recorded
    (every row written before `add()` learned to stamp it) match no seat and are
    silently invisible here — fail-open, by construction.

    FAIL-OPEN: an unreadable ledger returns an err and NO finding. Absence
    unproven is never absence, and the caller must not block on the err.

    `snap` is the stop ladder's one `(state, unavailable)` observation; ordinary
    callers omit it and retain an independent current read.
    """
    import calendar
    import time
    if not sender:
        return None, None
    now = time.time() if now is None else now
    cutoff = now - hours * 3600
    current, unavailable = dispatches.snapshot() if snap is None else snap
    if unavailable:
        return None, ("dispatch ledger unavailable (%s) — review rounds "
                      "UNKNOWN, not zero" % unavailable)
    want = str(sender).casefold()
    # THE KEY WAS THE BUG, NOT THE PREDICATE. This matched the caller's
    # RESOLVED SEAT NAME against the ledger's recorded sender, and those are
    # not the same string for every seat. Measured on the live ledger
    # 2026-08-01, nine distinct senders: `helm-claude-2` authors 17 rows under
    # its own name and IS seen; `helm-claude` authors as bare `claude` (the
    # family floor) and was structurally invisible — TEN ROUNDS on one chain,
    # all night, zero detections. Same code, same rung, opposite outcomes,
    # decided entirely by which string got recorded. A seat in a ten-round
    # spiral looked identical to a seat with no rows at all, and NOTHING made
    # that visible, which is the property a guard may never have.
    seen = dispatches._sender_strings(now=now, hours=hours, snap=current)
    if want not in seen:
        # NOT ZERO ROUNDS — UNMATCHED. The distinction is the whole fix: this
        # seat may be running any number of rounds under a name this query
        # cannot reach, and saying so is the difference between a quiet gate
        # and a blind one.
        return None, ("no dispatch row is authored by %r — this seat writes "
                      "under a different name than it resolves to, so its "
                      "round count is UNKNOWN, not zero (ledger authors: %s)"
                      % (str(sender), ", ".join(sorted(seen)[:8]) or "none"))
    chains, observations, settled, live_chains = dispatches._spiral_fold(
        current, cutoff, want)
    best = None
    for key in sorted(chains):
        b = chains[key]
        # A SPENT PREFIX. Terminality alone was not enough, and the live ledger
        # is what said so: `land-pipeline-card` is ONE seven-round conversation
        # split across TWO buckets, because its first four rounds predate
        # `chain_root` and land in the legacy `lane:` bucket while the last three
        # carry a real chain. The approve arrives on the chained half, so the
        # legacy half's last round is FOREVER a `fix` — a fragment frozen one
        # step before the ending that already happened. It cannot terminate by
        # its own rows no matter how long you wait, so the terminality check
        # above can never reach it. That fragment is what was still firing after
        # the first fix, and it is why this needed a second one.
        #
        # THE LANE IS THE RIGHT KEY FOR THIS ONE FACT, AND ONLY THIS ONE. The
        # file's standing warning is that a REUSED lane name merges unrelated
        # work — so this never merges counts, and never lets one bucket's rounds
        # raise another's. It asks a strictly weaker question: has this lane been
        # decided SINCE this bucket's last round? Ordering is what makes that
        # safe. Work under a recycled name is NEWER than the old approve, so it
        # is untouched; only rounds that precede a decision are suppressed, and
        # rounds that precede a decision are history by definition.
        if b["last_order"] <= settled.get(dispatches._lane_stem(b["last"][2]), -1):
            continue
        # THE COUNTED ROUNDS ARE A VIEW, NOT A SECOND FOLD: the same tips minus
        # the reviewer's own adopted patch tips (task/3072) and the tips nobody
        # answered (task/2682). The prescription and its sharpener read this
        # view so the trajectory they judge is the one the count reports;
        # `span_h`/`since_h` keep every tip, because the question they answer
        # is when this conversation began.
        counted, unread = dispatches._round_view(b)
        view = dict(b, tips=counted)
        rounds = len(counted)
        if rounds < dispatches.SPIRAL_MELD_ROUNDS \
                and rounds + unread < dispatches.SPIRAL_BLOCK_ROUNDS:
            continue
        if dispatches._spiral_conversation_is_over(key, b, live_chains):
            continue
        # UNREAD: enough dispatches for a block, too few answers for a round.
        # Said as an advisory, never a meld: there is nothing to converge.
        unread_only = rounds < dispatches.SPIRAL_MELD_ROUNDS
        if unread_only:
            prescription = dispatches.SPIRAL_UNREAD
            evidence = dispatches._unread_evidence(rounds + unread,
                                        sum(dispatches._answered(b["observations"].get(t))
                                            for t in counted))
            rounds += unread
        else:
            prescription, evidence = dispatches._answered_reading(view, current)
            if unread:
                evidence += ("; %d dispatch(es) nobody answered are not "
                             "counted" % unread)
        # A BLOCKING READING OUTRANKS AN ADVISORY ONE, whatever the rounds: a
        # five-dispatch UNREAD chain or a four-round FINISH must not hide a
        # three-round MELD or UNDER-ARMED chain, which is the one the stop
        # rung walls. Within a tier, round/time ordering stands.
        rank = (rounds >= dispatches.SPIRAL_BLOCK_ROUNDS
                and prescription not in dispatches.SPIRAL_ADVISORY_PRESCRIPTIONS,
                rounds, b["last_order"])
        if best is None or rank > best[0]:
            best = (rank, {"chain": key, "lane": b["last"][2], "rounds": rounds,
                           "peer": b["last"][1],
                           "recipients": sorted(b["who"]),
                           "prescription": prescription,
                           "finding_evidence": evidence,
                           "tips": sorted(set(b["tips"])
                                          | dispatches._patch_tips_of(b["observations"])),
                           "span_h": (b["last"][0] - min(b["ts"].values()))
                           / 3600.0,
                           "since_h": max(0.0, now - min(b["ts"].values()))
                           / 3600.0})
    return (best[1], None) if best is not None else (None, None)


def chain_rounds(sender, parent_id, tip, snap=None, now=None,
                 hours=SPIRAL_WINDOW_H):
    """What the review chain `parent_id` belongs to reads as, with `tip` sent
    next by `sender` — the dispatch door's view, from the rung's own fold.

    -> (info, err). info = {chain, lane, parent, rounds_before, rounds_after,
    new_round, adopted_patch, fan_out, unread, answered_rounds, prescription,
    evidence, verdicts, melded}. `rounds_after` counts the way the rung will
    once this row exists: the reviewer's adopted patch tip and a tip already
    on the chain (fan-out) add no round, and a tip nobody answered drops out
    once this newer one replaces it. `prescription` reads the ANSWERED rounds
    only, which is the trajectory the door can know before the next read.
    `verdicts` are the chain's answered rows, newest first; `melded` names
    the newest AGREED meld recorded on the chain, if any.

    FAIL-OPEN, like the rung: an unreadable ledger or an unresolvable parent
    returns an err and no info, and the door never blocks on it."""
    import time
    now = time.time() if now is None else now
    current, unavailable = dispatches.snapshot() if snap is None else snap
    if unavailable:
        return None, "dispatch ledger unavailable (%s)" % unavailable
    parent, err = dispatches._resolve_row(current or {}, parent_id)
    if err:
        return None, err
    want = str(sender or "").casefold()
    chains, observations, _settled, _live = dispatches._spiral_fold(
        current, now - hours * 3600, want)
    key = parent.get("chain_root") or (
        "lane:" + dispatches._lane_stem(str(parent.get("lane") or "")))
    if key == dispatches.CHAIN_UNKNOWN:
        return None, "the parent's chain is UNKNOWN"
    obs = observations.get(key, {})
    b = chains.get(key) or {"tips": {}, "ts": {}, "observations": obs,
                            "who": set(), "last": None, "last_order": -1}
    tip = str(tip or "").lower()
    before, _unread_before = dispatches._round_view(b)
    fan_out = tip in b["tips"]
    pending = None if fan_out else {
        "tip": tip, "supersedes": parent["id"], "sender": sender,
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
        "chain_root": parent.get("chain_root"), "repo_id": parent.get("repo_id"),
        "repo_root": parent.get("repo_root")}
    if pending and dispatches._has_diff_handoff(parent):
        proof = dispatches._diff_application(pending, parent, current)
        if proof:
            pending["diff_application"] = proof
    adopted = tip in dispatches._adopted_patch_tips(obs, pending)
    after_tips = dict(b["tips"])
    if not fan_out:
        # THE UNSENT ROW WILL BE APPENDED LAST, so it takes the order after
        # every persisted row — a position, never a synthesized timestamp.
        after_tips[tip] = len(current or {})
    after, unread = dispatches._round_view(dict(b, tips=after_tips), pending)
    answered = {t: w for t, w in before.items() if dispatches._answered(obs.get(t))}
    prescription, evidence = dispatches._answered_reading(dict(b, tips=before), current)
    rows = [r for t in answered for r in obs.get(t, ())
            if r.get("status") == "verdict" or dispatches._reader_clean(r)]
    rows.sort(key=lambda r: str(r.get("verdict_ts") or r.get("hold_ts")
                                or r.get("ts") or ""), reverse=True)
    agreed = [r for rs in obs.values() for r in rs
              if r.get("meld_outcome") == "agreed"]
    agreed.sort(key=lambda r: str(r.get("verdict_ts") or r.get("hold_ts")
                                  or ""), reverse=True)
    return {"chain": key, "lane": str(parent.get("lane") or ""),
            "parent": parent, "rounds_before": len(before),
            "rounds_after": len(after), "new_round": not (adopted or fan_out),
            "adopted_patch": adopted, "fan_out": fan_out, "unread": unread,
            "answered_rounds": len(answered), "prescription": prescription,
            "evidence": evidence, "verdicts": rows,
            "tips": sorted(set(after_tips) | dispatches._patch_tips_of(obs)),
            "melded": agreed[0].get("meld_room") if agreed else None}, None


def _spiral_conversation_is_over(key, bucket, live_chains):
    """Did this chain's work SHIP, with nobody still waiting on a verdict?

    THE SECOND SETTLEDNESS SOURCE, and the rung needed one because polarity was
    its only one. A chain whose last decision is not in
    SPIRAL_TERMINAL_POLARITIES can never settle by the rule above, no matter
    what happened to its code — and a whole landing door produces exactly that
    state, so the gate kept firing on a conversation that had ended and
    prescribed a LIVE MELD, which is a conversation, about work already on
    trunk. The remedy names the failure: you cannot converge with anybody about
    a lane that shipped.

    A LANDING IS A STRONGER SETTLEDNESS SIGNAL THAN ANY VERDICT, which is why
    this belongs beside the polarity rule rather than inside it. It also keeps
    the two questions apart: SPIRAL_TERMINAL_POLARITIES answers "may this
    land", this answers "is anyone still arguing", and folding the second into
    the first is what would have put an endorsement into an authorization set.

    TWO CLAUSES, BOTH REQUIRED. An OPEN row no successor carries means
    somebody is waiting on a verdict right now, and no amount of landed history makes that untrue — a
    chain can ship one tip and immediately open the next round on the next.

    ANCESTRY ALONE CANNOT ANSWER THIS AND USING IT WOULD FAIL ON THE EXACT CASE
    THIS EXISTS FOR. `_tip_on_trunk` in this file is ancestry-only, and the
    incident that produced this clause was a chain landed by CHERRY-PICK: its
    content is on trunk under three other shas and ancestry truthfully says no.
    So this asks `landed_ever`, the named door for "reachable OR
    patch-present", and says which fact it means as that door's own contract
    demands of new callers.

    UNKNOWN NEVER SUPPRESSES. This clause only ever ADDS suppression, so
    failing to measure it leaves the rung exactly as it was — no regression,
    and no way to disarm a live spiral by breaking git. That is the opposite
    choice from `settled` above and deliberately so: an unreadable verdict
    could only hide a warning, while an unreadable landing would hide a BLOCK.
    """
    if key in live_chains:
        return False
    tip, repo_id = bucket.get("last_tip"), bucket.get("repo_id")
    if not dispatches._FULL_TIP.fullmatch(str(tip or "")) or not repo_id:
        return False
    try:
        from . import landreq
        # THE GITDIR, PASSED THROUGH UNCHANGED, AND THE FIRST CUT STRIPPED IT.
        # `_tip_on_trunk` two screens up peels "/.git" off with the comment
        # "ancestry wants the repo root", which is TRUE OF THAT CALL and false
        # of this one: landed_ever takes a gitdir. Copying the neighbouring
        # call site's argument convention along with its value made this answer
        # UNKNOWN for every row ever — measured against the live ledger, on the
        # exact chain this clause was written for.
        return landreq.landed_ever(str(repo_id), tip, "origin/main") is True
    except Exception:
        # A rung that cannot look says nothing, and NEVER takes the guard down:
        # this runs inside the Stop hook.
        return False


# ---------------------------------------------------------------------------
# PUBLISH BACK ONTO THE LEDGER
# ---------------------------------------------------------------------------
def owned():
    """The names this module owns, read from the LEDGER'S declaration.

    ONE SOURCE OF TRUTH. `dispatches._OWNER_NAMES` is both the tuple the
    retired-name rung reads and the tuple this publish loop walks, so a name
    added here without a declaration is not silently bound, and a declared
    name this module does not define fails loudly at publish rather than
    quietly at a caller.
    """
    token = __name__.rsplit(".", 1)[-1]
    for module, names in dispatches._OWNER_NAMES:
        if module == token:
            return names
    return ()


def _publish():
    """Bind the declared names onto the ledger module. Once, at import."""
    for _name in owned():
        setattr(dispatches, _name, globals()[_name])


_publish()
