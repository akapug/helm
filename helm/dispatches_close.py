"""`helm.dispatches_close` — WHAT PROOF ENDS A ROW, and what it refuses without.

ONE QUESTION, NOT "THE REST". Every name here answers the same one: a dispatch
row is about to stop being owed — discharged, withdrawn, abandoned, retired,
closed, or landed — and something must be TRUE before the ledger writes that
down. The six `_record_*_proven` writers are the locked doors, and everything
else in this file is the evidence ladder they consult: which delivery
declaration binds, which content-proof pair binds, which witness side, which
build-landed event, which contradiction may discharge, which proof mode one
reason may ever record, and — in `_close_event_error`, the largest single
answer in the ledger — why one `close` event may not bind to the state it
claims. The ledger's OTHER questions stayed behind: what a row IS, who may
receive one, what a verdict means, what a chain says, what a tip proves.

MEASURED AT THE CUT, THE REMAINDER REACHES IN THROUGH ONE FUNCTION. `_apply`
reads `_close_event_error` and `_delivered_report_event_error` on replay, and
nothing else in `dispatches` names anything in this file. That single in-edge
is what makes this the honest side to move: it is a leaf CONSUMER of the
ledger's own API, 22 definitions deep, and the file it left had 121,796 bytes
of headroom under the never-track ceiling while it was the hottest file in the
tree — nearly every cure lands an arm in it, and a file that crosses refuses
EVERY commit that touches it, not just the one that pushed it over.

THE CODE MOVED WHOLE, AND EVERY LEDGER NAME IS SPELLED `dispatches.NAME` AT
ITS CALL SITE — 266 reads of 81 distinct names. An import list, or a bare
global left behind, would bind each object ONCE at import, so an arm that
patches an attribute on `dispatches` and then drives a close would reach the
ORIGINAL here and measure nothing, while every structural guard stayed green:
how a name resolves is a property of the RUNTIME and those guards read TEXT.
The module spelling keeps the lookup at CALL TIME, exactly as a bare global
did inside the ledger. Names this file OWNS are spelled that way too, because
they are published back onto `dispatches` and arms patch them THERE.

WHICH NAMES THOSE ARE WAS RESOLVED BY `symtable`, NEVER BY MATCHING TEXT. A
local may share a top-level name, and the owner set was collected from EVERY
top-level binding form — tuple unpacking included, which is the one the first
cut of the `landreq` split missed: nine constants stayed bare, both modules
parsed, both import orders worked, every name published, and it surfaced only
as `NameError` when arms drove the code.

THE CYCLE IS BROKEN THE WAY THIS PACKAGE ALREADY BREAKS IT: this module
imports the ledger EAGERLY, and `dispatches` imports this one at the END of
its own module body, after every name it needs exists. A module object is in
`sys.modules` from the first line of its execution, so either import order
resolves.
"""
import os

from . import dispatches
from . import eventledger, foldckpt, pk
from .verdicts import WORK_POLARITIES as _WORK_POLARITIES


def _record_discharge_proven(rid, reviewed_tip, superseding_tip,
                             superseding_id, evidence, contrary_state,
                             contrary_target):
    """Append a Git-proven contrary discharge without rewriting its verdict.

    landreq owns the live Git + approved-successor proof. This ledger boundary
    revalidates immutable row identity under lock, then records exactly one
    post-verdict annotation. Identical retries are idempotent; conflicts cannot
    rewrite which round discharged the debt.
    """
    reviewed = str(reviewed_tip or "").strip().lower()
    superseding = str(superseding_tip or "").strip().lower()
    superseding_id = str(superseding_id or "").strip()
    if not dispatches._FULL_TIP.fullmatch(reviewed):
        return None, "discharge needs the original full reviewed commit id"
    if not dispatches._FULL_TIP.fullmatch(superseding):
        return None, "discharge needs a full 40- or 64-character superseding tip"
    if superseding == reviewed:
        return None, "a verdict tip cannot discharge itself"
    if not dispatches._ID.fullmatch(superseding_id):
        return None, "discharge needs the approved superseding dispatch id"
    evidence, err = dispatches._clean(evidence, "discharge evidence", 256)
    if err:
        return None, err
    if contrary_state not in ("landed", "merged-local"):
        return None, "discharge needs the observed contrary land state"
    if contrary_target not in ("local", "upstream"):
        return None, "discharge needs the observed contrary trunk target"
    path = dispatches.ledger_path()

    def attempt(txn):
        if not txn.held:
            return None, "ledger unwritable (%s) — discharge NOT recorded" % path
        current, unavailable = dispatches.snapshot()
        if unavailable:
            return None, "dispatch ledger unavailable: %s" % unavailable
        row, err = dispatches._resolve_row(current, rid)
        if err:
            return None, err
        if row.get("discharged"):
            same = row.get("reviewed_tip") == reviewed \
                and row.get("superseding_tip") == superseding \
                and row.get("superseding_id") == superseding_id \
                and row.get("discharge_ref") == evidence \
                and row.get("discharge_contrary") == contrary_state \
                and row.get("discharge_target") == contrary_target
            if same:
                return row, None
            return None, "dispatch %s already has a different discharge" % row["id"]
        if row.get("abandoned"):
            return None, ("dispatch %s is already ABANDONED with land state "
                          "UNKNOWN" % row["id"])
        if row.get("withdrawn"):
            return None, ("dispatch %s is already withdrawn — a debt is retired "
                          "once, by land (discharge) or by abandonment "
                          "(withdraw), never both" % row["id"])
        if row.get("close_reason"):
            return None, ("dispatch %s is already retired by close --reason %s; "
                          "a row is retired once — refusing a different closure"
                          % (row["id"], row["close_reason"]))
        if row.get("status") != "verdict" \
                or row.get("polarity") not in ("fix", "supersede"):
            return None, "dispatch %s is not a FIX/SUPERSEDE verdict" % row["id"]
        if row.get("reviewed_tip") != reviewed:
            return None, "discharge reviewed tip does not match dispatch %s" % row["id"]
        event = {"v": 3, "event": "discharge", "seq": row["seq"] + 1,
                 "id": row["id"], "ts": pk.now_ts(),
                 "reviewed_tip": reviewed,
                 "superseding_tip": superseding,
                 "superseding_id": superseding_id,
                 "discharge_ref": evidence,
                 "contrary_state": contrary_state,
                 "contrary_target": contrary_target}
        if not txn.append(event):
            return None, "ledger unwritable (%s) — discharge NOT recorded" % path

        def finish():
            out = dispatches._apply(row, event)
            pk.event("dispatch-discharge", row["id"], superseding)
            return out, None
        return txn.then(finish)
    return dispatches._ledger_write(attempt, path)



def _record_withdraw_proven(rid, reviewed_tip, evidence):
    """Append a Git-proven WITHDRAWAL of a do-not-land verdict — the mirror of
    _record_discharge_proven, for the row whose CORRECT resolution is "this is
    never landed at all", so no superseding tip will ever exist to discharge it.

    landreq owns the live Git proof (the reviewed change is provably ABSENT from
    trunk). This ledger boundary revalidates immutable row identity under lock,
    then records exactly one post-verdict annotation. Identical retries are
    idempotent; a conflicting withdraw is refused, never a rewrite. The verdict
    and the withdraw both stay visible — history is preserved, not rewritten."""
    reviewed = str(reviewed_tip or "").strip().lower()
    if not dispatches._FULL_TIP.fullmatch(reviewed):
        return None, "withdraw needs the original full reviewed commit id"
    evidence, err = dispatches._clean(evidence, "withdraw evidence", 256)
    if err:
        return None, err
    path = dispatches.ledger_path()

    def attempt(txn):
        if not txn.held:
            return None, "ledger unwritable (%s) — withdraw NOT recorded" % path
        current, unavailable = dispatches.snapshot()
        if unavailable:
            return None, "dispatch ledger unavailable: %s" % unavailable
        row, err = dispatches._resolve_row(current, rid)
        if err:
            return None, err
        if row.get("withdrawn"):
            same = row.get("reviewed_tip") == reviewed \
                and row.get("withdraw_ref") == evidence
            if same:
                return row, None
            return None, "dispatch %s already has a different withdraw" % row["id"]
        if row.get("abandoned"):
            return None, ("dispatch %s is already ABANDONED with land state "
                          "UNKNOWN" % row["id"])
        if row.get("discharged"):
            return None, ("dispatch %s is already discharged — a row is retired "
                          "once, by land (discharge) or by abandonment (withdraw), "
                          "never both" % row["id"])
        if row.get("close_reason"):
            return None, ("dispatch %s is already retired by close --reason %s; "
                          "a row is retired once — refusing a different closure"
                          % (row["id"], row["close_reason"]))
        if row.get("status") != "verdict" \
                or row.get("polarity") not in ("fix", "supersede"):
            return None, "dispatch %s is not a FIX/SUPERSEDE verdict" % row["id"]
        if row.get("reviewed_tip") != reviewed:
            return None, "withdraw reviewed tip does not match dispatch %s" % row["id"]
        event = {"v": 3, "event": "withdraw", "seq": row["seq"] + 1,
                 "id": row["id"], "ts": pk.now_ts(),
                 "reviewed_tip": reviewed,
                 "withdraw_ref": evidence}
        if not txn.append(event):
            return None, "ledger unwritable (%s) — withdraw NOT recorded" % path

        def finish():
            out = dispatches._apply(row, event)
            pk.event("dispatch-withdraw", row["id"], reviewed)
            return out, None
        return txn.then(finish)
    return dispatches._ledger_write(attempt, path)



def _record_abandon_proven(rid, reviewed_tip, reason, object_exists_probe,
                             interlock_probe):
    """Append an honest terminal when the reviewed commit is MISSING.

    landreq owns the public lifecycle decision and Git interpretation. This
    boundary re-resolves the immutable row on a read the ledger lock then
    proves current (`dispatches._ledger_write`), and invokes one bounded
    exact-object probe under that lock immediately before append. Only
    explicit MISSING (False) authorizes the event; EXISTS and UNKNOWN both
    refuse.
    """
    reviewed = str(reviewed_tip or "").strip().lower()
    if not dispatches._FULL_TIP.fullmatch(reviewed):
        return None, "abandon needs the original full reviewed commit id"
    reason, err = dispatches._clean(reason, "abandon reason", 256)
    if err:
        return None, err
    path = dispatches.ledger_path()

    def attempt(txn):
        if not txn.held:
            return None, "ledger unwritable (%s) — abandon NOT recorded" % path
        current, unavailable = dispatches.snapshot()
        if unavailable:
            return None, "dispatch ledger unavailable: %s" % unavailable
        row, err = dispatches._resolve_row(current, rid)
        if err:
            return None, err
        if row.get("abandoned"):
            if row.get("reviewed_tip") == reviewed \
                    and row.get("abandon_reason") == reason:
                return row, None
            return None, "dispatch %s already has a different abandon" % row["id"]
        terminal = dispatches._close_retired_by(row)
        if terminal:
            return None, "dispatch %s is already retired by %s" % (row["id"], terminal)
        if row.get("kind") != "review":
            return None, ("dispatch %s is not a review row — abandon can never "
                          "close a build dispatch" % row["id"])
        if row.get("status") != "verdict":
            return None, "dispatch %s is not a verdict" % row["id"]
        if row.get("polarity") == "supersede":
            return None, "dispatch %s is already terminal through SUPERSEDE" % row["id"]
        # THE WRITER ADMITS EXACTLY WHAT THE REPLAY ADMITS, AND THAT AGREEMENT
        # IS THE POINT. `_apply`'s abandon arm requires a WORK polarity; this
        # door blocked only `supersede`, so `concur` and historical UNDECLARED
        # passed HERE and were refused THERE. The divergence is worse than
        # either half alone: the ledger grows an abandon event the state
        # machine ignores, and the CLI prints ABANDONED over a row that stayed
        # REVIEWED — an operator told a write happened that did not.
        # (Reproduced at the exact lane tip before the cure: err=None, state
        # REVIEWED, history 2->3.) A blocklist must be widened for every
        # polarity ever added and is silently wrong until someone remembers;
        # this allowlist
        # refuses the next polarity by construction, which is the same shape
        # `_CLOSE_POLARITY` already uses for every close door.
        if row.get("polarity") not in _WORK_POLARITIES:
            return None, ("dispatch %s carries polarity %s, which speaks about "
                          "no landable work — abandon writes off REVIEWED WORK"
                          % (row["id"], row.get("polarity") or "UNDECLARED"))
        if row.get("reviewed_tip") != reviewed:
            return None, "abandon reviewed tip does not match dispatch %s" % row["id"]
        repo_id = str(row.get("repo_id") or "")
        if not os.path.isabs(repo_id) or os.path.realpath(repo_id) != repo_id:
            return None, "dispatch %s has no canonical repository binding" % row["id"]
        # THE MUTATION BOUNDARY STARTS HERE, UNDER THE LOCK: the fold above
        # ran without it, and `lock` proves that read is the ledger now held
        # before the live probes whose answers this event records.
        if not txn.lock():
            return None, "ledger unwritable (%s) — abandon NOT recorded" % path
        try:
            exists = object_exists_probe(repo_id, reviewed) \
                if callable(object_exists_probe) else None
        except Exception:                       # noqa: BLE001 — proof UNKNOWN
            exists = None
        if exists is True:
            return None, "reviewed commit exists at the mutation boundary"
        if exists is not False:
            return None, "reviewed commit state is UNKNOWN at the mutation boundary"
        try:
            interlocks = interlock_probe(repo_id, row.get("lane")) \
                if callable(interlock_probe) else None
        except Exception:                       # noqa: BLE001 — proof UNKNOWN
            interlocks = None
        if not isinstance(interlocks, dict):
            return None, "abandon interlock state is UNKNOWN at the mutation boundary"
        mention = interlocks.get("mention")
        if not isinstance(mention, dict) or mention.get("state") == "unknown":
            return None, "trunk mention state is UNKNOWN at the mutation boundary"
        if mention.get("state") in ("structured", "ambiguous", "tag"):
            evidence = mention.get("line") or mention.get("tag") \
                or mention.get("state")
            return None, ("trunk mention blocks abandon at the mutation boundary: "
                          "%s" % evidence)
        if mention.get("state") != "none":
            return None, "trunk mention state is UNKNOWN at the mutation boundary"
        if interlocks.get("branch_state") == "unlanded":
            return None, "unlanded lane branch blocks abandon at the mutation boundary"
        if interlocks.get("worktree_state") == "dirty":
            return None, "dirty worktree blocks abandon at the mutation boundary"
        if interlocks.get("branch_state") not in ("none", "merged") \
                or interlocks.get("worktree_state") not in ("none", "clean"):
            return None, "lane work state is UNKNOWN at the mutation boundary"
        event = {"v": 3, "event": "abandon", "seq": row["seq"] + 1,
                 "id": row["id"], "ts": pk.now_ts(),
                 "reviewed_tip": reviewed, "repo_id": repo_id,
                 "reason": reason, "object_state": "missing",
                 "object_proof_mode": "cat-file-batch-check",
                 "object_proof_version": 1,
                 "trunk_mention_state": "none",
                 "trunk_mention_proof_mode":
                 "structured-message-and-tag-scan",
                 "trunk_mention_proof_version": 2,
                 "branch_state": interlocks["branch_state"],
                 "branch_proof_mode": "git-ref-and-ancestry",
                 "branch_proof_version": 1,
                 "worktree_state": interlocks["worktree_state"],
                 "worktree_proof_mode": "git-worktree-status",
                 "worktree_proof_version": 1,
                 "land_state": "UNKNOWN"}
        if not txn.append(event):
            return None, "ledger unwritable (%s) — abandon NOT recorded" % path

        def finish():
            out = dispatches._apply(row, event)
            pk.event("dispatch-abandon", row["id"], reviewed)
            return out, None
        return txn.then(finish)
    return dispatches._ledger_write(attempt, path)



def _record_retire_proven(rid, reason, seat, note, measure):
    """Append ONE administrative retirement, re-measured under the lock.

    `measure` is landreq's probe for THIS reason: it is called with the
    freshly re-resolved raw row AND the snapshot it came from, which the lock
    has just proven is the ledger being held (`dispatches._ledger_write`: the
    fold ran without the lock, the probe runs under it), and answers
    (measurement, refusal). The snapshot rides along because a succession
    probe must resolve its carrier against the ledger being held, not
    against a read that no longer describes it. The
    caller's claim is never recorded — only what the probe says HERE, inside
    the lock, immediately before the append. That is the abandon boundary's
    law and it is the whole reason this verb can be trusted: the reachability
    it writes down is the reachability that existed at the instant of the
    write, not the one the operator saw minutes earlier on a listing.

    REFUSAL IS THE MEASURED CONTRADICTION, never absence: a probe that finds
    the proof chain REACHABLE refuses, and a probe that cannot measure at all
    also refuses (it returns its own UNKNOWN sentence). Retirement is only
    ever authorized by a positive finding of permanent unreachability.
    """
    if reason not in dispatches.RETIRE_REASONS:
        return None, ("retire --reason must be one of %s"
                      % "|".join(dispatches.RETIRE_REASONS))
    seat, err = dispatches._clean(seat, "retire seat", 64)
    if err:
        return None, err
    if not seat or not dispatches._TOKEN.fullmatch(seat):
        return None, ("retire needs the acting seat's exact token — an "
                      "administrative terminal with no actor on it is an "
                      "unauditable write")
    if note is not None:
        note, err = dispatches._clean(note, "retire note", dispatches._RETIRE_NOTE_CAP)
        if err:
            return None, err
    if not callable(measure):
        return None, ("retire has no measurement probe — refusing to record "
                      "an unreachability nobody measured")
    path = dispatches.ledger_path()

    def attempt(txn):
        if not txn.held:
            return None, "ledger unwritable (%s) — retire NOT recorded" % path
        current, unavailable = dispatches.retire_read()
        if unavailable:
            return None, "dispatch ledger unavailable: %s" % unavailable
        row, err = dispatches._resolve_row(current, rid, allow_retired=True)
        if err:
            return None, err
        if row.get("retired_admin"):
            # IDENTITY IS THE REASON ALONE. The measurement text is a reading
            # of a live world and a second run legitimately words it
            # differently; refusing a retry over changed prose would make the
            # verb non-idempotent for no gain, while a retry under a
            # DIFFERENT reason is a different claim and must refuse.
            if row.get("retire_reason") == reason:
                return row, None
            return None, ("dispatch %s is already retired under --reason %s"
                          % (row["id"], row["retire_reason"]))
        terminal = dispatches._close_retired_by(row)
        if terminal:
            return None, ("dispatch %s is already retired by %s — retirement "
                          "is for rows nothing else could close"
                          % (row["id"], terminal))
        if row.get("status") not in dispatches._RETIRABLE_STATUSES:
            return None, ("dispatch %s is %s, which bills nothing — retire "
                          "clears a LIVE obligation"
                          % (row["id"], row.get("status") or "in an unknown state"))
        # THE MEASUREMENT RUNS UNDER THE LOCK, against the snapshot `lock`
        # proves is the ledger held: the fold above ran without it.
        if not txn.lock():
            return None, "ledger unwritable (%s) — retire NOT recorded" % path
        try:
            measurement, refusal = measure(row, current)
        except Exception as ex:             # noqa: BLE001 — fail closed
            return None, ("the retire measurement raised at the mutation "
                          "boundary (%s) — nothing recorded"
                          % ex.__class__.__name__)
        if refusal:
            return None, refusal
        measurement, err = dispatches._clean(measurement, "retire measurement",
                                  dispatches._RETIRE_MEASUREMENT_CAP)
        if err:
            return None, err
        if not measurement:
            return None, ("the retire measurement produced no text at the "
                          "mutation boundary — a terminal whose proof cannot "
                          "be re-read is not recordable")
        event = {"v": 3, "event": "retire", "seq": row["seq"] + 1,
                 "id": row["id"], "ts": pk.now_ts(),
                 "retire_reason": reason, "retire_measurement": measurement,
                 "retire_seat": seat, "retire_note": note,
                 "retire_proof_version": dispatches._RETIRE_PROOF_V}
        if set(event) != dispatches._RETIRE_EVENT_FIELDS:
            missing = sorted(dispatches._RETIRE_EVENT_FIELDS - set(event))
            extra = sorted(set(event) - dispatches._RETIRE_EVENT_FIELDS)
            return None, ("retire does not bind: event fields do not match "
                          "the retire schema (missing=%s extra=%s)"
                          % (",".join(missing) or "-", ",".join(extra) or "-"))
        if not txn.append(event):
            return None, "ledger unwritable (%s) — retire NOT recorded" % path

        def finish():
            out = dispatches._apply(row, event)
            if not out.get("retired_admin"):
                # THE WRITE AND THE REPLAY MUST AGREE, asserted rather than assumed.
                # abandon's own history is the reason: its door admitted polarities
                # its `_apply` arm refused, so the ledger grew events the state
                # machine ignored while the CLI printed a terminal that never took.
                return None, ("retire appended an event the replay does not admit — "
                              "dispatch %s is UNCHANGED; this is a helm defect, not "
                              "a ledger repair" % row["id"])
            pk.event("dispatch-retire", row["id"], reason)
            return out, None
        return txn.then(finish)
    return dispatches._ledger_write(attempt, path)



def _record_close_landed_proven(rid, reviewed_tip, repo_id, trunk_ref,
                                trunk_sha, proof_mode):
    """Append one Git-proven landing closure without inventing a verdict.

    landreq owns the live repository/ref/proof work. This locked boundary
    revalidates the immutable verdict identity and writes one monotonic receipt.
    """
    reviewed = str(reviewed_tip or "").strip().lower()
    if not dispatches._FULL_TIP.fullmatch(reviewed):
        return None, "close-landed needs the original full reviewed commit id"
    repo_id, err = dispatches._clean(repo_id, "landing repo id", 4096)
    if err or not os.path.isabs(repo_id) or os.path.realpath(repo_id) != repo_id:
        return None, "close-landed needs a canonical absolute Git common-dir"
    trunk_ref = dispatches._valid_trunk_ref(trunk_ref)
    if not trunk_ref:
        return None, "close-landed needs a canonical branch or remote-tracking ref"
    trunk_sha = str(trunk_sha or "").strip().lower()
    if not dispatches._FULL_TIP.fullmatch(trunk_sha):
        return None, "close-landed needs the sampled trunk commit's full id"
    if proof_mode not in ("ancestor", "patch-equivalent"):
        return None, "close-landed proof mode must be ancestor or patch-equivalent"
    path = dispatches.ledger_path()

    def attempt(txn):
        if not txn.held:
            return None, "ledger unwritable (%s) — close-landed NOT recorded" % path
        current, unavailable = dispatches.snapshot()
        if unavailable:
            return None, "dispatch ledger unavailable: %s" % unavailable
        row, err = dispatches._resolve_row(current, rid)
        if err:
            return None, err
        if row.get("closed_by_landing"):
            same = row.get("landing_repo_id") == repo_id \
                and row.get("landing_trunk_ref") == trunk_ref
            if same:
                return row, None
            return None, "dispatch %s already has a different landing closure" % row["id"]
        if row.get("abandoned"):
            return None, ("dispatch %s is already ABANDONED with land state "
                          "UNKNOWN" % row["id"])
        if row.get("discharged") or row.get("withdrawn") \
                or row.get("close_reason"):
            return None, ("dispatch %s is already retired by %s" % (
                row["id"], "discharge" if row.get("discharged")
                else "withdraw" if row.get("withdrawn")
                else "close --reason %s" % row["close_reason"]))
        if row.get("status") != "verdict" or row.get("polarity") is not None:
            return None, "dispatch %s is not an UNDECLARED verdict" % row["id"]
        if row.get("reviewed_tip") != reviewed:
            return None, "close-landed reviewed tip does not match dispatch %s" % row["id"]
        event = {"v": 3, "event": "close-landed", "seq": row["seq"] + 1,
                 "id": row["id"], "ts": pk.now_ts(),
                 "reviewed_tip": reviewed,
                 "landing_repo_id": repo_id,
                 "landing_trunk_ref": trunk_ref,
                 "landing_trunk_sha": trunk_sha,
                 "landing_proof_mode": proof_mode,
                 "landing_proof_version": 1}
        if not txn.append(event):
            return None, "ledger unwritable (%s) — close-landed NOT recorded" % path

        def finish():
            out = dispatches._apply(row, event)
            pk.event("dispatch-close-landed", row["id"], trunk_sha)
            return out, None
        return txn.then(finish)
    return dispatches._ledger_write(attempt, path)



def discharging_row(row_id, current, is_landed=None, prefer=None):
    """(tier, discharging_row_id, why) — the row whose LAND discharges `row_id`,
    or (None, None, why) when nothing does.

    #177: A BUILD ROW HAS NO DOOR OF ITS OWN. Every reason in _CLOSE_POLARITY
    keys on the row's OWN polarity, and a build row that was never verdicted
    has none — its successor was minted first, so the parent sits AWAITING
    REVIEW forever while its work is on trunk. Measured 2026-08-04: five
    refusals across four verbs and two files (here and landreq's supersede
    rung), three rows, one predicate. One of them billed the fleet's most
    loaded reviewer 227 minutes for work that had landed three hours earlier.

    SO THE AUTHORITY IS ANOTHER ROW'S VERDICT, which is the #101 precedent in
    landreq: admit a row missing one binding ONLY when every other authority
    vouches. Both tiers demand the same three legs of that voucher — polarity
    approve, a gate that BINDS, and a reviewed tip actually on trunk.

    ONE TIER, AND IT IS A HARD EDGE: a successor whose supersedes-walk REACHES
    this row. `continues <row-id>` is recorded, never inferred.

    A SAME-LANE TIER WAS RULED IN AND THEN WITHDRAWN, and that belongs here as
    the cautionary half. It was ruled in on evidence that two rows shared a
    lane — evidence I produced by printing the labels truncated to 34
    characters, where 'jit-cooldown-measures-turns-not-compaction' and
    '...-not-context' render identically. They diverge at 35. The tier added
    for two specific rows would have discharged NEITHER of them, and would have
    admitted every future pair whose labels merely start alike.

    SO THIS REFUSES ROWS WHOSE RELATION WAS NEVER RECORDED, deliberately. A
    build row whose work landed under a row sharing neither its chain nor its
    lane cannot be discharged by derivation at all: no scan can prove a
    relation nobody wrote down, and a door that closed it anyway would be
    inventing the authority. Those need an ATTESTED close, where an accountable
    party asserts the linkage and the row records that it was ASSERTED rather
    than derived (#147) — legibly weaker on its face, which is exactly what
    keeps it from being laundering.

    Direction matters and it is the reverse of the intuitive read: the CHILD
    names the PARENT, so this SCANS for successors pointing AT row_id rather
    than reading a field on it. The walk itself is _chain_reaches — reused, not
    reimplemented, because two walks would eventually disagree about the same
    ledger.

    TIER 2 IS CONTENT IDENTITY, NOT A LABEL, and that is the whole reason it
    survives the paragraph above. The withdrawn tier asked "do two rows share
    a LANE STRING" — free text, renamable, and in the evidence that argued for
    it, truncated to 34 characters so two different lanes rendered
    identically. This one asks "do two rows BIND THE SAME COMMIT": a full
    40-hex id, recorded at dispatch time by the writer, exact, and incapable
    of colliding by rendering. When that commit is on trunk under another
    row's landed, gate-verified APPROVE, the content THIS row was minted to
    judge is already judged and already landed. That is strictly STRONGER
    than tier 1, which only proves a SUCCESSOR landed — possibly different
    content that superseded this row's.

    Measured 2026-08-12 on the live board: 46 land-request rows, 34 distinct
    lanes. Lane `project-raw-batch-is-unproven-end-to-end` carried THREE live
    rows, all three bound to ONE tip, in three different chains — a
    deliberate second cross-family review leg plus one redundant re-ask. No
    chain edge exists between parallel legs, by construction: they are peers,
    not a succession. So tier 1 is structurally blind to exactly the shape the
    fleet produces most, and the owner's question — "even if new reviews
    create new rows, when something lands they should all close, no?" — has no
    derivable answer without this tier.

    WHAT IT STILL REFUSES: a row in a different repository, a row whose bound
    tip differs by one byte, and a row that shares only a LANE NAME. A lane is
    a label; two unrelated efforts may reuse one, and nothing here closes them
    together.

    `prefer` names a candidate the CALLER already recorded — replay's own
    event. It ADMITS NOTHING: the named row must still pass every leg of a
    tier on its own. It decides only WHICH of several equally valid answers
    is reported, so a close recorded under one authority is not retroactively
    invalidated the day a second, later authority appears and happens to sort
    first. Without it, minting one new chain successor could turn a durable
    tip-tier close into a replay refusal.

    is_landed(tip) -> bool decides "on trunk"; injected so the caller owns the
    repo probe and this stays pure. A None answer from it is NOT landed:
    could-not-look is never looked-and-found."""
    target = str(row_id or "")
    row = (current or {}).get(target)
    if not isinstance(row, dict):
        return None, None, "no such row"
    if row.get("status") in dispatches.CLOSED_STATES:
        return None, None, "row is already terminal"

    def _vouches(cand):
        """The three legs every tier demands of the discharging row."""
        if str(cand.get("id") or "") == target:
            return False
        if cand.get("status") != "verdict" or cand.get("polarity") != "approve":
            return False
        tip = cand.get("reviewed_tip")
        if not tip or not is_landed:
            return False
        return is_landed(tip) is True

    # EVERY answer is collected before ONE is reported, because first-match
    # over a dict is ledger-insertion order and the writer's walk and the
    # replay walk see different populations (the writer's landed leg probes
    # git; replay's reads terminals). Collecting makes `prefer` possible and
    # makes the reported answer stable under a later append.
    found = []                       # [(tier, id)] in tier order, id-sorted
    # TIER 1 — a successor whose parent-walk reaches this row.
    chain_edge = row.get("supersedes") not in (None, dispatches.CHAIN_UNKNOWN)
    chain_hits = []
    for cand in (current or {}).values():
        if not isinstance(cand, dict) or str(cand.get("id") or "") == target:
            continue          # a row reaches ITSELF; that is not a successor
        reaches, _why = dispatches._chain_reaches(cand, target, current)
        if reaches is True:
            chain_edge = True
            if _vouches(cand):
                chain_hits.append(str(cand.get("id")))
    found.extend((dispatches.DISCHARGE_TIER_CHAIN, cid) for cid in sorted(chain_hits))
    # TIER 2 — a peer bound to the IDENTICAL commit, in the SAME repository.
    target_tip = dispatches.bound_tip(row)
    tip_hits = []
    if target_tip:
        for cand in (current or {}).values():
            if not isinstance(cand, dict):
                continue
            if str(cand.get("repo_id") or "") != str(row.get("repo_id") or ""):
                continue      # a stranger's repo proves nothing about ours
            if str(cand.get("reviewed_tip") or "").strip().lower() != target_tip:
                continue      # the CANDIDATE must have REVIEWED it, not merely
                              # been pointed at it
            if _vouches(cand):
                tip_hits.append(str(cand.get("id")))
    found.extend((dispatches.DISCHARGE_TIER_TIP, cid) for cid in sorted(tip_hits)
                 if cid not in chain_hits)
    if found:
        wanted = str(prefer or "")
        for tier, cid in found:
            if cid == wanted:
                return tier, cid, None
        tier, cid = found[0]
        return tier, cid, None
    if chain_edge:
        return None, None, ("a chain edge exists but no successor on it carries "
                            "a landed, gate-verified APPROVE")
    if target_tip:
        return None, None, ("nothing on the ledger records what discharged this "
                            "row — no chain successor, and no peer bound to %s "
                            "carries a landed, gate-verified APPROVE; it needs "
                            "an attested close, not a derived one"
                            % target_tip[:12])
    return None, None, ("nothing on the ledger records what discharged this row "
                        "— it needs an attested close, not a derived one")



def _delivery_error(event):
    """Why this event's DELIVERY DECLARATION may not bind — None when it does.

    Tri-state on purpose, and the middle state is the point: BOTH fields
    absent is a pre-declaration event (UNDECLARED, admissible, and rendered
    as unknown rather than live); "cli" carries no restart because a fresh
    process off main needs none; "process" MUST name what still holds
    pre-land code, because an unnamed restart obligation is one nobody can
    discharge. Anything else does not bind — a malformed declaration is never
    softened into the quiet answer."""
    klass = event.get("close_delivery_class")
    restart = event.get("close_delivery_restart")
    if klass is None:
        if restart is not None:
            return "a restart target without a declared delivery class"
        return None
    if klass not in dispatches.CLOSE_DELIVERY_CLASSES:
        return "close delivery class must be one of %s" \
            % "/".join(dispatches.CLOSE_DELIVERY_CLASSES)
    if klass == "cli":
        if restart is not None:
            return "a CLI-class land declares no restart target"
        return None
    cleaned, err = dispatches._clean(restart, "close delivery restart", 256)
    if err or cleaned != restart:
        return err or "close delivery restart must round-trip clean"
    return None



def _content_proof_pair_error(event, reviewed_tip):
    """None, or why this close event's content proof pair is inadmissible.

    THE PAIR IS ADMISSION, NOT DECORATION. content_witness and
    content_witness_anchor are OPTIONAL fields in the landed schema, so a
    forged or replayed close naming close_proof_mode=content-equivalent with
    NO witness was accepted and projected the row terminal — the exact thing
    this validator exists to make inert. Today's callers always pass the pair
    and the transport arms prove that; they prove nothing about whether the
    LEDGER refuses omission, which is a different question and the one a
    forgery asks.

    SHARED BY BOTH DOORS ON PURPOSE. The ordinary and BUILD validators are
    two entry points to one schema, and the BUILD one returns before the
    ordinary body is ever reached — so a check written into that body alone
    guards exactly half the rows and reads as if it guarded all of them.
    `reviewed_tip` is the caller's because the two doors bind different
    fields: ordinary witnesses `reviewed_tip`, BUILD witnesses the approved
    `landing_review_tip`.
    """
    # The domain string and the mode name are landreq's to own — it WRITES
    # these witnesses, and a second spelling here would drift the moment
    # either side is versioned. Lazy, because landreq imports this module too.
    from . import landreq
    witness = event.get("content_witness")
    anchor = event.get("content_witness_anchor")
    if event.get("close_proof_mode") != landreq.CONTENT_EQUIVALENT:
        if witness is not None or anchor is not None:
            # THE IFF, AND IT IS THE HALF THAT ROTS QUIETLY. A pair under any
            # OTHER mode is a proof nothing validates — it reads as evidence
            # on every surface that renders it while binding nothing at all.
            return "a content proof pair may only accompany content-equivalent"
        return None
    if not isinstance(witness, dict) or not anchor:
        return ("content-equivalent close needs both content_witness and "
                "content_witness_anchor")
    if anchor != dispatches._proof_anchor(landreq.CONTENT_ALGORITHM, witness):
        return "content_witness_anchor does not bind this witness"
    # BOTH SIDES ARE COMMIT RECORDS, NOT SHAS. `source`/`carrier` each hold
    # {commit,tree,parents} so replay can see history rewritten under a proof;
    # comparing the record itself to a sha refuses every honest close.
    # THE WHOLE IMMUTABLE RECORD, NOT THE BINDINGS I HAPPENED TO PICK
    # (a review's constructed failure is the argument): take the
    # real writer's accepted event, set matches=2 or algorithm to a foreign
    # value, RECOMPUTE the anchor, and replay. Source, trunk and carrier are
    # untouched, so a validator that checks only those returns clean and the
    # close projects TERMINAL — while `_content_equivalent_replay` would
    # reject that same witness as non-unique or wrong-algorithm. The ledger
    # would then hold a terminal close whose persisted proof cannot replay,
    # which is the one state this rung exists to make impossible. A checksum
    # over a record proves nobody EDITED it in transit; it says nothing about
    # whether the record was WELL-FORMED when it was signed.
    if set(witness) != dispatches._WITNESS_FIELDS:
        missing = sorted(dispatches._WITNESS_FIELDS - set(witness))
        extra = sorted(set(witness) - dispatches._WITNESS_FIELDS)
        return ("content witness fields do not match the v1 schema "
                "(missing=%s extra=%s)"
                % (",".join(missing) or "-", ",".join(extra) or "-"))
    # `type(...) is not int` and not a truthiness test: in Python True == 1,
    # so `witness["v"] != 1` ADMITS a boolean True, and this is a forgery
    # door where a bool is exactly what a hand-built payload carries.
    if type(witness.get("v")) is not int or witness.get("v") != 1:
        return "content witness is not a v1 record"
    if witness.get("algorithm") != landreq.CONTENT_ALGORITHM:
        return ("content witness was written by %r, not %r"
                % (witness.get("algorithm"), landreq.CONTENT_ALGORITHM))
    if type(witness.get("matches")) is not int or witness.get("matches") != 1:
        return ("content witness records %r matches — a proof granted on a "
                "non-unique carrier is not one" % (witness.get("matches"),))
    trunk_ref = witness.get("trunk_ref")
    if not isinstance(trunk_ref, str) or not trunk_ref.strip():
        return "content witness has no trunk_ref"
    # NON-EMPTY WAS NOT A LANGUAGE (a review caught my own rule broken in my
    # own lane). Replay RECOMPUTES both identities and compares them exactly,
    # so a forger who sets payload_digest to garbage and re-anchors passed a
    # non-empty test, the close projected TERMINAL, and _content_equivalent_
    # replay returned False on the very witness the ledger had blessed. An
    # optional field is the cheapest forgery; a field checked only for being
    # non-empty is the second cheapest.
    for field in ("payload_digest", "newline_fingerprint"):
        value = witness.get(field)
        if not isinstance(value, str) or not dispatches._CONTENT_IDENTITY.fullmatch(value):
            return ("content witness %s is not a 64-hex content identity, so "
                    "replay would recompute a different one and reject this "
                    "proof" % field)
    serr = dispatches._witness_side_error("source", witness.get("source"))
    if serr:
        return serr
    cerr = dispatches._witness_side_error("carrier", witness.get("carrier"))
    if cerr:
        return cerr
    if witness["source"]["commit"] != reviewed_tip:
        return "content witness proves a source other than the reviewed tip"
    if witness.get("trunk") != str(event.get("closing_trunk_sha") or ""):
        return "content witness was taken against a different trunk"
    if not dispatches._FULL_TIP.fullmatch(str(witness.get("trunk") or "")):
        return "content witness has no full-id pinned trunk"
    # SYNTAX IS NECESSARY AND CANNOT ESTABLISH TRUTH (a review's fourth point,
    # and it holds me to my own sentence). Every check above is SHAPE, so
    # a forger sets payload_digest to "0"*64 or source.tree to a different
    # VALID full id, re-anchors, and admission passes — while replay
    # recomputes the named commits and returns False. That is a TERMINAL close
    # whose persisted proof its own verifier rejects, which is the exact
    # invariant this validator claims to hold.
    #
    # FALSE REFUSES; None DOES NOT — and the asymmetry is deliberate, not a
    # softening of their prescription. This ONE rule runs at the writer AND at
    # replay (`_apply`), by design, so that a forged later event is inert. At
    # replay, None means THIS CHECKOUT cannot read the objects — a clone that
    # pruned them has an unreadable measurement, not a disproven proof
    # (landreq states this law where it mints the witness). Refusing on None
    # would silently UN-LAND honest rows in any pruned clone. Refusing on
    # FALSE closes the forgery, because a wrong-but-well-shaped value in a
    # readable repository is disproven, never unreadable.
    root = str(event.get("closing_repo_id") or "")
    if root:
        try:
            replayed, why_not = landreq._content_equivalent_replay(root, witness)
        except Exception:
            replayed, why_not = None, None      # unreadable, never disproven
        if replayed is False:
            return ("content witness does not replay against its own bound "
                    "repository (%s) — the proof is disproven, not merely "
                    "malformed" % (why_not or "no reason recorded"))
    return None



def _witness_side_error(side, rec):
    """None, or why this witness half is not the immutable record it claims.

    `source` and `carrier` are {commit, parents, tree} — the half a replay
    re-reads to prove history was not rewritten under the proof. A recorded
    tree or parent that is not a full object id cannot be compared against
    anything later, so it is a proof with a hole rather than a proof.
    """
    if not isinstance(rec, dict) or set(rec) != dispatches._WITNESS_SIDE_FIELDS:
        return ("content witness %s is not a {commit,parents,tree} record"
                % side)
    for field in ("commit", "tree"):
        if not dispatches._FULL_TIP.fullmatch(str(rec.get(field) or "")):
            return "content witness %s names no full-id %s" % (side, field)
    parents = rec.get("parents")
    if not isinstance(parents, list):
        return "content witness %s does not record a parent list" % side
    for parent in parents:
        if not isinstance(parent, str) or not dispatches._FULL_TIP.fullmatch(parent):
            return "content witness %s records a malformed parent" % side
    return None



def _build_landed_event_error(event, state, current, verdicts, verify_live):
    """Validate one OPEN BUILD close through an accepted review descendant."""
    # `close_actor` is OPTIONAL BY CONSTRUCTION — stamped only when the
    # writing process declared a seat — so it is subtracted before every
    # exact-set comparison here, exactly as at the writer. Leaving it in would
    # make these equalities answer about the environment that wrote the event
    # rather than about its proof shape, and would refuse on replay every
    # close a seated writer made.
    core = set(event) - dispatches._DELIVERY_FIELDS - dispatches._CONTENT_PROOF_FIELDS \
        - {"close_actor"}
    if core != (dispatches._BUILD_LANDED_EVENT_FIELDS - dispatches._DELIVERY_FIELDS
                - dispatches._CONTENT_PROOF_FIELDS):
        expected = (dispatches._BUILD_LANDED_EVENT_FIELDS - dispatches._DELIVERY_FIELDS
                    - dispatches._CONTENT_PROOF_FIELDS)
        missing = sorted(expected - core)
        extra = sorted(core - expected)
        return "close event fields do not match build-landed schema (missing=%s extra=%s)" \
            % (",".join(missing) or "-", ",".join(extra) or "-")
    derr = dispatches._delivery_error(event)
    if derr:
        return derr
    if state.get("status") != "open" or state.get("kind") != "build":
        return "build-landed needs one open BUILD dispatch"
    if state.get("close_reason"):
        return "already retired"
    repo, err = dispatches._clean(event.get("closing_repo_id"), "closing repo id", 4096)
    if err or not os.path.isabs(repo) or foldckpt.realpath(repo) != repo \
            or repo != state.get("repo_id"):
        return "closing repo id must match the BUILD dispatch repository"
    if not dispatches._valid_trunk_ref(event.get("closing_trunk_ref")):
        return "closing trunk ref must be a named branch/remote ref"
    if not dispatches._FULL_TIP.fullmatch(str(event.get("closing_trunk_sha") or "")):
        return "closing trunk sha must be the pinned full commit id"
    mode = event.get("close_proof_mode")
    if mode not in dispatches.CLOSE_PROOF_MODES:
        return "close proof mode must be one of %s" % "/".join(dispatches.CLOSE_PROOF_MODES)
    translated = event.get("translated_tip")
    if mode.startswith("translated-"):
        if not dispatches._FULL_TIP.fullmatch(str(translated or "")):
            return "a translated proof must carry the full translated tip"
    elif translated is not None:
        return "translated_tip on an untranslated proof"
    if current is None or verdicts is None:
        return "build-landed replay needs its one dispatch snapshot"
    review_id = str(event.get("landing_review_id") or "")
    review_tip = str(event.get("landing_review_tip") or "")
    if not dispatches._ID.fullmatch(review_id):
        return "landing review id must be a full dispatch id"
    if not dispatches._FULL_TIP.fullmatch(review_tip):
        return "landing review tip must be a full commit id"
    review = current.get(review_id)
    pair = verdicts.get(review_id)
    index = dispatches.verdict_index(verdicts, review_id)
    if review is None or review.get("status") != "verdict" or pair is None \
            or index is None:
        return "landing review is not one accepted durable verdict"
    if review.get("kind") != "review" or review.get("polarity") != "approve" \
            or review.get("repo_id") != repo \
            or review.get("reviewed_tip") != review_tip:
        return "landing review fields do not match an approved review in the repository"
    anchor = str(event.get("landing_review_verdict_anchor") or "")
    if not dispatches._PROOF_ANCHOR.fullmatch(anchor) or anchor != pair[1]:
        return "landing review verdict anchor does not match the accepted event"
    reaches, why = dispatches._chain_reaches(review, state.get("id"), current)
    if reaches is not True or review_id == state.get("id"):
        return "landing review is not an exact descendant of the BUILD dispatch%s" \
            % (" (%s)" % why if why else "")
    from . import landreq
    epoch = dispatches.gate_epoch(current, verdicts)
    requirement = landreq.gate_requirement(review, index=index, epoch=epoch)
    if requirement == "unknown" \
            or event.get("landing_review_gate_requirement") != requirement:
        return "landing review gate requirement does not match the verdict epoch"
    gate_id = str(review.get("gate") or "")
    if event.get("landing_review_gate") != gate_id \
            or requirement == "required" and not dispatches._GATE_ID.fullmatch(gate_id):
        return "landing review gate binding does not match the standing verdict"
    tier = event.get("landing_review_tier_state")
    if tier not in ("none", "ok"):
        return "landing review tier state must be none or ok"
    approval = event.get("landing_review_approval_anchor")
    if not dispatches._PROOF_ANCHOR.fullmatch(str(approval or "")) \
            or approval != dispatches._landed_review_approval_anchor(
                anchor, tier, requirement, gate_id):
        return "landing review approval anchor does not match its captured proof"
    if verify_live:
        refusal, live_tier = landreq._approval_refusal(
            review, index=index, epoch=epoch)
        if refusal or live_tier != tier:
            return "landing review approval no longer matches the locked proof"
    # THIS DOOR RETURNS BEFORE THE ORDINARY BODY EVER RUNS, so the pair check
    # written there guarded exactly half the landed rows while reading like it
    # guarded all of them. The BUILD witness binds the APPROVED REVIEW's tip,
    # not this BUILD row's own reviewed_tip.
    return dispatches._content_proof_pair_error(event, review_tip)



def _delivered_report_event_error(event, state, correction=False):
    """Validate one delivered-report close/correction at writer and replay.

    Artifact identity, the chat handoff reference, and concise evidence are
    distinct required fields. None is a transport ``delivery_ref`` and none is
    allowed to arrive through prose inference. The exact whole-object schema is
    shared by the locked append boundary and the replay reducer."""
    expected = dispatches._DELIVERED_REPORT_CORRECTION_FIELDS if correction \
        else dispatches._DELIVERED_REPORT_EVENT_FIELDS
    # The actor is orthogonal provenance and sits in no schema — subtracted
    # from what is compared, never added to what is expected.
    _core = set(event) - {dispatches.CLOSE_ACTOR_FIELD}
    if _core != expected:
        missing = sorted(expected - _core)
        extra = sorted(_core - expected)
        return ("%s fields do not match delivered-report schema "
                "(missing=%s extra=%s)" % (
                    "correction event" if correction else "close event",
                    ",".join(missing) or "-", ",".join(extra) or "-"))
    if event.get("v") != 3 or type(event.get("v")) is not int:
        return "delivered-report event must be a v3 object"
    want_event = "close-correction" if correction else "close"
    if event.get("event") != want_event \
            or event.get("close_reason") != "delivered-report":
        return "delivered-report event kind/reason does not match its schema"
    if str(event.get("id") or "") != state.get("id"):
        return "delivered-report event id does not match the standing dispatch"
    if type(event.get("seq")) is not int \
            or event.get("seq") != int(state.get("seq") or 0) + 1:
        return "delivered-report event sequence does not follow the standing dispatch"
    if type(event.get("close_proof_version")) is not int \
            or event.get("close_proof_version") != 1:
        return "delivered-report proof version must be 1"
    cleaned, err = dispatches.clean_delivered_report_refs(
        event.get("artifact_ref"), event.get("report_ref"),
        event.get("close_evidence"))
    if err:
        return err
    if cleaned != (event.get("artifact_ref"), event.get("report_ref"),
                    event.get("close_evidence")):
        return "delivered-report references must round-trip clean"
    if state.get("kind") != "build":
        return "delivered-report needs an explicit BUILD dispatch"
    if correction:
        if state.get("status") != "cancelled":
            return "delivered-report correction needs one cancelled BUILD dispatch"
        if event.get("corrects_event") != "cancel" \
                or event.get("corrects_reason") != state.get("cancel_reason"):
            return "delivered-report correction does not bind the standing cancel event"
    elif state.get("status") != "open":
        return "delivered-report needs one OPEN BUILD dispatch"
    return None



def _chain_declarers(rid, current, same_code):
    """(contrary polarity, sorted declarer ids, err) — the supersedes-
    descendant rows of `rid` whose verdicts declare a polarity about the
    SAME code, classified exactly as `landreq._chain_polarity` classifies
    them (approve vs contrary; conflict refuses, approve-only refuses,
    silence refuses). This is the LEDGER half of the chain-polarity walk,
    runnable wherever a `current` snapshot exists: the locked writer binds
    with it and replay re-walks the captured provenance through it."""
    kids = {}
    for row in (current or {}).values():
        if isinstance(row, dict) and row.get("supersedes"):
            kids.setdefault(str(row["supersedes"]), []).append(row)
    queue, seen, declares = list(kids.get(rid, ())), set(), []
    while queue:
        kid = queue.pop(0)
        kid_id = str(kid.get("id") or "")
        if not kid_id or kid_id in seen:
            continue    # a cycle is a malformed chain, not a walk for us
        seen.add(kid_id)
        queue.extend(kids.get(kid_id, ()))
        if kid.get("polarity") in ("approve", "fix", "supersede") \
                and kid.get("reviewed_tip") in same_code:
            declares.append((kid_id, kid["polarity"]))
    if not declares:
        return None, [], "the chain declares no polarity about this row's code"
    classes = {"approve" if pol == "approve" else "contrary"
               for _kid, pol in declares}
    if len(classes) > 1:
        return None, [], (
            "the chain holds CONFLICTING declared polarities about this "
            "row's code (%s) — a discharge never binds while the chain "
            "disagrees with itself"
            % ", ".join("%s=%s" % (kid[:12], pol.upper())
                        for kid, pol in sorted(declares)))
    if "approve" in classes:
        return None, [], ("the chain declares APPROVE about this row's code "
                          "— there is no contrary polarity to discharge")
    polarity = "supersede" if any(pol == "supersede"
                                  for _kid, pol in declares) else "fix"
    return polarity, sorted(kid for kid, _pol in declares), None



def _rebind_contradiction(candidate, row, current):
    """Bind the STATE legs of a discharging close from ONE (row, map) read.

    TWO CALLERS, ONE RULE (the per-case-handler cure):
    `landreq._retirement_stands` runs this at the DOOR so the admission and
    every rung downstream act on the same EFFECTIVE polarity it derived,
    and `_record_close_proven` runs it again UNDER THE LEDGER LOCK so the
    event records the truth it bound — a chain verdict that landed between
    the door's read and the lock is seen, not skipped. Returns the refusal
    when the given truth does not yield exactly one contrary declaration;
    at the writer that refusal is the FRESH-truth race answer."""
    if not (row.get("withdrawn") or row.get("close_reason") == "withdrawn") \
            or row.get("discharged") or row.get("closed_by_landing") \
            or row.get("abandoned") \
            or row.get("close_reason") not in (None, "withdrawn"):
        return "the standing retirement is no longer a bare withdrawal"
    reviewed = str(row.get("reviewed_tip") or "")
    if not dispatches._FULL_TIP.fullmatch(reviewed):
        return "the withdrawn row has no readable reviewed tip"
    candidate["contradicted_retirement"] = "close-withdrawn" \
        if row.get("close_reason") == "withdrawn" else "flag-withdrawn"
    own = row.get("polarity")
    if own:
        if own not in ("fix", "supersede"):
            return "the row's own polarity is not a contrary declaration"
        candidate.update(contradiction_polarity=own,
                         contradiction_polarity_via="own",
                         contradiction_same_code=[reviewed])
        return None
    from . import landreq               # DEFERRED — landreq imports us.
    same, serr = landreq._same_code_tips(row)
    if serr:
        return serr
    polarity, via, derr = dispatches._chain_declarers(row["id"], current, same)
    if derr:
        return derr
    # THE PROOF SET CARRIES ONLY FULL IDENTITIES: the
    # translation sidecar may hold an UNRESOLVED short identity, and a
    # captured member the replay grammar must refuse would fail the very
    # write it rode in on. Classification above still sees the whole set —
    # losslessly, because every ledger reviewed_tip is full, so a short
    # member can never have matched a declarer anyway; it was never
    # load-bearing for the discharge.
    candidate.update(contradiction_polarity=polarity,
                     contradiction_polarity_via=via,
                     contradiction_same_code=sorted(
                         tip for tip in same if dispatches._FULL_TIP.fullmatch(tip)))
    return None



def _contradiction_discharge_error(event, state, reason, current=None):
    """Why a RETIRED row's close event is NOT an admitted discharging close
    of a contradicted withdrawal — None when its captured proof admits it.

    The doors (`landreq._retirement_stands`) re-derive the contradiction LIVE
    and the locked writer REBINDS the state legs (`_rebind_contradiction`)
    the instant before this rule runs. This shared rule re-checks the
    LEDGER-side rungs from the event's captured fields — retirement class,
    polarity against the row's own declaration or a RE-WALK of the captured
    chain provenance through `current`, the same-code set, the immutable
    identities — never git: the live git leg stays the door's, strictly
    stronger, exactly the discharged/resolved asymmetry. Returns the verbatim
    `already retired` for every event NOT carrying the captured proof and for
    every state whose retirement is not a bare contradicted-withdrawal
    candidate, so the retired-once law is unchanged everywhere outside this
    one carve-out."""
    carried = [key for key in dispatches._CONTRADICTION_PROOF_FIELDS
               if event.get(key) is not None]
    if not carried or reason not in dispatches._CONTRADICTION_DISCHARGE_REASONS:
        return dispatches._ALREADY_RETIRED
    if not (state.get("withdrawn") or state.get("close_reason") == "withdrawn") \
            or state.get("discharged") or state.get("closed_by_landing") \
            or state.get("abandoned") \
            or state.get("close_reason") not in (None, "withdrawn"):
        return dispatches._ALREADY_RETIRED
    if len(carried) != len(dispatches._CONTRADICTION_PROOF_FIELDS):
        return ("a discharging close carries the whole captured contradiction "
                "proof or none of it (missing=%s)"
                % ",".join(key for key in dispatches._CONTRADICTION_PROOF_FIELDS
                           if key not in carried))
    retirement = "close-withdrawn" \
        if state.get("close_reason") == "withdrawn" else "flag-withdrawn"
    if event.get("contradicted_retirement") != retirement:
        return ("captured retirement class does not match the standing "
                "withdrawal (%s)" % retirement)
    polarity = event.get("contradiction_polarity")
    if polarity not in ("fix", "supersede"):
        return "captured contradiction polarity must be fix or supersede"
    same_code = event.get("contradiction_same_code")
    if not isinstance(same_code, list) or not 1 <= len(same_code) <= 2 \
            or len(set(same_code)) != len(same_code) \
            or not all(type(tip) is str and dispatches._FULL_TIP.fullmatch(tip)
                       for tip in same_code):
        return ("captured same-code set must be one or two full commit ids")
    if str(state.get("reviewed_tip") or "") not in same_code:
        return "captured same-code set does not contain the reviewed tip"
    via = event.get("contradiction_polarity_via")
    own = state.get("polarity")
    if via == "own":
        if not own or polarity != own:
            return ("captured polarity does not match the row's own declared "
                    "verdict")
    elif isinstance(via, list):
        # TYPED chain provenance, RE-WALKED — never trusted as prose. The
        # declaring rows must still stand in `current` with exactly one
        # contrary class about this code, and every captured id must be in
        # that re-walked set (a superset re-walk tolerates later same-class
        # declarations, exactly as discharged tolerates a chain that still
        # vouches; a CONFLICT refuses).
        if own:
            return ("a row with its own declared polarity carries no chain "
                    "provenance")
        # NO LENGTH CAP, on purpose: the producer walks
        # the real chain uncapped, and the siblings in _CLOSE_STATE_FIELDS
        # bound captured proofs by RE-DERIVATION (discharged re-walks
        # discharging_row, carried re-runs carriage_proof), never by a magic
        # number. The actual bound is three rungs: uniqueness, id grammar,
        # and membership in the re-walked declaring set below — a forged
        # long list dies there, an honestly long chain does not.
        if not via or len(set(via)) != len(via) \
                or not all(type(v) is str and dispatches._ID.fullmatch(v) for v in via):
            return "captured chain provenance must be full dispatch ids"
        if current is None:
            return ("a chain-declared discharge needs the full ledger to "
                    "re-walk its provenance")
        walked, ids, derr = dispatches._chain_declarers(state["id"], current,
                                             set(same_code))
        if derr:
            return derr
        if walked != polarity or not set(via) <= set(ids):
            return ("captured chain provenance does not match the ledger's "
                    "declared polarity about this row's code")
    else:
        return ("captured polarity provenance must be \"own\" or a list of "
                "declaring dispatch ids")
    if event.get("contradiction_land_leg") not in ("ancestor",
                                                   "patch-equivalent"):
        return "captured land leg must be ancestor or patch-equivalent"
    if not dispatches._valid_trunk_ref(event.get("contradiction_trunk_ref")):
        return ("captured contradiction trunk ref must be a named "
                "branch/remote ref")
    sha = str(event.get("contradiction_trunk_sha") or "")
    if not dispatches._FULL_TIP.fullmatch(sha):
        return ("captured contradiction trunk sha must be the pinned full "
                "commit id")
    # Contradictions decidable from immutable identities (the subsumed
    # precedent): `landed_ever` is deterministic per (tip, pin), so the very
    # pin the withdrawal proved ABSENCE on cannot also carry the land.
    if sha == str(state.get("absence_trunk_sha") or ""):
        return ("the contradiction cannot be pinned to the very trunk sha "
                "the withdrawal proved absence on")
    return None



def _close_mode_error(reason, event):
    """None, or why this close payload's proof mode is inadmissible.

    ONE MODE RULE, REACHED BY BOTH DOORS — a cross-family round-3 FIX, and the
    hole it names is an ORDERING one. `_record_close_proven` consults
    `_close_idempotent` BEFORE `_close_event_error`, so a retry of a RETIRED
    row was reconciled into SUCCESS by an identity tuple while the open-row
    validator would have refused the very same payload. Same bytes, two
    answers, decided by whether the row happened to be retired already.

    VALIDATE BEFORE RECONCILE is the cure, and this is the thing validated
    early: the pair rule first, then the reason's exact mode. It lives in one
    function because the alternative — restating the mode check at the writer —
    is two spellings that agree today and drift the moment either is versioned.

    IT MEETS THE PAIR RULE BY CONSTRUCTION, which is not incidental.
    `tests.test_lr_close.ProofPairAdmissionReachesEveryDoorTest` requires every
    function that INSPECTS `close_proof_mode` to reach
    `_content_proof_pair_error` on the call graph, and blesses the delegating
    shape in its own docstring. My round-2 cure removed the mode from
    `_close_idempotent`'s tuple to satisfy that rule and thereby opened this
    hole; putting the mode back in the tuple would reopen the rule. Threading
    between them means the mode is validated by something that MEETS the rule,
    and the identity tuple keeps deciding no proof question at all.
    """
    # THE TWO DOORS BIND DIFFERENT TIPS, and the pair rule's own docstring
    # says so: the ordinary close witnesses `reviewed_tip`, the BUILD close
    # witnesses the approved `landing_review_tip`. Passing the ordinary field
    # for both made a legitimate build witness read as "a source other than the
    # reviewed tip" — a true sentence about the wrong question.
    witnessed = (event.get("landing_review_tip")
                 if event.get("close_proof_version") == 2
                 else event.get("reviewed_tip"))
    perr = dispatches._content_proof_pair_error(event, str(witnessed or ""))
    if perr:
        return perr
    want = dispatches.CLOSE_EXACT_PROOF_MODE.get(reason)
    if want is not None and event.get("close_proof_mode") != want:
        return ("close --reason %s records proof mode %r, but this terminal "
                "may only ever record %r — it renders as CLOSED_CHAIN_PROOF "
                "and may never wear a witness it did not measure"
                % (reason, event.get("close_proof_mode"), want))
    return None



def author_evidence_claimed(row):
    """Does this verdict CLAIM verdict-time author evidence at all?

    True when ANY of the three fields is present — the row is making a claim,
    so an incomplete or unreadable one is a contradiction. False when NONE is
    present: the verdict predates the stamp entirely, and refusing on that
    absence downgrades every aged-out legitimate row rather than catching a
    bad one."""
    return isinstance(row, dict) and any(
        key in row for key in dispatches.VERDICT_AUTHOR_EVIDENCE_FIELDS)



def _carried_unverdicted(reason, state):
    """A `carried` close aimed at a row that is STILL OWED and was never
    reviewed: the one kind of row this reason may close without a verdict.

    ABSENCE OF A REVIEWED TIP IS NOT THE PREDICATE. A cancelled row and an
    already-closed row carry no reviewed tip either, and they are the larger
    population; keyed on the missing field alone, a hand-appended carried
    event binds a retired row whose work happens to be on trunk and relabels
    a terminal that was already written. Retirement is terminal, so the row
    must also be outside CLOSED_STATES."""
    return reason == "carried" \
        and state.get("status") not in dispatches.CLOSED_STATES \
        and not state.get("reviewed_tip")


#: The advisory-read polarities that NAME A FINDING. A CONCUR blocks nothing
#: and names none; a FIX or a SUPERSEDE is a finding by definition.
_FINDING_READ_POLARITIES = ("fix", "supersede")


def held_discharge_error(state):
    """Why a HELD rung that never got a verdict may NOT close without one, or
    None when it may.

    A VERDICT IS WHERE FINDINGS LIVE, AND A CLOSE REASON IS NOT A VERDICT. So
    a held rung closes on its chain's landed APPROVE only when the hold itself
    says there is nothing to record: a SOURCE-CLEAN hold, the reader's
    structured claim that the read found nothing. An ordinary hold names its
    findings, if it has any, in prose that no reader can count, and an
    owner-gated hold waits on a decision that a land does not make. Both keep
    the row, and the listing says a verdict is owed.

    A DIFFERENT EXIT ANSWER ON THE ROW KEEPS IT TOO. An advisory read that
    FIXes, SUPERSEDEs or names an exit answer (worse than main, imperfect) is
    a finding the source-clean claim did not answer.

    THE LOCAL FINDINGS NOTE IS NOT READ HERE, on purpose: it is a note for the
    approving reader to adjudicate, and nothing that reads a verdict reads
    it."""
    if not isinstance(state, dict) or state.get("status") != "held":
        return "it is not a held row"
    if state.get("owner_gated") is True:
        return ("its hold is OWNER-GATED: it waits on the owner's decision, "
                "and a land does not answer it")
    if not dispatches._clean_tip_of(state):
        return ("its hold is not source-clean, so any findings it names live "
                "only in the hold's prose: a verdict is where findings live, "
                "and a close reason is not a verdict")
    for read in state.get("advisory_reads") or ():
        if not isinstance(read, dict) \
                or str(read.get("polarity") or "") in _FINDING_READ_POLARITIES \
                or read.get("exit_answer"):
            return ("an advisory read on it names a finding or a different "
                    "exit answer, which the source-clean hold did not answer")
    return None


def held_rung_remedy(row):
    """The next move a held rung owes when `held_discharge_error` keeps it,
    in one sentence naming the verbs. The refusal and the listing both print
    it, so the two cannot come to name different doors."""
    rid = str((row or {}).get("id") or "")[:12]
    tip = str((row or {}).get("tip") or "<tip>")
    if (row or {}).get("owner_gated") is True:
        return ("the OWNER owes the next move; once he has decided, "
                "`helm dispatch release %s` returns the row to the fleet"
                % rid)
    return ("its holder records the read as a verdict: `helm dispatch "
            "release %s`, then `helm dispatch verdict %s %s --fix ...`, "
            "which closes as `superseded` once a later APPROVE on its chain "
            "has reached trunk; a read that found nothing is held again with "
            "`--source-clean %s` and closes as `discharged`"
            % (rid, rid, tip, tip[:12]))



def _close_event_error(event, state, current=None, verdicts=None,
                       verify_families=False, position=None):
    """Why one `close` EVENT may NOT bind to `state` — None when it binds.

    ONE rule for the writer (checked immediately before append, under the
    lock) and for replay (`_apply`), so a forged later event that the writer
    would refuse is inert on replay: the three-layer immutability trio
    carried whole. Every field is validated or the event does not apply."""
    if not isinstance(event, dict):
        return "event is not an object"
    reason = event.get("close_reason")
    if reason not in dispatches.CLOSE_REASONS:
        return "close reason must be one of %s" % "/".join(dispatches.CLOSE_REASONS)
    if event.get("v") != 3 or type(event.get("v")) is not int \
            or event.get("event") != "close":
        return "close event must be a v3 close object"
    if str(event.get("id") or "") != state.get("id"):
        return "close event id does not match the standing dispatch"
    if type(event.get("seq")) is not int \
            or event.get("seq") != int(state.get("seq") or 0) + 1:
        return "close event sequence does not follow the standing dispatch"
    version = event.get("close_proof_version")
    if reason == "delivered-report":
        return dispatches._delivered_report_event_error(event, state)
    if reason == "landed" and version == 2:
        return dispatches._build_landed_event_error(
            event, state, current, verdicts, verify_families)
    # A separate proof-version door, not a new global meaning of CONCUR.
    # Old readers refuse version 3; readers never consult the rollout switch.
    compose_landed = reason == "landed" and type(version) is int and version == 3
    if compose_landed:
        # THIS PROOF READS GATE RECEIPTS, GATE AUTHORITY AND POLICY HISTORY —
        # stores no fold checkpoint can re-verify — so a fold that met one
        # writes none (task/2770). No such close is on the ledger today.
        foldckpt.taint("a compose-landed v3 close read gate receipts")
        from . import compose_contract
        required = dispatches._CLOSE_EVENT_BASE_FIELDS - {"close_evidence"}
        required |= {"closing_repo_id", "closing_trunk_ref", "closing_trunk_sha",
                     "close_proof_mode", "close_delivery_class",
                     "close_delivery_restart", "compose_land_proof", "compose_land_anchor"}
        # THE SIXTH AND LAST EXACT-SET GATE ON A CLOSE EVENT. This one is a
        # required/allowed PAIR rather than an equality, and the actor sits
        # on the ALLOWED side for the same reason it sits in no schema
        # anywhere: present or absent by environment, never part of a proof
        # shape. Enumerating all six at once is the cure for the shape this
        # lane hit three times — a field added to a candidate is a membership
        # in every table that judges that candidate, and they do not announce
        # themselves.
        allowed = required | {"close_evidence", "translated_tip",
                              dispatches.CLOSE_ACTOR_FIELD} | dispatches._CONTENT_PROOF_FIELDS
        if not required <= set(event) or not set(event) <= allowed:
            return "compose-land close fields do not match schema"
        proof = event.get("compose_land_proof")
        err = compose_contract.proof_error(proof, state, current)
        if err:
            return err
        if event.get("compose_land_anchor") != dispatches._proof_anchor("compose-land-v1", proof) \
                or proof["repo"] != event.get("closing_repo_id") \
                or proof["pinned"] != event.get("closing_trunk_sha"):
            return "compose-land proof does not bind the close"
    elif "compose_land_proof" in event or "compose_land_anchor" in event:
        return "compose-land proof belongs only to landed proof version 3"
    if reason in ("subsumed", "resolved"):
        expected = dispatches._CLOSE_EVENT_BASE_FIELDS | set(dispatches._CLOSE_STATE_FIELDS[reason])
        got = set(event) - {dispatches.CLOSE_ACTOR_FIELD}
        if reason == "resolved":
            # The discharge overlay is OPTIONAL by construction (all-or-none,
            # judged at the retirement gate); the WHOLE-OBJECT rule here is
            # everything else — a resolved event that omits a proof field is
            # refused by NAME instead of binding on its base gates
            # (this reason had no door at all).
            expected -= set(dispatches._CONTRADICTION_PROOF_FIELDS)
            got -= set(dispatches._CONTRADICTION_PROOF_FIELDS)
        if got != expected:
            missing = sorted(expected - got)
            extra = sorted(got - expected)
            return "close event fields do not match %s schema (missing=%s extra=%s)" \
                % (reason, ",".join(missing) or "-", ",".join(extra) or "-")
    # THE ORDINARY DOOR'S HALF OF THE PAIR CHECK, shared with the BUILD door
    # so one schema has one admission rule. The witness here binds the ROW's
    # reviewed_tip — `_close_ladder_landed` searches from `lr["reviewed_tip"]`,
    # which is NOT the row's current `tip`; binding this to `tip` would refuse
    # every honest close.
    perr = dispatches._content_proof_pair_error(event, str(event.get("reviewed_tip") or ""))
    if perr:
        return perr
    # #177 — the discharged domain gate stands BEFORE the verdict gate, and
    # FIRST inside it: a verdicted row (including UNDECLARED, whose polarity
    # is None — the one value _CLOSE_POLARITY["discharged"] admits) must
    # never reach this door's ledger walk at all. Measured 2026-08-04 by the
    # integrator's adversarial pass: nested under `status != "verdict"`, a
    # forged discharged close on an UNDECLARED verdict row SKIPPED every
    # check here and bound downstream — replay closed a verdicted row on a
    # discharging id naming nothing on the ledger.
    if reason == "discharged":
        # A HELD RUNG IS A ROW THAT NEVER GOT A VERDICT TOO, and it is the
        # one a re-tip leaves behind: its successor lands and nothing closes
        # it. It takes this door only when its hold records zero findings
        # (`held_discharge_error`); every other hold keeps the row, because
        # the findings it names are owed a verdict.
        if state.get("status") == "held":
            herr = dispatches.held_discharge_error(state)
            if herr:
                return "discharged refuses this HELD row: %s" % herr
        elif state.get("status") != "open":
            return "discharged closes an OPEN build row, never a verdict"
    if state.get("status") != "verdict":
        if reason == "discharged":
            # #177 — the ONE door a polarity-less row has. The writer and this
            # replay arm re-derive the discharge from the SAME ledger read
            # rather than trusting the event's say-so: the recorded
            # discharging row must still vouch (landed, gate-verified,
            # APPROVE) for THIS row right now. An event naming a row that no
            # longer reaches — or never did — is inert, not binding.
            if current is None:
                return "discharged needs the full ledger to re-walk the chain"
            dis_id = str(event.get("discharging_id") or "")
            if not dispatches._ID.fullmatch(dis_id):
                return "discharging id must be a dispatch id"
            if not dispatches._FULL_TIP.fullmatch(str(event.get("discharging_tip") or "")):
                return "discharging tip must be a full commit id"
            if event.get("discharge_tier") not in dispatches.DISCHARGE_TIERS:
                return "discharge tier must be one of %s" % "|".join(
                    dispatches.DISCHARGE_TIERS)
            # Replay never probes git; the landed leg reads the LEDGER's own
            # terminal — a `landed` close on the discharging row, or the
            # legacy close-landed closure — and could-not-tell is UNKNOWN,
            # which discharging_row fails closed.
            def _replay_landed(tip):
                for cand in (current or {}).values():
                    if not isinstance(cand, dict):
                        continue
                    if str(cand.get("reviewed_tip") or "") != str(tip):
                        continue
                    if cand.get("close_reason") == "landed" \
                            or cand.get("closed_by_landing"):
                        return True
                return None
            # `prefer=dis_id` reports THIS event's own authority when it is
            # still one of the valid answers; it admits nothing the walk would
            # not admit on its own. Without it, appending one new chain
            # successor after a tip-tier close would make the walk report a
            # different (equally valid) row and turn a durable close into a
            # replay refusal.
            tier, by, why = dispatches.discharging_row(
                state["id"], current, is_landed=_replay_landed, prefer=dis_id)
            if tier is None:
                return "discharge refused on re-walk: %s" % why
            if by != dis_id:
                return ("event names %s but the ledger says %s discharges "
                        "this row" % (dis_id[:12], str(by)[:12]))
            dis_row = (current or {}).get(by) or {}
            if str(dis_row.get("reviewed_tip") or "") != \
                    str(event.get("discharging_tip")):
                return "discharging tip does not match the discharging row"
            # The two callers inject DIFFERENT landed legs — the writer adds
            # a live git probe the replay arm cannot run. The event records
            # which authority vouched, so the claim survives the asymmetry:
            # the writer's leg is strictly stronger, never weaker.
        elif dispatches._carried_unverdicted(reason, state):
            # THE SECOND DOOR A POLARITY-LESS ROW HAS, and the comment above
            # calling `discharged` the ONE door predates `carried` existing.
            # The two are not interchangeable: `discharged` demands a
            # chain-linked discharging row, and `carried` exists precisely
            # for rows where NO such discharge was ever recorded — so the
            # door that was supposed to serve these rungs provably cannot.
            #
            # NOTHING IS UNGATED BY ADMITTING IT. `carried`'s own arm below
            # re-derives `carriage_proof` from this same snapshot and
            # compares the recorded (base, tip) and witness against the
            # re-derivation, so a forged carried event is refused there
            # whatever this gate says. Authority here is a MEASUREMENT of
            # trunk, not a verdict, which is the whole point of the reason.
            #
            # NARROW BY CONSTRUCTION: only a LIVE row carrying no standing
            # verdict takes this branch (`_carried_unverdicted`). A verdicted
            # row keeps every rung below, including the reviewed-tip equality
            # check, and a retired row falls to the refusal beneath.
            pass
        else:
            return "not a verdict row"
    discharge_admitted = False
    if state.get("discharged") or state.get("withdrawn") \
            or state.get("closed_by_landing") or state.get("abandoned") \
            or state.get("close_reason"):
        # THE ONE CARVE-OUT: a discharging close of a CONTRADICTED withdrawal
        # carrying the captured proof binds; everything else keeps the
        # verbatim retired-once refusal (`_contradiction_discharge_error`).
        cerr = dispatches._contradiction_discharge_error(event, state, reason, current)
        if cerr:
            return cerr
        discharge_admitted = True
    elif any(event.get(key) is not None
             for key in dispatches._CONTRADICTION_PROOF_FIELDS):
        return "captured contradiction proof on a row that was never retired"
    reviewed = str(event.get("reviewed_tip") or "")
    # A never-verdicted `carried` row has no standing verdict to match, for
    # the same reason it has no reviewed tip to record: only a verdict writes
    # that field, and its identity is pinned instead by `carried_base` /
    # `carried_tip`, which the `carried` arm below re-derives and compares.
    #
    # THIS EXEMPTION IS LOAD-BEARING AND WAS PROVEN SO BY REMOVING IT. It
    # went in from reading rather than from an observed refusal, so it was
    # reverted and the suite re-run: the close then failed with "close does
    # not bind: reviewed tip does not match the standing verdict". Restored
    # on that evidence, not on the argument.
    #
    # NARROW: the check still binds a carried close on a row that DOES carry
    # a verdict, so the regression control one class up keeps its meaning.
    carried_unverdicted = dispatches._carried_unverdicted(reason, state)
    if reason != "discharged" and not carried_unverdicted \
            and (not dispatches._FULL_TIP.fullmatch(reviewed)
                 or reviewed != state.get("reviewed_tip")):
        return "reviewed tip does not match the standing verdict"
    polarity = state.get("polarity")
    if polarity is None and discharge_admitted:
        # A chain-declared contrary IS the row's effective polarity for the
        # discharge doors: `resolved`'s domain has no None, so
        # without this a polarity-silent contradicted withdrawal carries a
        # verified capture and still dies one rung later. The captured value
        # was just re-walked against the ledger by the carve-out above.
        polarity = event.get("contradiction_polarity")
    if not compose_landed and polarity not in dispatches._CLOSE_POLARITY[reason]:
        return "verdict polarity outside --reason %s's domain" % reason
    version = event.get("close_proof_version")
    if not compose_landed and (type(version) is not int or version != 1):
        return "close proof version must be 1"
    # The ts is deliberately NOT validated here. A close's WHEN rides the
    # projection's typed closure-stamp machinery (absent / unreadable /
    # impossible, each first-class), and the standing physics is explicit:
    # the flag asserts the retirement, the stamp only says when. Making a
    # corrupt ts byte refuse the whole event would RESURRECT proven-retired
    # debt — the one direction that machinery exists to prevent.
    evidence = event.get("close_evidence")
    if evidence is not None or reason != "landed":
        # evidence is OPTIONAL for landed (git alone authors that terminal)
        # and REQUIRED everywhere else; present it must always be clean.
        cleaned, err = dispatches._clean(evidence, "close evidence", 256)
        if err or cleaned != evidence:
            return err or "close evidence must round-trip clean"
    if reason == "landed":
        repo, err = dispatches._clean(event.get("closing_repo_id"), "closing repo id", 4096)
        if err or not os.path.isabs(repo):
            return "closing repo id must be an absolute path"
        if not dispatches._valid_trunk_ref(event.get("closing_trunk_ref")):
            return "closing trunk ref must be a named branch/remote ref"
        if not dispatches._FULL_TIP.fullmatch(str(event.get("closing_trunk_sha") or "")):
            return "closing trunk sha must be the pinned full commit id"
        mode = event.get("close_proof_mode")
        if mode not in dispatches.CLOSE_PROOF_MODES:
            return "close proof mode must be one of %s" % \
                "/".join(dispatches.CLOSE_PROOF_MODES)
        translated = event.get("translated_tip")
        if mode.startswith("translated-"):
            if not dispatches._FULL_TIP.fullmatch(str(translated or "")):
                return "a translated proof must carry the full translated tip"
        elif translated is not None:
            return "translated_tip on an untranslated proof"
        derr = dispatches._delivery_error(event)
        if derr:
            return derr
    elif reason == "superseded":
        superseding = str(event.get("superseding_tip") or "")
        if not dispatches._FULL_TIP.fullmatch(superseding):
            return "superseding tip must be a full commit id"
        if superseding == reviewed:
            return "a verdict tip cannot supersede itself"
        if not dispatches._ID.fullmatch(str(event.get("superseding_id") or "")):
            return "superseding id must be a dispatch id"
        if event.get("close_contrary_state") not in ("landed", "none"):
            return "contrary state must be landed or none"
        if event.get("close_contrary_target") not in ("local", "upstream"):
            return "contrary target must be local or upstream"
        if not dispatches._valid_trunk_ref(event.get("closing_trunk_ref")):
            return "closing trunk ref must be a named branch/remote ref"
        if not dispatches._FULL_TIP.fullmatch(str(event.get("closing_trunk_sha") or "")):
            return "closing trunk sha must be the pinned full commit id"
        # #101 — the translated-object-superseded door records HOW the row's
        # destroyed reviewed object was adjudicated. Both fields are present
        # together or neither; a plain supersession never carries them.
        mode = event.get("close_proof_mode")
        translated = event.get("translated_tip")
        if mode == "translated-superseded":
            if not dispatches._FULL_TIP.fullmatch(str(translated or "")):
                return "a translated supersession must carry the full translated tip"
        elif mode in ("archived-superseded", "attested-superseded"):
            if translated is not None:
                return "an archived/attested supersession has no translated object"
        elif mode is not None:
            return "superseded proof mode must be a door mode or absent"
        elif translated is not None:
            return "translated_tip on an untranslated supersession"
    elif reason == "withdrawn":
        if not dispatches._valid_trunk_ref(event.get("absence_trunk_ref")):
            return "absence trunk ref must be a named branch/remote ref"
        if not dispatches._FULL_TIP.fullmatch(str(event.get("absence_trunk_sha") or "")):
            return "absence trunk sha must be the pinned full commit id"
        mode = event.get("close_proof_mode")
        translated = event.get("translated_tip")
        if mode is None:
            # legacy pre-translation withdrawn events carry neither field
            if translated is not None:
                return "translated_tip on an untranslated absence proof"
        elif mode not in dispatches.WITHDRAWN_PROOF_MODES:
            return "withdrawn proof mode must be one of %s" \
                % "/".join(dispatches.WITHDRAWN_PROOF_MODES)
        elif mode == "translated-absent":
            if not dispatches._FULL_TIP.fullmatch(str(translated or "")):
                return ("a translated absence proof must carry the full "
                        "translated tip")
        elif translated is not None:
            return "translated_tip on an untranslated absence proof"
    elif reason == "stranded":
        repo, err = dispatches._clean(event.get("closing_repo_id"), "closing repo id", 4096)
        if err or not os.path.isabs(repo):
            return "closing repo id must be an absolute path"
        if not dispatches._FULL_TIP.fullmatch(str(event.get("control_sha") or "")):
            return "control sha must be the full trunk object the repo proved"
        if event.get("close_proof_mode") != "object-pruned":
            return "stranded proof mode must be object-pruned"
    elif reason == "expired":
        # THE ARM THE REASON SHIPPED WITHOUT, and the hole is the one
        # `chain-proof` names four branches below BY NAME: "WITHOUT THIS ARM
        # the reason would fall through to `return None` below and be accepted
        # on its base gates alone — which is exactly the hole a new reason
        # silently inherits, because the chain's default is ACCEPT." `expired`
        # was registered in the polarity and persistence maps and inherited
        # precisely that. An event naming a correct id, seq and tip was
        # admitted with NO closing repo, NO control sha and NO proof mode —
        # every field the ladder measured could be absent and the record still
        # replayed as a terminal.
        #
        # THE TIER IS CHECKED AS WELL AS THE SHAPE, because proving the tip
        # absent is only half this door's claim. The other half is that the
        # verdict AUTHORIZES NOTHING, and without a recorded hold kind a
        # stamped authorizing approve and a pre-tier one are the same bytes
        # here — so the one population this door must never touch was
        # indistinguishable from the one it exists for.
        #
        # AND THE CAPTURE IS VERIFIED, NOT RE-RESOLVED, which is `subsumed`'s
        # side of the split this file contains both halves of: "mutable roster
        # or policy drift must not resurrect an already-recorded terminal". The
        # live re-derivation happens ONCE, under the writer's lock, where a
        # refusal can still stop the write. Here it would only let a later
        # approval-tier edit dissolve a terminal this ledger already recorded.
        repo, err = dispatches._clean(event.get("closing_repo_id"), "closing repo id",
                           4096)
        if err or not os.path.isabs(str(repo or "")):
            return "closing repo id must be an absolute path"
        if not dispatches._FULL_TIP.fullmatch(str(event.get("control_sha") or "")):
            return "control sha must be the full trunk object the repo proved"
        merr = dispatches._close_mode_error(reason, event)
        if merr:
            return merr
        from . import landreq            # DEFERRED — landreq imports us.
        # THE CAPTURE IS BOUND TO THE STANDING RECORD, NOT MERELY SPELLED FROM
        # AN ALLOWED VOCABULARY. A membership test alone admits a valid-shaped
        # event over a STAMPED AUTHORIZING APPROVE carrying close_hold_kind
        # "advisory" — replay clean, writer refusing, the same row and two
        # answers, which is the disagreement this arm exists to end. The
        # derivation
        # reads only immutable verdict evidence, so this is a re-derivation and
        # not a re-resolution: nothing a later policy edit touches enters it.
        want = landreq.recorded_hold_kind(state)
        if want not in landreq.NONAUTHORIZING_HOLDS:
            return ("expired closes a verdict that AUTHORIZES NOTHING, and "
                    "this row's own verdict evidence makes it %s — only %s "
                    "may ever wear this terminal"
                    % (want, "/".join(landreq.NONAUTHORIZING_HOLDS)))
        if event.get("close_hold_kind") != want:
            return ("expired records hold kind %r while this row's own "
                    "immutable verdict evidence says %r — a capture that names "
                    "an allowed word without binding the record is not a proof"
                    % (event.get("close_hold_kind"), want))
    elif reason == "endorsement-moot":
        # THE ARM EVERY NEW REASON OWES, written BECAUSE the branch above says
        # what happens without one: the chain's default is ACCEPT, so a reason
        # with no arm is admitted on its base gates alone and an event naming a
        # correct id, seq and tip replays as a terminal carrying no proof at
        # all. This door's proof is two facts and both are checked here.
        #
        # THE TRUNK PAIR IS THE PRESENCE HALF. `expired` pins a `control_sha`
        # because an absence door must first show the repository could read
        # ANYTHING; a presence door instead records WHICH TRUNK the ancestry
        # was read against, because "on trunk" is meaningless without naming
        # the trunk and a bare sha cannot be re-run by a later hand.
        repo, err = dispatches._clean(event.get("closing_repo_id"),
                                      "closing repo id", 4096)
        if err or not os.path.isabs(str(repo or "")):
            return "closing repo id must be an absolute path"
        if not dispatches._valid_trunk_ref(event.get("closing_trunk_ref")):
            return ("closing trunk ref must be the ref the ancestry was "
                    "proved against")
        if not dispatches._FULL_TIP.fullmatch(
                str(event.get("closing_trunk_sha") or "")):
            return "closing trunk sha must be the full trunk object pinned"
        merr = dispatches._close_mode_error(reason, event)
        if merr:
            return merr
        from . import landreq            # DEFERRED — landreq imports us.
        # THE NONAUTHORIZATION HALF, AND IT IS THE HALF THAT MAKES THE DOOR
        # SAFE. Without it a close claiming a tip on trunk claims nothing about
        # the verdict, which is `landed` wearing another reason's name — so a
        # stamped authorizing APPROVE and an endorsement would be the same
        # bytes here and the polarity law would hold only at the CLI.
        #
        # `advisory` ALONE, which is STRICTER than `expired`'s pair. `pre-tier`
        # is an APPROVE, and an approve over landed work already has `landed`;
        # admitting it here would give it a second, weaker landing door. The
        # derivation reads only immutable verdict evidence, so replay touches
        # no mutable policy and a later tier edit can neither dissolve nor
        # resurrect this terminal.
        want = landreq.recorded_hold_kind(state)
        if want != "advisory":
            return ("endorsement-moot closes an ENDORSEMENT, and this row's "
                    "own verdict evidence makes it %s — only advisory may "
                    "ever wear this terminal" % want)
        if event.get("close_hold_kind") != want:
            return ("endorsement-moot records hold kind %r while this row's "
                    "own immutable verdict evidence says %r — a capture that "
                    "names an allowed word without binding the record is not "
                    "a proof" % (event.get("close_hold_kind"), want))
    elif reason == "subsumed":
        if not state.get("verdict_ref") or not state.get("repo_id"):
            return "subsumed needs an approved verdict on a readable repository"
        if state.get("chain_root") == dispatches.CHAIN_UNKNOWN:
            return "subsumed needs a readable work chain"
        if state.get("kind") != "review":
            return "subsumed needs an original review dispatch"
        confirmation_id = str(event.get("confirmation_id") or "")
        confirmation_tip = str(event.get("confirmation_tip") or "")
        confirmation_ref, err = dispatches._clean(event.get("confirmation_ref"),
                                       "confirmation ref", 4096)
        if not err and len(dispatches._GATE_TOKEN_RE.sub("", confirmation_ref)) > 256:
            err = ("confirmation ref is over the 256-char statement budget "
                   "(gate: tokens excluded)")
        if not dispatches._ID.fullmatch(confirmation_id):
            return "confirmation id must be a full dispatch id"
        if not dispatches._FULL_TIP.fullmatch(confirmation_tip):
            return "confirmation tip must be a full commit id"
        if err or not dispatches._subsumption_ref(
                confirmation_ref, state.get("polarity")):
            return ("confirmation verdict needs an explicit FIX findings-answered "
                    "statement" if state.get("polarity") == "fix" else
                    "confirmation verdict needs an explicit subsumption statement")
        if not dispatches._ID.fullmatch(str(evidence or "")) \
                or not confirmation_id.startswith(evidence):
            return "close evidence selector does not identify confirmation id"
        original_author = str(event.get("original_author") or "")
        confirmation_recipient = str(event.get("confirmation_recipient") or "")
        original_family = str(event.get("original_author_family") or "")
        confirmation_family = str(event.get("confirmation_recipient_family") or "")
        if original_author != state.get("sender") \
                or not dispatches._TOKEN.fullmatch(original_author):
            return "original author binding does not match the standing verdict"
        if not dispatches._TOKEN.fullmatch(confirmation_recipient):
            return "confirmation recipient binding is malformed"
        if not dispatches._TOKEN.fullmatch(original_family) \
                or not dispatches._TOKEN.fullmatch(confirmation_family) \
                or original_family == confirmation_family:
            return "subsumed needs two distinct canonical identity families"
        err = dispatches._family_evidence_error(
            event.get("original_family_evidence"), original_author,
            original_family, event.get("original_family_anchor"))
        if err:
            return "original author %s" % err
        err = dispatches._family_evidence_error(
            event.get("confirmation_family_evidence"), confirmation_recipient,
            confirmation_family, event.get("confirmation_family_anchor"))
        if err:
            return "confirmation recipient %s" % err
        repo, err = dispatches._clean(event.get("closing_repo_id"), "closing repo id", 4096)
        if err or not os.path.isabs(repo) or foldckpt.realpath(repo) != repo \
                or repo != state.get("repo_id"):
            return "closing repo id must match the original canonical repository"
        if not dispatches._valid_trunk_ref(event.get("closing_trunk_ref")):
            return "closing trunk ref must be a named branch/remote ref"
        if not dispatches._FULL_TIP.fullmatch(str(event.get("closing_trunk_sha") or "")):
            return "closing trunk sha must be the pinned full commit id"
        if event.get("close_proof_mode") not in ("ancestor", "patch-equivalent"):
            return "confirmation proof mode must be ancestor or patch-equivalent"
        if event.get("original_proof_mode") != "absent":
            return "original proof mode must be absent"
        # The original is deliberately off trunk and may be pruned after lane
        # cleanup, so replay cannot re-run Git without resurrecting valid debt.
        # Reject contradictions still decidable from immutable event identities;
        # the locked writer below owns the full live Git proof.
        trunk_sha = str(event.get("closing_trunk_sha") or "")
        if reviewed == trunk_sha:
            return "original Git proof contradicts the pinned trunk sha"
        if confirmation_tip == reviewed:
            return "one reviewed tip cannot be both present and absent on trunk"
        if current is None or verdicts is None:
            return "subsumed replay needs its one dispatch snapshot"
        confirmation = current.get(confirmation_id)
        if confirmation is None or confirmation.get("status") != "verdict":
            return "confirmation dispatch is not a standing verdict"
        original_pair = verdicts.get(state["id"])
        confirmation_pair = verdicts.get(confirmation_id)
        original_index = dispatches.verdict_index(verdicts, state["id"])
        confirmation_index = dispatches.verdict_index(verdicts, confirmation_id)
        if original_index is None or confirmation_index is None \
                or confirmation_index <= original_index:
            return "confirmation verdict must be later than the original"
        original_anchor = str(event.get("original_verdict_anchor") or "")
        confirmation_anchor = str(event.get("confirmation_verdict_anchor") or "")
        if not dispatches._PROOF_ANCHOR.fullmatch(original_anchor) \
                or not original_pair or original_anchor != original_pair[1]:
            return "original verdict anchor does not match the accepted event"
        if not dispatches._PROOF_ANCHOR.fullmatch(confirmation_anchor) \
                or not confirmation_pair \
                or confirmation_anchor != confirmation_pair[1]:
            return "confirmation verdict anchor does not match the accepted event"
        expected_chain = state["id"] if state.get("chain_root") is None \
            else state.get("chain_root")
        if confirmation.get("repo_id") != state.get("repo_id") \
                or confirmation.get("chain_root") != expected_chain:
            return "confirmation verdict must be linked to the same chain/repo"
        if confirmation.get("recipient") != confirmation_recipient:
            return "confirmation recipient does not match the standing verdict"
        if confirmation.get("kind") != "review" \
                or confirmation.get("polarity") != "approve" \
                or confirmation.get("reviewed_tip") != confirmation_tip \
                or confirmation.get("verdict_ref") != confirmation_ref:
            return "confirmation fields do not match the standing review verdict"
        from . import landreq
        epoch = dispatches.gate_epoch(current, verdicts)
        requirement = landreq.gate_requirement(
            confirmation, index=confirmation_index, epoch=epoch)
        if requirement == "unknown" \
                or event.get("confirmation_gate_requirement") != requirement:
            return "confirmation gate requirement does not match the verdict epoch"
        gate_id = str(confirmation.get("gate") or "")
        if event.get("confirmation_gate") != gate_id \
                or requirement == "required" \
                and not dispatches._GATE_ID.fullmatch(gate_id):
            return "confirmation gate binding does not match the standing verdict"
        tier = event.get("confirmation_tier_state")
        if tier not in ("none", "ok"):
            return "confirmation tier state must be none or ok"
        approval_anchor = event.get("confirmation_approval_anchor")
        if not dispatches._PROOF_ANCHOR.fullmatch(str(approval_anchor or "")) \
                or approval_anchor != dispatches._subsumed_approval_anchor(
                    confirmation_anchor, tier, requirement, gate_id):
            return "confirmation approval anchor does not match its captured proof"
        if verify_families:
            refusal, live_tier = landreq._approval_refusal(
                confirmation, index=confirmation_index, epoch=epoch)
            if refusal or live_tier != tier:
                return "confirmation approval no longer matches the locked proof"
            original_families, original_evidence, original_live_anchor, why = \
                dispatches._approval_identity_family_evidence(original_author)
            if why or original_families != {original_family} \
                    or original_evidence != event.get("original_family_evidence") \
                    or original_live_anchor != event.get("original_family_anchor"):
                return "original author family is UNKNOWN or conflicts"
            confirmation_families, confirmation_evidence, \
                confirmation_live_anchor, why = \
                dispatches._approval_identity_family_evidence(confirmation_recipient)
            if why or confirmation_families != {confirmation_family} \
                    or confirmation_evidence != \
                    event.get("confirmation_family_evidence") \
                    or confirmation_live_anchor != \
                    event.get("confirmation_family_anchor"):
                return "confirmation recipient family is UNKNOWN or conflicts"
    elif reason == "resolved":
        # THE DOOR THIS REASON NEVER HAD (the carried
        # comment below WARNED that a reason without an arm here is accepted
        # on its base gates alone, and resolved was exactly that hole: a
        # next-seq event could omit or forge every proof field and bind
        # through _apply). Whole-object now, the subsumed shape: every
        # captured field validated, the confirmation re-derived from the
        # LEDGER — never from the event's say-so, never from git.
        confirmation_id = str(event.get("confirmation_id") or "")
        confirmation_tip = str(event.get("confirmation_tip") or "")
        if not dispatches._ID.fullmatch(confirmation_id):
            return "confirmation id must be a full dispatch id"
        if not dispatches._FULL_TIP.fullmatch(confirmation_tip):
            return "confirmation tip must be a full commit id"
        confirmation_ref, err = dispatches._clean(event.get("confirmation_ref"),
                                       "confirmation ref", 4096)
        if err or confirmation_ref != event.get("confirmation_ref"):
            return err or "confirmation ref must round-trip clean"
        if not dispatches.resolution_statement(confirmation_ref):
            return ("confirmation evidence must OPEN with `Resolution "
                    "verified on trunk: <concrete resolution>`")
        if not dispatches._ID.fullmatch(str(evidence or "")) \
                or not confirmation_id.startswith(evidence):
            return "close evidence selector does not identify confirmation id"
        original_author = str(event.get("original_author") or "")
        if original_author != str(state.get("sender") or "") \
                or not dispatches._TOKEN.fullmatch(original_author):
            return "original author binding does not match the standing row"
        repo, err = dispatches._clean(event.get("closing_repo_id"), "closing repo id",
                           4096)
        if err or not os.path.isabs(repo) or foldckpt.realpath(repo) != repo \
                or repo != state.get("repo_id"):
            return "closing repo id must match the original canonical repository"
        if not dispatches._valid_trunk_ref(event.get("closing_trunk_ref")):
            return "closing trunk ref must be a named branch/remote ref"
        if not dispatches._FULL_TIP.fullmatch(str(event.get("closing_trunk_sha") or "")):
            return "closing trunk sha must be the pinned full commit id"
        if event.get("close_proof_mode") != "resolved-on-pinned-trunk":
            return "resolved proof mode must be resolved-on-pinned-trunk"
        if current is None or verdicts is None:
            return "resolved replay needs its one dispatch snapshot"
        if confirmation_id == state["id"]:
            return "a row can never confirm its own resolution"
        confirmation = current.get(confirmation_id)
        if confirmation is None or confirmation.get("status") != "verdict" \
                or confirmation.get("kind") != "review":
            return "confirmation dispatch is not a standing review verdict"
        from . import landreq            # DEFERRED — landreq imports us.
        if confirmation.get("polarity") not in landreq.CONFIRMATION_POLARITIES:
            return ("confirmation polarity is outside resolved's domain "
                    "(approve or supersede, never fix)")
        if confirmation.get("reviewed_tip") != confirmation_tip \
                or confirmation.get("verdict_ref") != confirmation_ref:
            return "confirmation fields do not match the standing review verdict"
        if confirmation.get("repo_id") != state.get("repo_id"):
            return "confirmation verdict is not on the same repository"
        original_index = dispatches.verdict_index(verdicts, state["id"])
        confirmation_index = dispatches.verdict_index(verdicts, confirmation_id)
        if original_index is None or confirmation_index is None \
                or confirmation_index <= original_index:
            return "confirmation verdict must be later than the original"
    elif reason == "carried":
        # THE THIRD SITE (task/756). The ladder authorized with
        # `carriage_proof`, the writer re-derived it under the lock, and this
        # arm derives it AGAIN from the same ledger read — the `discharged`
        # shape, and for the same reason: an event that merely SAYS the work
        # was carried is prose with a schema. Re-derivation is what makes a
        # forged close INERT rather than merely unlikely.
        #
        # WITHOUT THIS ARM the reason would fall through to `return None`
        # below and be accepted on its base gates alone — which is exactly
        # the hole a new reason silently inherits, because the chain's
        # default is ACCEPT.
        if current is None:
            return "carried needs the full ledger to re-derive its proof"
        gitdir = event.get("closing_repo_id")
        if not dispatches._clean(gitdir, "closing repo id", 4096)[0] \
                or not os.path.isabs(str(gitdir)):
            return "closing repo id must be an absolute path"
        if not dispatches._valid_trunk_ref(event.get("closing_trunk_ref")):
            return "closing trunk ref must be a named branch/remote ref"
        if not dispatches._FULL_TIP.fullmatch(str(event.get("closing_trunk_sha") or "")):
            return "closing trunk sha must be the pinned full commit id"
        row = (current or {}).get(event.get("id"))
        if not isinstance(row, dict):
            return "carried names a row this ledger does not carry"
        carried, detail = dispatches.carriage_proof(
            row, current, dispatches._CarrierView(current), gitdir,
            event.get("closing_trunk_ref"))
        if carried is not True:
            # A PROVEN CARRIED CLOSE STAYS PROVEN WHEN ITS OWN WORK REACHES
            # TRUNK (task/2863). The witnesses go silent for an ancestor tip
            # because `git cherry` has an EMPTY RANGE and an empty range
            # affirms every possible trunk -- correct at CLOSE time, where an
            # unproven close must not be minted, and wrong here, where the
            # close was already proven against a recorded trunk. Left as a
            # refusal it meant a STRONGER FACT ABOUT THE WORLD produced a
            # WEAKER STATE: the work is now literally in trunk's history and
            # the row returns to OPEN.
            #
            # MEASURED BEFORE IT WAS BUILT: of the whole board, this is the
            # ONLY mechanism that moves a carried close's fold answer. Every
            # content-level advance -- trunk editing, deleting, re-adding or
            # REVERTING a path the row touched -- leaves the answer alone,
            # because the fallthrough to the history family catches it. And
            # the trigger is not "this lane landed": anything DESCENDING from
            # the reviewed tip reaching trunk makes that tip an ancestor, so
            # a rule keyed on the lane would miss it.
            #
            # ONE PROBE, AND ONLY FOR A ROW THE WITNESSES COULD NOT AFFIRM.
            # It asks the recorded tip against the CURRENT trunk, so the cost
            # is one is-ancestor per silent carried close and never a tier of
            # fallbacks. Measured on the live board: all 508 carried closes
            # carry a pin that still resolves.
            pinned_tip = str(event.get("carried_tip") or "")
            # THE PINNED TIP MUST BE THE ROW'S OWN WORK TIP, derived here the
            # way `carriage_proof` derives it, or an event naming any commit
            # trunk already holds would bind a close nothing ever proved.
            if dispatches._FULL_TIP.fullmatch(pinned_tip) \
                    and pinned_tip != dispatches._work_tip_of(row, current):
                return ("carried tip %s is not the work tip this row binds"
                        % pinned_tip[:12])
            if dispatches._FULL_TIP.fullmatch(pinned_tip):
                reached = dispatches._ancestry_authorizes(
                    gitdir, pinned_tip, event.get("closing_trunk_ref"))
                if reached is None:
                    # THE REFUSAL NAMES ITSELF RATHER THAN GUESSING. An
                    # unreadable probe is not a measured no, and spelling it
                    # as one would un-close a good row on a git that failed
                    # to run.
                    return ("carried could not re-ask whether %s has since "
                            "become history of %s — the probe did not run, "
                            "which is not a finding that the work is absent"
                            % (pinned_tip[:12],
                               event.get("closing_trunk_ref")))
                if reached is True:
                    # THE WITNESS RUNG STILL BINDS. There is no re-derived
                    # detail to compare a pair or a witness name against, so
                    # the one check that does not need it is kept rather than
                    # dropped: an event still cannot claim a witness that
                    # does not exist.
                    if event.get("close_proof_mode") not in dispatches.CARRIED_WITNESSES:
                        return ("carried proof mode must name a witness (%s)"
                                % "/".join(dispatches.CARRIED_WITNESSES))
                    return None
            return ("trunk no longer affirms it carries this work (%s) — a "
                    "carried close is only as live as its measurement"
                    % (detail if isinstance(detail, str) else carried))
        # The recorded pair must be the pair the re-derivation USED, or the
        # event is describing a different measurement than the one that
        # passed.
        if event.get("carried_base") != detail.get("base") \
                or event.get("carried_tip") != detail.get("tip"):
            return "carried base/tip do not match the re-derived work pair"
        # AND IT MUST NAME THE WITNESS THAT ANSWERED. Without this rung the
        # two families are distinguishable only by `carried_base` being
        # absent, so an event could claim the stronger content replay while
        # the range-identity witness is what actually authorized it — a
        # record that overstates its own proof. The comparison is against
        # the RE-DERIVED witness, never against the tuple alone.
        if event.get("close_proof_mode") not in dispatches.CARRIED_WITNESSES:
            return ("carried proof mode must name a witness (%s)"
                    % "/".join(dispatches.CARRIED_WITNESSES))
        if event.get("close_proof_mode") != detail.get("witness"):
            return ("carried proof mode names the %s witness, but %s is what "
                    "re-derived it" % (event.get("close_proof_mode"),
                                       detail.get("witness")))
    elif reason == "chain-proof":
        # THE HOLE THE `carried` COMMENT ABOVE PREDICTED BY NAME (row
        # 9f09ff4b858e). "WITHOUT THIS ARM the reason would fall through to
        # `return None` below and be accepted on its base gates alone — which
        # is exactly the hole a new reason silently inherits, because the
        # chain's default is ACCEPT." `chain-proof` was that new reason: the
        # LADDER derived `chain_path` outside the lock and handed it to the
        # writer, and from there nothing — not the locked pre-append check, not
        # replay — ever looked at it again. A next-seq close event naming edges
        # this ledger does not have BOUND, and a real close whose chain had
        # since forked replayed as terminal forever.
        #
        # SO THE PATH IS RE-WALKED, NOT READ. Everything below compares the
        # event against a re-derivation from the SAME snapshot this validator
        # was handed — the `discharged` and `carried` shape, and for their
        # reason: an event that merely SAYS an authority transfer happened is
        # prose with a schema. `_chain_authority` is the single joined answer,
        # so lifecycle, polarity, capability, tier and chronology all come from
        # one walk rather than from rungs restated here and drifting.
        #
        # AND REPLAY RE-DERIVES RATHER THAN READING A CAPTURE, which is the one
        # judgement call here and is `carried`'s side of a split this file
        # contains both halves of. `subsumed` captures its tier/gate answers and
        # re-verifies them live only at the writer, because "mutable roster or
        # policy drift must not resurrect an already-recorded terminal";
        # `carried` re-measures on every replay because "a carried close is only
        # as live as its measurement". chain-proof belongs with `carried`: its
        # entire claim is that the DURABLE chain-plus-verdict record is the
        # proof, so a record the ledger no longer supports is not a stale
        # measurement, it is a false statement. The drift exposure is also
        # narrow — `approval_tier_for_verdict` resolves the FAMILY from the
        # verdict's own immutable author evidence, and the only live input is a
        # declared approval-tier prior, whose declaration means exactly "these
        # approvals no longer authorize". No git is probed here; the landing
        # proof stays the ladder's and the writer's, as `resolved` keeps it.
        if current is None or verdicts is None:
            return ("chain-proof needs its one dispatch snapshot to re-walk "
                    "the chain")
        repo, err = dispatches._clean(event.get("closing_repo_id"), "closing repo id",
                           4096)
        if err or not os.path.isabs(str(repo or "")):
            return "closing repo id must be an absolute path"
        if not dispatches._valid_trunk_ref(event.get("closing_trunk_ref")):
            return "closing trunk ref must be a named branch/remote ref"
        if not dispatches._FULL_TIP.fullmatch(str(event.get("closing_trunk_sha") or "")):
            return "closing trunk sha must be the pinned full commit id"
        merr = dispatches._close_mode_error(reason, event)
        if merr:
            return merr
        from . import landreq            # DEFERRED — landreq imports us.
        # THE RECORDED PATH IS FOLLOWED, NOT RE-DERIVED.
        # Re-running the write-time walk here would refuse the moment anyone
        # opened a parallel round afterwards, dissolving an append-only
        # terminal by a later append. Uniqueness was decided under the writer
        # lock and captured; replay verifies the captured edges are still
        # edges and that the capture is internally coherent.
        perr = landreq.chain_path_intact(
            state["id"], current, event.get("chain_path"),
            event.get("chain_fork_census"),
            cutoff=event.get("chain_census_cutoff"), position=position)
        if perr:
            return "chain-proof does not replay: " + perr
        frontier = current.get(str(event.get("carried_tip") or ""))
        if not isinstance(frontier, dict):
            return ("chain-proof names an authorizing row this ledger does "
                    "not carry")
        # THE IMMUTABLE RUNGS STILL RUN. Following the recorded path replaces
        # re-deriving UNIQUENESS, not re-deriving whether the frontier was ever
        # an authorizing approve at all — lifecycle, polarity, the gate token,
        # the writer's capability and the verdict order are ledger facts no
        # later append can retract, and dropping them let a hand-appended close
        # over an OPEN frontier replay as terminal.
        ferr = landreq.chain_frontier_error(
            frontier, current, verdicts, state["id"], event.get("chain_path"))
        if ferr:
            return "chain-proof does not replay: " + ferr
        proof = {"chain_path": event.get("chain_path")}
        ftip = str(frontier.get("reviewed_tip")
                   or frontier.get("ref") or "")
        if str(event.get("carried_base") or "") != ftip:
            return ("chain-proof base %s is not the authorizing descendant's "
                    "tip %s" % (str(event.get("carried_base"))[:12],
                                ftip[:12]))
        if str(event.get("carried_tip") or "") != str(frontier.get("id") or ""):
            return ("chain-proof names %s as the authorizing row, but this "
                    "ledger walks to %s"
                    % (str(event.get("carried_tip"))[:12],
                       str(frontier.get("id"))[:12]))
        # THE EDGES ARE THE PROOF, so they are the thing compared. A close that
        # records only endpoints says a transfer happened and cannot show WHICH
        # supersedes links carried it — and those links are the durable half,
        # the part that survives the history rewrite which took the objects.
        # Endpoint equality is NOT enough on its own: two different paths can
        # share a start and an end, and the malformed and forged cases both
        # live in between.
        if event.get("chain_path") != proof["chain_path"]:
            return ("the recorded supersedes path is not the path this ledger "
                    "walks — the edges ARE this proof and they disagree")
        # THE AUTHORITY IS VERIFIED STRUCTURALLY, NEVER RE-ASKED (a review's
        # ruling 2). Everything above re-walks IMMUTABLE ledger topology and is
        # free to disagree with the event. The tier is the opposite kind of
        # fact: it is MUTABLE POLICY, and re-resolving it here would let an
        # approval-tier edit made next month resurrect or dissolve a terminal
        # this ledger already recorded. So replay checks that the captured
        # decision is INTERNALLY COHERENT and unmodified, and refuses a
        # capture that does not bind its own fields.
        tier_state = event.get("chain_tier_state")
        if tier_state not in ("none", "ok", "unknown"):
            return ("chain-proof tier state must be none, ok or unknown — "
                    "%r was never a reading this door produces" % (tier_state,))
        attestation = event.get("chain_attestation")
        if tier_state == "unknown":
            # RULING ONE ON THE REPLAY SIDE: an UNMEASURABLE tier is admissible
            # only against the recorded act that admitted it. Absence here is
            # not an old row being generous to itself; it is a close claiming
            # an authority nobody ever took responsibility for.
            if not isinstance(attestation, dict):
                return ("chain-proof recorded an UNMEASURABLE approval tier "
                        "with no attestation — capability absence is not "
                        "authority, and this close names nobody who admitted it")
            from . import landreq
            if set(attestation) != set(landreq.CHAIN_ATTEST_FIELDS):
                return "chain-proof attestation schema violation (keys)"
            if attestation.get("v") != 1 \
                    or not dispatches._TOKEN.fullmatch(str(attestation.get("attester")
                                                or "")) \
                    or not str(attestation.get("evidence") or "").strip() \
                    or not str(attestation.get("ts") or "").strip():
                return ("chain-proof attestation must name the attesting seat, "
                        "what it read, and when")
        elif attestation is not None:
            return ("chain-proof carries an attestation on a MEASURED tier, "
                    "where nothing asked for a judgement")
        from . import landreq
        anchor = landreq._chain_authority_anchor(
            frontier, tier_state, event.get("chain_gate_requirement"),
            attestation)
        if event.get("chain_authority_anchor") != anchor:
            return ("the recorded authority capture does not bind its own "
                    "fields — a decision made against a world that has since "
                    "moved may still be re-read, but never re-written")
    return None



def _close_idempotent(reason, row, event):
    """True when a second identical close is a RETRY of the standing one.

    Per-reason identity, deliberately narrow:
      landed      closing repo + trunk REF only — the sha is EXCLUDED so a
                  retry after trunk movement reconciles instead of refusing
                  (the recorded pin stays the historical anchor)
      superseded  superseding tip + evidence
      withdrawn   evidence
      stranded    closing repo + evidence
      expired     closing repo + evidence; the moving control sha and the
                  live-re-derived hold kind are EXCLUDED
      endorsement-moot  closing repo + trunk REF + evidence; the moving trunk
                  sha and the live-re-derived hold kind are EXCLUDED
      delivered-report artifact ref + report ref + evidence
      subsumed    canonical confirmation + captured authorization + repo/trunk
                  ref; selector spelling and moving trunk sha are excluded"""
    if row.get("close_reason") != reason:
        return False
    if reason == "landed":
        if row.get("close_proof_version") == 3 or event.get("close_proof_version") == 3:
            return all(row.get(k) == event.get(k) for k in (
                "close_proof_version", "compose_land_proof", "compose_land_anchor",
                "closing_repo_id", "closing_trunk_ref"))
        if row.get("landing_review_id") or event.get("landing_review_id"):
            stable = ("landing_review_id", "landing_review_tip",
                      "landing_review_verdict_anchor",
                      "landing_review_tier_state",
                      "landing_review_gate_requirement", "landing_review_gate",
                      "landing_review_approval_anchor", "closing_repo_id",
                      "closing_trunk_ref")
            return all(row.get(key) == event.get(key) for key in stable)
        return row.get("closing_repo_id") == event.get("closing_repo_id") \
            and row.get("closing_trunk_ref") == event.get("closing_trunk_ref")
    if reason == "superseded":
        return row.get("superseding_tip") == event.get("superseding_tip") \
            and row.get("close_evidence") == event.get("close_evidence")
    if reason == "withdrawn":
        return row.get("close_evidence") == event.get("close_evidence")
    if reason == "stranded":
        return row.get("closing_repo_id") == event.get("closing_repo_id") \
            and row.get("close_evidence") == event.get("close_evidence")
    if reason == "delivered-report":
        return all(row.get(key) == event.get(key)
                   for key in dispatches._DELIVERED_REPORT_PAYLOAD_FIELDS)
    if reason == "chain-proof":
        # THE RETRY DOOR THE REASON SHIPPED WITHOUT. Falling through
        # to the `return False` below made an IDENTICAL re-run of a proven
        # close read as a DIFFERENT closure and take the retired-once refusal —
        # so a caller whose first write succeeded but whose answer was lost
        # could never reconcile. The moving trunk SHA is excluded for the same
        # reason `landed` excludes it: a retry after trunk moved is the same
        # close, and the recorded pin stays the historical anchor.
        # close_proof_mode is DELIBERATELY ABSENT, and a suite-wide rule is
        # why: `tests.test_lr_close.ProofPairAdmissionReachesEveryDoorTest`
        # forbids any function that INSPECTS close_proof_mode from deciding a
        # proof question without reaching `_content_proof_pair_error`. I put it
        # in this tuple and the rule caught it. It also bought nothing —
        # `_close_event_error` pins chain-proof's mode to the literal
        # "chain-proof" unconditionally, so two chain-proof closes can never
        # differ on it. `landed` and `subsumed` leave it out for the same
        # reason.
        # THE CENSUS AND ITS CUTOFF ARE EXCLUDED, for the reason `landed`
        # excludes the moving trunk sha: they are the WRITE-TIME MEASUREMENT,
        # not the closure. A retry necessarily re-derives a LATER cutoff — the
        # ledger grew by at least the standing close — and after any later fork
        # a re-derived census differs too. Comparing them made every honest
        # retry read as "a different closure". The recorded pair stays the
        # historical anchor and a retry returns the STANDING row untouched.
        stable = ("closing_repo_id", "closing_trunk_ref", "carried_base",
                  "carried_tip", "chain_path", "close_evidence",
                  # THE CAPTURED AUTHORITY IS IDENTITY, not decoration: a
                  # retry that would record a DIFFERENT tier reading, or newly
                  # carry an attestation the standing close never had, is a
                  # different closure wearing the same evidence.
                  "chain_tier_state", "chain_gate_requirement",
                  "chain_attestation", "chain_authority_anchor")
        return all(row.get(key) == event.get(key) for key in stable)
    if reason == "expired":
        # THE RETRY DOOR THIS REASON ALSO SHIPPED WITHOUT. Falling through to
        # `return False` below made a concurrent or lost-response retry of a
        # proven expiry read as a DIFFERENT closure and take the retired-once
        # refusal, so the caller could never reconcile — `chain-proof` records
        # the identical hole three branches above.
        #
        # THE MOVING TRUNK SHA IS EXCLUDED, for the reason `landed` excludes
        # it: a retry after trunk moved is the same close, and the recorded pin
        # stays the historical anchor. THE HOLD KIND IS EXCLUDED for the same
        # shape of reason — it is re-derived live at every write, so comparing
        # it would make a policy edit between two honest retries read as two
        # different closures. What remains is what the closure IS: this
        # repository, proving this row's absent tip, with this measured line.
        return row.get("closing_repo_id") == event.get("closing_repo_id") \
            and row.get("close_evidence") == event.get("close_evidence")
    if reason == "endorsement-moot":
        # THE RETRY DOOR, WRITTEN AT BIRTH rather than after the first lost
        # response. Both reasons above record this hole as one they shipped
        # with: falling through to `return False` below makes an IDENTICAL
        # re-run of a proven close read as a DIFFERENT closure and take the
        # retired-once refusal, so a caller whose write succeeded and whose
        # answer was lost can never reconcile.
        #
        # THE TRUNK REF STAYS AND THE TRUNK SHA GOES, which is the split
        # `landed` made and for its reason: a retry after trunk MOVED is the
        # same close and the recorded pin is the historical anchor, while the
        # REF names which trunk the claim was ever about and a retry naming a
        # different one is a different claim. THE HOLD KIND IS EXCLUDED
        # because the lock re-derives it at every write, so comparing it would
        # make a record edit between two honest retries read as two closures.
        return row.get("closing_repo_id") == event.get("closing_repo_id") \
            and row.get("closing_trunk_ref") == event.get("closing_trunk_ref") \
            and row.get("close_evidence") == event.get("close_evidence")
    if reason == "subsumed":
        stable = ("confirmation_id", "confirmation_tip", "confirmation_ref",
                  "original_author", "confirmation_recipient",
                  "original_author_family", "confirmation_recipient_family",
                  "original_verdict_anchor", "confirmation_verdict_anchor",
                  "original_family_evidence", "confirmation_family_evidence",
                  "original_family_anchor", "confirmation_family_anchor",
                  "confirmation_tier_state", "confirmation_gate_requirement",
                  "confirmation_gate", "confirmation_approval_anchor",
                  "original_proof_mode", "closing_repo_id",
                  "closing_trunk_ref")
        return all(row.get(key) == event.get(key) for key in stable)
    return False



def _writer_landed(current, tip):
    """The discharged door's live landed leg: the discharging row's own
    terminal names the repo and trunk (a `landed` close, or the legacy
    close-landed closure); the reviewed tip must be an ancestor of it NOW.
    UNKNOWN (no terminal, no probe answer) fails closed."""
    for cand in (current or {}).values():
        if not isinstance(cand, dict):
            continue
        if str(cand.get("reviewed_tip") or "") != str(tip):
            continue
        if cand.get("close_reason") == "landed":
            return dispatches._tip_on_trunk(cand.get("closing_repo_id"),
                                 cand.get("closing_trunk_ref"), tip)
        if cand.get("closed_by_landing"):
            return dispatches._tip_on_trunk(cand.get("landing_repo_id"),
                                 cand.get("landing_trunk_ref"), tip)
    return None



def _record_close_proven(rid, reason, reviewed_tip, evidence=None,
                         carried_base=None, carried_tip=None,
                         closing_repo_id=None, closing_trunk_ref=None,
                         closing_trunk_sha=None, proof_mode=None,
                         translated_tip=None, content_witness=None,
                         content_witness_anchor=None, superseding_tip=None,
                         superseding_id=None, contrary_state=None,
                         contrary_target=None, absence_trunk_ref=None,
                         absence_trunk_sha=None, control_sha=None,
                         withdrawing_seat=None,
                         confirmation_id=None, confirmation_tip=None,
                         confirmation_ref=None, original_author=None,
                         confirmation_recipient=None,
                         original_author_family=None,
                         confirmation_recipient_family=None,
                         original_verdict_anchor=None,
                         confirmation_verdict_anchor=None,
                         original_family_evidence=None,
                         confirmation_family_evidence=None,
                         original_family_anchor=None,
                         confirmation_family_anchor=None,
                         confirmation_tier_state=None,
                         confirmation_gate_requirement=None,
                         confirmation_gate=None,
                         confirmation_approval_anchor=None,
                         original_proof_mode=None, close_proof_version=1,
                         landing_review_id=None, landing_review_tip=None,
                         landing_review_verdict_anchor=None,
                         landing_review_tier_state=None,
                         landing_review_gate_requirement=None,
                         landing_review_gate=None,
                         landing_review_approval_anchor=None,
                         delivery_class=None, delivery_restart=None,
                         artifact_ref=None, report_ref=None,
                         discharging_id=None, discharging_tip=None,
                         discharge_tier=None, contradiction=None,
                         chain_path=None, chain_fork_census=None,
                         chain_census_cutoff=None, chain_tier_state=None,
                         chain_gate_requirement=None, chain_attestation=None,
                         chain_authority_anchor=None,
                         compose_manifest=None, compose_gate=None,
                         compose_landed_by=None, dry_run=False):
    """Append ONE proven `close` event without rewriting its verdict.

    landreq owns every live Git/liveness proof; this boundary re-validates
    the immutable row state per reason and records exactly one terminal
    annotation. Identical retries are idempotent under the per-reason
    identity; everything else refuses. The git proofs necessarily ran
    OUTSIDE the lock (seconds-wide window) — the row-STATE facts they
    depended on are all re-read here, on a fold the append then proves is
    still the ledger (`dispatches._ledger_write`), and the recorded pinned
    trunk sha keeps a proof that went stale-but-was-true auditable. The scoped
    compose-land v3 path and the `discharged` re-walk re-measure their live
    Git/gate inputs UNDER the lock (`txn.lock()` before them); callers supply
    candidate artifacts, never a trusted protected set. A `dry_run` runs
    every one of these checks without the lock and appends nothing."""
    reviewed = str(reviewed_tip or "").strip().lower()
    build_landed = reason == "landed" and close_proof_version == 2
    compose_landed = reason == "landed" and type(close_proof_version) is int \
        and close_proof_version == 3
    if compose_landed:
        from . import compose_contract
        err = compose_contract.writer_error()
        if err:
            return None, err
    elif compose_manifest is not None or compose_gate is not None or compose_landed_by is not None:
        return None, "compose evidence belongs only to landed proof version 3"
    delivered_report = reason == "delivered-report"
    discharged = reason == "discharged"
    # A CARRIED CLOSE ON A NEVER-VERDICTED ROW HAS NO REVIEWED TIP TO GIVE.
    # Only a VERDICT writes `reviewed_tip`, so on a rung that never got one
    # the ladder passes None here — while its own proof has already pinned
    # the commit in `carried_base`/`carried_tip`, which the replay arm
    # re-derives and compares against the recorded pair. So the identity is
    # proven by the fields this reason actually carries, and demanding a
    # reviewed tip demands a review that never happened.
    #
    # `carried` ARRIVED AFTER THIS LIST WAS WRITTEN (task/756) and was never
    # added to it, which is the whole defect: `_close_ladder_carried` derived
    # a valid carriage proof and then refused at the write, so every dry run
    # said the door would open and every real close said it would not. The
    # exemption is NARROW — the tip stays required whenever one is supplied,
    # so a verdicted row still binds its reviewed tip rather than its filed
    # one (tests/test_lr_close.py, the REGRESSION CONTROL arm).
    carried_unverdicted = reason == "carried" and not reviewed
    if not build_landed and not delivered_report and not discharged \
            and not carried_unverdicted \
            and not dispatches._FULL_TIP.fullmatch(reviewed):
        return None, "close needs the original full reviewed commit id"
    if reason not in dispatches.CLOSE_REASONS:
        return None, "close reason must be one of %s" % "/".join(dispatches.CLOSE_REASONS)
    if type(close_proof_version) is not int \
            or close_proof_version not in ((1, 2, 3) if reason == "landed" else (1,)):
        return None, "close proof version is not valid for this reason"
    if evidence is not None and not delivered_report:
        evidence, err = dispatches._clean(evidence, "close evidence", 256)
        if err:
            return None, err
    if delivered_report:
        values, err = dispatches.clean_delivered_report_refs(
            artifact_ref, report_ref, evidence)
        if err:
            return None, err
        artifact_ref, report_ref, evidence = values
    if closing_repo_id is not None \
            and os.path.realpath(closing_repo_id) != closing_repo_id:
        return None, "close needs a canonical absolute Git common-dir"
    candidate = {"v": 3, "event": "close", "id": None, "ts": None,
                 "close_reason": reason,
                 "close_proof_version": close_proof_version}
    if build_landed:
        candidate.update(
            landing_review_id=landing_review_id,
            landing_review_tip=landing_review_tip,
            landing_review_verdict_anchor=landing_review_verdict_anchor,
            landing_review_tier_state=landing_review_tier_state,
            landing_review_gate_requirement=landing_review_gate_requirement,
            landing_review_gate=landing_review_gate,
            landing_review_approval_anchor=landing_review_approval_anchor,
            translated_tip=translated_tip,
            # Unconditionally, both keys — a v2 row written from here always
            # carries its delivery answer, so "absent" can only ever mean
            # "written before this field existed", never "we forgot".
            close_delivery_class=delivery_class,
            close_delivery_restart=delivery_restart)
    elif delivered_report:
        candidate["close_evidence"] = evidence
    elif discharged:
        # A polarity-less row has no reviewed tip of its own; the event's
        # authority is the discharging row's identity, carried explicitly.
        if evidence is not None:
            candidate["close_evidence"] = evidence
    else:
        candidate["reviewed_tip"] = reviewed
        if evidence is not None:
            candidate["close_evidence"] = evidence
    for key, value in (("closing_repo_id", closing_repo_id),
                       ("closing_trunk_ref", closing_trunk_ref),
                       ("closing_trunk_sha", closing_trunk_sha),
                       # THE PAIR WAS ACCEPTED AS KWARGS AND SILENTLY DROPPED
                       # (task/close-ladder). `_close_event_error` re-derives
                       # the pair and compares it to these two fields, so
                       # events that never carried them compared a real sha
                       # against absent and the writer refused ITS OWN close.
                       # `carried` therefore never wrote once: 390 closes in
                       # the live ledger across eight reasons, zero carried.
                       # Only a dry run had ever been exercised, and a dry run
                       # appends nothing so it never reaches this validator —
                       # the same blindness `close_proof_mode=content-
                       # equivalent` was found under (task/777).
                       # `carried_base` is legitimately absent under the
                       # reached-trunk witness, which derives its own range;
                       # the None-skip below is what records that honestly
                       # instead of inventing a base to fill the field.
                       ("carried_base", carried_base),
                       ("carried_tip", carried_tip),
                # THE PATH TRAVELS WITH THE ENDPOINTS. A reader that has only
                # base and tip knows an authority transfer was claimed and
                # cannot re-walk the edges that carried it — and those edges
                # are the durable half of this proof, the part that survives
                # the history rewrite which took the objects.
                ("chain_path", chain_path),
                ("chain_fork_census", chain_fork_census),
                ("chain_census_cutoff", chain_census_cutoff),
                ("chain_tier_state", chain_tier_state),
                ("chain_gate_requirement", chain_gate_requirement),
                ("chain_attestation", chain_attestation),
                ("chain_authority_anchor", chain_authority_anchor),
                       ("close_proof_mode", proof_mode),
                       ("translated_tip", translated_tip),
                       ("superseding_tip", superseding_tip),
                       ("superseding_id", superseding_id),
                       ("close_contrary_state", contrary_state),
                       ("close_contrary_target", contrary_target),
                       ("absence_trunk_ref", absence_trunk_ref),
                       ("absence_trunk_sha", absence_trunk_sha),
                       # WHO WITHDREW. Optional by construction, like the
                       # delivery pair: every historical withdrawn close
                       # carries none and must keep replaying, so absence
                       # reads UNRECORDED and never a seat.
                       ("withdrawing_seat", withdrawing_seat),
                       ("control_sha", control_sha),
                       ("confirmation_id", confirmation_id),
                       ("confirmation_tip", confirmation_tip),
                       ("confirmation_ref", confirmation_ref),
                       ("original_author", original_author),
                       ("confirmation_recipient", confirmation_recipient),
                       ("original_author_family", original_author_family),
                       ("confirmation_recipient_family",
                        confirmation_recipient_family),
                       ("content_witness", content_witness),
                       ("content_witness_anchor", content_witness_anchor),
                       ("original_verdict_anchor", original_verdict_anchor),
                       ("confirmation_verdict_anchor",
                        confirmation_verdict_anchor),
                       ("original_family_evidence", original_family_evidence),
                       ("confirmation_family_evidence",
                        confirmation_family_evidence),
                       ("original_family_anchor", original_family_anchor),
                       ("confirmation_family_anchor",
                        confirmation_family_anchor),
                       ("confirmation_tier_state", confirmation_tier_state),
                       ("confirmation_gate_requirement",
                        confirmation_gate_requirement),
                       ("confirmation_gate", confirmation_gate),
                       ("confirmation_approval_anchor",
                        confirmation_approval_anchor),
                       ("original_proof_mode", original_proof_mode),
                       ("close_delivery_class", delivery_class),
                       ("close_delivery_restart", delivery_restart),
                       ("artifact_ref", artifact_ref),
                       ("report_ref", report_ref),
                       ("discharging_id", discharging_id),
                       ("discharging_tip", discharging_tip),
                       ("discharge_tier", discharge_tier)):
        if key in dispatches._CLOSE_STATE_FIELDS[reason] and value is not None:
            candidate[key] = value
    # The contradicted-withdrawal discharge proof, captured whole from the
    # door's `_retirement_stands` re-derivation (landreq threads it through);
    # the same reason-tuple gate as above, so a reason outside the discharge
    # doors drops it and the retired-once law below refuses verbatim.
    for key in dispatches._CONTRADICTION_PROOF_FIELDS:
        if key in dispatches._CLOSE_STATE_FIELDS[reason] \
                and (contradiction or {}).get(key) is not None:
            candidate[key] = contradiction[key]
    if compose_landed:
        candidate["close_delivery_restart"] = delivery_restart
    # THE ACTOR IS EXCLUDED FROM EVERY EXACT-SET SCHEMA CHECK, because it is
    # OPTIONAL BY CONSTRUCTION: the writer stamps it only when this process
    # declares a seat, so `set(candidate)` would otherwise differ between a
    # seated writer and an unseated one and the equality would be a statement
    # about the environment rather than about the proof shape. The actor is
    # provenance, orthogonal to every proof these schemas pin.
    _actorless = set(candidate) - {"close_actor"}
    if build_landed:
        expected = dispatches._BUILD_LANDED_EVENT_FIELDS - {"seq"} - dispatches._CONTENT_PROOF_FIELDS
        if _actorless - dispatches._CONTENT_PROOF_FIELDS != expected:
            missing = sorted(expected - _actorless)
            extra = sorted(_actorless - expected)
            return None, ("close does not bind: close candidate fields do not "
                          "match build-landed schema (missing=%s extra=%s)" % (
                              ",".join(missing) or "-",
                              ",".join(extra) or "-"))
    if delivered_report:
        expected = dispatches._DELIVERED_REPORT_EVENT_FIELDS - {"seq"}
        if _actorless != expected:
            missing = sorted(expected - _actorless)
            extra = sorted(_actorless - expected)
            return None, ("close does not bind: close candidate fields do not "
                          "match delivered-report schema (missing=%s extra=%s)" % (
                              ",".join(missing) or "-",
                              ",".join(extra) or "-"))
    if reason == "subsumed":
        expected = (dispatches._CLOSE_EVENT_BASE_FIELDS
                    | set(dispatches._CLOSE_STATE_FIELDS[reason])) - {"seq"}
        if _actorless != expected:
            missing = sorted(expected - _actorless)
            extra = sorted(_actorless - expected)
            return None, ("close does not bind: close candidate fields do not "
                          "match %s schema (missing=%s extra=%s)" % (
                              reason, ",".join(missing) or "-",
                              ",".join(extra) or "-"))
    path = dispatches.ledger_path()
    # A REHEARSAL NEVER TAKES THE LEDGER LOCK. Every check below reads the
    # fold without it (`dispatches._ledger_write`), and a dry run returns its
    # rehearsal before any append, so it neither takes the lock nor reaches
    # the locked last try. The /api/lr rebuild asks `off_frontier_closable` a
    # rehearsal per row; a rehearsal that held the lock made that rebuild hold
    # it without a break while every fleet `dispatch send` and `dispatch
    # verdict` waited behind questions that can never append. A real close
    # runs the same checks and appends only if the ledger is still the one
    # they read.
    given_candidate = candidate

    def attempt(txn):
        # EACH TRY STARTS FROM THE CALLER'S CANDIDATE: the checks below stamp
        # fields onto it, and a try that read a ledger since moved must not
        # hand those stamps to the next.
        candidate = dict(given_candidate)
        if not txn.held:
            return None, "ledger unwritable (%s) — close NOT recorded" % path
        current, verdicts, unavailable = dispatches.snapshot_with_verdicts()
        if unavailable:
            return None, "dispatch ledger unavailable: %s" % unavailable
        row, err = dispatches._resolve_row(current, rid)
        if err:
            return None, err
        if compose_landed:
            # THE RE-MEASUREMENT RUNS UNDER THE LOCK, as it always has: `lock`
            # proves the snapshot above is the ledger now held, and from here
            # to the append is one critical section. A rehearsal takes no lock.
            if not dry_run and not txn.lock():
                return None, "ledger unwritable (%s) — close NOT recorded" % path
            # Do not trust a caller's empty protected set, tree or gate claim.
            # Re-measure candidates against this lock's canonical row snapshot.
            # A retry validates the same captured composition at its ORIGINAL
            # pin. Today's moved trunk is not a replacement for historical proof.
            pin = row.get("closing_trunk_sha") if row.get("close_reason") == "landed" \
                and row.get("close_proof_version") == 3 else closing_trunk_sha
            authority = (current, verdicts, dispatches.gate_epoch(current, verdicts))
            if compose_landed_by is not None:
                inherited, err = compose_contract.peer_pin(
                    row, current, compose_landed_by, closing_repo_id,
                    closing_trunk_ref, compose_manifest, compose_gate, authority=authority)
                if err or inherited != pin:
                    return None, err or "scoped sibling pin differs from recorded landing"
            proof, err = compose_contract.capture(
                row, current, closing_repo_id, pin, compose_manifest, compose_gate,
                authority=authority)
            if err:
                return None, "close does not bind: " + err
            candidate.update(compose_land_proof=proof,
                             compose_land_anchor=dispatches._proof_anchor("compose-land-v1", proof))
        if reason == "subsumed":
            selected, err = dispatches._resolve_row(
                current, evidence, noun="confirmation dispatch",
                list_hint="helm dispatch list")
            if err:
                return None, "close does not bind: %s" % err
            if selected["id"] != confirmation_id:
                return None, ("close does not bind: confirmation selector no "
                              "longer identifies the captured confirmation")
        # THE ACTOR, ON EVERY CLOSE, STAMPED BY THE LOCK AND NEVER TYPED.
        # Measured across 14342 ledger rows: ONLY the `dispatch` event records
        # who acted. Every terminal — close, cancel, discharge, withdraw,
        # abandon, close-landed — records nobody, and the single exception is
        # `withdrawing_seat` on 32 of 846 closes. So "which seat closed this
        # row" has never been answerable, and the fleet's own usage census
        # could report the reason and the repo and not the hand.
        #
        # RESOLVED, NEVER PASSED. `_acting_seat` reads this process's declared
        # identity for the same reason the attestation does: letting a caller
        # supply the name would make the accountable half of the record the
        # one part a forger controls. A process with no declared seat records
        # nothing rather than guessing — an absent actor is honest, an
        # invented one is worse than none.
        from . import landreq as _landreq   # DEFERRED — landreq imports us.
        actor = _landreq._acting_seat()
        if actor:
            candidate["close_actor"] = actor

        if reason in ("expired", "endorsement-moot"):
            # THE HOLD KIND IS THE LOCK'S, NEVER THE CALLER'S — the same law
            # `discharged` states one branch below about its tier. The ladder
            # classified this row against a projection built outside the lock;
            # a verdict appended since is in the ledger NOW, and the terminal
            # must record the authority that was re-derived at the write. It is
            # stamped HERE, before the mode rule and before the idempotence
            # fork, so both paths judge the same bytes.
            from . import landreq        # DEFERRED — landreq imports us.
            candidate["close_hold_kind"] = landreq.recorded_hold_kind(row)

        # VALIDATE BEFORE RECONCILE. The idempotence
        # check below is an IDENTITY test, not a validator, and it used to be
        # the FIRST thing a retry met — so a payload the open-row door refuses
        # was reconciled into SUCCESS purely because the row was already
        # retired. Same bytes, two answers, decided by lifecycle position.
        # The mode rule runs here, before that fork, so both paths agree.
        merr = dispatches._close_mode_error(reason, candidate)
        if merr:
            return None, "close does not bind: " + merr
        retired = dispatches._close_retired_by(row)
        if retired:
            if dispatches._close_idempotent(reason, row, candidate):
                return row, None
            # A discharging close REBINDS its state legs from THIS locked
            # read before anything judges it — a chain verdict that landed
            # after the door's read is seen here, and the write refuses or
            # binds on the FRESH truth. The shared validator then re-checks
            # the whole rebound capture; a refusal there that is not the
            # bare retired-once falls through to its NAMED message.
            if contradiction and reason in dispatches._CONTRADICTION_DISCHARGE_REASONS:
                rerr = dispatches._rebind_contradiction(candidate, row, current)
                if rerr:
                    return None, "close does not bind: %s" % rerr
            if dispatches._contradiction_discharge_error(candidate, row, reason,
                                              current) == dispatches._ALREADY_RETIRED:
                return None, ("dispatch %s is already retired by %s; a row is "
                              "retired once — refusing a different closure"
                              % (row["id"], retired))
        if reason == "chain-proof":
            # THE CUTOFF IS READ HERE AND NOWHERE ELSE. It needs the ledger's
            # event COUNT, which the folded snapshot does not carry — and
            # reading around `snapshot_with_verdicts` to get it silently
            # disabled `test_locked_writer_rechecks_selector_uniqueness`, which
            # injects THROUGH that function. The seam stays; the extra read is
            # confined to the one reason that needs a cutoff rather than
            # charged to every close. A DRY RUN reads it with nothing to bind
            # it, so an append between the two reads can make its preview
            # cutoff one event ahead of its census; a real close appends only
            # if the ledger has not moved since before both reads, so for it
            # they cannot differ.
            locked_events, cerr = eventledger.checked_events(dispatches.ledger_path())
            if cerr:
                return None, "dispatch ledger unavailable: %s" % cerr
            census_cutoff = len(locked_events)
            # RE-DERIVED HERE, OVERWRITING THE CALLER — the same law
            # `discharged` states one branch below ("THE TIER IS THE LOCK'S,
            # NEVER THE CALLER'S"). The ladder walked an unlocked snapshot; a
            # sibling appended since then is in the ledger NOW, and a census
            # that predates it would record one child for a node that has two.
            # The cutoff travels with it so replay can judge a later sibling by
            # RECORD rather than by re-deriving a uniqueness it must not touch.
            from . import landreq
            candidate = dict(
                candidate,
                chain_fork_census=landreq.chain_fork_census(
                    row["id"], current, candidate.get("chain_path")),
                chain_census_cutoff=census_cutoff)
        event = dict(candidate, id=row["id"], seq=row["seq"] + 1,
                     ts=pk.now_ts())
        if reason == "discharged":
            # UNDER THE LOCK FROM HERE (`lock` proves the snapshot above is the
            # ledger held), so the live probe below answers at the write.
            if not dry_run and not txn.lock():
                return None, "ledger unwritable (%s) — close NOT recorded" % path
            # WRITER-SIDE re-walk under the lock, with the live git probe the
            # replay arm cannot run: the discharging row's recorded landing
            # names repo + trunk, and the reviewed tip must STILL be an
            # ancestor of it. The candidate's fields then go through
            # _close_event_error like every other reason, which re-walks the
            # same ledger with the content-addressed landed leg.
            tier, by, why = dispatches.discharging_row(
                row["id"], current,
                is_landed=lambda tip: dispatches._writer_landed(current, tip),
                prefer=str(candidate.get("discharging_id") or ""))
            if tier is None:
                return None, "close does not bind: discharge refused — %s" % why
            if by != str(candidate.get("discharging_id") or ""):
                return None, ("close does not bind: the ledger says %s "
                              "discharges this row, not %s"
                              % (str(by)[:12],
                                 str(candidate.get("discharging_id"))[:12]))
            # THE TIER IS THE LOCK'S, NEVER THE CALLER'S. The ladder walked
            # outside the lock; a chain edge can be appended between that walk
            # and this one, and the event must record the authority that was
            # actually re-derived at write, not the one the caller happened to
            # see. Same law as every other field on this door.
            event["discharge_tier"] = tier
            dis_row = current.get(by) or {}
            if str(dis_row.get("reviewed_tip") or "") != \
                    str(candidate.get("discharging_tip") or ""):
                return None, ("close does not bind: discharging tip does not "
                              "match the discharging row's reviewed tip")
        err = dispatches._close_event_error(
            event, row, current=current, verdicts=verdicts,
            verify_families=reason == "subsumed" or build_landed)
        if err:
            return None, "close does not bind: %s" % err
        if dry_run:
            # A REHEARSAL AND A REAL CLOSE DIFFER IN NOTHING BUT THE
            # WRITE, which is the whole of task/2857. A ladder that answers
            # the rehearsal from its own proof, and returns before this
            # function runs, reports WOULD close for a row this writer
            # refuses -- the instrument lying in the direction an operator
            # acts on. The measured shape: a concur-polarity row whose work
            # IS carried, where the ladder's proof succeeds and the writer
            # refuses "verdict polarity outside --reason carried's domain".
            # On the board that state held 84 of 469 sweep candidates, so
            # trusting the rehearsal would have produced 84 refusals
            # mid-batch. The ladder's proof was never the thing that was
            # wrong: the row-STATE checks it skipped all live here, and a
            # ladder that answers before this function runs skips them all.
            # A rehearsal runs them on an unlocked snapshot (see above), so
            # its answer is a reading at one moment; the real close runs
            # the same checks and appends only if the ledger is still the
            # one they read.
            #
            # THE RESULT IS THE EVENT ITSELF, minus the two fields only an
            # append can fill. A rehearsal that reported a hand-built
            # summary could drift from what would actually be written --
            # the same class of defect one layer up -- and this cannot: the
            # caller is shown the exact candidate that just passed
            # `_close_event_error`. `proof_mode` is carried beside
            # `close_proof_mode` because the ladders spelled it the short
            # way and the dry-run printer already reads either.
            rehearsal = {k: v for k, v in event.items()
                         if k not in ("v", "event", "id", "ts")}
            rehearsal.update(dry_run=True, id=row["id"], reason=reason)
            if event.get("close_proof_mode") is not None:
                rehearsal["proof_mode"] = event["close_proof_mode"]
            return rehearsal, None
        if not txn.append(event):
            return None, "ledger unwritable (%s) — close NOT recorded" % path

        def finish():
            out = dispatches._apply(row, event, current=current, verdicts=verdicts)
            pk.event("dispatch-close", row["id"], reason)
            return out, None
        return txn.then(finish)
    return dispatches._ledger_write(attempt, path)



def record_delivered_report_correction(rid, artifact_ref, report_ref, evidence):
    """Explicitly correct one historical cancelled BUILD to delivered-report.

    The original cancel event and reason remain in history and on the projected
    row. No wording is interpreted: only this append-only event, carrying fresh
    artifact identity and chat handoff references, changes the canonical
    terminal. Identical retries reconcile; any different annotation conflicts."""
    values, err = dispatches.clean_delivered_report_refs(
        artifact_ref, report_ref, evidence)
    if err:
        return None, err
    artifact_ref, report_ref, evidence = values
    path = dispatches.ledger_path()

    def attempt(txn):
        if not txn.held:
            return None, ("ledger unwritable (%s) — delivered-report correction "
                          "NOT recorded" % path)
        current, unavailable = dispatches.snapshot()
        if unavailable:
            return None, "dispatch ledger unavailable: %s" % unavailable
        row, err = dispatches._resolve_row(current, rid)
        if err:
            return None, err
        if row.get("delivered_report_correction"):
            values = (artifact_ref, report_ref, evidence)
            if all(row.get(key) == value for key, value in zip(
                    dispatches._DELIVERED_REPORT_PAYLOAD_FIELDS, values)):
                return row, None
            return None, ("dispatch %s already has a delivered-report correction; "
                          "refusing different artifact/report evidence" % row["id"])
        if row.get("close_reason"):
            return None, ("dispatch %s is already retired by close --reason %s; "
                          "a historical correction only annotates a cancellation"
                          % (row["id"], row["close_reason"]))
        event = {"v": 3, "event": "close-correction",
                 "seq": row["seq"] + 1, "id": row["id"], "ts": pk.now_ts(),
                 "close_reason": "delivered-report", "close_proof_version": 1,
                 "artifact_ref": artifact_ref, "report_ref": report_ref,
                 "close_evidence": evidence, "corrects_event": "cancel",
                 "corrects_reason": row.get("cancel_reason")}
        err = dispatches._delivered_report_event_error(event, row, correction=True)
        if err:
            return None, "delivered-report correction does not bind: %s" % err
        if not txn.append(event):
            return None, ("ledger unwritable (%s) — delivered-report correction "
                          "NOT recorded" % path)

        def finish():
            out = dispatches._apply(row, event)
            pk.event("dispatch-close-correction", row["id"], "delivered-report")
            return out, None
        return txn.then(finish)
    return dispatches._ledger_write(attempt, path)


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
