#!/usr/bin/env python3
"""helm seats — the gate exemption's proof: a held lane lease is PARKED ON
SOMEONE ELSE'S VERB (a review pending, an approve awaiting the land, a
source-clean hold), so the stop may end and the lease stays.

SPLIT OUT OF `seats_delegation` whole (task/2387, when the proof grew its
repository and bound-branch legs past that module's line budget); every
consumer imports it from here, and `seats` re-exports `_gate_pending` and
`_lane_stem`. The room comes from `seats_delegation._lease_worktree`,
imported where it is called, so neither module imports the other at load.
"""


def _lane_stem(name):
    """ONE normalisation, owned by landreq (#156): this module's own copy
    stripped only -review/-rN/-xrev and kept the lane/ prefix, so the
    gate-pending exemption below family-matched DIFFERENTLY from the
    discharge admit — the same silent half-match, one module over. The
    stem one work family travels under is landreq._lane_stem's answer:
    family prefixes AND round/role suffixes stripped, case-folded."""
    from . import landreq
    return landreq._lane_stem(name)
def _room_repo_id(room):
    """The git COMMON DIR `room` belongs to, asked of the dispatch writer's
    own function (`dispatches._repo_info`, which stamps each row's
    `repo_id`), so the two sides of the comparison cannot drift. None is
    UNKNOWN, and it blocks."""
    try:
        from . import dispatches
        return (dispatches._repo_info(room) or {}).get("repo_id") or None
    except Exception:                 # noqa: BLE001 — unreadable, not matching
        return None


def _lane_family(row, family, room_id):
    """Whether dispatch `row` is lane family `family`'s IN THIS REPOSITORY.

    THE REPOSITORY FIRST. The dispatch ledger is global, and both legs below
    are strings another repository reproduces for free: a sibling clone
    carries the same branch name, and a commit id resolves in every store
    holding the object. A row with no `repo_id`, or another one, is no
    correspondence.

    TWO WAYS TO CORRESPOND. The row's `lane` is a LABEL its sender typed;
    its `ref_branch` is the local branch the write door found at the
    reviewed commit (`refs/heads/<branch>`), which git derived and nobody
    typed. A label that is not the lease's family (`-port` is no round
    suffix) with a bound branch that names the lane exactly is the lane's
    row; neither matching is no row of this lane, as before."""
    if not room_id or str(row.get("repo_id") or "") != room_id:
        return False
    if _lane_stem(row.get("lane")) == family:
        return True
    ref_branch = str(row.get("ref_branch") or "")
    return ref_branch.startswith("refs/heads/") and \
        _lane_stem(ref_branch[len("refs/heads/"):]) == family


def _gate_pending(resource, snap=None, cwd=None):
    """(dispatch_id, reviewer, stage) POSITIVELY proving lease `resource`
    backs a lane that is PARKED ON SOMEONE ELSE'S VERB, else None. Two
    stages, one TWO-PART proof — the dispatch row must correspond to THE
    LANE (its recorded lane is the lease's lane, stem-family matched) AND
    its --ref must match the claimed worktree's current HEAD (full or as
    its abbreviation). Ref alone never suffices: every fresh lane starts
    at some existing tip, so ANY open review row at that tip would
    otherwise exempt every lane freshly branched from it — measured live
    2026-07-31, lane approved-lane-exempts-stop branched at an unlanded
    stack tip was exempted by ANOTHER lane's review row (1267f509,
    verdict-polarity-consolidated) purely because the refs coincided.
    Lane name alone never suffices either (a NAME is not proof of a gate
    at THIS tip); the exemption needs BOTH. The row is a kind=review
    dispatch that is either

      * OPEN (stage "pending") — a reviewer holds the verdict, or
      * a VERDICT row with polarity=approve AND a bound gate token
        (stage "approved") — the review is DONE, the gate is proven, and
        the only remaining verb is the INTEGRATOR's land, or
      * a HELD row with a recorded holder and a source-clean tip (stage
        "source-clean", task/3097): its HELD tip is what must equal HEAD.

    Each later arm closes the hole its predecessor left one lifecycle stage
    on (verb-designed-noun-lifecycle-holed): a finished review must not block
    a holder whose only owed verb is someone else's. A FIX or SUPERSEDE verdict
    does NOT exempt (rework is owed); an UNGATED approve authorizes nothing.

    Same verifiable-saturated-state law as _delegated_build, one lifecycle
    stage later (LEASE-HELD-WHILE-GATE-PENDING, measured live 2026-07-29:
    three seats force-continued all evening on gate-pending leases, and
    release-or-finish are both wrong — release opens the lane mid-gate,
    finishing is the reviewer's move). UNKNOWN → BLOCK at every rung: an
    unparseable lease, an unrev-parseable HEAD, an unreadable ledger, no
    matching row, a CANCELLED row, a fix/supersede/ungated verdict, a ref
    that does not match the CURRENT head (a moved head is new work, not a
    pending gate), a row recorded for a DIFFERENT lane family, an
    unreadable OWED FRONTIER or a snapshot it cannot index, a room whose
    repository cannot be read, and rows from ANY OTHER repo (or none) all
    return None and the claims block stands exactly as before. Recomputed
    on EVERY stop, never cached. No leg of the proof is evidence alone:
    lane NAME without the ref↔HEAD match is a coincidence of naming, ref
    without the lane one of branching, and both without the repository one
    of cloning (`_lane_family`, which also reads a row's bound
    `ref_branch` as naming its lane, task/2387).

    `snap` is the caller's ALREADY-READ ledger state, threaded rather than
    re-read: one stop asks this ledger twice (here, and again for the release
    advice's review read) and the read measured 190ms on a 1,577-event
    ledger. Omitted, it reads its own — the contract is unchanged.

    `cwd` anchors the lane room the way `_lease_worktree` takes it: the stop
    resident, which runs from no checkout, anchors each lease at the
    repository its claim recorded."""
    from .seats_delegation import _lease_worktree   # a load-time cycle
    room = _lease_worktree(resource, cwd=cwd)
    if not room:
        return None
    room_id = _room_repo_id(room)       # the repo the HEAD below is read from
    if not room_id:
        return None
    family = _lane_stem(str(resource).split(":", 2)[2])
    try:
        from . import dispatches, vcs
        head = vcs.backend(room).head_sha(room)
    except Exception:
        return None
    if not head:
        return None
    if snap is None:
        try:
            rows = dispatches.rows()
        except Exception:
            return None           # unreadable ledger: UNKNOWN, blocks
    else:
        rows = snap
    # THE OWED FRONTIER IS ONE AUTHORITY INPUT, resolved before any row is
    # read (task/2387). A superseded parent and the successor carrying its
    # work BOTH match this lane and head, so only the frontier's rows feed
    # the PENDING arm; a closed row still feeds the approved arm. An eligible
    # row is what GRANTS the exemption, so a frontier that cannot be measured
    # — `owed` raising, or a snapshot that is not the mapping it indexes —
    # answers BLOCK for the whole predicate. Keeping every row eligible on
    # failure was fail-OPEN (a lane read as in gate because the walk threw),
    # and filtering only the open half would let an approve beside an
    # unmeasured open row read as the lane's one gate. A measured EMPTY
    # frontier is a valid answer and still decides.
    if not isinstance(rows, dict):  # snapshot() keys rows by dispatch id
        return None
    try:
        owed_ids = frozenset(r.get("id") for r in dispatches.owed(rows))
    except Exception:
        return None
    rows = rows.values()

    found = stage = None
    for row in rows:
        if not isinstance(row, dict):
            continue
        if row.get("kind") != "review":
            continue
        # LANE CORRESPONDENCE, half of the two-part proof: the row must be
        # THIS lane's (stem-family match — round/role renames are one
        # family), or a sibling lane freshly branched at the same tip
        # inherits a gate it was never in
        if not _lane_family(row, family, room_id):
            continue
        status = row.get("status")
        ref = str(row.get("ref") or "")
        if status == "held":      # a plain or holderless hold keeps blocking
            tip = str(row.get("source_clean_tip") or "")
            if not (tip and row.get("hold_actor")):
                continue
            ref, row_stage = tip, "source-clean"
        elif status in dispatches.CLOSED_STATES:
            # the APPROVED arm: only a polarity=approve verdict carrying a
            # BOUND gate token survives closure — cancelled rows, fix and
            # supersede verdicts (rework owed), and ungated approves (they
            # authorize nothing) all fall through and keep blocking
            if not (status == "verdict"
                    and row.get("polarity") == "approve"
                    and dispatches._GATE_ID.fullmatch(
                        str(row.get("gate") or ""))):
                continue
            row_stage = "approved"
        else:
            if row.get("id") not in owed_ids:
                continue          # a successor already carries this obligation
            row_stage = "pending"
        # the ledger stores the ref AS TYPED at dispatch time — full or
        # abbreviated; a match is exact-or-prefix in EITHER direction of
        # the head string, never a lane-name coincidence
        if ref and (head == ref or head.startswith(ref)
                    or ref.startswith(head)):
            if found is not None:
                return None       # two live gates for one head: ambiguous
            found, stage = row, row_stage
    if not found:
        return None
    return (str(found.get("id") or ""), str(found.get("recipient") or ""),
            stage)
