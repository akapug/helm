"""helm work — the claims cluster: check-in/check-out at the desk plus the
CLI arg leaf helpers. Moved verbatim from the pre-split helm/work.py.
"""
import os
import sys

from .. import pk, seats, vcs
from ._common import DEFAULT_TTL, _VALUE_FLAGS
from ._lanes import (
    _disposable_worktree_occupant, _occupants, lane_branch, lane_path, resource,
    worktrees,
)
from ._gc import (
    RETIRABLE, _base, _branch_triage, _delete_lane_branch, _dirty, _has_branch,
    _merge_state, _proof_word, _retire_disposable_occupants,
    _runtime_source_in, _wip_commit,
)


# ---------------------------------------------------------------------------
# check-in / check-out
# ---------------------------------------------------------------------------


def _warn_inherited_base(root, lane):
    """SAY SO when a new room will inherit commits the fleet does not have.

    A room branches from `_base` — the LOCAL integration branch NAME, which is
    the right thing to branch from and says NOTHING about whether the fleet has
    those commits; `_trunk` is the ref that answers that (the same split _gc
    documents, biting from the other side). So when local trunk sits AHEAD of
    the remote, every room claimed afterward silently carries the extra commits
    and the claim output looks exactly like a clean one.

    MEASURED 2026-08-03: an unreviewed commit sat on the shared checkout's main
    for an hour. A codex seat claimed a lane, inherited it for free, and caught it
    only because an incident was already running — nothing in `helm work claim`
    said a word. Rooms claimed before it were clean; rooms claimed after were
    not; the two were indistinguishable at the desk.

    WARNS, NEVER REFUSES. Local-ahead is a legitimate transient — a seat that
    just pushed and has not fetched reads exactly this way — and refusing would
    block a land mid-flight, which is worse than the disease. The failure here
    was SILENCE, so ending the silence is the whole fix.

    Best-effort by construction: a read that fails says nothing rather than
    guessing, because a false alarm at check-in teaches people to ignore it."""
    try:
        v = vcs.backend(root)
        base, trunk = _base(root), v.trunk_ref(root)
        # `base == trunk` is a SPAWN-AVOIDING SHORT-CIRCUIT, not a guard, and I
        # am labelling it so because a mutation proved it: deleting it leaves
        # every test green, since with no remote `trunk_ref` returns the local
        # name and ahead_behind(main, main) is (0, 0) anyway. It saves a git
        # process on every claim in a remote-less repo and changes no behaviour.
        # Calling it a guard would be claiming a test covers something no test
        # can distinguish.
        if not base or not trunk or base == trunk:
            return None
        counts = v.ahead_behind(root, trunk, base)
        if not counts:
            return None            # unreadable -> silent, never a guess
        # AS PRINTED, so these are STRINGS — `ahead_behind` says so in its own
        # docstring and my first draft still guarded with isinstance(ahead,int),
        # which is False for '1' and silently disabled the whole warning. A
        # defensive type check that fails CLOSED is how the guard against
        # silence became silent; the probe caught it, the tests could not have
        # told me WHY.
        _behind, ahead = counts
        try:
            ahead = int(str(ahead).strip())
        except (TypeError, ValueError):
            return None
        if ahead <= 0:
            return None
    except Exception:                        # noqa: BLE001 — advisory only
        return None
    line = ("helm work: NOTE — %s is %d commit(s) AHEAD of %s, so room %r "
            "inherits them. They are NOT on the remote and NOT reviewed by "
            "anything the fleet can see. Verify with: git merge-base "
            "--is-ancestor <sha> %s" % (base, ahead, trunk, lane,
                                        lane_branch(lane)))
    sys.stderr.write(line + "\n")
    return line

def _grant_repo(root):
    """The canonical gitdir this check-in is taking a lease out FOR.

    Returns the gitdir string, or `seats.UnresolvedRepo` when a repository WAS
    named and could not be resolved. It never returns None: this call always
    has a root, so "omitted" is not one of its answers — and conflating the two
    is the defect below.

    ONE DERIVATION, AND IT IS THE ONE THE ROW CARRIES (task/2437 round three).
    `dispatches._repo_info` is what stamps a dispatch row's `repo_id` — realpath
    of `--git-common-dir` — so recording the grant's provenance through the SAME
    resolver is what makes the two comparable at all; a second spelling of "this
    repository" would read as a different repository the first day a checkout is
    reached through a symlink. `_row_work_elsewhere` in this same file already
    scopes rows by that call, so this is the file's existing instrument rather
    than a new one.

    UNREADABLE IS NOT UNKNOWN, AND IT IS CERTAINLY NOT "THE PREVIOUS ANSWER"
    (task/2437 round four). The first cut returned None here, and None at the
    grant door means OMITTED — `seats_claims.claim` reads an omitted repository
    as "this extension states nothing new about provenance" and KEEPS the
    binding the grant already had. So a transient `git rev-parse` failure while
    re-claiming lane L from checkout B, against a live lease the seat already
    holds for same-basename checkout A, extended A's binding: the check-in
    succeeded, `progress_state` then read A WORKING (an overdue obligation
    silenced by work nobody is doing) and B IDLE. A failed observation of B
    became a positive assertion about A, which is the one direction
    `_is_overdue` forbids.

    So the failure travels as `seats.UnresolvedRepo`, carrying the path and the
    reason. A FRESH grant records UNKNOWN provenance from it (None in the row —
    what every pre-field grant carries, and readers suppress nothing on it), and
    an EXTENSION of a live grant is REFUSED with the path named, because there
    the alternative is inheriting an authority this call just failed to confirm.
    The check-in refuses before the room is minted, so a retry costs nothing."""
    try:
        from .. import dispatches
        repo_id = (dispatches._repo_info(root) or {}).get("repo_id") or None
    except Exception as exc:         # noqa: BLE001 — every failure is one answer
        return seats.UnresolvedRepo(root, "%s: %s" % (type(exc).__name__, exc))
    if not repo_id:
        return seats.UnresolvedRepo(
            root, "no Git common dir answered for this path")
    return repo_id


def claim(root, lane, seat, ttl=DEFAULT_TTL, lease=None, session=None):
    """(rc, line). Check-in: the lease FIRST (seats.claim, unmodified — held
    by someone else is the existing refusal), then the room. Idempotent: a
    registered room is reused, a parked lane branch re-opens; re-claim with
    --lease extends. A room that fails to mint hands the lease straight back.

    THE GRANT RECORDS WHICH REPOSITORY IT IS FOR. `resource()` is
    `worktree:<root-basename>:<lane>`, which two repositories of the same
    basename share, so the lease key alone cannot say whose lane is being
    worked. The overdue surfaces read that answer off the grant now, and a room
    left standing by an unlanded release is no longer mistaken for one."""
    res, v = resource(root, lane), vcs.backend(root)
    ok, msg, lease_id = seats.claim(res, seat, ttl=ttl, lease=lease,
                                    session=session, repo=_grant_repo(root))
    if not ok:
        return 1, "helm work: " + msg
    path, branch = lane_path(root, lane), lane_branch(lane)
    if path not in {w["path"] for w in worktrees(root)}:
        # The ref-guard cannot tell branch creation apart from a forbidden
        # `checkout -b` — `worktree add -b` runs it with cwd AND toplevel both
        # equal to the shared checkout. So the ONE sanctioned branch creator
        # announces itself, scoped to this single call rather than the process
        # env. A parked lane branch re-opens in place (no base).
        # A PARKED LANE RE-OPENS ON ITS OWN BRANCH and inherits nothing new, so
        # the base — and the inheritance warning that goes with it — applies
        # only when this call actually CUTS a branch.
        fresh = not _has_branch(root, branch)
        if fresh:
            _warn_inherited_base(root, lane)
            _warn_kindred_lane(root, lane)
            # REFUSE LIVE WORK, WARN TERMINAL HISTORY. The detector used to only
            # print, and a probe measured what that was worth: the hit came
            # back, this call ignored it, add_worktree ran with base=main and
            # returned 0. A room minted over live work is the trap the check
            # exists to stop, so the check has to be able to STOP it.
            live, terminal, unproven, unknown = _row_work_elsewhere(
                root, lane)
            if live:
                # THE LEASE GOES BACK, same as a failed mint below. A refusal
                # that keeps the lease locks the label out of the very room the
                # claimer is being redirected to.
                seats.release(res, seat, lease=lease_id, session=session)
                # "LIVE", never "OPEN": the live set now includes
                # HELD rows and rows whose FIX verdict closed the review turn,
                # and calling those OPEN is a status the ledger will contradict
                # the moment someone looks it up.
                # THE MESSAGE MAY NOT CLAIM MORE THAN THE SET SUPPORTS.
                # It used to say the work "is NOT on"
                # the base and the room "would be" minted EMPTY. Both are true
                # of a MEASURED-absent tip and NEITHER is proven for a live
                # UNKNOWN one — which is returned precisely because nothing
                # could be established about it. I had just cured that same
                # overclaim in the docstring HEADER and never looked at the
                # sentence the human actually reads.
                # THE EVIDENCE ENDS THE RE-WORDING: this
                # bucket is fed by `landed_state`, whose evidence is `git
                # cherry` — and vcs.py's own docstring says a '+' "is NOT
                # evidence the work is absent; it is only the absence of
                # evidence that it is present", because once a lane has MERGED
                # TRUNK IN the comparison set is empty and EVERY lane commit
                # reads '+', including one whose patch-id matches trunk
                # exactly. So NOT_ANCESTOR never licensed the word NOT.
                #
                # Rounds seven and eight each cured a DIFFERENT sentence and
                # left this one, because each fixed the surface it was shown.
                # The defect was never the wording: it was reading a state
                # named for ANCESTRY as a verdict about CONTENT. The two
                # buckets below stay separate — evidence-read-and-none-found is
                # genuinely different from tip-unreadable — but neither may say
                # the work is absent, because neither measured that.
                # THREE BUCKETS, THREE DIFFERENT FAILURES, AND A ROW MAY ONLY
                # BE NAMED BY THE ONE THAT ACTUALLY HAPPENED TO IT (Q2 of the
                # meld). `unproven` means a tip whose landedness could
                # not be READ; `unknown` means nothing was measured at all —
                # there was no base to compare against, or the measurement
                # aborted before reaching the row. Those tips may read
                # perfectly. Folding them into `unproven` told the operator to
                # go fetch an object that was never the problem, and folding
                # them into the measured-absent sentence claimed a comparison
                # that never ran. Both overclaim; the refusal does not need
                # either to stand.
                unproven_present = [t for t in live
                                    if t not in unproven and t not in unknown]
                parts = []
                if unproven_present:
                    parts.append("NOT PROVEN on %s: %s"
                                 % (base_name(root),
                                    ", ".join(t[:12] for t in unproven_present[:4])))
                if unproven:
                    parts.append("landedness UNVERIFIABLE (tip unreadable): %s"
                                 % ", ".join(t[:12] for t in unproven[:4]))
                if unknown:
                    # THIS BUCKET IS ABOUT LIVENESS PROVENANCE, NOT ABOUT
                    # LANDEDNESS. A message saying landedness was
                    # "NEVER ESTABLISHED (no readable base, or the check
                    # aborted before these rows)" is false on BOTH halves
                    # of the row that motivated the bucket, whose landedness
                    # WAS read and whose check reached it fine. What is true of
                    # every member is only that the loop did not finish
                    # classifying it, so the fallback re-derived its liveness.
                    parts.append("liveness DERIVED, not measured (the check "
                                 "did not finish classifying these rows): %s"
                                 % ", ".join(t[:12] for t in unknown[:4]))
                # The headline may not call a row LIVE when its liveness was
                # DERIVED rather than measured; it says how many rows are in
                # play and lets the parts above attribute each one.
                held = ("%d row(s), %d of them with liveness DERIVED rather "
                        "than measured," % (len(live), len(unknown))
                        if unknown else "%d LIVE row(s)" % len(live))
                return 1, ("helm work: REFUSED — %s name lane '%s' "
                           "and none is proven to be on %s, so this room may be "
                           "minted EMPTY over live work.\n"
                           "  %s\n"
                           "  find the branch holding it: git branch --contains "
                           "%s\n"
                           "  then claim THAT lane, or close the row(s) first "
                           "(lease returned)"
                           % (held, lane, base_name(root),
                              "; ".join(parts), live[0]))
            if terminal:
                # SIBLING SENTENCE. The LIVE path stopped
                # claiming absence and this one kept claiming it, because I
                # cured the sentence I was shown instead of sweeping the ones
                # asking the same question. "never reached" and "starts EMPTY"
                # are both absence claims resting on the same NOT_ANCESTOR the
                # live path just admitted it cannot read that way.
                print("[helm work] NOTE: %d CLOSED row(s) named lane '%s' and "
                      "their work is NOT PROVEN to be on %s — this room starts "
                      "at the base, which is correct for new work but is NOT a "
                      "resume of: %s"
                      % (len(terminal), lane, base_name(root),
                         ", ".join(t[:12] for t in terminal[:4])),
                      file=sys.stderr)
        rc, _out, err = v.add_worktree(
            root, path, branch,
            base=_base(root) if fresh else None,
            env={"HELM_WORK_CLAIM": "1"})
        if rc != 0:
            seats.release(res, seat, lease=lease_id, session=session)
            return 1, "helm work: worktree add failed — %s (lease returned)" % err
    v.lock_worktree(root, path, "lease:" + lease_id[:8])
    return 0, "%s\t%s\t%s\t%d" % (path, branch, lease_id, ttl)



def _same_label(recorded, claimed):
    """Do these two name the SAME lane, ignoring a redundant `lane/` prefix?

    The second finding: a live row recorded as `lane/work` is invisible
    to someone claiming `work`, because an exact string compare treats the
    prefix as part of the name. MEASURED on the live ledger: 69 distinct labels
    carry it. The branch is `lane/<label>` either way, so the prefix is a
    spelling of the same lane and never a different one.

    I HAD SEEN THIS AND NOT ACTED ON IT — a prefixed label sat in my own census
    output and I read it as someone else's data-quality curiosity rather than a
    hole in the predicate I was writing.
    """
    def norm(x):
        s = str(x or "").strip()
        return s[len("lane/"):] if s.startswith("lane/") else s
    a, b = norm(recorded), norm(claimed)
    return bool(a) and a == b


def base_name(root):
    """The base branch name for a message, or a neutral word if unreadable."""
    try:
        return _base(root) or "the base"
    except Exception:
        return "the base"


def _row_is_live(row):
    """Is this row still a live claim? — asked WITHOUT the object store.

    `_duplicate_branch_live` reads the ROW, so it answers when the thing that
    raised is the VCS. Its one raising part is `_close_retired_by`, so the
    fallback re-derives the answerable half instead: a still-open state, or a
    CONTRARY verdict whose branch survives the review turn. Read from
    `dispatches.CANCELLABLE_STATES`, never transcribed.

    This predicate exists because forcing `alive = True` when the check raised
    RESURRECTED terminal history through the door the previous round had just
    closed (measured on row 4171ff6cae91): a row closed weeks ago
    whose predicate happened to raise would strand a lane nobody can unblock.
    Erring terminal is the expensive direction, so the cure re-derives rather
    than flipping the default.
    """
    # THE IMPORT IS LOCAL BECAUSE ITS CALLER'S WAS. `_row_work_elsewhere`
    # imports `dispatches` lazily inside its own body, so lifting this
    # predicate to module scope left it reading a name that does not exist
    # there — a helper extracted without the scope it was extracted FROM. Same
    # shape as the five findings this function has already produced: a new
    # thing blind to something the old path knew, this time in my refactor
    # rather than in my logic.
    from .. import dispatches
    try:
        return bool(dispatches._duplicate_branch_live(row))
    except Exception:                    # noqa: BLE001 — see the docstring
        status = str(row.get("status") or "")
        return (status in dispatches.CANCELLABLE_STATES
                or (status == "verdict"
                    and row.get("polarity") in ("fix", "supersede")))


def _row_work_elsewhere(root, lane):
    """(live, terminal, unproven, unknown) — row tips NOT PROVEN on the base.

    THE SIGNATURE IS THE THIRD SENTENCE ON THIS DOCSTRING TO CARRY A CLAIM THE
    CODE DOES NOT MAKE. Rounds seven, eight and ten each corrected what this
    docstring SAYS about absence while leaving what it says about its own
    ARITY: every return below is a 3-tuple — six of them — under a two-name
    contract, so a caller who followed the documented shape unpacks and fails.
    `unproven` is the subset of `live` whose landedness could not be READ at
    all, and it is returned separately precisely because it must never be
    folded into the other two.

    NOT PROVEN, never "not on it". The returned
    set holds two things that DIFFER IN REASON AND NOT IN STRENGTH: a tip whose
    content check returned NOT_ANCESTOR, and a LIVE tip whose landedness could
    not be established at all. NEITHER IS A MEASUREMENT OF ABSENCE.

    The first was called "MEASURED absent" here for nine rounds and it never
    was. `landed_state` reads `git cherry`, which compares a lane's commits
    against UPSTREAM-ONLY commits — and a lane branched from trunk's tip has
    none, so the comparison set is EMPTY and every commit reads '+' from zero
    evidence. Measured on plain git: an ordinary lane, one commit, never merged
    anything, `rev-list --count tip..main` = 0, `git cherry` = '+'. That is the
    NORMAL shape, not an edge case, so the strong reading was almost never
    available and the word MEASURED was doing work no instrument supported.
    Absence needs a different instrument entirely (task/1470 owns that); until
    one exists, nothing on this path may say the work is not there.

    THAT SENTENCE SURVIVED TWO AUDITS OF THIS DOCSTRING because I automated
    them with keywords drawn from the paragraphs I had just written, and the
    header uses none of that vocabulary. A zero from a keyword scan is a fact
    about the keywords. The third pass read all 29 sentences unfiltered, and
    this was the only false one left.

    `helm work claim <label>` MINTS a branch from the base when the label is not
    already a branch. Right for new work, WRONG for a label a row already names:
    the room comes up EMPTY while the row's reviewed tip sits on a branch nobody
    checked out, and every surface then agrees the lane is clean with nothing to
    do. The verb MANUFACTURES the trap on demand (task/409).

    SPLIT LIVE FROM TERMINAL, because the right answer differs. LIVE
    means `dispatches._duplicate_branch_live` — NOT `owed()`. That distinction
    is the whole predicate: owed() excludes HELD rows and rows whose FIX verdict
    closed the REVIEW TURN, while the work identity those rows name is still
    active. Measured on the live ledger, owed() saw 2 rows where
    _duplicate_branch_live saw 523. Live work REFUSES; a terminal row is history
    whose label may legitimately be reused, so that only WARNS.

    Warning on both left a hole, and a probe measured it precisely:
    the detector returned a hit, claim ignored it, add_worktree ran with
    base=main, rc=0. A detector whose caller discards it changed nothing.

    AND I HAD ARGUED AGAINST REFUSING FROM A ZERO. I measured that none of the
    affected labels carried a live obligation and concluded a refusal had
    nothing to bite on — but "zero live today" is a fact about this hour, not a
    property of the verb, and the guard has to be right when the next one
    appears.

    SCOPED TO THIS REPO. The ledger carries rows from OTHER repositories —
    measured, not assumed: row repo_ids resolve to git dirs outside this project
    entirely — and a lane label is only unique WITHIN a repo, so an unscoped
    read can refuse a claim here over a foreign row's tip.

    ANCESTRY ALONE IS THE WRONG INSTRUMENT AND A DOGFOOD PROVED IT: the fold
    REBASES, so a landed lane fails ancestry while every line of its content is
    on trunk. `landed_state` adds the CONTENT verdict via git cherry.

    UNKNOWN IS ASYMMETRIC, and this paragraph used to say it was not. On a
    TERMINAL row an unresolvable tip says nothing — nothing distinguishes work
    that landed under another name from work abandoned, and dead history is not
    a claim on anybody's attention. On a LIVE row it REFUSES: the row is itself
    positive evidence that work EXISTS, so an unreadable tip is not the absence
    of a claim but the inability to CHECK one already made. Refusing there is
    recoverable — fetch the branch, or close the row — while minting an empty
    room over live work is the failure this function exists to prevent.

    WHICH READ FAILED IS THE WHOLE DISTINCTION, and the earlier blanket
    "no refusal may rest on an unread fact" was false the moment live-UNKNOWN
    began refusing (I had fixed the two paragraphs a review
    cited and left this one, which is patching the instance instead of the
    class).

    A failed LEDGER read — snapshot unavailable, repo_id unresolvable — answers
    ([], [], []). Nothing is known there, not even whether a row exists, so any
    verdict would be invented.

    THAT ARITY WAS WRONG HERE TOO, and it is the fourth time this one docstring
    has stated a contract the code does not keep (a review of the fix
    for the first three). The paragraph three above says, in its own words,
    that fixing the cited instances and leaving the sibling is "patching the
    instance instead of the class" — and the very next paragraph did it again.
    A prose contract has no compiler, so the only thing that keeps it honest is
    checking EVERY arity claim in a docstring when any one of them moves.

    A failed TIP read is different in kind: the ROW WAS READ. Its existence and
    its liveness are established facts, and only the verification of where its
    work sits is missing. On live work that refuses, because the evidence of a
    claim is present and only the check is unavailable. On terminal history it
    stays silent, because a dead row is not a claim on anybody's attention.

    So the rule is not "never act on an unread fact" — it is that a refusal may
    rest on a READ ROW whose TIP could not be checked, and never on an unread
    ledger."""
    try:
        from .. import dispatches
        snap, unavailable = dispatches.snapshot()
        # FOUR VALUES ON EVERY PATH. These two returned THREE after the
        # contract widened, so an unreadable snapshot or an unscopeable repo
        # raised a ValueError at the caller's unpack instead of answering
        # silently — the two paths whose whole purpose is to stay quiet became
        # the two that could crash the claim door. THIRD time this widening
        # bit a return I did not enumerate; an AST arity sweep over the
        # function found them in one pass where reading did not.
        if unavailable or not isinstance(snap, dict):
            return [], [], [], []            # unreadable -> silent, never a guess
        here = (dispatches._repo_info(root) or {}).get("repo_id")
        if not here:
            return [], [], [], []            # cannot scope -> may not judge
        # LIVENESS IS THE WORK'S, NOT THE REVIEW TURN'S (a review, round three).
        # I used dispatches.owed(), which excludes HELD rows and rows whose FIX
        # verdict closed the REVIEW — but helm's own _duplicate_branch_live says
        # a FIX or SUPERSEDE "close a REVIEW turn, not the work identity", so
        # that branch stays active until the close vocabulary retires it.
        # MEASURED on the live ledger: owed() sees 2 rows, _duplicate_branch_live
        # sees 523. My refusal was blind to 521 live lanes — it would essentially
        # never have fired, which is the worst possible shape for a guard,
        # because it looks armed.
        # A LIVE ROW WITH NO TIP IS UNKNOWN, NOT ABSENT (measured:
        # replay a live legacy row with tip=null and migration=needs-redispatch
        # and it VANISHED here, before anything could classify it). Requiring a
        # tip to be counted at all made the tipless row invisible to the very
        # door that exists to refuse on unreadable live work — the filter
        # answered "no claim" to a question it never asked.
        mine = [r for r in snap.values()
                if isinstance(r, dict)
                and _same_label(r.get("lane"), lane)
                and str(r.get("repo_id") or "") == str(here)]
        if not mine:
            return [], [], [], []
        live, terminal, unproven = [], [], []
        measured = {}          # id(row) -> landed_state, recorded AT the
                               # read so a later raise cannot discard it
        v, base = vcs.backend(root), _base(root)
        if not base:
            # AN UNREADABLE BASE IS NOT AN ABSENCE OF WORK (the
            # third check of the same round). `mine` is already non-empty here
            # — these rows MATCHED — so answering ([], [], []) tells the caller
            # nobody holds this lane while we are holding proof that somebody
            # might. Every LIVE row strands as unproven instead; a terminal one
            # still says nothing, by the same asymmetry the loop uses.
            # These are UNKNOWN, not tip-unreadable: every tip here may read
            # perfectly. What is missing is the BASE to compare against, so
            # landedness was never established at all. They still enter `live`
            # so the door refuses (conservative direction), but they are
            # reported as unestablished rather than borrowing a sentence about
            # the object store that was never measured for them.
            blind = [str(r.get("tip") or ("%s(no tip)"
                                          % str(r.get("id") or "?")[:12]))
                     for r in mine if _row_is_live(r)]
            return sorted(set(blind)), [], [], sorted(set(blind))
        resolved = set()
        for row in mine:
            tip = str(row.get("tip") or "")
            if not tip:
                # No tip to check. If the row is LIVE that is precisely the
                # asymmetry twenty lines below: a live row is positive evidence
                # that work exists, so an absent tip is inability to check a
                # claim already made, never absence of the claim.
                #
                # THE SAME ORDERING BUG, ONE BRANCH OVER. The commit before
                # this one moved `resolved.add` off the "we reached this row"
                # position and onto each CLASSIFICATION, precisely so a raise
                # in the liveness call could not leave a row marked resolved
                # and unclassified — which the fallback then skips, opening the
                # door over an open row. It fixed the TIPPED path and left this
                # one exactly as it was. A tipless row whose liveness raises is
                # marked, unclassified, skipped by the fallback, and the claim
                # succeeds over live work.
                #
                # Marked AFTER the call that can raise, on both outcomes: a
                # tipless row that is measured NOT live is genuinely
                # classified — nothing more is knowable about it — so it stays
                # out of the fallback deliberately rather than by omission.
                alive_tipless = dispatches._duplicate_branch_live(row)
                resolved.add(id(row))
                if alive_tipless:
                    token = "%s(no tip)" % str(row.get("id") or "?")[:12]
                    live.append(token)
                    unproven.append(token)
                continue
            state = v.landed_state(root, tip, base)
            measured[id(row)] = state
            if state in (vcs.ANCESTOR, vcs.PATCH_EQUIVALENT):
                resolved.add(id(row))    # classified: landed, deliberately dropped
                continue                 # landed by object OR by content
            alive = dispatches._duplicate_branch_live(row)
            if state != vcs.NOT_ANCESTOR and not alive:
                resolved.add(id(row))    # classified: terminal, says nothing
                # UNKNOWN ON TERMINAL HISTORY SAYS NOTHING. Nothing can tell
                # work that landed under another name from work abandoned, and
                # a closed row is not a claim on anybody's attention.
                continue
            # UNKNOWN ON *LIVE* WORK REFUSES. I applied
            # "unknown is not guilty" uniformly and that collapsed an asymmetry:
            # a LIVE row is itself positive evidence that work EXISTS, so an
            # unreadable tip is not absence of a claim, it is inability to check
            # a claim already made. Minting an empty room over it is the
            # expensive direction; refusing until the object is readable is
            # recoverable — fetch the branch, or close the row.
            if state != vcs.NOT_ANCESTOR:
                # returned because it could not be CHECKED, not because it was
                # measured absent — the renderer must not conflate the two
                unproven.append(tip)
            # RESOLVED MEANS CLASSIFIED, NOT ATTEMPTED. This
            # was set right after `landed_state`, so a raise in the LIVENESS
            # call below left the row marked resolved and never classified —
            # the fallback then skipped it and an OPEN row was ERASED by the
            # very cure that exists to stop rows being erased. And the hostile
            # arm could not see it: it raises in `landed_state`, which is
            # BEFORE the mark, never in the liveness call, which is after.
            # Marked at each point the row actually lands in a bucket.
            resolved.add(id(row))
            (live if alive else terminal).append(tip)
        return sorted(set(live)), sorted(set(terminal)), sorted(set(unproven)), []
    except Exception:
        # A CHECK MAY NEVER BREAK A CLAIM — BUT ERASING EVERY ROW IS NOT
        # "NOT BREAKING" (measured: a live tipped row whose
        # landed_state raises was ERASED by this blanket except, and the claim
        # door opened over it). Returning empty answers ABSENT to a question
        # that FAILED, which is the one thing the live-UNKNOWN rule forbids.
        # Anything already collected as live stays live and unproven; a failure
        # before `mine` exists genuinely knows nothing and still answers empty.
        # PRESERVE WHAT WAS MEASURED; TOUCH ONLY WHAT WAS NOT. Three rounds
        # of this branch were the same mistake — a NEW answer written beside
        # the loop instead of a REPAIR of it, each blind to something the loop
        # already knew. A second read named the last two instances directly:
        # reprocessing EVERY candidate resurrected a row the loop had already
        # measured ANCESTOR/PATCH_EQUIVALENT (landed, correctly dismissed) as
        # live work; and folding `stranded` into `unproven` rewrote a cleanly
        # measured NOT_ANCESTOR row as "tip unreadable", which is a claim about
        # the object store that was never true of that row.
        #
        # So the fallback now adds ONLY rows the loop never reached, and every
        # completed classification crosses unchanged.
        try:
            had_live, had_term, had_unproven = (
                list(live), list(terminal), list(unproven))
            had_measured = dict(measured)
            seen_ids = set(resolved)
        except Exception:                # noqa: BLE001 — raised before the
            had_live, had_term, had_unproven = [], [], []   # accumulators bound
            had_measured = {}
            seen_ids = set()
        try:
            candidates = [r for r in mine if id(r) not in seen_ids]
        except Exception:                # noqa: BLE001 — mine never bound
            return [], [], [], []
        # UNKNOWN LIVENESS IS ITS OWN BUCKET, NOT A LOUDER LIVE. An unreached
        # row whose liveness we had to re-derive is POSSIBLY live; rendering it
        # as definite LIVE, and as `unproven` (which reads specifically as
        # "the tip could not be read"), overclaims twice about a row nothing
        # measured. It strands — the conservative direction is right — but it
        # says what it is.
        # A RAISE DOES NOT UN-MEASURE WHAT `landed_state` ALREADY READ.
        # `landed_state` runs BEFORE the liveness call, so a row
        # whose LIVENESS raised already carries a real landedness verdict — and
        # the loop threw it away, because `unproven.append` sits AFTER the call
        # that raised. `unproven` means "the tip could not be READ", which is a
        # fact about the OBJECT STORE; which later step failed cannot change it.
        blind, recovered = [], []
        for r in candidates:
            if not _row_is_live(r):
                continue
            token = str(r.get("tip") or ("%s(no tip)"
                                         % str(r.get("id") or "?")[:12]))
            blind.append(token)
            # A recorded state on a CANDIDATE is never landed: the landed arms
            # `continue` after marking resolved, so they are not candidates.
            if (id(r) in had_measured
                    and had_measured[id(r)] != vcs.NOT_ANCESTOR):
                recovered.append(token)
        return (sorted(set(had_live) | set(blind)), sorted(set(had_term)),
                sorted(set(had_unproven) | set(recovered)), sorted(set(blind)))


def _warn_kindred_lane(root, lane):
    """Say so when a lane whose name LEADS this one already exists.

    THE FAILURE IS SILENCE AT THE ONE MOMENT SOMEONE IS ABOUT TO DUPLICATE
    WORK. MEASURED 2026-08-04: I claimed `gate-import-verb` while
    `lane/gate-import` sat on disk — already built, already content-reviewed —
    and `helm work claim` said nothing, because `_has_branch` asks only
    whether THIS exact name exists. I had written a premise six hours earlier
    telling myself to sweep for exactly this and did not run it. That is the
    canon that says a behaviour a human must repeat becomes a hook, never
    another advisory line: the premise existed and was read and was ignored
    under load, so the check belongs HERE, at the keystroke.

    THE PREDICATE IS PREFIX CONTAINMENT ON >=2 LEADING TOKENS, and it is
    deliberately the simplest thing that catches the real case. Measured over
    the live 138-branch set it fires on THREE pairs — gate-import ~
    gate-import-verb (the real one), plus two round-chains whose stem a new
    lane would be shadowing, which is worth knowing too. A refinement that
    stripped -rN round suffixes was tested and REJECTED: it produced FOUR
    pairs, not fewer, because stripping let land-receipts-r3 newly match
    land-receipts-revert-fix. The cruder rule measured better.

    WARNS, NEVER REFUSES — the same law `_warn_inherited_base` states above
    and for the same reason. A successor lane under a new name is legitimate
    and common; refusing would block it, and a check-in that cries wolf
    teaches people to scroll past the line that matters. Best-effort by
    construction: a read that fails says nothing rather than guessing."""
    stem = str(lane or "").split("-")
    if len(stem) < 2:
        return
    try:
        rc, out, _e = vcs.backend(root).text(
            root, "for-each-ref", "--format=%(refname:short)", "refs/heads/lane")
    except Exception:                              # noqa: BLE001
        return
    if rc != 0:
        return
    kin = []
    for ref in out.splitlines():
        other = ref.strip()[len("lane/"):]
        if not other or other == lane:
            continue
        tok = other.split("-")
        n = min(len(stem), len(tok))
        if n >= 2 and stem[:n] == tok[:n]:
            kin.append(other)
    if not kin:
        return
    print("helm work: a lane whose name leads this one already exists — %s. "
          "Check it before building: it may hold the work you are about to "
          "start (git log origin/main..lane/%s, helm dispatch list --open). "
          "Claiming anyway is fine if this is genuinely separate."
          % (", ".join("lane/" + k for k in sorted(kin)[:4]), sorted(kin)[0]),
          file=sys.stderr)


def release_lane(root, lane, seat, lease=None, session=None, park=False,
                 superseded=None):
    """(rc, [lines]). Surrender the lease, but retire the room only when Git
    proves the lane landed. A wrong or expired confirmation token mutates
    nothing: a park commit first strictly refreshes the existing grant; a clean
    release performs the single authoritative ledger write. Unlanded work keeps
    BOTH branch and worktree and emits triage evidence. A holder who lost their
    token reads it back off `helm work list` (seats.own_leases).

    `superseded` is the THIRD STATE — work that will never land, deliberately —
    and it retires the ROOM ONLY. See the comment at the retirement itself for
    why that needs no landedness proof and what it is still forbidden to do."""
    res, path, branch = resource(root, lane), lane_path(root, lane), lane_branch(lane)
    # VALIDATED BEFORE ANYTHING MUTATES, so a malformed reason costs nothing —
    # the same ordering the lease token already gets. `dispatches._clean` is
    # this repo's one authority for "one printable line", and a fourth copy of
    # that predicate would be a fourth thing to keep in step.
    if superseded is not None:
        from .. import dispatches
        superseded, why = dispatches._clean(superseded, "--superseded reason", 256)
        if why:
            return 1, ["helm work: %s — nothing released" % why]
    v, lines = vcs.backend(root), []
    room = path in {w["path"] for w in worktrees(root)}
    if room and _runtime_source_in(path):
        return 1, ["helm work: %s supplies this running helm binary — room and "
                   "lease kept; release it from another checkout" % path]
    occupied = _occupants(path) if room else []
    disposable = bool(occupied) and all(
        _disposable_worktree_occupant(pid) for pid in occupied)
    if occupied and not disposable:
        return 1, ["helm work: %s is OCCUPIED by cwd pid(s) %s — room and "
                   "lease kept; move every live pane/process out before release"
                   % (path, ",".join(occupied))]
    if room and _dirty(path):
        if not park:
            return 1, ["helm work: %s is DIRTY — two exits, no third: commit "
                       "in the room and re-run, or --park (WIP-commits onto "
                       "%s; nothing is ever discarded)" % (path, branch)]
        ok, msg = seats.refresh_claim(res, seat, lease=lease, session=session,
                                      ttl=DEFAULT_TTL)
        if not ok:
            return 1, ["helm work: " + msg]
        # AUTONOMOUS=FALSE: this is `helm work release --park`, an operator
        # naming this exact write after being told the two exits. The
        # activity guard exists to stop a SWEEP guessing that a room is
        # abandoned; a person saying "park it" has supplied the provenance
        # the sweep would have had to invent. Every correctness clause in
        # `_wip_commit` still applies — shared branch, mid-operation and
        # dangling-conflict refuse a park exactly as they refuse a rescue.
        rc, _out, err = _wip_commit(path, "wip: parked %s %s"
                                    % (lane, pk.now_ts()), autonomous=False)
        if rc != 0:
            return 1, ["helm work: park commit failed — %s (room untouched, "
                       "lease kept)" % err]
        lines.append("helm work: parked WIP onto %s" % branch)
    ok, msg = seats.release(res, seat, lease=lease, session=session)
    if not ok:
        return 1, lines + ["helm work: " + msg]
    lines.append("helm work: " + msg)

    exists = _has_branch(root, branch)
    # FOUR states through one read (see `_merge_state`): the lane retires on
    # ancestry OR on patch identity, because our protocol lands work REBASED
    # and the ancestry-only question made every well-behaved seat keep its
    # branch forever. `state` rides down to the delete so the line an agent
    # reads at release says WHICH proof retired the lane.
    state = _merge_state(root, branch) if exists else None
    merged = state in RETIRABLE
    # THE REAPER HAD TWO STATES FOR A THREE-STATE WORLD. It asks LANDED (retire
    # everything) or NOT-LANDED (keep everything) — and a lane whose work will
    # never land, deliberately, is neither. That is not a rare shape: a class
    # solved independently and more strongly on trunk, or a lane whose one
    # surviving finding is declined by canon, leaves a fully argued room
    # standing forever on the strength of a predicate nobody asked the right
    # question. task/1622's room-triage protocol already says dead or
    # superseded work closes WITH EVIDENCE; what it had no way to offer was a
    # door that TAKES evidence.
    #
    # WHAT THIS DOOR OPENS, AND WHAT IT IS STILL FORBIDDEN TO DO. Two acts hide
    # behind one `merged`: removing the ROOM, which costs one `helm work claim`
    # to rebuild, and DELETING THE BRANCH, which is the act that can put commits
    # beyond reach. ONLY THE FIRST IS OPENED HERE. A superseded release retires
    # the room and KEEPS the branch, so every commit the lane carries stays
    # exactly where it was — which is why it can proceed without a landedness
    # proof: it never touches the thing that proof protects. The dirty and
    # occupancy guards above are untouched and still refuse first, so an
    # assertion can no more discard uncommitted bytes than a proof can.
    retire_room = merged or bool(superseded)
    if superseded and merged:
        lines.append("helm work: --superseded was not needed and was not used "
                     "— %s is PROVEN landed (%s) and retires on that proof, "
                     "branch included" % (lane, _proof_word(state)))
    if room:
        if retire_room and disposable:
            stopped, stop_error = _retire_disposable_occupants(path)
            if stopped:
                lines.append("helm work: stopped disposable Orca shell pid(s) %s" %
                             ",".join(stopped))
            if stop_error:
                return 1, lines + ["helm work: room stays (%s) — lease released; "
                                   "`helm work gc` retries only after a fresh "
                                   "landedness proof" % stop_error]
        v.unlock_worktree(root, path)
        if retire_room:
            rc, _out, err = v.remove_worktree(root, path)
            if rc != 0:
                return 1, lines + ["helm work: room stays (%s) — lease released; "
                                   "`helm work gc` retries only after a fresh "
                                   "landedness proof" % err]
            lines.append("helm work: room %s removed" % path)
        else:
            lines.append("helm work: room %s kept — lane is not proven landed" % path)
    if exists:
        if merged:
            lines += ["helm work: " + ln
                      for ln in _delete_lane_branch(root, branch, state)]
        else:
            # THE EVIDENCE MUST NOT CONTRADICT THE ACT, AND THE ACT HAS THREE
            # OUTCOMES RATHER THAN TWO. Keying this on `superseded` — the
            # REQUEST — was the same defect one branch over: a lane whose room
            # is not registered retires nothing, and the line still announced
            # a retirement. So it keys on what THIS CALL ACTUALLY DID, which
            # is `retire_room AND room`: the request asked, and a room existed
            # to answer with.
            if retire_room and room:
                disposition = "branch kept, room retired on stated evidence"
            elif room:
                disposition = "worktree + branch kept"
            else:
                # NEITHER PHRASE IS TRUE HERE. "worktree + branch kept" claims
                # a worktree that never existed to keep, which is the mirror
                # of the bug above rather than a safe default.
                disposition = "branch kept; no room was registered"
            note = _branch_triage(root, lane, branch, state=state,
                                  disposition=disposition)
            if superseded:
                # THE EVIDENCE THAT DID NOT AUTHORIZE THIS STILL TRAVELS WITH
                # IT. A reader three weeks out needs both halves: the stated
                # reason the work will never land, and the landedness read that
                # says it has not landed — so the assertion can never be
                # mistaken for a proof it did not make.
                note = ("SUPERSEDED %s: %s — room retired on that stated "
                        "reason; branch %s is KEPT and every commit it carries "
                        "stays reachable there (`helm work claim %s` re-opens "
                        "the room). The landedness read below did NOT "
                        "authorize this. %s"
                        % (lane, superseded, branch, lane, note))
            lines.append("helm work: " + note)
            # A HOUSEKEEPING VERB WAKES NOBODY. Releasing a lease is routine,
            # and "not landed by ancestry or patch identity" is the EXPECTED
            # state of a lane whose row is still gating — so the plain triage
            # note says nothing a reader can act on. Addressing it to the
            # integrator anyway costs a reader turn per lane per release —
            # measured at five wakes in twenty-seven seconds for lanes that
            # seat had already ruled into landing rows, each wake a turn on the
            # one credential the owner asked to spare. A reader woken by what
            # it cannot act on learns to stop reading, which is the single
            # thing a wake budget cannot afford.
            #
            # SUPERSEDED IS THE EXCEPTION AND IT IS NOT AN EXCEPTION TO THE
            # RULE, it is the rule applied. That branch carries a STATED REASON
            # THIS WORK WILL NEVER LAND — a claim no proof authorized, made by
            # one seat about a lane others may be waiting on. It is exactly the
            # decision a reader must see and can act on, so it keeps its
            # addressed row.
            #
            # NOTHING IS LOST FOR THE ORDINARY CASE. The note is already on
            # stdout for the seat that ran the verb, and every lane room with
            # its state is in `helm work list` — a census read when someone
            # chooses to look, which is where a census belongs, rather than a
            # push per lane per release.
            if superseded:
                try:
                    from .. import chat
                    from ..seats_integrator import integrator_addressed
                    # THE ADDRESSEE IS RESOLVED, NEVER SPELLED. A literal
                    # mention is a bet that the roster still carries that name;
                    # the send door accepts any token and reports success, so a
                    # triage note addressed to a name nobody holds is written,
                    # reaches nobody, and is reported as posted. The note still
                    # goes out when no integrator resolves — it carries
                    # UNROSTERED and the reason, because dropping it loses the
                    # triage along with the delivery.
                    chat.post(integrator_addressed(note))
                except Exception:
                    pass
    elif room:
        lines.append("helm work: TRIAGE %s: branch %s is unreadable/missing — "
                     "room kept" % (lane, branch))
    return 0, lines


def release_stale_lane(root, lane, seat, session=None):
    """(rc, [lines]). Release a claim whose holder is demonstrably dead.
    The lease token is deliberately NOT required: the safety is in the
    liveness proof, not in a token a dead process can never produce.

    Only the claim is released — unlocking the worktree so another seat
    can claim it. Room/branch cleanup is left for `helm work gc` to handle,
    since a stale holder's room may still carry unlanded or dirty work."""
    res, path = resource(root, lane), lane_path(root, lane)
    v = vcs.backend(root)
    ok, msg = seats.release_stale(res, seat, session=session)
    if not ok:
        return 1, ["helm work: " + msg]
    lines = ["helm work: " + msg]
    room = path in {w["path"] for w in worktrees(root)}
    if room:
        v.unlock_worktree(root, path)
        lines.append("helm work: room %s unlocked (claim released)" % path)
    return 0, lines


def _positional(rest):
    out, skip = [], False
    for a in rest:
        if skip:
            skip = False
        elif a in _VALUE_FLAGS:
            skip = True
        elif not a.startswith("--"):
            out.append(a)
    return out


def _infer_lane(root, cwd=None):
    """Inside a room, release needs no lane argument. A per-seat HOME worktree
    (`<root>-wt/seats/<seat>`) shares the container but is NOT a lane room —
    inferring 'seats' there would aim `helm work release` at a lane that does
    not exist, so it answers None and the usage line asks for the lane. The
    peek container (`<root>-wt/peeks/<sha12>`) is the same shape for the same
    reason: a peek is not a lane and release must never aim at it."""
    from ..harness import SEAT_HOME_DIRNAME
    from ._common import PEEK_DIRNAME
    box = root.rstrip(os.sep) + "-wt" + os.sep
    cwd = cwd or os.getcwd()
    if not cwd.startswith(box):
        return None
    lane = cwd[len(box):].split(os.sep)[0]
    return None if lane in (SEAT_HOME_DIRNAME, PEEK_DIRNAME) else lane
