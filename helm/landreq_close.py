"""Closing a land request: the ladder that retires a row, and the chain walk.

WHY A ROW STOPS BEING OWED, which is a different question from what the
ledger knows about a landing. The close ladder asks it one reason at a time
-- landed, carried, superseded, subsumed, resolved, withdrawn, expired -- and
the chain walk is what several of those rungs consult to decide whether
anyone downstream still owes anything.

THE CODE MOVED WHOLE, AND EVERY LEDGER NAME IS SPELLED `landreq.NAME` AT ITS
CALL SITE, for the reason `landreq_cli` gives at length: an import list binds
each object once at import, and a test that patches the ledger and drives a
rung would then reach the original here and measure nothing. Names this
module owns are spelled the same way, because they are published back onto
`landreq` and arms patch them there.

THE REMAINDER STILL CALLS IN, which is the difference from `landreq_cli`:
fifteen ledger-side names reach these rungs. That is safe, and it is measured
rather than assumed -- every one of those references sits inside a function
body and none in the module body, so they resolve at CALL time, after the
tail import and the publish loop have bound the names back.
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

from . import landreq
from . import dispatches, foldcheck, foldcompose, home, pk, projscope, query, vcs
from .seats_common import recipient_matches
from .store import load as store_load
from .work import _lanes


def _chain_repo(row):
    """The writer's repository compatibility rule, without rewriting history.

    AN ABSENT repo_id IS THE CANONICAL LEGACY HOME, which is why naming it is
    safe rather than a guess. Those rows were written before the field
    existed, by the one helm home that served the fleet, so
    `dispatches.home_repo_id()` — the SAME identity the write door stamps on
    every row it accepts now — is the only repository they can honestly
    belong to. Reading it is what keeps a legacy round joined to its own
    successors instead of floating away from the chain it is part of.

    IT NEVER INVENTS ONE. Where that call refuses — a helm package outside a
    Git working tree establishes no identity, and says so — the row gets no
    chain key at all, its chain reads UNKNOWN, and nothing is merged with
    anything. A PRESENT-but-empty repo_id is damaged evidence and takes the
    same road. The read costs two Git subprocesses per uncached call and is
    paid once per such row per index build; the population is CLOSED — the
    door stamps a repository on every row it accepts now — so it only
    shrinks."""
    repo = row.get("repo_id")
    if repo is None:
        repo, _why = dispatches.home_repo_id()
    # ABSENT is the legacy home repository; PRESENT empty is damaged evidence.
    return dispatches._real(repo) if isinstance(repo, str) and repo else None

def chain_key(row):
    """(repo, chain_root) — the identity a CHAIN is scoped by, or None.

    THE BOUND REPOSITORY PLUS THE CHAIN ROOT, NEVER THE LANE LABEL, which is
    the same seal `contest_index` states one screen up: a lane is renamable
    free text, so a chain continued under a new label would split into two
    identities and two chains that happen to share a label would collapse into
    one. Only an ABSENT legacy root names itself by its own id. UNKNOWN is
    unreadable identity, never a new root or a shared bucket for damaged rows.

    IT READS A RAW DISPATCH ROW AND A PROJECTED ONE INTERCHANGEABLY, because
    `_lr` copies `chain_root`, `repo_id` and `id` through unchanged. One key
    function for both shapes is what stops the ledger side and the board side
    from disagreeing about which rows are the same piece of work.

    A row with NEITHER a root nor an id has no work identity at all and joins
    to nothing. A ledger row with damaged identity instead fails UNKNOWN."""
    return landreq._chain_key(row, landreq._chain_repo(row))

def _chain_key(row, repo):
    """Build a key using the repository identity this read already resolved."""
    root = row.get("chain_root")
    if root == dispatches.CHAIN_UNKNOWN:
        return None
    root = str(root or row.get("id") or "").strip()
    return (repo, root) if repo and root else None

def chain_contributor_index():
    """(((repo, root) -> (wrote, approved, unknown)), err), scoped to work.

    INDEPENDENCE IS A PROPERTY OF THE CHAIN, NOT OF THE ROW. `wrote` retains
    every sender and recorded patch author; `approved` retains each raw APPROVE
    and its ACCEPTED append position, so eligibility uses the same evidence as
    `_lr`, not a polarity string or an invented position in a smaller list.

    RECORDED CONTRIBUTORS ARE SUBMISSION PROVENANCE, NOT PROVEN GIT AUTHORSHIP.
    A patch author is the recipient through whom the cure was submitted, not a
    claim about the commit's Git author. Display bytes remain unchanged; seat
    identity is compared by the canonical recipient owner at the point of use.

    The shared fold is memoised by ledger path/size/mtime. Only readable chain
    identities are memoised here; authority is NEVER frozen by a dispatch-file
    cache because policy availability and gate receipts can change separately.

    Damage propagates through actual root/predecessor links and known chain
    membership, never through lane labels, tips, or a common UNKNOWN bucket.
    It poisons the connected scopes without merging their contributor sets, so
    an unrelated damaged repository cannot change another chain's ANSWER.

    THE SCOPING IS ABOUT THE ANSWERS AND NOT ABOUT THE CACHE, and those were
    one sentence here until a reviewer measured them apart. ONE unreadable row
    anywhere in the fold leaves `broken` non-empty and suppresses the memo for
    the WHOLE board, so the index is rebuilt once per non-terminal READY row
    instead of once per ledger state. That is deliberate and it is not the
    same claim: an identity that could not be read is not a property of the
    ledger bytes this key is made of — the same file read from another helm
    home answers differently — so freezing it would cache an environment. An
    unrelated damaged repository de-memoises the board; it does not blind
    it."""
    key = landreq._fold_key()
    if key is not None and landreq._CHAIN_CONTRIB_MEMO.get("key") == key:
        return landreq._CHAIN_CONTRIB_MEMO["chains"], None
    rows, verdicts, err = landreq._ledger_fold()
    if err:
        return {}, err                       # transient failures are not cached
    rows = rows or {}
    chains, members, links, identities, repos, broken = {}, {}, {}, {}, {}, set()
    for rid, row in rows.items():
        repos[rid] = landreq._chain_repo(row)
        identities[rid] = landreq._chain_key(row, repos[rid])
        links[rid] = set()
    for rid, row in rows.items():
        ident = identities[rid]
        # Explicit links also connect damaged middle rounds to their readable
        # neighbours. Two KNOWN different repositories never share a scope.
        for parent in (row.get("supersedes"), row.get("chain_root")):
            if parent not in rows or parent == rid:
                continue
            if repos[rid] and repos[parent] and repos[rid] != repos[parent]:
                continue
            links[rid].add(parent)
            links[parent].add(rid)
        if ident is None:
            broken.add(rid)
            continue
        peer = members.setdefault(ident, rid)
        links[rid].add(peer)
        links[peer].add(rid)
        wrote, approved, _unknown = chains.setdefault(ident, (set(), [], None))
        # Reading is not authorship. The patch recipient contributes only
        # when this round actually records a cure.
        names = [row.get("sender")]
        if row.get("patch_tip"):
            names.append(row.get("patch_author") or row.get("recipient"))
        for name in names:
            if isinstance(name, str) and name.strip():
                wrote.add(name.strip())
        if str(row.get("polarity") or "").strip().lower() == "approve":
            position = dispatches.verdict_index(verdicts, rid)
            approved.append((row, position))
    pending, seen = list(broken), set()
    while pending:
        rid = pending.pop()
        if rid in seen:
            continue
        seen.add(rid)
        ident = identities[rid]
        if ident in chains:
            wrote, approved, _unknown = chains[ident]
            chains[ident] = (wrote, approved, "linked chain identity is UNKNOWN")
        pending.extend(links[rid] - seen)
    if key is not None and not broken:
        landreq._CHAIN_CONTRIB_MEMO.clear()
        landreq._CHAIN_CONTRIB_MEMO.update(key=key, chains=chains)
    return chains, None

def chain_contributors(lr, index=None):
    """(wrote, approved, err) for THIS row's chain — the join, done once."""
    if index is None:
        index, err = landreq.chain_contributor_index()
        if err:
            return frozenset(), (), err
    ident = landreq.chain_key(lr)
    if ident is None:
        why = "chain identity is UNKNOWN" if lr.get("id") or lr.get("chain_root") else None
        return frozenset(), (), why
    wrote, approved, err = index.get(ident, ((), (), None))
    return frozenset(wrote), tuple(approved), err

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
    which DELETES a landing from the owner's view (a review's refutation of the
    first cut). Repetition is a cost; invisibility is a lie. This index is
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
        # tip — which is why ca318bb5812f (a real landing) was claimed by FIVE
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
    # a review refuted in the renderer, reappearing one layer down in the
    # producer. An ambiguous root is not a DECLARED root, so it answers None
    # and the receipt renders alone.
    return {tip: roots.pop() for tip, roots in seen.items() if len(roots) == 1}

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

def _chain_children(lrs):
    """{parent id -> [child rows]} for every supersedes edge on the board."""
    kids = {}
    for lr in lrs.values():
        parent = lr.get("supersedes")
        if parent:
            kids.setdefault(parent, []).append(lr)
    return kids

def _chain_forest(raw, lrs):
    """({parent id -> [child ids]}, {id -> row-or-None}, err) over the RAW rows.

    BUILT FROM THE LEDGER, NOT THE PROJECTION. project() drops
    status=cancelled at the top of its loop, while the chain grammar legally
    permits a successor of a cancelled parent — so a projection-only graph
    SILENTLY DISCONNECTS `p -> cancelled -> live`, and the live carrier stops
    absorbing p's debt (immutable-tip audit). A cancelled node is
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
            proot, croot = landreq._effective_root(prow), landreq._effective_root(row)
            if parent in raw and (proot is None or croot is None):
                # AN ABSENT ROOT IS UNKNOWN IDENTITY, NOT A PASS (a review
                # refuting 8ad1feb). The first version required BOTH roots to
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

def _chain_unfinished(row, snapshot):
    """([descendant], [(node, why)]) — the SAME-CHAIN successors of `row`, at
    any depth, that are not terminal, and the nodes whose chain membership the
    walk could not decide.

    EVERY DESCENDANT, NOT THE CARRIER. `dispatches.carrier` answers who holds
    the debt and stops at the first successor that took it; a terminal
    successor that took it once still counts as a carrier there. This asks
    whether anything on the chain is still on the board, so it walks past
    every successor and reports each one that is not finished.

    AN EDGE WHOSE IDENTITY DISAGREES IS DAMAGE, NEVER A FOREIGN EDGE.
    `dispatches._same_chain` answers False for an UNKNOWN or absent root and
    for a root that names another chain alike, and a walk that skips on False
    reads the subtree under any of them as clear. No producer writes a child
    whose root differs from its parent's chain: `_resolve_chain` roots it at
    `root or parent["id"]`, and `_cured_operation_reconciliation` matches only
    that value. So every child the walk meets whose root is not this chain's,
    whether UNKNOWN, absent or a well-formed id naming anything else (a row
    that is itself a root included), is UNRESOLVED and not walked.
    `_chain_forest` refuses the same shape for the whole board. A row whose
    own root is UNKNOWN has no chain to compare against, so it is UNRESOLVED
    before the walk starts.

    AN EDGE THE INDEX CANNOT FOLLOW IS DAMAGE TOO. `_successor_index` keys a
    child by its raw `supersedes`, so a child whose `supersedes` replays
    UNKNOWN, or names a row the snapshot does not hold, is never reached from
    here. Any row carrying this chain's root with such a parent field is
    UNRESOLVED: it may be a successor of this work that the walk cannot see.
    So is a row carrying this chain's root whose `supersedes` replays absent,
    from a key dropped or written null: only the chain's root row itself
    supersedes nothing, and a legacy row carries no root to match.

    Each unresolved node is returned with the reason, so a caller that permits
    on an empty first list refuses on the second.
    """
    unknown = dispatches.CHAIN_UNKNOWN
    rid = str(row.get("id") or "")
    if row.get("chain_root") == unknown:
        return [], [(row, "its own chain_root is UNKNOWN")]
    chain = row.get("chain_root") or rid
    kids = dispatches._successor_index(snapshot)
    seen, frontier, out, unresolved = {rid}, [row], [], []
    for other in snapshot.values():
        if not isinstance(other, dict) or other is row \
                or other.get("chain_root") != chain:
            continue
        parent = other.get("supersedes")
        if not parent:
            if str(other.get("id") or "") != chain:
                unresolved.append((other, "it carries this chain's root but "
                                          "supersedes nothing, so no walk "
                                          "reaches it"))
        elif parent == unknown:
            unresolved.append((other, "its supersedes is UNKNOWN, so no walk "
                                      "reaches it"))
        elif str(parent) not in snapshot:
            unresolved.append((other, "its supersedes names %s, which the "
                                      "snapshot does not hold"
                               % str(parent)[:12]))
    while frontier:
        parent = frontier.pop()
        for kid in kids.get(str(parent.get("id") or ""), ()):
            kid_id = str(kid.get("id") or "")
            if not kid_id or kid_id in seen:
                continue
            seen.add(kid_id)
            root = kid.get("chain_root")
            if root is None:
                unresolved.append((kid, "chain_root absent"))
                continue
            if root == unknown:
                unresolved.append((kid, "chain_root UNKNOWN"))
                continue
            if not dispatches._same_chain(row, kid):
                unresolved.append((kid, "chain_root %s is not this chain's %s"
                                   % (str(root)[:12], chain[:12])))
                continue
            frontier.append(kid)
            if kid.get("status") != "cancelled" \
                    and not dispatches._close_retired_by(kid):
                out.append(kid)
    return out, unresolved

def close_landed(rid, repo=None, trunk=None, live=False, needs_restart=None):
    """DEPRECATED for exactly one release: `helm lr close --reason landed` is
    the one terminal verb and this alias only maps arguments onto it — no
    second code path. Its historical UNDECLARED-only domain is a subset of
    landed's (approve rows are now admissible too); `--repo`/`--trunk` pass
    through, and legacy CLOSED-BY-LANDING rows answer retries idempotently
    under the same repo + trunk-ref-alias identity. The live-step declaration
    passes through too — a deprecated spelling is not a door around it."""
    return landreq.close(rid, "landed", repo=repo, trunk=trunk, live=live,
                 needs_restart=needs_restart)

def _close_trunk(lr, gitdir, trunk):
    """(ref, pinned sha, target, err) for the repo-proof reasons (landed /
    stranded): an explicit --trunk is always canonical; a bound row without
    one uses the discharge trio; an unbound row REQUIRES --trunk (the
    _canonical_trunk refusal is exactly that requirement)."""
    if str(trunk or "").strip() or not lr.get("repo_id"):
        ref, sha, err = landreq._canonical_trunk(gitdir, trunk)
        if err:
            return None, None, None, err
        target = "upstream" if ref.startswith("refs/remotes/") else "local"
        return ref, sha, target, None
    return landreq._trio_trunk(gitdir)

def _close_ladder_delivered_report(rid, artifact_ref, report_ref, evidence,
                                   dry_run):
    """Close one OPEN BUILD with artifact identity plus a chat handoff receipt."""
    if not dry_run:
        out, err = dispatches._record_close_proven(
            rid, "delivered-report", None, evidence=evidence,
            artifact_ref=artifact_ref, report_ref=report_ref)
        if err:
            return None, err
        return landreq.get(out["id"])
    values, err = dispatches.clean_delivered_report_refs(
        artifact_ref, report_ref, evidence)
    if err:
        return None, err
    artifact_ref, report_ref, evidence = values
    current, unavailable = dispatches.snapshot()
    if unavailable:
        return None, unavailable
    row, err = landreq._resolve_row_for_diagnosis(current, rid)
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
            return landreq.get(row["id"])
        return None, ("dispatch %s is already retired by %s; a row is retired "
                      "once — refusing a different closure" %
                      (row["id"], retired))
    err = dispatches._close_event_error(event, row)
    if err:
        return None, "close does not bind: %s" % err
    return {"dry_run": True, "id": row["id"],
            "reason": "delivered-report", "artifact_ref": artifact_ref,
            "report_ref": report_ref, "evidence": evidence}, None

def _close_result(out, err, reason, dry_run, already_retired=None):
    """Normalize the one successful close result contract.

    Ladders own proof and writes; this boundary owns the shape every caller sees.
    A dry run is therefore marked here even if a new ladder forgets, while an
    explicit contradiction or malformed success refuses instead of becoming a
    plausible terminal report.
    """
    if err:
        return out, err
    if not isinstance(out, dict):
        return None, ("close --reason %s returned an invalid success result — "
                      "refusing to report a terminal action" % reason)
    marked = out.get("dry_run")
    if dry_run:
        if marked is False:
            return None, ("close --reason %s returned dry_run=false for a "
                          "rehearsal" % reason)
        reported = out.get("reason") if marked is True else None
        if reported is not None and reported != reason:
            return None, ("close --reason %s returned a dry-run result for "
                          "--reason %s" % (reason, reported))
        normalized = dict(out)
        normalized["dry_run"] = True
        normalized["reason"] = reason
        # The two pre-projection reasons return a marked summary when fresh and
        # the standing row when idempotent; every projected reason supplies the
        # pre-call retirement state explicitly.
        if already_retired is None:
            already_retired = marked is not True
        normalized["idempotent"] = bool(already_retired)
        normalized["would_append"] = not already_retired
        return normalized, None
    if marked:
        return None, ("close --reason %s returned a dry-run result for a live "
                      "close" % reason)
    return out, None

def chain_credits(lr, lrs=None):
    """Every author this row's chain records, lane owner first.

    ONE LANE, SEVERAL AUTHORS is the shape the review procedure now has: a
    reviewer of either family who finds a MECHANICAL defect commits the cure on
    a branch off the exact reviewed tip and records it on the verdict
    (`patch_tip`), and the lane owner or integrator rebases onto that tip. The
    reviewer therefore wrote part of what lands, and the landing record has to
    say so — the old procedure had no second name to record because the
    reviewer was never allowed to write code.

    DERIVED FROM THE CHAIN, NOT FROM ONE ROW, because the cure that carries a
    reviewer's commit is usually an EARLIER round than the one that lands. Rows
    that share this row's work identity (`chain_root`, falling back to the id —
    landreq's own "a root names itself" seal) contribute their author and their
    recorded patch author. Sorted by id after this row so two readers of one
    ledger print the same order.

    A name appears ONCE however many rounds it wrote, and an absent or blank
    name contributes nothing: a credit list is a statement about people, and an
    empty string in it is a claim that somebody anonymous wrote code.
    """
    rows = [lr]
    if isinstance(lrs, dict):
        root = lr.get("chain_root") or lr.get("id")
        rows += sorted((row for row in lrs.values()
                        if isinstance(row, dict)
                        and row.get("id") != lr.get("id")
                        and (row.get("chain_root") or row.get("id")) == root),
                       key=lambda row: str(row.get("id") or ""))
    out = []
    for row in rows:
        names = [row.get("author")]
        if row.get("patch_tip"):
            names.append(row.get("patch_author") or row.get("reviewer"))
        for name in names:
            if isinstance(name, str) and name.strip() and name not in out:
                out.append(name)
    return out

def close(rid, reason, evidence=None, tip=None, repo=None, trunk=None,
          dry_run=False, live=False, needs_restart=None,
          artifact_ref=None, report_ref=None, fan_out=True,
          attester=None, attest=None, compose_manifest=None, compose_gate=None,
          compose_landed_by=None):
    """(result, err) — the one terminal verb. On a write, `result` is the
    refreshed land request; on an idempotent retry, the standing one; under
    dry_run a normalized summary marks `would_append` versus `idempotent`
    (nothing appended on EITHER path — a dry-run refusal returns the refusing
    rung's exact message).

    `live` / `needs_restart` are the LIVE-step declaration, required for
    --reason landed and refused on every other reason (see `_delivery`).

    `fan_out` is INTERNAL and false only on the recursive closes a landed
    sweep issues (`_close_same_tip_siblings`). It is not a CLI flag: a caller
    who wants the peers left open is asking for the double-count the sweep
    exists to end, and would be choosing it for the OWNER'S board."""
    reason = str(reason or "").strip()
    compose_requested = compose_manifest is not None or compose_gate is not None \
        or compose_landed_by is not None
    if compose_requested:
        from . import compose_contract
        if reason != "landed" or compose_manifest is None or not compose_gate:
            return None, "compose manifest and gate belong together to --reason landed"
        err = compose_contract.writer_error()
        if err:
            return None, err
        if not isinstance(compose_manifest, dict):
            compose_manifest, err = compose_contract.read_manifest(compose_manifest)
            if err:
                return None, err
    if (live or needs_restart is not None) and reason != "landed":
        return None, ("the delivery declaration (--live / --needs-restart) "
                      "belongs to --reason landed — no other terminal claims "
                      "a change reached the running fleet")
    if reason not in landreq.CLOSE_CLI_REASONS:
        return None, ("close --reason must be one of %s"
                      % "|".join(landreq.CLOSE_CLI_REASONS))
    if reason != "delivered-report" \
            and (artifact_ref is not None or report_ref is not None):
        return None, "artifact/report refs belong only to --reason delivered-report"
    if reason == "delivered-report" \
            and (tip is not None or repo is not None or trunk is not None):
        return None, ("delivered-report carries no tip override, repository, or "
                      "trunk proof")
    def finish(result, already_retired=None):
        out, err = result
        return landreq._close_result(out, err, reason, dry_run, already_retired)

    if reason == "delivered-report":
        return finish(landreq._close_ladder_delivered_report(
            rid, artifact_ref, report_ref, evidence, dry_run))
    if evidence is not None:
        evidence, err = dispatches._clean(evidence, "close evidence", 256)
        if err:
            return None, err
    if reason == "out-of-scope":
        return finish(landreq._close_out_of_scope(rid, evidence, dry_run))
    # ONE projection for the row AND its chain. The polarity gates below
    # consult the chain's declared polarity (`_chain_polarity`), and the chain
    # must come from the same read as the row it speaks for — a second read
    # would let an append between them show a child the row's own read never
    # saw. An unavailable projection is the UNREADABLE-CHAIN case: no door
    # opens, in words, right here. Hydrate that chain only; the projection still
    # carries the complete raw instant for topology and integrity.
    raw_rows = None
    if compose_requested:
        lrs, raw_rows, unavailable = landreq.project_raw(selector=rid)
    else:
        lrs, unavailable = landreq.project(selector=rid)
    if unavailable:
        return None, unavailable
    lr, err = landreq._resolve_row_for_diagnosis(
        lrs, rid, noun="land request", list_hint="helm lr list")
    if err:
        return None, err
    routes = landreq.close_routes(
        lr, lrs, evidence=evidence, tip=tip, repo=repo, trunk=trunk,
        dry_run=dry_run, live=live, needs_restart=needs_restart,
        fan_out=fan_out, raw_rows=raw_rows, attester=attester, attest=attest,
        compose_manifest=compose_manifest, compose_gate=compose_gate,
        compose_landed_by=compose_landed_by)
    result = routes[reason]()
    # WHO WROTE THE THING THAT LANDED. A lane may carry several authors — the
    # lane owner, plus any REVIEWER whose committed cure this chain rebased
    # onto or cherry-picked — and a close that names only the sender writes the
    # other authors out of work they wrote. Computed from the SAME projection
    # the gates above read, so the credit line and the polarity gate can never
    # disagree about the chain. Attached to the result rather than derived at
    # the renderer: `--json` consumers need the same list the printed line
    # carries.
    if reason == "landed":
        out, err = result
        if not err and isinstance(out, dict):
            result = (dict(out, credited_authors=landreq.chain_credits(lr, lrs)), err)
    return finish(result, already_retired=bool(landreq._retired_by(lr)))

def close_routes(lr, lrs, evidence=None, tip=None, repo=None, trunk=None,
                 dry_run=False, live=False, needs_restart=None, fan_out=True,
                 raw_rows=None, attester=None, attest=None,
                 compose_manifest=None, compose_gate=None,
                 compose_landed_by=None):
    """{reason: () -> (result, err)} — the eleven evidence doors, BOUND TO ONE
    PROJECTION.

    EXTRACTED SO A CALLER CAN ASK THEM ALL. `close` indexes this table with the
    operator's typed reason and runs exactly one door. A caller that DERIVES
    the disposition instead has to ask every door "would you admit this row",
    and it must ask them all against the SAME read: `close` re-projects on
    every call, so thirteen typed calls are thirteen projections of a ledger other
    seats are appending to, and two answers in one classification could then
    describe two different boards. `_retire_context` states the same rule for
    the retire sweep — measuring per row "would let a hundred classifications
    straddle a roster change".

    DRY IS THE ASKING MODE and it is a measured property, not an assumed one:
    `tests.test_lr_close_actor.EveryLadderAnswersWithoutWriting` drives every
    reason dry against a real ledger and asserts nothing appends, with a
    control proving the counter moves for a real close in the same fixture.

    `delivered-report` and `out-of-scope` are DELIBERATELY ABSENT. Both are
    decided before the projection this table is bound to — they close an OPEN
    BUILD and an OPEN row respectively, neither of which carries the reviewed
    evidence every door here reads."""
    return {
        "landed": lambda: landreq._close_ladder_landed(
            lr, evidence, repo, trunk, dry_run, lrs,
            live=live, needs_restart=needs_restart, fan_out=fan_out,
            raw_rows=raw_rows, compose_manifest=compose_manifest,
            compose_gate=compose_gate, compose_landed_by=compose_landed_by),
        "superseded": lambda: landreq._close_ladder_superseded(
            lr, evidence, tip, dry_run, lrs),
        "withdrawn": lambda: landreq._close_ladder_withdrawn(
            lr, evidence, dry_run, lrs),
        "stranded": lambda: landreq._close_ladder_stranded(
            lr, evidence, repo, trunk, dry_run),
        "discharged": lambda: landreq._close_ladder_discharged(
            lr, evidence, dry_run, lrs),
        "resolved": lambda: landreq._close_ladder_resolved(
            lr, evidence, repo, trunk, dry_run, lrs),
        "carried": lambda: landreq._close_ladder_carried(
            lr, evidence, repo, trunk, dry_run, lrs),
        "chain-proof": lambda: landreq._close_ladder_chain_proof(
            lr, evidence, repo, trunk, dry_run, lrs,
            attester=attester, attest=attest),
        "subsumed": lambda: landreq._close_ladder_subsumed(
            lr, evidence, repo, trunk, dry_run),
        "expired": lambda: landreq._close_ladder_expired(
            lr, evidence, repo, trunk, dry_run),
        "endorsement-moot": lambda: landreq._close_ladder_endorsement_moot(
            lr, evidence, repo, trunk, dry_run, lrs),
    }

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
    kids = landreq._chain_children(lrs)
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
    same, serr = landreq._same_code_tips(lr)
    if serr:
        return None, None, serr
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

def _close_landed_proof(gitdir, reviewed, trunk_ref, pinned,
                        stored_pid=None):
    """(proof mode, translated tip, err) for one reviewed tip on pinned trunk."""
    proof = landreq._landing_proof(gitdir, reviewed, pinned)
    if proof not in ("absent", "unknown"):
        # PROVED ONCE, STORED HERE. Every later read of this row answers from
        # the store instead of re-deriving it against a 600-commit window that
        # will eventually scroll past this landing entirely.
        landreq._remember_landing(gitdir, reviewed, pinned, stored_pid)
        return proof, None, None, None
    # THE LADDER IS ORDERED AND EVERY RUNG RUNS IN ORDER. Git having said
    # ABSENT is not a reason to skip the recorded translation map: a rewritten
    # sha is exactly the case where Git cannot see the change and a MECHANICAL
    # identity map can, and the map's answer is then proved by Git again.
    # Consulting it only for UNKNOWN meant a recorded rewrite whose old sha
    # Git called absent never got asked about.
    git_said = ("Git proved %s is neither an ancestor of %s@%s nor "
                "patch-equivalent to it" % (reviewed[:12], trunk_ref,
                                            pinned[:12])) \
        if proof == "absent" else \
        ("Git could not prove whether %s landed on %s@%s"
         % (reviewed[:12], trunk_ref, pinned[:12]))
    table, terr = landreq._ref_translations_checked()
    if terr:
        # FAIL CLOSED ON BOTH ENTRY STATES. An unreadable map is an unreadable
        # MEASUREMENT, never an absence, so it refuses rather than letting the
        # ladder continue past a rung nobody could read.
        return None, None, None, (
            "%s, and the ref-translation sidecar is unreadable (%s) — absence "
            "cannot be adjudicated; landing closure NOT recorded"
            % (git_said, terr))
    new = table.get(reviewed)
    if not new:
        return landreq._close_landed_content(
            gitdir, reviewed, trunk_ref, pinned,
            "%s and no recorded translation names it" % git_said, stored_pid)
    if not landreq._sha(new):
        full = landreq._git(gitdir, *landreq._REF_ARGV, new + "^{commit}")
        if full is not None and full.returncode == 0 and full.stdout.strip():
            new = full.stdout.strip().lower()
    translated = landreq._landing_proof(gitdir, new, pinned)
    if translated not in ("ancestor", "patch-equivalent"):
        return landreq._close_landed_content(
            gitdir, reviewed, trunk_ref, pinned,
            "%s, directly or through its recorded translation %s"
            % (git_said, new[:12]), stored_pid)
    # THE TRANSLATED RUNG FEEDS THE STORE, TOO. `new` is the sha the recorded
    # translation names and Git has just proved it is on trunk, so it is the
    # candidate that answers for this row's keys.
    landreq._remember_landing(gitdir, reviewed, pinned, stored_pid, carriers=(new,))
    return "translated-" + translated, new, None, None

def _close_landed_content(gitdir, reviewed, trunk_ref, pinned, git_said,
                          stored_pid=None):
    """THE FOURTH RUNG, reached only after Git and the translation map fail.

    `git_said` is what the STRONGER rungs concluded, carried in so a refusal
    names the whole ladder rather than only the last step — an author told
    "no content match" without being told Git also could not place it has
    been handed the least useful half of the answer.

    A HIT IS NOT A CLOSURE. This returns a proof MODE; every precedence door
    the caller owns — open review, owed verdict, contrary/FIX debt,
    supersession, retired/withdrawn — still ranks ahead of it and is checked
    elsewhere. This rung speaks only to whether the CHANGE reached trunk.
    """
    carrier, why = landreq._content_equivalent_carrier(gitdir, reviewed, trunk_ref,
                                               pinned)
    if carrier:
        identity = landreq._commit_content_identity(gitdir, reviewed)
        witness = landreq._content_equivalent_witness(
            gitdir, reviewed, carrier, trunk_ref, pinned, identity, 1)
        if not witness:
            # THE PROOF IS ONLY AS GOOD AS ITS RECORD. A carrier we cannot
            # WITNESS is a claim replay could never re-check without redoing
            # the search this rung exists to make unnecessary, so it is
            # refused rather than closed on a fact nobody can revisit.
            return None, None, None, (
                "%s; a content carrier was found (%s) but its witness could "
                "not be recorded, so the proof would be unverifiable — "
                "landing closure NOT recorded" % (git_said, carrier[:12]))
        # THE WITNESS MUST REPLAY AT THE WRITE BOUNDARY (the integrator's
        # ruling, capability-checked where the capability is issued). Both
        # commits are in hand HERE and nowhere later is cheaper, so a witness
        # that cannot immediately re-derive its own claim is refused instead
        # of recorded — a proof nobody can re-check is worse than no proof,
        # because the row then LOOKS proven.
        #
        # THE TRI-STATE IS ASYMMETRIC BETWEEN THE TWO ENDS, deliberately. At
        # WRITE, None (unreadable) is a REFUSAL: we built this witness from
        # these objects seconds ago, so failing to re-read them means the
        # measurement is broken right now. At READ, None must NOT be False —
        # a clone that later pruned an object has an unreadable measurement,
        # not a disproven proof. Same helper, opposite handling, because the
        # question differs: "can I mint this?" versus "was this ever true?"
        replayed, why_not = landreq._content_equivalent_replay(gitdir, witness)
        if replayed is not True:
            return None, None, None, (
                "%s; a content carrier was found (%s) but its witness does "
                "not replay at write (%s) — landing closure NOT recorded"
                % (git_said, carrier[:12], why_not or "unverifiable"))
        # THE CARRIER DOES NOT RIDE IN `translated_tip`. That field means
        # "the RECORDED TRANSLATION of this sha" — a mechanical ref-migration
        # mapping — and the writer rightly refuses it on an untranslated
        # proof ("translated_tip on an untranslated proof"). A content carrier
        # is a different fact discovered a different way, and it already has a
        # home: witness["carrier"]. Putting it in the translation slot would
        # make two unlike proofs read identically to every consumer of that
        # field.
        # AND THIS RUNG FEEDS THE STORE TOO. The point of the feed is that an
        # expensive proof is paid for once, and this is the MOST expensive
        # rung there is — it searched trunk for a content carrier. Storing
        # only the cheap ancestry rung fed the store exactly where it was
        # needed least. The carrier is the sha that answers, so it leads.
        landreq._remember_landing(gitdir, reviewed, pinned, stored_pid,
                          carriers=(carrier,))
        return landreq.CONTENT_EQUIVALENT, None, witness, None
    if why:
        return None, None, None, (
            "%s, and the content rung could not answer either: %s; landing "
            "closure NOT recorded" % (git_said, why))
    return None, None, None, ("%s, and no trunk commit carries its content; "
                              "landing closure NOT recorded" % git_said)

def _close_ladder_build_landed(lr, repo, trunk, dry_run, live=False,
                               needs_restart=None, fan_out=True):
    """Close one BUILD obligation through an authorized review descendant.

    Carries the SAME live-step declaration as the ordinary landed ladder: a
    BUILD row closes with `--reason landed` too, and leaving one landed door
    undeclared would reopen the exact hole the other door closes. It carries
    the SAME same-tip sweep for the same reason: the two landed doors are
    reached by the ONE verb, and a fan-out on only one of them would mean the
    owner's board collapses its duplicates or not depending on whether the row
    that happened to land was a build or a review."""
    current, verdicts, unavailable = dispatches.snapshot_with_verdicts()
    if unavailable:
        return None, "dispatch ledger unavailable: %s" % unavailable
    parent = current.get(lr["id"])
    if parent is None:
        return None, "BUILD dispatch is absent from the locked evidence read"
    if parent.get("status") != "open" or parent.get("kind") != "build":
        return None, "chain-walked landed closure needs one OPEN BUILD dispatch"
    gitdir, err = landreq._close_repo(parent, repo)
    if err:
        return None, err
    trunk_ref, pinned, _target, err = landreq._close_trunk(parent, gitdir, trunk)
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
                    and landreq._determinate_root(row) is not None \
                    and landreq._determinate_root(parent) is not None \
                    and landreq._determinate_root(row) != landreq._determinate_root(parent):
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
                # disjoint verdict (FIX r3, second pass).
                unchained.append(row["id"][:12])
            continue
        refusal, tier = landreq._approval_refusal(row, index=index, epoch=epoch)
        if refusal:
            misses.append("%s: %s" % (row["id"][:12], refusal))
            continue
        requirement = landreq.gate_requirement(row, index=index, epoch=epoch)
        if requirement not in ("none", "required"):
            misses.append("%s: gate requirement UNKNOWN" % row["id"][:12])
            continue
        approved.append((index, row, tier, requirement))
    approved.sort(key=lambda item: item[0])
    unknown = []
    for _index, review, tier, requirement in approved:
        proof, translated, witness, why = landreq._close_landed_proof(
            gitdir, review["reviewed_tip"], trunk_ref, pinned,
            stored_pid=review.get("patch_id"))
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
        declared, derr = landreq._delivery(live, needs_restart)
        if derr:
            return None, derr
        delivery_class, delivery_restart = declared
        # BOTH LANDED DOORS OR NEITHER. This ladder's own docstring says a
        # BUILD row closes with --reason landed too, and leaving one door
        # unchecked would reopen exactly the hole the other one closes.
        # THE TRANSLATED TIP, NOT THE ORIGINAL. This ladder already
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
        refusal = landreq._process_class_refusal(
            parent["id"], gitdir,
            str(translated or review["reviewed_tip"]).lower(),
            trunk_ref, pinned, delivery_class)
        if refusal:
            return None, refusal
        fields["close_delivery_class"] = delivery_class
        fields["close_delivery_restart"] = delivery_restart
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
            content_witness=witness,
            content_witness_anchor=(
                dispatches._proof_anchor(landreq.CONTENT_ALGORITHM, witness)
                if witness else None),
            translated_tip=translated,
            delivery_class=delivery_class, delivery_restart=delivery_restart,
            dry_run=dry_run)
        if err:
            return None, err
        if dry_run:
            return landreq._rehearsed(row, fields)
        out, err = landreq.get(row["id"])
        if out is not None and fan_out:
            # THE TIP THIS BUILD LANDED UNDER is the reviewed tip of the
            # approving descendant, not anything on the parent — a build row's
            # own ref is a BASE. Fail-open exactly as the ordinary door: the
            # parent is durably closed by now.
            try:
                closed, refused = landreq._close_same_tip_siblings(
                    parent, str(review["reviewed_tip"]), trunk_ref, live,
                    needs_restart)
            except Exception as exc:
                closed, refused = [], [("-", "sibling sweep crashed: %s" % exc)]
            if closed:
                out["closed_siblings"] = closed
            if refused:
                out["sibling_refusals"] = [{"id": rid, "why": why}
                                           for rid, why in refused]
        return out, err
    if not approved and not misses and not unknown and unchained:
        # every candidate fell at the SAME rung, and it is not landedness:
        # say the class and its cure, never the generic refusal. The sample
        # is sorted and labelled a sample — an unordered dict-walk slice
        # presented bare reads as 'nearest', which it never was (a FIX)
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

def _close_same_tip_siblings(lr, reviewed, trunk_ref, live, needs_restart,
                             compose_manifest=None, compose_gate=None, dry_run=False):
    """([closed], [(id, why)]) — close every live peer bound to the same tip,
    and every never-verdicted chain predecessor inside the landed head.

    FAIL-OPEN, ALWAYS. The row this was called for is already durably closed
    by the time we get here; a sweep that raises or refuses must never report
    failure for a land that succeeded. Every refusal is DATA on the result, so
    the operator sees which peers are still open and why, rather than a silent
    partial.

    EACH PEER RE-PROVES ITSELF. The TRUNK PIN and DELIVERY CLASS are inherited
    operator declarations about content, not authorization for another row.
    A scoped CONCUR peer also receives candidate manifest/gate addresses; its
    own immutable opt-in, verdict, effects and whole-tree gate are revalidated. Delivery is a property of
    WHICH FILES landed, and by construction these rows bind the identical
    commit, so one declaration about that content is the declaration for all
    of them. Everything else — the repository binding, the verdict, the
    polarity, the gate, the ancestry proof, the process-class check — is
    re-derived per row by the same ladder that closed the first one.
    """
    peers, unavailable = landreq._same_tip_siblings(lr, reviewed)
    if unavailable:
        return [], [("-", "dispatch ledger unavailable: %s" % unavailable)]
    closed, refused = [], []
    # THE RUNG A RE-TIP LEAVES BEHIND closes on the same land. Selected apart
    # from the peers, because the relation is the CHAIN and the containment,
    # never the tip; closed through the same `discharged` door, which asks
    # a held one about its hold.
    preds, unavailable = landreq._landed_head_predecessors(lr, reviewed)
    if unavailable:
        refused.append(("-", "dispatch ledger unavailable: %s" % unavailable))
    ids = {str(p.get("id")) for p in peers}
    preds = [p for p in preds if str(p.get("id")) not in ids]
    for peer in peers + preds:
        rid = str(peer.get("id"))
        if str(peer.get("polarity") or "") in landreq._SIBLING_DEBT_POLARITIES:
            refused.append((rid, "carries a %s verdict on the same tip — "
                                 "findings are debt, not duplication"
                            % str(peer.get("polarity")).upper()))
            continue
        note = ("same reviewed tip %s landed under %s"
                % (reviewed[:12], str(lr.get("id"))[:12])) \
            if rid in ids else \
            ("chain successor %s landed %s, and this rung's tip %s is inside "
             "that head" % (str(lr.get("id"))[:12], reviewed[:12],
                            str(dispatches.bound_tip(peer))[:12]))
        if peer.get("verdict_ref"):
            # It has its own verdict, so it owns the LANDED door outright.
            #
            # NO --repo, DELIBERATELY. `gitdir` here is a GIT DIR and `--repo`
            # takes a WORKING TREE — passing it made every peer refuse with
            # "not a readable Git working tree" while the land reported
            # success, which is the silent-partial this sweep exists to end
            # (caught by this lane's own arm, not by review). Each peer was
            # selected BECAUSE its repo_id equals this row's, so its own
            # binding resolves to the same repository with no override at all.
            # The TRUNK pin is still inherited: that one is the operator's
            # declaration about where this content landed.
            extra = {"compose_manifest": compose_manifest,
                     "compose_gate": compose_gate} \
                if peer.get("polarity") == "concur" and compose_manifest is not None else {}
            if extra and lr.get("close_reason") == "landed" \
                    and lr.get("close_proof_version") == 3:
                # Address only: both the ladder and locked writer resolve the
                # original pin and composition from the canonical closed row.
                extra["compose_landed_by"] = lr["id"]
            out, err = landreq.close(rid, "landed", evidence=note,
                             trunk=trunk_ref, live=live,
                             needs_restart=needs_restart, fan_out=False,
                             **(dict(extra, dry_run=True) if dry_run else extra))
        else:
            # Use the actual discharge door in rehearsal too: a shared tip and
            # a CONCUR close do not manufacture an APPROVE successor.
            out, err = landreq.close(rid, "discharged", evidence=note, fan_out=False,
                             **({"dry_run": True} if dry_run else {}))
            if dry_run and isinstance(err, str) \
                    and err.startswith("close --reason discharged refused:") \
                    and lr.get("polarity") == "approve":
                # The primary has not been appended in a dry run. Rehearse
                # only that terminal, then ask the SAME discharge derivation
                # and live ancestry predicate as the writer. This applies to
                # an unverdicted REVIEW peer too; a bare BUILD's ref is a base
                # and never makes it a same-tip sibling. Patch-equivalent
                # landing alone cannot satisfy the discharge ancestry leg.
                current, unavailable = dispatches.snapshot()
                primary = (current or {}).get(lr["id"])
                if not unavailable and isinstance(primary, dict) \
                        and primary.get("polarity") == "approve":
                    prospective = dict(current)
                    prospective[lr["id"]] = dict(
                        primary, close_reason="landed",
                        closing_repo_id=lr.get("closing_repo_id"),
                        closing_trunk_ref=lr.get("closing_trunk_ref"))
                    tier, by, why = dispatches.discharging_row(
                        rid, prospective, is_landed=lambda tip:
                        dispatches._writer_landed(prospective, tip))
                    if tier is not None:
                        out, err = {"id": rid, "reason": "discharged",
                                    "discharging_id": by}, None
        if err or not isinstance(out, dict):
            refused.append((rid, err or "close returned no result"))
        else:
            closed.append({"id": rid,
                           "reason": out.get("close_reason") or "-",
                           "lane": peer.get("lane"),
                           "relation": "same reviewed tip" if rid in ids
                           else "chain predecessor inside the landed head"})
    return closed, refused


def _landed_head_predecessors(lr, reviewed):
    """([rows], unavailable) — the landed row's chain PREDECESSORS that never
    got a verdict and whose own tip is inside the landed head.

    A RE-TIP LEAVES ONE RUNG BEHIND, every time. The successor is dispatched
    with --supersedes, it lands, and the rung it replaced still holds (or
    sits open) with no verdict, so no landed door takes it and a seat must
    notice it and close it by hand. This names those rungs for the same
    sweep that closes the same-tip peers, and each one then re-proves itself
    through `discharged`: its authority is this row's landed APPROVE, and a
    held rung is asked about its hold there.

    CONTAINMENT IS THE SELECTOR, AND A BARE LANE LABEL NEVER IS. A rung is
    named only when its bound tip is an ANCESTOR of the reviewed tip that
    landed, so the work it bound is in the head, byte for byte. A rebased
    rung is not contained, stays out of the sweep, and keeps its own doors.
    Could-not-tell is not contained.

    ONLY A ROW THAT NEVER GOT A VERDICT. A verdicted rung owns its own
    door, and a FIX is debt this sweep must not touch; a build row binds no
    tip (its ref is a BASE), so it is never named here."""
    tip = str(reviewed or "").strip().lower()
    if not dispatches._FULL_TIP.fullmatch(tip):
        return [], None
    current, unavailable = dispatches.snapshot()
    if unavailable:
        return [], unavailable
    repo_id = str(lr.get("repo_id") or "")
    node = current.get(str(lr.get("id") or ""))
    seen, out = {str(lr.get("id") or "")}, []
    while isinstance(node, dict):
        parent = node.get("supersedes")
        if not parent or parent == dispatches.CHAIN_UNKNOWN or parent in seen:
            break
        seen.add(parent)
        node = current.get(parent)
        if not isinstance(node, dict) \
                or str(node.get("repo_id") or "") != repo_id \
                or not query.query_is_open(node) \
                or dispatches._close_retired_by(node) \
                or node.get("verdict_ref") or node.get("reviewed_tip"):
            continue
        bound = dispatches.bound_tip(node)
        if bound and bound != tip \
                and dispatches._tip_on_trunk(repo_id, tip, bound) is True:
            out.append(node)
    return out, None

# THE SENTENCES A REFUSAL ENDS ON WHEN IT NAMES ANOTHER DOOR. Each door one of
# these offers is a PREDICTION about a command the reader has not run, so each
# is written from a measurement: the arms in `RefusalsNameMeasuredDoorsTest`
# build a row that reaches the refusal, drive every door the sentence offers,
# and pin the list of offered doors. A backticked name, or `--reason NAME`, is
# an OFFER; a door named only to say it refuses is written plainly, so the
# arms never mistake a warning for a promise.
#
# CONTRARY DEBT HAS SEVERAL DOORS AND NO ONE OF THEM IS THE ANSWER. A refusal
# that offers `superseded` alone for it ("wants --reason superseded or stays
# open as contrary debt") is false in four measured modes, one row each: a
# rebase-landed FIX with no chain link is taken by `carried` and refused by
# `superseded`; a FIX merged by ancestry and later confirmed is taken by
# `resolved`; a FIX whose tip never reached trunk is taken by `withdrawn`; a
# FIX whose tip object gc pruned is taken by `stranded`. So the list below is
# written as a list, each door with the proof it asks, and it claims to be no
# more than that: other doors can take a contrary in other modes, and none of
# these is promised to open.
_CONTRARY_DOORS = (
    "NO NEXT DOOR IS PROMISED. Doors that take contrary debt, each on its own "
    "proof: `superseded` a later APPROVE on the same chain whose tip reached "
    "trunk, `resolved` this tip on trunk by ANCESTRY and a later cross-family "
    "confirmation stating the resolution, `carried` the whole range up to "
    "this tip on trunk by patch identity, `withdrawn` this work provably "
    "ABSENT from trunk, and `stranded` a tip object this repository no longer "
    "holds while nothing live holds the work. Ask the one whose proof this "
    "row has and read its own answer; a door that refuses writes nothing, and "
    "the row stays OPEN as contrary debt")

# AN OPEN ROW'S MOOT OBLIGATION. `out-of-scope` is the door, and it refuses
# two measured states a reader of "it is moot" would not expect: work that
# SHIPPED (it names that work's door) and work still LIVE on a lane-family ref
# or worktree. `dispatch cancel` writes the same cancel event with neither
# guard, so it is not offered here: over shipped work it records what the
# out-of-scope guard refuses as false.
_MOOT_OPEN_ROW = (
    "if the review obligation is moot, `out-of-scope` retires it; it refuses "
    "work that SHIPPED or is still LIVE on a lane-family ref or worktree, and "
    "says which")

# THE DOORS OF A ROW NOBODY REVIEWED. Offering `discharged` alone for it is
# false on the plainest such row: an open review with no successor, where
# `discharged` refuses ("nothing on the ledger records what discharged this
# row") and `out-of-scope` admits. `discharged` admits only once the successor
# or same-tip peer that carried the work is itself closed landed, because it
# reads that row's RECORDED landing, not a live probe.
_UNVERDICTED_DOORS = (
    "Doors for an unverdicted row, each on its own proof: `discharged` once a "
    "chain successor or a same-tip peer that carried its work is itself "
    "closed landed, `out-of-scope` when its obligation is moot and the work "
    "neither SHIPPED nor is still LIVE, and for a BUILD row `landed` through "
    "the approved review that descends from it")

def _close_ladder_landed(lr, evidence, repo, trunk, dry_run, lrs=None,
                         live=False, needs_restart=None, fan_out=True,
                         raw_rows=None, compose_manifest=None, compose_gate=None,
                         compose_landed_by=None):
    reviewed = lr.get("reviewed_tip")
    scoped = compose_manifest is not None
    sweep_options = {"compose_manifest": compose_manifest, "compose_gate": compose_gate} \
        if scoped else {}
    preview_options = dict(sweep_options, trunk=trunk, live=live,
                           needs_restart=needs_restart)
    if scoped and lr.get("polarity") != "concur":
        return None, "compose-land exception belongs only to this row's own CONCUR"
    if lr.get("closed_by_landing") or lr.get("close_reason") == "landed":
        if scoped:
            from . import compose_contract
            if lr.get("close_proof_version") != compose_contract.PROOF_VERSION:
                return None, "scoped retry does not match the recorded landed proof"
            proof, err = compose_contract.capture(
                (raw_rows or {}).get(lr["id"], {}), raw_rows,
                lr.get("closing_repo_id"), lr.get("closing_trunk_sha"),
                compose_manifest, compose_gate)
            if err or proof != lr.get("compose_land_proof"):
                return None, err or "scoped retry changes the recorded composition"
        out, err = landreq._landed_idempotent(lr, repo, trunk)
        # A RETRY MUST BE ABLE TO FINISH THE SWEEP. The sweep is fail-open, so
        # a peer can be left open by a transient refusal — and if the only
        # verb that sweeps refuses to run twice, the operator's repair for a
        # partial fan-out is to close each straggler by hand, which is the
        # state this whole change exists to end. The row's own close is
        # untouched: `_landed_idempotent` already proved this is the SAME
        # closure, and nothing below appends to this row.
        #
        # AND THE DRY-RUN CHECK IS NOT DECORATION — the first cut of this
        # branch did not have it, and it was caught by running the real verb
        # against the LIVE board rather than by re-reading the code. `close`
        # hands `dry_run` to this ladder and every OTHER branch honours it;
        # this one reached the sweep straight past it, so `--dry-run` on an
        # already-landed row would have issued REAL closes on its peers. A
        # rehearsal that writes is worse than no rehearsal.
        if out is not None and fan_out and reviewed:
            if dry_run:
                would, keep = landreq._sweep_preview(lr, reviewed, **preview_options)
                if would or keep:
                    out = dict(out, would_close_siblings=would,
                               would_leave_open_siblings=keep)
                return out, err
            try:
                closed, refused = landreq._close_same_tip_siblings(
                    lr, reviewed, out.get("closing_trunk_ref") or trunk,
                    live, needs_restart, **sweep_options)
            except Exception as exc:
                closed, refused = [], [("-", "sibling sweep crashed: %s" % exc)]
            if closed or refused:
                out = dict(out)
            if closed:
                out["closed_siblings"] = closed
            if refused:
                out["sibling_refusals"] = [{"id": rid, "why": why}
                                           for rid, why in refused]
        return out, err
    if landreq._retired_by(lr):
        return None, landreq._retired_refusal(lr)
    if not lr.get("verdict_ref") or not reviewed:
        if lr.get("kind") == "build":
            return landreq._close_ladder_build_landed(lr, repo, trunk, dry_run,
                                              live=live,
                                              needs_restart=needs_restart,
                                              fan_out=fan_out)
        return None, ("%s has no verdict — Git cannot prove a review "
                      "happened; deliver a verdict, or %s"
                      % (lr["id"], _MOOT_OPEN_ROW))
    polarity, via, perr = landreq._chain_polarity(lr, lrs)
    if perr:
        return None, perr
    if polarity in ("fix", "supersede"):
        if lr.get("polarity"):
            return None, ("%s is a FIX/SUPERSEDE verdict — its reviewed tip "
                          "on trunk is a CONTRARY, not a resolution, and this "
                          "door never takes it. %s"
                          % (lr["id"], _CONTRARY_DOORS))
        return None, ("%s carries a chain-declared %s about its reviewed "
                      "code (via %s) — its reviewed tip on trunk is a "
                      "CONTRARY, not a resolution, and this door never takes "
                      "it. %s"
                      % (lr["id"], polarity.upper(), via, _CONTRARY_DOORS))
    if lr.get("polarity") == "concur" and not scoped:
        return None, "CONCUR does not authorize landing; prospective compose evidence required"
    gitdir, err = landreq._close_repo(lr, repo)
    if err:
        return None, err
    trunk_ref, pinned, _target, err = landreq._close_trunk(lr, gitdir, trunk)
    if err:
        return None, err
    if scoped:
        from . import compose_contract
        if compose_landed_by is not None:
            pinned, err = compose_contract.peer_pin(
                (raw_rows or {}).get(lr["id"], {}), raw_rows, compose_landed_by,
                gitdir, trunk_ref, compose_manifest, compose_gate)
            if err:
                return None, err
        _capture, err = compose_contract.capture(
            (raw_rows or {}).get(lr["id"], {}), raw_rows, gitdir, pinned,
            compose_manifest, compose_gate)
        if err:
            return None, err
    # ONE SHARED PROOF OWNER FOR BOTH LANDED DOORS. This door carried its own
    # copy of the whole ladder — its own _landing_proof, its own absent
    # refusal, its own translation rung — while the BUILD door called
    # `_close_landed_proof`. Byte-equivalent in intent and already drifting:
    # a rung added to the shared owner was reachable from the BUILD door only,
    # so a land request went through here and never met it. Two copies of a
    # ladder is two ladders.
    proof, translated, witness, why = landreq._close_landed_proof(
        gitdir, reviewed, trunk_ref, pinned)
    if why:
        return None, why
    # THE LIVE STEP, last rung on purpose. Git decides whether this reached
    # TRUNK and says so in its own words first; only a change Git already
    # proved landed is ever asked whether it reached the RUNNING FLEET. Put
    # this rung earlier and an unlanded row would be told to declare its
    # delivery class instead of being told it never landed.
    declared, derr = landreq._delivery(live, needs_restart)
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
    # docstring: a40f21d3 hardened a '..' traversal escape in peek-birth,
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
    if delivery_class == landreq.DELIVERY_CLI:
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
    refusal = landreq._process_class_refusal(
        lr["id"], gitdir, str(translated or reviewed).lower(), trunk_ref,
        pinned, delivery_class)
    if refusal:
        return None, refusal
    row, err = dispatches._record_close_proven(
        lr["id"], "landed", reviewed, evidence=evidence,
        closing_repo_id=gitdir, closing_trunk_ref=trunk_ref,
        closing_trunk_sha=pinned, proof_mode=proof, translated_tip=translated,
        content_witness=witness,
        content_witness_anchor=(
            dispatches._proof_anchor(landreq.CONTENT_ALGORITHM, witness)
            if witness else None),
        delivery_class=delivery_class, delivery_restart=delivery_restart,
        dry_run=dry_run,
        **(dict(sweep_options, close_proof_version=3,
                compose_landed_by=compose_landed_by) if scoped else {}))
    if err:
        return None, err
    if dry_run:
        prospective = dict(lr, closing_repo_id=gitdir, closing_trunk_ref=trunk_ref)
        would, keep = landreq._sweep_preview(
            prospective, reviewed, **preview_options) if fan_out else ([], [])
        return landreq._rehearsed(row, {
            "dry_run": True, "id": lr["id"], "reason": "landed",
            "proof_mode": proof, "translated_tip": translated,
            "closing_repo_id": gitdir, "closing_trunk_ref": trunk_ref,
            "closing_trunk_sha": pinned,
            "close_delivery_class": delivery_class,
            "close_delivery_restart": delivery_restart,
            # A REHEARSAL THAT HIDES THE FAN-OUT IS THE WRONG REHEARSAL.
            # This land closes more than one row; the dry run has to say
            # which, or the operator rehearses a different action. The
            # preview runs AFTER the writer now (task/2857) so a land the
            # writer would refuse never gets a sibling list at all.
            "would_close_siblings": would,
            "would_leave_open_siblings": keep})
    # THE FOLD WITNESSES ITS OWN LAND. Receipting used to be reachable ONLY
    # from the manual `helm lr land`, so a land closed here was never
    # witnessed unless somebody remembered a second verb. Measured 2026-08-05:
    # 56 folds, ONE receipt; the owner's LANDED card is fed only by the
    # receipt index and had been frozen since 00:11Z, and he asked why.
    _rec, unwitnessed, blocked = landreq._witness_land(lr, reviewed, gitdir,
                                               trunk_ref, pinned)
    out, err = landreq.get(row["id"])
    if out is not None and unwitnessed and blocked:
        # TWO STATES, TWO FIELDS, BECAUSE THEY WANT OPPOSITE ACTIONS
        # (row 141d46c5a6ab). `unwitnessed` means we looked, found nothing, and
        # could not write one — re-minting is the cure. THIS means the record
        # is unreadable, corrupt or self-contradicting, so whether a receipt
        # ALREADY EXISTS is UNKNOWN — and re-minting is the WORST available
        # move, because a second row beside an unreadable or conflicted one is
        # exactly the duplicate this whole path exists to prevent. Serializing
        # it as `unwitnessed` did not merely mislabel it: the CLI line under
        # that field prescribes `helm lr land`, so an UNKNOWN was handing the
        # operator the duplicate mint as its remedy.
        out["witness_unknown"] = unwitnessed
        out["witness_state"] = blocked
    elif out is not None and unwitnessed:
        # THE FACT TRAVELS AS DATA, NEVER AS A PRINT FROM DOWN HERE. A library
        # that writes to a stream decides for every caller at once: it broke
        # eight CLI tests asserting a clean stderr, and it would have put a
        # bare line into `--json` output that a parser cannot survive. Worse,
        # the owner's new LANDED card wants a per-row witnessed/unwitnessed
        # BADGE, and a badge cannot be derived from something that was
        # printed and discarded. So the close REPORTS and each surface
        # renders: the CLI prints it, JSON carries it, the card can badge it.
        out["unwitnessed"] = unwitnessed
    # THE SWEEP RUNS LAST, AFTER THE ROW IS DURABLY CLOSED, and it can only
    # ADD to the result — the same posture as receipting one rung above, for
    # the same reason: this land is already real and nothing down here may
    # take it back. `fan_out=False` on the recursive closes is what keeps one
    # land from re-sweeping a set every member of it already covered.
    if out is not None and fan_out:
        try:
            closed, refused = landreq._close_same_tip_siblings(
                out if scoped else lr, reviewed, trunk_ref, live, needs_restart,
                **sweep_options)
        except Exception as exc:               # never cost a recorded land
            closed, refused = [], [("-", "sibling sweep crashed: %s" % exc)]
        if closed:
            out["closed_siblings"] = closed
        if refused:
            out["sibling_refusals"] = [{"id": rid, "why": why}
                                       for rid, why in refused]
    return out, err

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
    standing, _early = landreq._retirement_stands(lr, lrs)
    if standing:
        return None, standing
    reviewed = lr.get("reviewed_tip")
    if not lr.get("verdict_ref") or not reviewed:
        # CORRECT REFUSAL, KEPT (task/2090): a never-verdicted row has nothing
        # to supersede, and this door does not widen to route it — the carried
        # door is where a never-verdicted row whose work is on trunk belongs.
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
        door, derr = landreq._translated_superseded_door(lr, superseding, dry_run,
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
        return landreq.get(original["id"])       # a racing identical close reconciles
    if original.get("discharged") \
            and original.get("superseding_tip") == superseding \
            and original.get("discharge_ref") == evidence:
        return landreq.get(original["id"])       # a racing identical DISCHARGE too
    # The capture rides the FRESH-snapshot re-derivation, the one closest to
    # the write; the door-entry check above only opened the ladder. The chain
    # map comes from the SAME `current` read as `original` — dispatch rows
    # carry every key the chain walk reads — because pairing a fresh row
    # with the entry-time `lrs` let a dry-run promise "would close" about a
    # chain a just-landed verdict had already conflicted
    # (the locked write re-derives again, but a promise surface may not be
    # staler than the read it quotes).
    standing, contradiction = landreq._retirement_stands(original, current)
    if standing:
        return None, standing
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
        refusal, _tier = landreq._approval_refusal(row, index=index, epoch=epoch)
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
            proven, why = landreq._handoff_proven(row, lr, current, contrary_chain)
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
    proof = landreq._landed(gitdir, reviewed, superseding)
    if proof is None \
            and landreq._vanished_absence(gitdir, reviewed) == "absent":
        # A VANISHED REVIEWED OBJECT STILL REACHES THE TRANSLATED DOOR, whose
        # first arm exists for exactly that object (destroyed + translated)
        # and adjudicates it on its own rungs. The landing ladder no longer
        # scores a vanished object `absent`, so this routing is asked for by
        # name instead of inherited from that collapse; `False` here only
        # means "not contained", and the door below records no absence.
        proof = False
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
        door, derr = landreq._translated_superseded_door(lr, superseding, dry_run,
                                                 lrs)
        if derr is not None or door is not None:
            return door, derr
        return None, "superseding tip does not contain the contrary reviewed change"
    trunk_ref, pinned, contrary_target, err = landreq._trio_trunk(gitdir)
    if err:
        return None, err
    original_on_trunk = landreq._landed(gitdir, reviewed, pinned)
    # THE POLARITY GATE. When the retirement carve-out ADMITTED this row,
    # the polarity it acts on is the EFFECTIVE one that admission derived —
    # one value, computed once in `_retirement_stands`, no second per-case
    # read. Otherwise the chain speaks only when the row
    # is silent (`_chain_polarity` — boundary and refusal shapes there): a
    # chain-declared FIX/SUPERSEDE row walks the contrary branch exactly as
    # an own-declared one does, because the chain is the work identity.
    if contradiction:
        polarity = contradiction["contradiction_polarity"]
    else:
        polarity, _via, perr = landreq._chain_polarity(lr, lrs)
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
    superseding_landed = landreq._landed(gitdir, superseding, pinned)
    if superseding_landed is None:
        return None, ("Git could not determine whether the superseding tip "
                      "reached trunk")
    if superseding_landed is not True:
        return None, "approved superseding tip has not reached the authoritative trunk"
    row, err = dispatches._record_close_proven(
        lr["id"], "superseded", reviewed, evidence=evidence,
        superseding_tip=superseding, superseding_id=approved[0][1]["id"],
        contrary_state=contrary_state, contrary_target=contrary_target,
        closing_trunk_ref=trunk_ref, closing_trunk_sha=pinned,
        contradiction=contradiction, dry_run=dry_run)
    if err:
        return None, err
    if dry_run:
        return landreq._rehearsed(row, {
            "dry_run": True, "id": lr["id"], "reason": "superseded",
            "superseding_tip": superseding,
            "superseding_id": approved[0][1]["id"],
            "contrary_state": contrary_state,
            "contrary_target": contrary_target,
            "closing_trunk_ref": trunk_ref,
            "closing_trunk_sha": pinned})
    return landreq.get(row["id"])

def _close_ladder_subsumed(lr, evidence, repo, trunk, dry_run):
    """Approved work reimplemented by a later cross-family confirmation."""
    if not evidence:
        return None, ("close --reason subsumed needs --evidence "
                      "<confirmation-row-id-or-prefix>")
    current, verdicts, unavailable = dispatches.snapshot_with_verdicts()
    if unavailable:
        return None, "dispatch ledger unavailable: %s" % unavailable
    original, err = landreq._resolve_row_for_diagnosis(current, lr["id"])
    if err:
        return None, err
    confirmation, err = landreq._resolve_row_for_diagnosis(
        current, evidence, noun="confirmation dispatch",
        list_hint="helm dispatch list --all")
    if err:
        return None, err
    if original.get("close_reason") == "subsumed":
        if confirmation["id"] != original.get("confirmation_id"):
            return None, landreq._retired_refusal(original)
        gitdir, err = landreq._close_repo(original, repo)
        if err:
            return None, err
        if trunk:
            trunk_ref, _sha_, err = landreq._canonical_trunk(gitdir, trunk)
            if err:
                return None, err
            if trunk_ref not in landreq._trunk_aliases(
                    original.get("closing_trunk_ref")):
                return None, landreq._retired_refusal(original)
        return landreq.get(original["id"])
    # subsumed takes no capture: it proves the original ABSENT from trunk,
    # and a contradicted withdrawal's change is PRESENT — its own later rung
    # answers with the honest refusal instead of retired-once. The chain map
    # is `current` — the SAME read `original` came from, so a chain-declared
    # contrary polarity opens this rung from truth no staler than the row it
    # judges (round 3 threaded the entry-time `lrs` here; round 4 replaces
    # it with the fresher same-read map for the dry-run promise's sake).
    standing, contradiction = landreq._retirement_stands(original, current,
                                                 repo=repo)
    if standing:
        return None, standing
    reviewed = original.get("reviewed_tip")
    original_polarity = original.get("polarity")
    if contradiction:
        # THE EFFECTIVE POLARITY the carve-out derived — own or
        # chain-declared — is the one this door acts on from here down,
        # including the confirmation-phrase rung
        # (re-reading the row's own field here refused the very
        # chain-declared row the admission had just proved).
        original_polarity = contradiction["contradiction_polarity"]
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
    refusal, tier = landreq._approval_refusal(
        confirmation, index=confirmation_index, epoch=epoch)
    if refusal:
        return None, "confirmation approval refuses: %s" % refusal
    requirement = landreq.gate_requirement(
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
        landreq._one_approval_family(original_author, "original author")
    if err:
        return None, err
    confirmation_family, confirmation_family_evidence, \
        confirmation_family_anchor, err = landreq._one_approval_family(
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
    gitdir, err = landreq._close_repo(original, repo)
    if err:
        return None, err
    trunk_ref, pinned, _target, err = landreq._close_trunk(
        original, gitdir, trunk)
    if err:
        return None, err
    confirmation_proof = landreq._landing_proof(gitdir, confirmation_tip, pinned)
    if confirmation_proof == "unknown":
        return None, "Git could not determine whether the confirmation is on trunk"
    if confirmation_proof not in ("ancestor", "patch-equivalent"):
        return None, "confirmation reviewed tip is absent from current pinned trunk"
    original_proof = landreq._landing_proof(gitdir, reviewed, pinned)
    if original_proof == "unknown":
        # A VANISHED ORIGINAL IS ADJUDICATED HERE, BY NAME. `_landing_proof`
        # answers `unknown` for an object this clone cannot resolve, because
        # most doors reading it carry nothing that could stand in for the
        # missing measurement. This one does: the cross-family confirmation
        # above is PROVEN on trunk, so the work is answered by the
        # confirmation and not by the absence (`_vanished_proof` states the
        # bound). A tip that still resolves never reaches the vanished pass
        # and keeps its `unknown`.
        original_proof = landreq._vanished_absence(gitdir, reviewed)
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
        original_proof_mode="absent", dry_run=dry_run)
    if err:
        return None, err
    if dry_run:
        return landreq._rehearsed(row, fields)
    return landreq.get(row["id"])

def _close_ladder_withdrawn(lr, evidence, dry_run, lrs=None):
    if not evidence:
        # THE MISSING-EVIDENCE SENTENCE NAMED ONE OF THE TWO ANSWERS THIS DOOR
        # ADMITS (task/2619). Git proves the reviewed change absent from trunk
        # and can say nothing about WHY, and there are two honest whys: the
        # verdict's resolution was carried out elsewhere, or the author agreed
        # with the FIX and the artifact should not exist at all. A refusal that
        # names only the first tells the second author they are at the wrong
        # door, which is how a correctly-finished lane ends up with no terminal
        # anyone can find.
        return None, ("withdraw needs evidence — the attestation git cannot "
                      "carry: either the verdict's resolution was carried out, "
                      "or the verdict is ACCEPTED and the artifact should not "
                      "exist (say so, and name where the refutation is "
                      "recorded)")
    if lr.get("close_reason") == "withdrawn" \
            and lr.get("close_evidence") == evidence:
        return lr, None
    if lr.get("withdrawn"):
        if lr.get("withdraw_ref") == evidence:
            return lr, None
        return None, "dispatch %s already has a different withdraw" % lr["id"]
    if landreq._retired_by(lr):
        return None, landreq._retired_refusal(lr)
    # THE POLARITY GATE READS THE CHAIN when the row itself is silent
    # (`_chain_polarity` — boundary and refusal shapes documented there). A
    # row a person declined in the verdict PROSE, with an UNDECLARED polarity
    # field, could reach no terminal at all until a chained round declared
    # the machine-readable half — be5e82bbe0b5 sat REVIEWED 8d08h that way.
    polarity, via, perr = landreq._chain_polarity(lr, lrs)
    if perr:
        return None, perr
    # APPROVE JOINS FIX AND SUPERSEDE, because the question withdraw answers
    # is about the WORK and not about the verdict's sign: an APPROVE whose
    # lane conflicts with trunk and will never land is exactly as stuck as a
    # FIX, and refusing it leaves those rows with no terminal at all —
    # measured, 18 withdrawable approvals across 400 open rows carrying a
    # reviewed tip.
    #
    # CONCUR IS ADMITTED TOO, on the reading that settles the apparent clash
    # with "concur authorizes nothing": that law is about AUTHORIZATION, and
    # retiring a row whose work is provably absent from trunk permits nothing
    # and settles nothing. Every landing and settlement door still refuses
    # concur; withdrawn is the single named exception.
    #
    # AN UNDECLARED POLARITY STAYS OUT and keeps its own refusal, whose
    # negative control is named "the gate did not loosen". The census measured
    # zero withdrawable undeclared rows, so admitting them would loosen a
    # guard for no population at all.
    if polarity not in ("approve", "concur", "fix", "supersede"):
        if lr.get("polarity"):
            return None, "%s is not a withdrawable verdict row" % lr["id"]
        return None, ("%s is not a withdrawable verdict row, and no chained "
                      "round declares one about its reviewed code — withdraw "
                      "retires work that will not land, on a declared "
                      "verdict" % lr["id"])
    # The invariant that matters is below and is untouched: Git must prove the
    # reviewed change ABSENT. A landed row still cannot be withdrawn, whatever
    # its polarity says.
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
    trunk_ref, pinned, _target, err = landreq._trio_trunk(gitdir)
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
    # THE TYPED PROOF, NOT THE LOSSY WRAPPER. `_landed` collapses `ancestor`
    # and `patch-equivalent` into one True and `absent` and `unknown` into one
    # falsey, and this door needs all four apart: the ruling admits a
    # withdrawal only when the tip is off trunk BY ANCESTRY *AND* its patch is
    # ABSENT by patch-id, and it must refuse on either kind of landing and on
    # an unknown. Recording WHICH proof answered is what lets a later reader
    # tell a cherry-picked land from an unreachable sha.
    relation = landreq._landing_proof(gitdir, reviewed, pinned)
    landed = None if relation == "unknown" else (relation != "absent")
    proof, translated = "absent", None
    if landed is not True:
        table, terr = landreq._ref_translations_checked()
        if terr:
            return None, ("the ref-translation sidecar is unreadable (%s) — "
                          "absence cannot be adjudicated" % terr)
        new = table.get(reviewed)
        if new:
            if not landreq._sha(new):
                full = landreq._git(gitdir, *landreq._REF_ARGV,
                            new + "^{commit}")
                if full is not None and full.returncode == 0 \
                        and full.stdout.strip():
                    new = full.stdout.strip().lower()
            tlanded = landreq._landed(gitdir, new, pinned)
            if tlanded is None:
                return None, ("Git could not prove the reviewed change is "
                              "absent from trunk through its recorded "
                              "translation %s — withdraw is FAIL-CLOSED on an "
                              "unknown land state" % new[:12])
            if tlanded is True:
                # ONE DOOR IS OFFERED, AND ONLY FOR ONE VERDICT. `landed`
                # follows the same recorded translation and admits an APPROVE
                # over it. Measured on a FIX over the same translated shape,
                # every door asked refused: `landed` as a contrary, `carried`
                # and `resolved` over a reviewed object this repository no
                # longer holds, `superseded` for want of a later approve. So
                # nothing is offered for the other verdicts.
                return None, ("the reviewed change IS on trunk through its "
                              "recorded translation %s — a withdraw over "
                              "landed work is false. `landed` follows the "
                              "same recorded translation and is the door for "
                              "an APPROVE; for any other verdict no door is "
                              "promised here, and this refusal leaves the "
                              "row OPEN" % new[:12])
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
        # THE DOOR FOR WORK ON TRUNK FOLLOWS THE VERDICT, not one door for
        # every verdict. Offering `superseded` here ("if superseded") is false
        # for an APPROVE even with a later approve on its chain: `superseded`
        # refuses it as "a landed outcome", and `landed` admits it. A CONCUR
        # is `endorsement-moot`'s, which refuses one that reaches trunk only
        # by patch identity, so the sentence carries that condition.
        return None, ("the reviewed change IS on trunk (%s) — a withdraw over "
                      "landed work is false. The door for work on trunk "
                      "follows the verdict, each on its own proof: `landed` "
                      "for an APPROVE, `endorsement-moot` for a CONCUR whose "
                      "tip is on trunk by ANCESTRY, and for a FIX or "
                      "SUPERSEDE contrary `superseded`, `resolved` or "
                      "`carried`. Ask the one that fits this row and read its "
                      "own answer; a door that refuses writes nothing, and "
                      "the row stays OPEN" % relation)
    row, err = dispatches._record_close_proven(
        lr["id"], "withdrawn", reviewed, evidence=evidence,
        proof_mode=proof, translated_tip=translated,
        absence_trunk_ref=trunk_ref, absence_trunk_sha=pinned,
        withdrawing_seat=landreq._acting_seat() or None, dry_run=dry_run)
    if err:
        return None, err
    if dry_run:
        return landreq._rehearsed(row, {
            "dry_run": True, "id": lr["id"], "reason": "withdrawn",
            "proof_mode": proof, "translated_tip": translated,
            "polarity": polarity, "polarity_via_chain": via,
            "absence_trunk_ref": trunk_ref,
            "absence_trunk_sha": pinned,
            "withdrawing_seat": landreq._acting_seat() or None})
    return landreq.get(row["id"])

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
        gitdir, err = landreq._close_repo(lr, repo)
        if err:
            return None, err
        if lr.get("closing_repo_id") == gitdir \
                and lr.get("close_evidence") == evidence:
            return lr, None
        return None, landreq._retired_refusal(lr)
    if landreq._retired_by(lr):
        return None, landreq._retired_refusal(lr)
    if not lr.get("verdict_ref") or not reviewed:
        return None, ("%s is an open row — its obligation is the review; %s"
                      % (lr["id"], _MOOT_OPEN_ROW))
    gitdir, err = landreq._close_repo(lr, repo)
    if err:
        return None, err
    trunk_ref, pinned, _target, err = landreq._close_trunk(lr, gitdir, trunk)
    if err:
        return None, err
    # rung 1 — POSITIVE CONTROL: a repo that cannot read a known-good object
    # is not a repo whose "missing" means anything (THE mass-termination
    # guard). The trunk sha it just resolved is the known-good object.
    if landreq._object_exists(gitdir, pinned) is not True:
        return None, ("the repository at %s cannot prove its own trunk "
                      "object — refusing to call anything pruned in a repo "
                      "that cannot read a known-good one" % gitdir)
    # rung 2 — OBJECT-MISSING, bare full-sha form pinned (the ^{commit} peel
    # exits 128 on missing, indistinguishable from corrupt).
    present = landreq._object_exists(gitdir, reviewed)
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
    table, terr = landreq._ref_translations_checked()
    if terr:
        return None, ("the ref-translation sidecar is unreadable (%s) — "
                      "absence cannot be adjudicated" % terr)
    new = table.get(reviewed)
    if new:
        alive = landreq._object_exists(gitdir, new)
        if alive is True:
            # `landed` IS OFFERED FOR THE VERDICTS IT TAKES. It follows the
            # recorded translation and admits an APPROVE or an undeclared
            # verdict once that translation reached trunk; a FIX whose
            # translation reached trunk is refused there as a contrary, so
            # "--reason landed if it reached trunk" is false for this
            # ladder's FIX and SUPERSEDE rows.
            return None, ("%s translates to live object %s (recorded rewrite) "
                          "— adjudicate through the translated identity: "
                          "`--reason landed` follows it and admits an APPROVE "
                          "or an undeclared verdict whose translation reached "
                          "trunk; for a FIX or SUPERSEDE no door is promised "
                          "here, since landed refuses a contrary, and the row "
                          "stays on the frontier" % (reviewed[:12], new[:12]))
        if alive is None:
            return None, ("Git could not determine whether translated object "
                          "%s exists — stranded is FAIL-CLOSED on an unknown "
                          "object state" % new[:12])
    # rung 4 — LANE-FAMILY REFS (interlock law 2: probe the stem, generous).
    matches, ferr = landreq._lane_family_refs(gitdir, lr.get("lane"), lr.get("branch"))
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
    wts, werr = landreq._lane_family_worktrees(gitdir, lr.get("lane"),
                                       lr.get("branch"))
    if werr:
        return None, ("the repository's worktree registry could not be read "
                      "(%s) — stranded is FAIL-CLOSED on an unknown worktree "
                      "state" % werr)
    if wts:
        return None, ("a lane-family worktree exists at %s — uncommitted or "
                      "unpushed work may live there; stranded refused"
                      % wts[0]["path"])
    row, err = dispatches._record_close_proven(
        lr["id"], "stranded", reviewed, evidence=evidence,
        closing_repo_id=gitdir, control_sha=pinned,
        proof_mode="object-pruned", dry_run=dry_run)
    if err:
        return None, err
    if dry_run:
        return landreq._rehearsed(row, {
            "dry_run": True, "id": lr["id"], "reason": "stranded",
            "closing_repo_id": gitdir, "control_sha": pinned,
            "proof_mode": "object-pruned",
            "blob_containment": ("not computable — a pruned tip has no "
                                 "readable blobs to take; advisory only, "
                                 "never gates, never authors (scoped out "
                                 "of this slice by design)")})
    return landreq.get(row["id"])

def _close_expired_one(lr, evidence, repo, trunk, dry_run):
    """THE DOORLESS-REVIEW DOOR: a verdict that authorizes nothing, on work
    that never reached trunk.

    WHAT MAKES THIS DIFFERENT FROM EVERY SIBLING is that nothing here is
    wrong. `stranded` needs a destroyed substrate, `withdrawn` proves absence,
    `resolved` needs the row's own tip landed, `subsumed` needs a later
    approval — each keys on something having HAPPENED. This population's
    defining fact is that nothing happened and nothing will: a CONCUR or a
    pre-tier APPROVE was recorded, the tip never landed, and no door exists.
    Measured on the live board, 32 of 35 open rows, 11 to 43 days old.

    THE POSITIVE CONTROL IS NOT OPTIONAL ON A BULK DOOR. `stranded` learned
    it the expensive way and calls it THE mass-termination guard: a repository
    that cannot read a known-good object is not a repository whose "absent"
    means anything. This door closes rows in batches over a projection, so a
    momentarily unreadable repo could otherwise expire the whole board in one
    command. The trunk sha it just resolved is the known-good object.

    THE PREDICATE IS SHARED WITH THE CENSUS, deliberately and by CALL rather
    than by restatement. `helm lr expired --census` has to report exactly the
    population this door would touch; a second copy of the rule would drift,
    and the drift would be invisible because both surfaces would still look
    authoritative. One function answers both, and its three-state return is
    what lets the census bucket UNMEASURED apart from REFUSE.

    AND THE DISPOSITION LEAVES BY THE RETURN, which is why this is the
    three-value function and `_close_ladder_expired` is the facade over it. A
    row the census ADMITTED can answer UNMEASURED HERE — the lane refs were
    readable then and are not now — and folding that into the caller's refused
    bucket restates the exact collapse the predicate's third state exists to
    prevent, one layer further out. (state, row, err); the ladder signature the
    close table dispatches on drops the state and is otherwise identical.
    """
    # THE EVIDENCE IS THE MEASUREMENT, NOT THE OPERATOR'S PROSE. The predicate
    # yields a DISPOSITION AND AN EVIDENCE LINE, so this door is a RUNG a
    # general close command can absorb rather than a reason carrying its own
    # vocabulary. `expired_verdict` already produces that line and it
    # names the verdict kind, the tip and why the tip is absent — everything a
    # hand-typed sentence was being asked for, derived instead of retyped.
    # A caller may still pass its own line and it is preferred when given.
    reviewed = lr.get("reviewed_tip")
    if lr.get("close_reason") == "expired":
        # THE RETRY IDENTITY IS THE REPOSITORY, NOT THE SENTENCE. This branch
        # runs BEFORE the derivation below, so on the DEFAULT path — the one
        # the batch uses, and the only one a script will ever take — `evidence`
        # is still None while the standing row holds the derived line. Compared
        # literally, every honest retry of an expired close read as A DIFFERENT
        # CLOSURE and took the retired-once refusal, so a caller whose first
        # write succeeded and whose answer was lost could never reconcile.
        #
        # NONE MEANS "NO CLAIM ABOUT THE EVIDENCE", which is exactly what the
        # default path means: the line is DERIVED FROM THE MEASUREMENT, not
        # typed by an operator, so its absence is not a disagreement. An
        # explicit line that matches reconciles too; only a DIFFERENT explicit
        # line is a different closure and still refuses.
        #
        # RE-DERIVING HERE INSTEAD WAS THE OTHER OPTION AND IT IS WORSE: it
        # would re-read trunk and the lane refs for a row that is already
        # terminal, so an unreadable repo would turn a clean retry into an
        # error about a close that already happened.
        gitdir, err = landreq._close_repo(lr, repo)
        # A REPOSITORY THIS PROCESS CANNOT READ IS UNMEASURED, NEVER REFUSED.
        # Every rung below that fails on a READ — the repo, the trunk ref, the
        # trunk object — is the census's UNMEASURED answer arriving one layer
        # later, and calling it a refusal is the same collapse finding 4 names.
        # The rungs that refuse are the ones that MEASURED something: already
        # retired, no verdict, landed, a live lane's tip.
        if err:
            return landreq.EXPIRE_UNMEASURED, None, err
        if lr.get("closing_repo_id") == gitdir \
                and (evidence is None
                     or lr.get("close_evidence") == evidence):
            return landreq.EXPIRE_ADMIT, lr, None
        return landreq.EXPIRE_REFUSE, None, landreq._retired_refusal(lr)
    if landreq._retired_by(lr):
        return landreq.EXPIRE_REFUSE, None, landreq._retired_refusal(lr)
    if not lr.get("verdict_ref") or not reviewed:
        return landreq.EXPIRE_REFUSE, None, (
            "%s is an open row — its obligation is the review "
            "itself, and a review nobody has given cannot expire; %s"
            % (lr["id"], _MOOT_OPEN_ROW))
    gitdir, err = landreq._close_repo(lr, repo)
    if err:
        return landreq.EXPIRE_UNMEASURED, None, err
    trunk_ref, pinned, _target, err = landreq._close_trunk(lr, gitdir, trunk)
    if err:
        return landreq.EXPIRE_UNMEASURED, None, err
    # THE MASS-TERMINATION GUARD, first and unconditional.
    if landreq._object_exists(gitdir, pinned) is not True:
        return landreq.EXPIRE_UNMEASURED, None, (
            "the repository at %s cannot prove its own trunk object "
            "— refusing to call any tip absent in a repo that "
            "cannot read a known-good one" % gitdir)
    lane_tips, lane_err = landreq._lane_tips_with_work(gitdir, pinned)
    # RE-DERIVED HERE, AT THE WRITE, against the trunk object this ladder just
    # proved readable. The census may have previewed from a warm projection;
    # nothing is closed on that reading.
    state, why = landreq.expired_measured(lr, gitdir, pinned, lane_tips, lane_err)
    if state != landreq.EXPIRE_ADMIT:
        return state, None, why
    evidence = evidence or why
    row, err = dispatches._record_close_proven(
        lr["id"], "expired", reviewed, evidence=evidence,
        closing_repo_id=gitdir, control_sha=pinned,
        proof_mode="nonauthorizing-verdict-unlanded-tip", dry_run=dry_run)
    if err:
        # THE WRITER REFUSED A ROW THE MEASUREMENT ADMITTED. That is a
        # measured no about the record, not an unreadable input — and it is
        # reachable from a REHEARSAL too, which is the whole of task/2857.
        # While a real write was the only path here, a dry run reported
        # EXPIRE_ADMIT for a row the write would have turned away.
        return landreq.EXPIRE_REFUSE, None, err
    if dry_run:
        out, _ = landreq._rehearsed(row, {
            "dry_run": True, "id": lr["id"], "reason": "expired",
            "closing_repo_id": gitdir, "control_sha": pinned,
            "proof_mode": "nonauthorizing-verdict-unlanded-tip",
            "hold_kind": landreq.review_hold_kind(lr), "why": why})
        return landreq.EXPIRE_ADMIT, out, None
    out, oerr = landreq.get(row["id"])
    return landreq.EXPIRE_ADMIT, out, oerr

def _close_ladder_expired(lr, evidence, repo, trunk, dry_run):
    """The (row, err) door the close table dispatches on.

    The disposition is dropped HERE and nowhere else: a caller that needs to
    bucket UNMEASURED apart from REFUSE calls `_close_expired_one` directly,
    and every caller that only needs "did it close" keeps the two-value shape
    every sibling ladder has."""
    _state, row, err = landreq._close_expired_one(lr, evidence, repo, trunk, dry_run)
    return row, err

def _close_ladder_endorsement_moot(lr, evidence, repo, trunk, dry_run,
                                   lrs=None):
    """THE NONAUTHORIZING-VERDICT-OVER-LANDED-WORK DOOR — the fourth cell of a
    table whose other three cells each already had a reason.

    ONE GIT FACT, FOUR VERDICT SHAPES. `landed`, `resolved` and `discharged`
    all measure THIS ROW'S OWN REVIEWED TIP AGAINST TRUNK and differ only in
    what the row's verdict was: an authorizing APPROVE, a contrary FIX or
    SUPERSEDE, or no verdict at all. The fourth shape is a CONCUR — a verdict
    that authorizes nothing — and it had no cell. Both live specimens that
    forced this door are ancestors of trunk, so `withdrawn` and `expired`,
    the only two doors a concur may open, refuse them BY CONSTRUCTION: each
    requires Git to prove the work ABSENT, and this work is demonstrably
    present. Every one of the ten doors was measured against such a row and
    every one refused.

    WHY NOT A WIDER `landed`, WHICH WOULD OPEN TODAY. `landed` is the APPROVE
    cell of that same table, and its polarity tuple IS the authorization set:
    a concur closed there records an endorsement as the authority a land ran
    under, which is the one thing "concur authorizes nothing" forbids. The two
    other non-approve cells were each given their OWN reason rather than
    folded into `landed`, with that argument stated both times. There is also
    a carved concur-to-`landed` path already — the bounded composition proof —
    and it is gated on prospective opt-in, declared effects, reversibility and
    a whole-tree gate. Widening the tuple would hand that path away for free.

    WHY NOT A WIDER `carried` OR `chain-proof`, WHICH WOULD NOT OPEN AT ALL.
    Both refuse this population one rung BEFORE polarity is consulted, so
    admitting concur to either tuple changes no outcome: `carried` replays the
    row's chain-bound work onto HEAD and asks whether the result IS HEAD,
    which already-landed work cannot answer, and `chain-proof` requires the
    reviewed object to be PRUNED while these objects all read. A widened tuple
    there would be a policy change that buys nothing and costs the rule.

    WHAT THIS DOOR CLAIMS, AND WHY IT GRANTS NOTHING. Exactly two facts, both
    measured here: the verdict AUTHORIZES NOTHING, and the reviewed tip is an
    ANCESTOR of the pinned trunk. Those two together say the landing holds some
    authority that is NOT this row's — because a row that authorizes nothing
    cannot have authorized the land it is being retired against. The close
    therefore records where the authority was not, and transfers none. That is
    the same sentence `withdrawn` and `expired` carry over a measured absence,
    which is why this is the law's own consequence and not an exception to it.

    THE HOLD KIND IS READ FROM THE POLARITY RULE DIRECTLY, NOT THROUGH
    `review_hold_kind`, AND THAT IS NOT A SHORTCUT. That function answers
    `unbillable` for any row whose lifecycle state is not REVIEWED, which is a
    BILLING precondition — and this door's entire population has already moved
    PAST REVIEWED to LANDED, because being on trunk is what defines it. Asking
    it here would refuse every row the door exists for, and would refuse them
    with a sentence saying the row "is not a recorded review at all" about a
    row carrying a recorded review. The verdict is required separately below,
    so nothing is loosened: a row with no verdict never reaches the
    classification.

    ANCESTRY ONLY, WHICH IS `resolved`'S RULING AND FOR ITS REASON. A
    patch-identity match proves an identical delta exists on trunk, never that
    THIS reviewed work landed — and a door that retires a row on a claim about
    somebody else's commit is recording a guess in a binding ledger."""
    reviewed = lr.get("reviewed_tip")
    if lr.get("close_reason") == "endorsement-moot":
        # THE RETRY IDENTITY IS THE REPOSITORY AND THE LINE, and this branch
        # runs FIRST for the reason `expired` states at the same rung: on the
        # default path the line is DERIVED from the measurement below, so a
        # retry arrives with `evidence` None while the standing row holds the
        # derived sentence. Comparing those literally would make every honest
        # retry read as a different closure. None means "no claim about the
        # evidence"; only a DIFFERENT explicit line is a different closure.
        gitdir, err = landreq._close_repo(lr, repo)
        if err:
            return None, err
        if lr.get("closing_repo_id") == gitdir \
                and (evidence is None
                     or lr.get("close_evidence") == evidence):
            return lr, None
        return None, landreq._retired_refusal(lr)
    if landreq._retired_by(lr):
        return None, landreq._retired_refusal(lr)
    if not lr.get("verdict_ref"):
        return None, (
            "%s carries no verdict — this door retires an ENDORSEMENT, and a "
            "row nobody has reviewed is not one. %s"
            % (lr["id"], _UNVERDICTED_DOORS))
    if not reviewed:
        return None, "%s has no reviewed tip to prove on trunk" % lr["id"]
    kind = landreq._hold_kind(lr.get("polarity"), lr.get("tier_kind"))
    if kind != "advisory":
        # THE OTHER THREE CELLS ARE NAMED, because a refusal that only says no
        # sends the operator back around the same ten doors this row already
        # failed. `pre-tier` and `authorization-held` are both APPROVE and both
        # belong to `landed`. Anything else is a contrary or an undeclared
        # verdict, and neither has a single cell: an undeclared verdict over
        # landed work is `landed`'s (`discharged` refuses every verdict row),
        # and a contrary is taken by a different door in each measured mode,
        # so it gets the list `_CONTRARY_DOORS` carries.
        return None, (
            "%s is %s, not an endorsement — this door requires a CONCUR, the "
            "one verdict that authorizes nothing. %s"
            % (lr["id"], kind,
               "An approve over landed work is landed's row."
               if kind in ("pre-tier", "authorization-held")
               else "An undeclared verdict over landed work is landed's "
                    "row. For a contrary: %s" % _CONTRARY_DOORS))
    gitdir, err = landreq._close_repo(lr, repo)
    if err:
        return None, err
    trunk_ref, pinned, _target, terr = landreq._close_trunk(lr, gitdir, trunk)
    if terr:
        return None, terr
    # RUNG 0 — THE POSITIVE CONTROL, and it is not ceremony here either. Every
    # later rung reads this repository, so a repository that cannot produce its
    # own trunk object proves nothing about what reached it, in either
    # direction (stranded's guard, reused exactly as `resolved` reuses it).
    if landreq._object_exists(gitdir, pinned) is not True:
        return None, ("the repository at %s cannot prove its own trunk object "
                      "— refusing to adjudicate a landing over an unreadable "
                      "substrate" % gitdir)
    proof = landreq._landing_proof(gitdir, reviewed, pinned)
    if proof not in ("ancestor", "patch-equivalent", "absent"):
        return None, ("Git could not determine whether %s reached trunk — "
                      "refusing to retire an endorsement over an unknown land "
                      "state" % reviewed[:12])
    if proof == "absent":
        # THE MIRROR DOORS, NAMED. This is the boundary that keeps the door
        # from being a way to retire a concur by choosing a reason: an ABSENT
        # concur is exactly the population `withdrawn` and `expired` were built
        # for, and admitting it here would make this a third absence door
        # wearing a presence door's proof mode.
        return None, ("%s is NOT on trunk — an endorsement over ABSENT work "
                      "is withdrawn's question, or expired's; this door "
                      "proves the opposite fact" % reviewed[:12])
    if proof != "ancestor":
        return None, ("%s reaches trunk only by PATCH IDENTITY, not ancestry "
                      "— that proves an identical delta exists, never that "
                      "THIS endorsed work landed, and an endorsement may not "
                      "be retired against somebody else's commit"
                      % reviewed[:12])
    why = ("%s recorded a %s verdict that authorizes nothing, and its "
           "reviewed tip %s is an ancestor of %s — the landing holds an "
           "authority that is not this row's"
           % (lr["id"], kind, reviewed[:12], trunk_ref))
    evidence = evidence or why
    row, err = dispatches._record_close_proven(
        lr["id"], "endorsement-moot", reviewed, evidence=evidence,
        closing_repo_id=gitdir, closing_trunk_ref=trunk_ref,
        closing_trunk_sha=pinned,
        proof_mode="nonauthorizing-verdict-landed-tip", dry_run=dry_run)
    if err:
        return None, err
    if dry_run:
        out, _ = landreq._rehearsed(row, {
            "dry_run": True, "id": lr["id"], "reason": "endorsement-moot",
            "closing_repo_id": gitdir, "closing_trunk_ref": trunk_ref,
            "closing_trunk_sha": pinned,
            "proof_mode": "nonauthorizing-verdict-landed-tip",
            "hold_kind": kind, "why": why})
        return out, None
    out, oerr = landreq.get(row["id"])
    return out, oerr

def _close_ladder_resolved(lr, evidence, repo, trunk, dry_run, lrs=None):
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

    WHY THE CONFIRMATION MAY BE SUPERSEDE (the integrator's amendment A):
    subsumption spends POLARITY as its check and can demand an APPROVE. This
    door cannot — the TEMPORAL deadlock it exists to end is a reviewer who
    content-verified the work and could bind only SUPERSEDE because every
    receipt in existence PREDATED the dispatch (measured on ef0abe53:
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
        return None, landreq._retired_refusal(lr)
    standing, contradiction = landreq._retirement_stands(lr, lrs, repo=repo)
    if standing:
        return None, standing
    # `lr` is the PROJECTION row, which carries `state`/`verdict_ref` — never
    # the dispatch store's `status`. Gating on `status` here silently refused
    # every real row at the first rung (caught by the integration walk, not by
    # reading). The polarity half is already enforced upstream by
    # _CLOSE_POLARITY["resolved"], so what is left to prove is that a verdict
    # was actually recorded.
    if not lr.get("verdict_ref"):
        return None, ("%s carries no verdict — resolved retires REVIEWED "
                      "debt. %s" % (lr["id"], _UNVERDICTED_DOORS))
    reviewed = lr.get("reviewed_tip")
    if not reviewed:
        return None, "%s has no reviewed tip to prove on trunk" % lr["id"]
    gitdir, err = landreq._close_repo(lr, repo)
    if err:
        return None, err
    trunk_ref, pinned, _target, terr = landreq._trio_trunk(gitdir)
    if terr:
        return None, terr
    # rung 0 — POSITIVE CONTROL. A repo that cannot read its own trunk object
    # proves nothing about what reached it (stranded's guard, reused).
    if landreq._object_exists(gitdir, pinned) is not True:
        return None, ("the repository at %s cannot prove its own trunk object "
                      "— refusing to adjudicate a resolution over an "
                      "unreadable substrate" % gitdir)
    if trunk:
        named_ref, _named_sha, cerr = landreq._canonical_trunk(gitdir, trunk)
        if cerr:
            return None, cerr
        if named_ref not in landreq._trunk_aliases(trunk_ref):
            return None, ("%s is not this row's landing trunk (%s)"
                          % (trunk, trunk_ref))
    # rung 1 — THE RESOLUTION IS MEASURED. Ancestry only: containment by a
    # later commit is exactly what the 87cf5f81 refusal rejects ("a later
    # approve of unrelated work is not a resolution of this one, however
    # plausibly its commit contains the change").
    # NOT `_landed`, and the difference is the whole ruling: _landed admits
    # "patch-equivalent" as well as "ancestor", so it would have let a row in
    # whose delta merely REAPPEARED on trunk under some other commit.
    # The refutation pass predicted this exact hole and it
    # then reproduced live — b71f8dab (whose reviewed tip is NOT an ancestor of
    # trunk; its 3-line delta landed under a different commit) sailed past this
    # rung
    # to the phrase check while the comment above it claimed ancestry-only.
    # Ruling (i): the door stays NARROW and that row goes through #177 instead.
    # A patch-id match cannot distinguish "this work landed" from "someone
    # else wrote the same three lines", and a binding ledger may not guess.
    proof = landreq._landing_proof(gitdir, reviewed, pinned)
    if proof not in ("ancestor", "patch-equivalent", "absent"):
        return None, ("Git could not determine whether %s reached trunk — "
                      "refusing to resolve over an unknown land state"
                      % reviewed[:12])
    if proof == "absent":
        return None, ("%s is NOT on trunk — this row's work is absent, which "
                      "is withdrawn's question, not resolved's"
                      % reviewed[:12])
    if proof != "ancestor":
        # THE DOOR NAMED HERE WAS A CLOSED LOOP FOR TWO WEEKS
        # (task/close-ladder). This refusal used to send patch-equivalent rows
        # to `subsumed`, and `subsumed` refuses on the exact complement:
        # its own git rung requires `original_proof == "absent"`, because
        # SUBSUMED MEANS THE ORIGINAL WORK IS NOT ON TRUNK AND A LATER ROUND
        # REIMPLEMENTED IT. A patch-equivalent original is definitionally the
        # opposite — its very delta is what trunk carries. So the advice
        # pointed at the one door whose premise this row's measurement
        # falsifies, and every row that followed it hit "the original
        # reviewed change is on trunk and is not subsumed". Three live rows
        # sat on that loop for 2 to 14 hours.
        #
        # ...AND THE REPLACEMENT ADVICE WAS ITSELF FALSE FOR MOST OF THE
        # POPULATION, which is the second half of the same bug. `carried` is
        # the only reason whose SUBJECT is this state, so naming it as the
        # subject is correct; naming it as the REMEDY was a prediction about a
        # command the reader had not run, and the prediction is wrong for the
        # large majority of live rows that arrive here. `carried` demands patch
        # identity over the WHOLE range up to the tip, and it is fail-closed on
        # anything it cannot measure. Two modes were measured over the live
        # in-repo board and together they are most of this population: a commit
        # BENEATH the tip that `git cherry` cannot match (a prerequisite that
        # never landed, or a lane that merged trunk), and a tip object the
        # repository no longer holds at all. Both are UNMEASURABLE; both refuse.
        #
        # SO THIS REFUSAL NOW PROMISES NOTHING IT HAS NOT PROVEN. It names
        # `carried` as the only door to ASK, states that asking is not passing,
        # and says what is true when it refuses: the landing is unmeasured, and
        # OPEN is the honest state for it. A refusal that sends the reader in a
        # circle looks like guidance and is worse than one that admits the gap,
        # because the reader spends a round discovering the advice was false
        # and ends with no more information than before.
        #
        # ...AND IT MUST NOT CURE A FALSE PROMISE WITH A FALSE UNIVERSAL. "If
        # carried refuses, no close door covers this row" was a claim about
        # every door, measured against one. `stranded`'s whole subject is a
        # reviewed object this repository no longer holds, and it ADMITS such a
        # row — FIX and SUPERSEDE included — once nothing live still holds the
        # work: no recorded rewrite to a live object, no lane-family ref, no
        # lane-family worktree. So the gone-object mode is named with its own
        # door. That door retires the row as destroyed substrate (it renders
        # CANCELLED) and never records a landing, so naming it asserts nothing
        # about whether this work reached trunk.
        #
        # A GONE TIP REACHES THIS RUNG ONLY THROUGH A KEPT POSITIVE. The live
        # ladder cannot read a patch id out of an object that is not there, so
        # `patch-equivalent` for such a tip is the durable proof kept while the
        # object was still readable (`_kept_landing_proof`). Whatever word the
        # live ladder answers for an unreadable tip, then, it never sends one
        # here; and `carried`'s refusal of it comes from the carriage proof's
        # own object rung, not from the landing ladder. Both halves of the
        # sentence stay true however that word changes.
        #
        # THE ARMS NOW BUILD BOTH MODES. The advice arm alone could not have
        # caught this: its world is ONE `cherry-pick`, the one shape where
        # `carried` is certain to admit. `CarriedRebaseLandedTest` builds the
        # other two beside it — a stack whose prerequisite never landed, and a
        # tip gc pruned after its proof was kept — and measures `carried`,
        # `stranded` and this sentence against each.
        #
        # This rung is NOT weakened by any of it: the door stays ancestry-only
        # exactly as ruling (i) set it. Only the sentence changed.
        return None, ("%s reaches trunk only by PATCH IDENTITY, not ancestry "
                      "— that proves an identical delta exists, never that "
                      "THIS reviewed work landed. NO NEXT DOOR IS PROMISED: "
                      "`carried` is the only reason whose subject is this "
                      "state, but it asks patch identity over the WHOLE range "
                      "up to this tip rather than this one commit, and it is "
                      "fail-closed — an unmatched commit beneath the tip, or "
                      "a tip object this repository no longer holds, both read "
                      "as UNMEASURABLE and refuse. Ask it and read its own "
                      "answer (subsumed cannot take this row: subsumed "
                      "requires the original ABSENT from trunk); if it "
                      "refuses, OPEN is the honest state for a landing nobody "
                      "could measure. A tip object that is gone is a separate "
                      "fact with its own door: `stranded` retires the row as "
                      "destroyed substrate, never as a landing, and refuses "
                      "while anything live still holds the work — a recorded "
                      "rewrite, a lane-family ref or a lane-family worktree"
                      % reviewed[:12])
    # rung 2 — AN INDEPENDENT CONFIRMATION, re-derived from the ledger here
    # and again at replay; never taken from the event.
    current, verdicts, unavailable = dispatches.snapshot_with_verdicts()
    if unavailable:
        return None, "dispatch ledger unavailable: %s" % unavailable
    confirmation, err = landreq._resolve_row_for_diagnosis(
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
    # classifier's confirmation_row cannot drift apart (the defect it closes:
    # the classifier's first cut paraphrased this rung and dropped it).
    if confirmation.get("polarity") not in landreq.CONFIRMATION_POLARITIES:
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
    original_family, _oev, _oanchor, err = landreq._one_approval_family(
        lr.get("author") or "", "original author")
    if err:
        return None, err
    confirmation_family, _cev, _canchor, err = landreq._one_approval_family(
        confirmation.get("recipient") or "", "confirmation recipient")
    if err:
        return None, err
    if original_family == confirmation_family:
        return None, ("confirmation recipient is the author's own family (%s) "
                      "— resolved needs cross-family eyes" % original_family)
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
        proof_mode="resolved-on-pinned-trunk",
        contradiction=contradiction, dry_run=dry_run)
    if err:
        return None, err
    if dry_run:
        return landreq._rehearsed(row, {
            "dry_run": True, "id": lr["id"], "reason": "resolved",
            "confirmation_id": confirmation["id"],
            "overridden_reviewer": lr.get("reviewer"),
            "closing_trunk_ref": trunk_ref,
            "closing_trunk_sha": pinned,
            "resolution": statement})
    return landreq.get(row["id"])

def _close_ladder_carried(lr, evidence, repo, trunk, dry_run, lrs=None,
                          world=None):
    """The work is PROVEN CARRIED at trunk HEAD, and no chain-linked
    discharge exists to say so (task/756).

    `world` IS THE CALLER'S ALREADY-READ `(rows, carriers)` AND CHANGES NO
    RUNG. The ladder reads the dispatch ledger for one thing — the immutable
    (base, tip) this row's chain binds — and a sweep that classifies a
    thousand rows has already read that exact snapshot once. Re-reading it per
    row cost 1.8s per row on the live board, which is what kept the census
    from being able to ask this door whether it would open BEFORE offering the
    row to the owner as closable. Omitted, the ladder reads its own, exactly
    as before; supplied, the answer is about the same instant as the
    projection the caller classified from, which is stricter rather than
    looser (`project_raw`'s own law: one read, both views).

    THE CLASS THIS EXISTS FOR is measured, not hypothetical: rows whose work
    reached trunk but whose discharge was never RECORDED in the chain. A
    READY row whose lander closed nothing. A FIX-verdicted row whose work
    landed anyway. Every existing door refuses them for a correct reason —
    `landed` needs a non-contrary chain polarity, `discharged` needs an
    un-verdicted row, `subsumed` and `resolved` need a confirmation ROW that
    nobody ever wrote, `withdrawn` needs the work to be ABSENT and it is not,
    `stranded` needs the substrate destroyed and it is not. So they sit
    forever, and the anomaly count they hold up has a floor above zero BY
    CONSTRUCTION. That floor is the bug; this verb is its removal.

    THE GATE IS A MEASUREMENT, NOT AN ASSERTION. Authorization comes from
    `dispatches.carriage_proof`, which delegates to `rowworld` and tries two
    witness FAMILIES in strength order (task/close-ladder):

      carriage-replay  replay the row's work onto trunk HEAD from its
                       chain-bound base and ask whether the result IS HEAD;
      reached-trunk    `git cherry` the whole range up to the reviewed tip
                       and require every commit in it to read `-`.

    NEITHER IS `landed`'S PROOF, and the honest comparison matters because
    this docstring used to claim the first family was "strictly stronger"
    full stop — which stopped being the whole truth the moment a second
    family existed. Against `landed`: the replay family asks about HEAD's
    CONTENT, which ancestry and patch identity cannot see at all; the
    reached-trunk family asks the SAME patch-identity question `landed`
    asks but over the ENTIRE range instead of one commit, so a row whose top
    commit cherry-matches while a prerequisite beneath it never landed reads
    landed-eligible and is refused here. On both axes `carried` demands more
    than `landed`, which is what keeps a reason that closes MORE rows from
    being a Goodhart hole.

    WHY THE SECOND FAMILY IS NOT A LOOSENING OF THE FIRST. Both content
    witnesses go quiet as soon as trunk edits the same files again, and
    `_work_pair` correctly has no base at all for a `--new-work` review row —
    it is its own chain root. Widening either witness would have bought those
    rows with a proof that no longer discriminated. The second family buys
    them with a different, independently falsifiable measurement, and a `+`
    anywhere in the range REFUSES — the whole range must be `-`, so the
    second family is not reachable by a row the first would have rejected on
    content.

    EVIDENCE IS REQUIRED AND IS NOT THE PROOF. The prose says WHY nobody
    recorded the discharge — the thing git cannot know. If it were the
    authorization, this verb would be exactly the prose-only close the
    Goodhart guard forbids.

    TRI-STATE ALL THE WAY DOWN, AND STILL NO WITNESS MINTS ABSENCE. True
    authorizes. False stays reserved for a POSITIVE anti-carriage artifact,
    which neither family produces: a replay conflict is silence, and an
    unmatched `git cherry` commit proves only that the instrument could not
    match it — measured 2026-08-12, a lane that merges trunk after its own
    work lands reads `+` for commits trunk demonstrably has. None means NO
    WITNESS AFFIRMED — an unresolvable object, no immutable tip, a git that
    could not run, an empty range, an unmatched commit — and it REFUSES with
    the gap named rather than collapsing into either answer. The refusal for
    an unmatched commit NAMES the shas, so the operator can tell "never
    landed" from "this tip merged trunk" without re-deriving anything. The
    cross-repository case lands here
    honestly: if the row's objects are not in the repository whose trunk is
    being asked about, the proof cannot be stretched to cover it, and the
    refusal says which object is missing and where.

    NOTHING HERE CLOSES A ROW WHOSE WORK IS UNLANDED, and that is the
    property to attack first if this is ever re-opened. The reached-trunk
    witness is EXACTLY the discriminator the fleet already trusts for that
    question, applied more strictly than anywhere else it is used."""
    if not evidence:
        return None, ("close --reason carried needs evidence — the carriage "
                      "proof says the work IS on trunk; the evidence says "
                      "WHY no discharge was ever recorded, which is the half "
                      "Git cannot supply")
    if lr.get("close_reason") == "carried":
        gitdir, err = landreq._close_repo(lr, repo)
        if err:
            return None, err
        if lr.get("closing_repo_id") == gitdir \
                and lr.get("close_evidence") == evidence:
            return lr, None
        return None, landreq._retired_refusal(lr)
    if landreq._retired_by(lr):
        return None, landreq._retired_refusal(lr)
    gitdir, err = landreq._close_repo(lr, repo)
    if err:
        return None, err
    trunk_ref, pinned, _target, err = landreq._close_trunk(lr, gitdir, trunk)
    if err:
        return None, err
    # POSITIVE CONTROL, the house rung (`_close_ladder_stranded` set it): a
    # repository that cannot read a known-good object is not one whose
    # answers mean anything. The trunk sha it just resolved is that object.
    if landreq._object_exists(gitdir, pinned) is not True:
        return None, ("the repository at %s cannot prove its own trunk "
                      "object — refusing to measure carriage in a repo that "
                      "cannot read a known-good one" % gitdir)
    from . import rowworld              # DEFERRED, same reason as dispatches
    if world is not None:
        rows, carriers = world
    else:
        rows, _verdicts, unavailable = dispatches.snapshot_with_verdicts()
        if unavailable:
            return None, ("the dispatch ledger is unreadable (%s), so the "
                          "immutable (base, tip) this row is bound to cannot "
                          "be derived" % unavailable)
        carriers = rowworld._carriers(rows)
    row = rows.get(lr["id"])
    if not isinstance(row, dict):
        return None, ("%s is not in the dispatch ledger, so it has no chain "
                      "to bind a base and tip to" % lr["id"][:12])
    carried, detail = dispatches.carriage_proof(
        row, rows, carriers, gitdir, trunk_ref)
    if carried is None:
        return None, ("carriage could not be MEASURED for %s: %s — carried "
                      "is fail-closed on an unaskable question, exactly as "
                      "stranded is on an unknown object state"
                      % (lr["id"][:12], detail))
    if carried is False:
        # ONLY THE REPLAY FAMILY CAN REACH HERE, so the replay wording is
        # correct wording rather than lazy wording. The reached-trunk family
        # deliberately mints no False: an unmatched `git cherry` commit
        # proves the instrument could not match it, not that trunk lacks it
        # (see `rowworld._reached_trunk`), and it refuses through the None
        # branch above with the unmatched shas named.
        return None, ("trunk HEAD does not carry this row's work: replaying "
                      "%s..%s onto %s does not reproduce it. If the work "
                      "truly landed and was later replaced, that is a "
                      "CONTRARY to adjudicate, not a row to close"
                      % (str(detail.get("base"))[:12],
                         str(detail.get("tip"))[:12], trunk_ref))
    row, err = dispatches._record_close_proven(
        lr["id"], "carried", lr.get("reviewed_tip"), evidence=evidence,
        closing_repo_id=gitdir, closing_trunk_ref=trunk_ref,
        closing_trunk_sha=pinned, carried_base=detail["base"],
        carried_tip=detail["tip"], proof_mode=detail["witness"],
        dry_run=dry_run)
    if err or not dry_run:
        return row, err
    return landreq._rehearsed(row, {
        "dry_run": True, "id": lr["id"], "reason": "carried",
        "proof_mode": detail["witness"],
        "closing_trunk_ref": trunk_ref,
        "closing_trunk_sha": pinned,
        "carried_base": detail["base"],
        "carried_tip": detail["tip"]})

def _chain_authority(rid, rows, verdicts):
    """(frontier row, why-not, proof) — the row whose APPROVE carries rid's
    authority.

    THE PREDECESSOR ASKED THE WRONG QUESTION AND A REVIEWER FALSIFIED IT WITH A
    CONSTRUCTION. It collected every row sharing a chain_root and took the one
    nobody supersedes — a generic SAME-CHAIN EXISTENCE test. Chain membership is
    not descent: a cousin shares a root and carries nothing. Measured repro,
    kept verbatim as this door's primary negative: a root legacy APPROVE landed
    A, a child FIX doomed B, an OPEN unreviewed frontier back at A, prune B —
    the old shape reported would-append and closed the child while the frontier
    stayed OPEN, LAUNDERING unresolved FIX debt. A red row is strictly better
    than a false close.

    SO THE CONTRACT IS NARROWED TO AUTHORITY TRANSFER ALONG ONE EXPLICIT
    SUPERSEDES PATH. The claim is not "the frontier landed, therefore the member
    landed" — that inference is unsound and no condition rescues it. It is that
    a later TERMINAL, GATE-AUTHORIZED APPROVE which is a strict graph descendant
    of the target carried the target's obligations, and that descendant's
    content is on trunk. The pruned object may be absent precisely because the
    durable chain-plus-verdict record is the proof, never object reachability.

    EVERY STEP READS THE ONE SNAPSHOT IT WAS HANDED. Joining independent scans
    is how the parts drift apart between reads; this walks one dict and returns
    one answer. `verdicts` is the SECOND half of that snapshot — the fold's
    {id: (append index, anchor)} — and it is REQUIRED, because authority
    transfer is a claim about ORDER and no folded row carries its own position.

    THE THIRD RETURN IS THE PROOF, not a formatted string: `{"chain_path":
    [...]}`, the raw supersedes edges this walk actually took. The close records
    them and `dispatches._close_event_error` re-walks and compares, so a close
    that names edges this ledger does not have is INERT rather than merely
    unlikely.
    """
    if rid not in rows:
        return None, "no row %s in this snapshot" % rid, None
    # THE CHRONOLOGY IS AN INPUT, NOT A CONVENIENCE. `_fold` keys verdict order
    # on the ledger APPEND INDEX and no folded row carries it, so a caller that
    # cannot supply it cannot be answered — and the previous shape, which did
    # not ask for it at all, is exactly how an approve older than the debt it
    # claims to carry walked through ten green arms.
    if not isinstance(verdicts, dict):
        return None, ("the ledger's verdict order was not supplied, so no "
                      "approve can be placed in time against the debt it "
                      "claims to have carried"), None
    children = {}
    for r in rows.values():
        if isinstance(r, dict) and r.get("supersedes"):
            children.setdefault(str(r["supersedes"]), []).append(r)

    # FORWARD WALK, AND EXACTLY ONE CHILD AT EVERY STEP. A fork means two rows
    # claim to continue the same work, so no single row can be said to carry
    # its authority — that is the "no multiple frontier" clause, enforced where
    # the ambiguity actually appears rather than at the end.
    path, seen, cur = [], {rid}, rid
    while True:
        kids = children.get(str(cur)) or []
        if not kids:
            break
        if len(kids) > 1:
            return None, ("%s has %d successors, so no single row carries its "
                          "authority" % (str(cur)[:12], len(kids))), None
        nxt = str(kids[0].get("id"))
        if nxt in seen:                      # a cycle is corrupt, not a chain
            return None, "supersedes cycle at %s" % nxt[:12], None
        seen.add(nxt)
        path.append(kids[0])
        cur = nxt

    if not path:
        return None, ("%s has no successor, so nothing downstream can carry its "
                      "authority" % str(rid)[:12]), None

    frontier = path[-1]
    # ONE SPELLING OF THE IMMUTABLE RUNGS, shared with the replay arm. These
    # used to be restated here and nowhere else; when replay needed them too,
    # copying would have made two rules that agree until one is sharpened.
    ferr = landreq.chain_frontier_error(frontier, rows, verdicts, rid,
                                [str(r.get("id")) for r in path])
    if ferr:
        return None, ferr, None

    # THE PATH IS THE PROOF, so it travels with the answer rather than being
    # recomputed by whoever wants it. A close that records only the endpoints
    # says an authority transfer happened and cannot show WHICH edges carried
    # it — and those edges are ledger data that survive the history rewrite
    # that took the objects, which is the whole reason this door can exist.
    # THE FORK CENSUS IS THE CUTOFF. Uniqueness is a
    # WRITE-TIME finding: this walk refused above unless EVERY step had exactly
    # one successor, and recording that per-node child set is what lets replay
    # tell the two kinds of sibling apart. A sibling that appears LATER is a new
    # live obligation with its own lifecycle — it may not borrow this row's
    # authority path and it may not dissolve a terminal that was true when
    # written. A sibling recorded INSIDE the census is proof corruption, because
    # the walk that wrote it could not have passed with two.
    path_ids = [str(r.get("id")) for r in path]
    return frontier, None, {
        "chain_path": path_ids,
        "chain_fork_census": landreq.chain_fork_census(rid, rows, path_ids),
    }

def chain_fork_census(rid, rows, path_ids):
    """{node: [child ids]} for every node on the target->frontier path.

    EXTRACTED SO THE LOCKED WRITER CAN RE-DERIVE IT.
    The ladder computed this from an UNLOCKED snapshot and handed it to the
    writer as a kwarg, which is a TOCTOU with a name: a sibling appended
    between that walk and the lock was already in the ledger at close time, and
    the recorded census — taken before it existed — said the node had exactly
    one child. Replay then read that sibling as LATER than the close and let
    the terminal stand. The window is small and the consequence is a fork
    silently inheriting a closed row's authority path.
    """
    children = {}
    for r in (rows or {}).values():
        if isinstance(r, dict) and r.get("supersedes"):
            children.setdefault(str(r["supersedes"]), []).append(str(r["id"]))
    return {node: sorted(children.get(node) or [])
            for node in [str(rid)] + [str(x) for x in (path_ids or [])[:-1]]}

def chain_frontier_error(frontier, rows, verdicts, rid, path_ids):
    """None, or why the RECORDED frontier is not an authorizing one.

    THE IMMUTABLE RUNGS, factored out so the WRITE walk and the REPLAY arm run
    the same code instead of two spellings that agree today. Everything here is
    ledger data that an append cannot retract — lifecycle, polarity, the
    recorded gate token, the writer's stamped `gate_caps` against a FROZEN
    epoch, the obligation resolution along the path, and the verdict order.
    Ruling 2 permits exactly this on replay and forbids the TIER, which lives
    in `_chain_write_authority` and travels as a capture.

    I LEARNED THIS BY DELETING IT. Swapping the replay arm from the full walk
    to a path-integrity check dropped these rungs with the tier, so a
    hand-appended close over an OPEN frontier replayed as terminal — the exact
    laundering the door was narrowed to prevent, reintroduced while curing
    something else. An arm caught it; the factoring is what stops the next
    divergence.
    """
    fid = str((frontier or {}).get("id") or "")
    if str((frontier or {}).get("status") or "").lower() == "open":
        return ("the frontier %s is OPEN — an unreviewed frontier authorizes "
                "nothing" % fid[:12])
    if str((frontier or {}).get("polarity") or "").lower() != "approve":
        return ("the frontier %s is %r, not an approve"
                % (fid[:12], str((frontier or {}).get("polarity")
                                 or "unverdicted")))
    gate_id = str((frontier or {}).get("gate") or "").strip()
    if not gate_id:
        return ("the frontier %s carries no recorded gate token, so its "
                "approve was never land-authorizing" % fid[:12])
    if not dispatches._GATE_ID.fullmatch(gate_id):
        return ("the frontier %s records %r as its gate, which is not a minted "
                "receipt id — a nonempty string is not a capability"
                % (fid[:12], gate_id[:32]))
    index = dispatches.verdict_index(verdicts, fid)
    epoch = dispatches.gate_epoch(rows, verdicts)
    requirement = landreq.gate_requirement(frontier, index=index, epoch=epoch)
    if requirement == "unknown":
        return ("the frontier %s is not land-authorizing: %s"
                % (fid[:12], landreq._unknown_gate_caps_why(frontier)))
    if requirement == "required" and not dispatches._GATE_ID.fullmatch(gate_id):
        return "the frontier %s was approved with no minted gate receipt" % fid[:12]
    if index is None:
        return ("the frontier %s has no accepted verdict event, so its position "
                "in the ledger — and therefore whether it could have carried "
                "anything — is unmeasurable" % fid[:12])
    for node in [str(rid)] + [str(x) for x in (path_ids or [])[:-1]]:
        r = (rows or {}).get(node)
        if not isinstance(r, dict):
            return "the path names %s, which this ledger does not carry" % node[:12]
        polarity = str(r.get("polarity") or "").lower()
        if polarity in ("fix", "supersede") and node != str(rid):
            if not (r.get("close_reason") or "").strip() and \
                    str(r.get("status") or "").lower() not in ("cancelled",
                                                               "closed"):
                return ("%s on the path carries an unresolved %s — chain-proof "
                        "may not close beneath open debt"
                        % (node[:12], polarity.upper()))
        here = dispatches.verdict_index(verdicts, node)
        if here is None:
            if polarity in ("fix", "supersede"):
                return ("%s on the path carries %s debt with no accepted "
                        "verdict event, so it cannot be ordered against the "
                        "approve that claims to carry it"
                        % (node[:12], polarity.upper()))
            continue
        if index <= here:
            return ("the frontier %s predates %s in the ledger, so it cannot "
                    "have carried it — an approve written before the "
                    "obligation existed judged different work"
                    % (fid[:12], node[:12]))
    return None

def chain_path_intact(rid, rows, recorded_path, census, cutoff=None,
                      position=None):
    """None, or why a RECORDED chain proof no longer reads as one.

    THE REPLAY HALF, and it deliberately asks a weaker question than the write
    half. It does NOT re-derive uniqueness: a fork appended after the close is
    somebody else's live obligation, and refusing here would let a later append
    dissolve an append-only terminal — the same resurrection hazard that keeps
    mutable policy out of replay, arriving through topology instead.

    What it DOES verify is that the recorded edges are still edges, and that the
    recorded census is internally coherent with a walk that enforced uniqueness.
    A census naming two children for a node could never have been written by
    that walk, so finding one is tampering and must surface a REFUSAL rather
    than a silent reopen or a silent pass.
    """
    if not isinstance(recorded_path, list) or not recorded_path:
        return "the close records no supersedes path"
    if not isinstance(census, dict) or not census:
        return "the close records no fork census, so its cutoff is unknowable"
    # THE CUTOFF IS WHAT DATES THE CENSUS. Without it a
    # sibling is "later" only because a walk happened not to see it, which is
    # a statement about the walk and not about the ledger.
    if type(cutoff) is not int or cutoff < 0:
        return ("the close records no census cutoff, so a sibling cannot be "
                "dated against it — later by re-derivation is not later by "
                "record")
    if isinstance(position, int) and cutoff > position:
        return ("the census claims a cutoff of %d but this close sits at "
                "ledger position %d — a capture cannot postdate the event it "
                "authorizes" % (cutoff, position))
    walk = [str(rid)] + [str(x) for x in recorded_path[:-1]]
    # DERIVED THROUGH THE SAME FUNCTION THE WRITER USED, so the two sides
    # cannot disagree about what a census IS.
    actual = landreq.chain_fork_census(rid, rows, recorded_path)
    if sorted(census) != sorted(walk):
        return ("the recorded fork census does not cover the recorded path "
                "exactly — the cutoff and the edges disagree")
    for node, step in zip(walk, [str(x) for x in recorded_path]):
        recorded_kids = census.get(node)
        if not isinstance(recorded_kids, list) or len(recorded_kids) != 1:
            return ("the fork census records %d successors for %s, but the "
                     "walk that wrote it refuses any node with more than one "
                     "— this capture could not have been produced honestly"
                     % (len(recorded_kids or []), str(node)[:12]))
        if recorded_kids[0] != step:
            return ("the fork census and the recorded path disagree about "
                    "what follows %s" % str(node)[:12])
        if step not in actual.get(node, []):
            return ("the recorded edge %s -> %s is not in this ledger — the "
                    "edges ARE this proof and one of them is gone"
                    % (str(node)[:12], step[:12]))
        # EXACT, NOT MERELY PRESENT — this is the round-4 cure. Checking only
        # that the recorded child SURVIVES accepts a node that had TWO children
        # at close time and recorded one: the second is in the ledger before
        # the close, so it is not later, and the census that missed it was
        # taken outside the lock. Replay sees the prefix as of this event, so
        # any child visible here existed at the cutoff and must be in the
        # census; one appended afterwards is not visible and cannot fail this.
        if sorted(recorded_kids) != sorted(actual.get(node) or []):
            return ("the fork census records %s for %s but this ledger shows "
                    "%s as of the close — a sibling that existed at the cutoff "
                    "is not a later fork, and a census that missed it was "
                    "taken before the lock"
                    % (sorted(recorded_kids), str(node)[:12],
                       sorted(actual.get(node) or [])))
    return None

def chain_attestation(attester, evidence, now=None):
    """Build one durable tier attestation, or (None, why)."""
    seat, err = dispatches._canonical_recipient(attester or "")
    if err:
        return None, ("an attestation must name the seat that made it: %s"
                      % err)
    text, err = dispatches._clean(evidence, "attestation evidence", 256)
    if err or not str(text or "").strip():
        return None, (err or "an attestation must say WHAT was read — a "
                             "recorded act with no evidence is a default "
                             "wearing a signature")
    return {"v": 1, "attester": str(seat), "evidence": text,
            "ts": now or pk.now_ts()}, None

def _chain_authority_anchor(frontier, tier_state, requirement, attestation):
    """The content anchor binding one WRITE-TIME authority decision.

    Replay cannot re-ask a mutable policy, so it verifies STRUCTURE instead:
    recompute this over the recorded fields and a tampered capture stops
    matching. That is the whole of what an append-only terminal can honestly
    promise about a decision made against a world that has since moved.
    """
    return dispatches._proof_anchor(landreq.CHAIN_ATTEST_ALGORITHM, {
        "frontier": str((frontier or {}).get("id") or ""),
        "frontier_tip": str((frontier or {}).get("reviewed_tip")
                            or (frontier or {}).get("ref") or ""),
        "gate": str((frontier or {}).get("gate") or ""),
        "tier_state": tier_state,
        "gate_requirement": requirement,
        "attestation": attestation})

def _chain_write_authority(frontier, rows, verdicts, attester=None,
                           attest=None):
    """(authority bundle, why) — the WRITE-TIME half `_chain_authority` refuses.

    RULING ONE, AND IT REVERSES MY OWN ROUND-2 CALL. I had admitted an
    UNMEASURABLE tier silently, reasoning that refusing on absence downgrades
    every aged-out legitimate row — which is true, and is NOT a licence to mint
    authority from ignorance. The line falls here:
    REFUSING on absence and AUTHORIZING on absence are different acts, and a
    security boundary may only do the first by default. The 183-row cost I
    measured is an argument for building a DOOR, never for leaving the wall
    open.

    SO THE DOOR IS EXPLICIT AND RECORDED. `outside` refuses, always. `ok` and
    `none` admit. `unknown` admits ONLY against a per-row attestation naming
    the seat that made it and what that seat read — a deliberate, logged act
    that survives replay as a captured anchor. Nothing is admitted silently,
    and no aged row is silently condemned either.
    """
    fid = str((frontier or {}).get("id") or "")
    index = dispatches.verdict_index(verdicts, fid)
    epoch = dispatches.gate_epoch(rows, verdicts)
    requirement = landreq.gate_requirement(frontier, index=index, epoch=epoch)
    try:
        tier_state, tier_why = dispatches.approval_tier_for_verdict(frontier)
    except Exception:                   # noqa: BLE001 — fail closed
        tier_state, tier_why = "unknown", "approval-tier check raised"
    if tier_state == "outside":
        return None, ("the frontier %s was approved by a reviewer the approval "
                      "tier does not permit (%s) — no attestation admits a "
                      "MEASURED violation" % (fid[:12], tier_why))
    attestation = None
    if tier_state == "unknown":
        # UNKNOWN HAS THREE FACES AND ONLY ONE IS ATTESTABLE.
        # "The tier could not be evaluated" collapses a
        # LEGACY ABSENCE — a verdict written before the author stamp existed —
        # together with a MALFORMED row proof and an UNREADABLE policy. Only
        # the first is a gap a reader can close by looking at the row: the
        # other two are the world contradicting itself, and a per-row
        # attestation cannot repair either. Letting one seat's reading paper
        # over a corrupt policy file would make the door exactly the laundry
        # it was built to replace.
        # TRUNK'S KIND VOCABULARY DECIDES THIS, AND IT IS STRICTLY RICHER
        # THAN THE STRUCTURAL SPLIT I WROTE (compose seam, c014b1e2 x this
        # chain). My three-way test could only see two worlds — the row's own
        # stamp and the policy file — so a TRANSIENT unknown (a LIVE step
        # failed; the stored evidence is intact and the next read may answer)
        # and an UNNAMED one (the recipient is not one canonical seat, so
        # there is no seat to be in or out of the tier at all) would both have
        # fallen through to ATTESTABLE. Signing for either is wrong in a way
        # nobody would notice: the first wants ONE re-read, the second has no
        # subject to attest about. The kind is consulted first and my probes
        # remain the derivation where no kind was measured.
        kind = dispatches.tier_unknown_kind(tier_state)
        if kind == dispatches.TIER_TRANSIENT:
            return None, ("the frontier %s has a TRANSIENT unevaluable tier "
                          "(%s) — the stored evidence is intact and the next "
                          "read may answer, so re-run once; an attestation "
                          "would sign for a question nobody has asked yet"
                          % (fid[:12], tier_why))
        if kind == dispatches.TIER_UNNAMED:
            return None, ("the frontier %s names a reviewer that is not one "
                          "canonical seat (%s) — there is no seat to be inside "
                          "or outside the tier, so there is nothing an "
                          "attestation could be about" % (fid[:12], tier_why))
        if kind == dispatches.TIER_DAMAGED:
            return None, ("the frontier %s has a DAMAGED approval-tier proof "
                          "(%s) — a stored proof that is malformed or "
                          "self-contradicting is a CONTRADICTION, not an "
                          "absence, and no attestation admits one"
                          % (fid[:12], tier_why))
        # TIER_DARK is the attestable world, and it is exactly what my own
        # structural test called a LEGACY ABSENCE: nothing is stored to
        # re-read, and no command reaches an answer that does not exist.
        #
        # UNCLASSIFIED IS WHERE MY PROBES STAY ALIVE, and gating them on
        # `kind is None` made them dead code — `tier_unknown_kind` never
        # returns None for an unknown, it returns UNCLASSIFIED. Blanket-
        # refusing that would have deleted my three-way split outright, which
        # is the one thing this compose may not do. So for an unknown whose
        # kind nobody MEASURED, the probes answer the same question by direct
        # measurement: a row that CLAIMS author evidence it cannot back is a
        # contradiction, an unreadable policy is a broken world, and what
        # remains is structurally the DARK case. That derives a DECISION from
        # evidence — it does not invent a trunk classification, which is what
        # `TIER_UNCLASSIFIED` exists to forbid.
        if kind in (None, dispatches.TIER_UNCLASSIFIED) \
                and dispatches.author_evidence_claimed(frontier):
            return None, ("the frontier %s CLAIMS verdict-time author "
                          "evidence and it does not read (%s) — a malformed "
                          "proof is a CONTRADICTION, not an absence, and no "
                          "attestation admits one" % (fid[:12], tier_why))
        if not landreq._approval_policy_readable(frontier.get("repo_id")):
            return None, ("the approval-tier policy itself is unreadable or "
                          "malformed (%s) — an attestation records what a seat "
                          "read about a ROW and cannot repair a policy nobody "
                          "can read; fix the prior, then re-close"
                          % tier_why)
        if not attest:
            return None, (
                "the frontier %s has an UNMEASURABLE approval tier (%s), and "
                "an unmeasurable tier is not an admissible one. Capability "
                "absence is not proof of authority. Re-close with `--attest "
                "\"<what you read>\"` as the discharging seat: that records a "
                "per-row, durable act naming who admitted it and on what "
                "evidence, instead of admitting it silently"
                % (fid[:12], tier_why))
        attestation, err = landreq.chain_attestation(attester, attest)
        if err:
            return None, err
    elif attest:
        return None, ("the frontier %s has a MEASURED approval tier (%s), so "
                      "an attestation would record a judgement nothing asked "
                      "for — drop --attest" % (fid[:12], tier_state))
    return {"chain_tier_state": tier_state,
            "chain_gate_requirement": requirement,
            "chain_attestation": attestation,
            "chain_authority_anchor": landreq._chain_authority_anchor(
                frontier, tier_state, requirement, attestation)}, None

def _close_ladder_chain_proof(lr, evidence, repo, trunk, dry_run, lrs=None,
                              attester=None, attest=None):
    """The work landed, and a history rewrite took the object that proved it.

    THIS EXISTS BECAUSE EVERY MEASURING DOOR CORRECTLY REFUSED, and that is
    the argument for it rather than against it. A chain whose reviewed-tip
    OBJECTS were pruned cannot answer the question any other reason asks:
    `landed` needs a proof it cannot compute, `carried` is fail-closed on an
    unaskable carriage, `subsumed` and `resolved` need a confirmation bound
    into the chain, `withdrawn` would record the work as ABSENT when it is
    not, and `out-of-scope` is blocked by declared resolution debt. Nine
    refusals, every one right, and eleven rows rendering CHANGES_REQUESTED
    forever with an anomaly floor above zero BY CONSTRUCTION.

    IT IS NOT A MEASUREMENT OF CARRIAGE AND MUST NEVER READ AS ONE. The
    question `carried` answers — did THIS row's diff reach trunk — died with
    the base object and no verb can resurrect it. What this proves is
    strictly weaker and strictly stated: the chain's FRONTIER content is on
    trunk, and this row sits beneath that frontier in the chain. The step
    from frontier to row rides on what supersession MEANS, which is a
    semantic assumption rather than a measurement. So the close renders as
    CLOSED_CHAIN_PROOF, never as carriage, and a reader who wants carriage
    must go and find that it is unavailable rather than be handed a
    lookalike.

    THREE CONDITIONS, ALL DATA, NONE OF THEM PROSE:
      (1) the row's own reviewed tip OBJECT IS ABSENT from the repository —
          this is the discriminator, and it fails CLOSED both ways: a tip
          that is PRESENT belongs to a measuring door, and a repository that
          cannot answer at all proves nothing;
      (2) the chain FRONTIER's tip is `patch-equivalent` (or `ancestor`) on
          trunk, RE-MEASURED here through `_landing_proof` rather than read
          back from a recorded field — a remembered proof carries only its
          author's confidence;
      (3) a same-chain APPROVE exists. Its CONTENT is a human judgement and
          this makes no use of it; its EXISTENCE is a ledger fact meaning a
          cross-family reviewer looked at this chain. That is the human leg,
          and it is checked, not trusted.
    """
    if not evidence:
        return None, ("close --reason chain-proof needs evidence — the three "
                      "conditions say the chain's work is on trunk; the "
                      "evidence says WHY this row's own object is gone, "
                      "which is the half the ledger cannot supply")
    # NO SECOND RETRY AUTHORITY. A retry must not short-circuit
    # on closing_repo_id + evidence and return SUCCESS
    # BEFORE `_close_trunk` runs — or the same evidence against another,
    # or a NONEXISTENT, trunk reconciles cleanly. Two retry identities existed
    # and the weaker one answered first. Now a chain-proof retry re-runs the
    # WHOLE ladder (repo, trunk, object-absence, authority, landing proof) and
    # reconciles at the writer against the full canonical tuple, which is the
    # only place retry identity is defined.
    retry = lr.get("close_reason") == "chain-proof"
    if not retry and landreq._retired_by(lr):
        return None, landreq._retired_refusal(lr)
    gitdir, err = landreq._close_repo(lr, repo)
    if err:
        return None, err
    trunk_ref, pinned, _target, err = landreq._close_trunk(lr, gitdir, trunk)
    if err:
        return None, err
    # THE HOUSE POSITIVE CONTROL: a repository that cannot read a known-good
    # object is not one whose ABSENCE answers mean anything — and absence is
    # this reason's whole first condition, so the control is load-bearing here
    # rather than ceremonial.
    if landreq._object_exists(gitdir, pinned) is not True:
        return None, ("the repository at %s cannot prove its own trunk "
                      "object — refusing to read an absent tip as pruned in "
                      "a repo that cannot read a present one" % gitdir)
    reviewed = lr.get("reviewed_tip")
    if not landreq._sha(reviewed):
        return None, ("%s cites no full reviewed tip, so there is no object "
                      "whose absence could be established"
                      % str(lr.get("id"))[:12])
    present = landreq._object_exists(gitdir, reviewed)
    if present is not False:
        return None, ("%s is not pruned in %s (object reads %r) — a row whose "
                      "tip still EXISTS belongs to a measuring door, not to "
                      "chain-proof" % (reviewed[:12], gitdir, present))
    rows, verdicts, unavailable = dispatches.snapshot_with_verdicts()
    if unavailable:
        return None, ("the dispatch ledger is unreadable (%s), so neither the "
                      "chain frontier nor its approve can be established"
                      % unavailable)
    # ONE JOINED ANSWER FROM ONE SNAPSHOT. Descendant path, lifecycle,
    # polarity, recorded authorization and obligation resolution are decided
    # together in _chain_authority rather than joined from separate scans here
    # — the parts drift apart between reads, and the drift is invisible.
    # NAMED `authority`, NOT `proof`, AND THE ARMS CAUGHT WHY: `proof` is
    # already this function's landing-proof STRING twelve lines below, so the
    # first cut shadowed it and every non-dry-run close raised TypeError. Two
    # different proofs in one door earn two different names.
    frontier, why, authority = landreq._chain_authority(lr["id"], rows, verdicts)
    if frontier is None:
        return None, ("chain-proof refuses: %s" % why)
    ftip = frontier.get("reviewed_tip") or frontier.get("ref")
    if not landreq._sha(ftip):
        return None, ("the authorizing descendant %s cites no full tip, so its "
                      "landing cannot be re-measured"
                      % str(frontier.get("id"))[:12])
    # RE-MEASURED, NEVER READ FROM A RECORDED FIELD. A stored landing verdict is
    # a claim about a trunk that has since moved.
    # MEASURED AGAINST THE PIN, NOT THE REF (round 3). This asked
    # `_landing_proof` about `trunk_ref` — a MOVING name — and then recorded
    # `closing_trunk_sha=pinned` as though that were the thing measured. Two
    # different facts wearing one field: the answer came from wherever the ref
    # pointed at that instant, and the audit trail claimed a commit. A proof
    # that cannot be re-run against its own recorded anchor is not durable, and
    # durability across a moving trunk is this door's entire premise.
    proof = landreq._landing_proof(gitdir, ftip, pinned)
    if proof not in ("patch-equivalent", "ancestor"):
        return None, ("the authorizing descendant %s does not land on the "
                      "pinned trunk commit %s (%s, reads %r) — authority "
                      "cannot transfer from content that is not on trunk"
                      % (ftip[:12], pinned[:12], trunk_ref, proof))
    # THE FRONTIER *IS* THE APPROVE. The predecessor searched the chain for any
    # row with an approve polarity and took the first — which admits an
    # ancestor, a sibling and an ungated verdict, none of which transfer
    # anything. _chain_authority has already proven this row is a strict
    # descendant, terminal, and gate-authorized as recorded.
    approve = frontier
    authority_bundle, why = landreq._chain_write_authority(
        frontier, rows, verdicts, attester=attester, attest=attest)
    if authority_bundle is None:
        return None, ("chain-proof refuses: %s" % why)
    row, err = dispatches._record_close_proven(
        lr["id"], "chain-proof", lr.get("reviewed_tip"), evidence=evidence,
        closing_repo_id=gitdir, closing_trunk_ref=trunk_ref,
        closing_trunk_sha=pinned, carried_base=ftip,
        carried_tip=approve.get("id"), proof_mode="chain-proof",
        chain_path=authority["chain_path"],
        chain_fork_census=authority["chain_fork_census"],
        dry_run=dry_run, **authority_bundle)
    if err or not dry_run:
        return row, err
    return landreq._rehearsed(row, {
        "dry_run": True, "id": lr["id"], "reason": "chain-proof",
        "proof_mode": "chain-proof",
        "closing_trunk_ref": trunk_ref,
        "closing_trunk_sha": pinned,
        "pruned_tip": reviewed,
        "frontier_id": frontier.get("id"),
        "frontier_tip": ftip,
        "frontier_landing": proof,
        "approve_id": approve.get("id"),
        "tier_state": authority_bundle["chain_tier_state"],
        "attested": bool(authority_bundle["chain_attestation"])})

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
        return None, landreq._retired_refusal(lr)
    if landreq._retired_by(lr):
        return None, landreq._retired_refusal(lr)
    if lr.get("verdict_ref"):
        return None, ("%s is a verdict row — its own polarity owns a door; "
                      "discharged is for a row that never got one" % lr["id"])
    # The projection rows and the dispatch snapshot carry the same ids; the
    # walk needs the store's rows (chain edges live there), and the landed
    # leg probes the discharging row's own recorded landing.
    current, unavailable = dispatches.snapshot()
    if unavailable:
        return None, "dispatch ledger unavailable: %s" % unavailable
    # A HELD ROW IS ASKED ABOUT ITS HOLD BEFORE ITS CHAIN, and the refusal
    # says what is owed instead. The writer asks the same predicate under the
    # lock, so this is the sentence, never the gate.
    held = current.get(lr["id"])
    if isinstance(held, dict) and held.get("status") == "held":
        herr = dispatches.held_discharge_error(held)
        if herr:
            return None, ("close --reason discharged refuses HELD %s: %s. "
                          "What is owed: %s"
                          % (lr["id"][:12], herr,
                             dispatches.held_rung_remedy(held)))
    tier, by, why = dispatches.discharging_row(
        lr["id"], current,
        is_landed=lambda tip: dispatches._writer_landed(current, tip))
    if tier is None:
        return None, "close --reason discharged refused: %s" % why
    dis_row = current.get(by) or {}
    row, err = dispatches._record_close_proven(
        lr["id"], "discharged", None, evidence=evidence,
        discharging_id=by,
        discharging_tip=dis_row.get("reviewed_tip"),
        discharge_tier=tier, dry_run=dry_run)
    if err:
        return None, err
    if dry_run:
        return landreq._rehearsed(row, {
            "dry_run": True, "id": lr["id"], "reason": "discharged",
            "discharging_id": by,
            "discharging_tip": dis_row.get("reviewed_tip"),
            "discharge_tier": tier})
    return landreq.get(row["id"])

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
    row, err = landreq._resolve_row_for_diagnosis(current, rid)
    if err:
        return None, err
    if not evidence:
        return None, ("close --reason out-of-scope needs evidence — why the "
                      "review obligation is moot")
    if row.get("status") == "verdict":
        if row.get("polarity") in ("fix", "supersede"):
            # `superseded` OR `withdrawn` IS NOT THE WHOLE ANSWER: a FIX
            # rebase-landed with no chain link refuses both and is taken by
            # `carried`; a FIX merged by ancestry and later confirmed is
            # `resolved`'s. The debt gets the same measured list the landed
            # door gives it.
            return None, ("%s carries a declared resolution debt — "
                          "attestation alone never retires it. %s"
                          % (row["id"], _CONTRARY_DOORS))
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
    trunk_ref, pinned, _t, terr = landreq._trio_trunk(gitdir_ship)
    if terr:
        return None, ("%s — whether this work SHIPPED is UNKNOWN and "
                      "out-of-scope is FAIL-CLOSED on it" % terr)
    tier, by, _why = dispatches.discharging_row(
        row["id"], current,
        is_landed=lambda tip: landreq._landed(gitdir_ship, tip, pinned) is True)
    if tier:
        # A REFUSAL THAT NAMES NO DOOR IS A DEAD END, and this one has a door:
        # the `discharged` ladder is the other half of this route. This half
        # refuses the wrong terminal; that half supplies the right one. Read
        # the LIVE reason list rather than hard-coding the name — that door
        # landed in a separate lane, and naming a reason this build does not
        # have would trade a dead end for a wrong turn.
        #
        # AND THE DOOR IS OFFERED WITH ITS CONDITION, because the two halves
        # do not read the same landing. This half asks git whether the
        # discharger's tip is on trunk; `discharged` reads the discharger's
        # RECORDED `landed` close (`_writer_landed`). Measured on a build row
        # whose successor was merged and not yet closed: this refusal fires
        # and `discharged` refuses, then admits once the successor is closed
        # landed. An unconditional "close it `--reason discharged`" is false
        # for the whole window between a merge and its close.
        route = ("close it `--reason discharged` once %s is itself closed "
                 "landed — that door reads the discharging row's RECORDED "
                 "landing and refuses while it has none" % str(by)[:12]
                 if "discharged" in landreq.CLOSE_CLI_REASONS
                 else "it wants the reason that proves the land")
        return None, ("%s is not out of scope — its work is ON TRUNK, "
                      "discharged by %s (%s). Closing it here would record "
                      "shipped work as unwanted; %s"
                      % (row["id"], str(by)[:12], tier, route))
    # LIVENESS BLOCK (D1's law applied here too): a live claim is never
    # orphaned through this door. Tri-state — unreadable refuses.
    gitdir, tiprow = row.get("repo_id"), row.get("tip")
    matches, ferr = landreq._lane_family_refs(gitdir, row.get("lane"), row.get("ref"))
    if ferr:
        return None, ("%s — the dispatched work's liveness is UNKNOWN and "
                      "out-of-scope is FAIL-CLOSED on it" % ferr)
    for ref, obj in matches:
        live = obj == tiprow
        if not live and tiprow:
            ancestry = landreq._ancestry(gitdir, tiprow, obj)
            if ancestry == landreq.UNDETERMINED:
                return None, ("Git could not relate the dispatched tip to "
                              "lane-family ref %s — liveness is UNKNOWN and "
                              "out-of-scope is FAIL-CLOSED on it" % ref)
            live = ancestry == landreq.ANCESTOR
        if live:
            return None, ("the dispatched work is live at %s — closing the "
                          "row orphans a live claim; rebind the obligation "
                          "(helm dispatch rebind) or finish the lane" % ref)
    wts, werr = landreq._lane_family_worktrees(gitdir, row.get("lane"),
                                       row.get("ref"))
    if werr:
        return None, ("the repository's worktree registry could not be read "
                      "(%s) — the dispatched work's liveness is UNKNOWN and "
                      "out-of-scope is FAIL-CLOSED on it" % werr)
    for wt in wts:
        dirty = landreq._worktree_dirty(wt.get("path"))
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
    local_ref, up_ref = landreq._trunk_refs(gitdir, cache)
    # THE PINNED COMMIT, NOT THE NAME — the same class as `_git_observe` and
    # `_base_behind`, reached by a shorter route: this site never resolved at
    # all, it handed `_landed` the ref NAME directly. Nothing here can corrupt a
    # witness (this is a close ladder, outside any projection read-set), and the
    # annotation gates nothing in either direction — but "landed_check: true"
    # measured against a binding that a concurrent fetch was moving is still a
    # claim helm cannot re-derive, and "unknown" is already this field's honest
    # value for a trunk it could not pin.
    target = landreq._resolved_ref(gitdir, up_ref or local_ref, cache) \
        if (up_ref or local_ref) else ""
    if tiprow and target:
        state = landreq._landed(gitdir, tiprow, target)
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

def _close_label(lr):
    """The display label of one close terminal: the reason, upper-cased, and
    LANDED_REWRITTEN when the landing proof went through the recorded
    translation — the sha changed, the change landed."""
    if lr.get("close_reason") == "landed" and str(
            lr.get("close_proof_mode") or "").startswith("translated-"):
        return "LANDED_REWRITTEN"
    if lr.get("close_reason") == "landed" and \
            lr.get("close_proof_mode") == landreq.CONTENT_EQUIVALENT:
        # THE WEAKEST MECHANICAL RUNG SAYS SO ON THE SURFACE. A reader seeing
        # a bare LANDED cannot tell ancestry from a context-erased content
        # match, and those are not the same claim — the second ignores context
        # by construction. It is the same reason LANDED_REWRITTEN exists.
        return "LANDED_CONTENT"
    return str(lr.get("close_reason") or "").upper().replace("-", "_")

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
    for _ in range(landreq._MAX_CHAIN_HOPS):
        nxt, ambiguous = landreq._map_lookup(translations, cur)
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


# ---------------------------------------------------------------------------
# PUBLISH BACK ONTO THE LEDGER
# ---------------------------------------------------------------------------
def _publish():
    """Bind every name this module owns back onto `landreq`.

    ONE SOURCE OF TRUTH. `landreq._OWNER_NAMES` is both the tuple the
    retired-name rung reads and the tuple this loop walks, so the declaration
    and the runtime binding cannot drift -- there is no second list to forget.
    """
    for module, names in landreq._OWNER_NAMES:
        if module != __name__.rsplit(".", 1)[-1]:
            continue
        for _name in names:
            setattr(landreq, _name, globals()[_name])


_publish()
