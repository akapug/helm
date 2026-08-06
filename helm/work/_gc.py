"""helm work — the gc cluster: git verdict helpers, the live-lease read,
the rescue commit, housekeeping (scan/enact/orphans), and the room board.
Moved verbatim from the pre-split helm/work.py.
"""
import os
import signal
import time

from .. import pk, seats, vcs
from ._lanes import (
    _disposable_worktree_occupant, _occupants, _panes_bound_to, _room_status,
    find_root, lane_rows, managed_room_kind,
    auto_rows, resource, worktrees,
)


def _dirty(path):
    """Uncommitted/untracked bytes in the room? An unreadable room reads as
    DIRTY — callers must fail toward rescue, never toward discard.

    A PROJECTION of `_room_status`, the same way `_wrote_ago` is, because this
    used to be a SECOND status authority: its own `git status --porcelain` call
    answering only a boolean. That is how `helm work list` kept printing "dirty"
    for a room sitting in a DANGLING CONFLICT even after `_room_status` learned to
    name it — the board read the cruder oracle. Caught by dogfooding the real
    state, not by the unit tests, which tested `_room_status` directly. One
    question, one authority."""
    return _room_status(path)["dirty"]


def _base(root):
    """The integration branch NAME — for BRANCHING FROM and for naming. Not
    the landedness authority; `_trunk` is. Two different questions that were
    one function until 2026-07-30, which is how the reaper came to ask whether
    work was contained in a ref nobody else reads."""
    return vcs.backend(root).base_branch(root)


def _trunk(root):
    """The ref that answers 'does the FLEET have this?' — `origin/<base>` when
    a remote exists, else the local name. See `vcs.trunk_ref` for why asking
    the local ref is wrong in both directions and destructive in one."""
    return vcs.backend(root).trunk_ref(root)


def _has_branch(root, branch):
    return vcs.backend(root).has_branch(root, branch)


def _merge_state(root, branch):
    """vcs.ANCESTOR / PATCH_EQUIVALENT / NOT_ANCESTOR / UNKNOWN — did the lane's
    WORK reach the trunk? THE authority; `_merged` is its boolean projection
    (the `_dirty`/`_room_status` pattern: one question, one authority).

    FOUR states, not three, and the fourth is why every well-behaved lane used
    to accumulate. This asked `ancestry` alone, which is SHA IDENTITY, while
    our protocol lands work REBASED — the integrator rebases the chain onto
    current trunk and gates the rebased tree, so the landed commit carries a
    different object id. Ancestry then answers "no" TRUTHFULLY, the lane reads
    unlanded, and the agent that followed the rule correctly keeps its branch
    forever. Measured on this repo 2026-08-03: of 107 `lane/*` branches, 20
    were ancestry-reachable and 7 more were entirely on trunk under other
    shas — invisible here, uncollectable by any reaper.

    `vcs.landed_state` adds the content answer via `git cherry` (git's own
    patch-id comparison), keeps ancestry as the decisive fast path, and folds
    NOTHING: a mixed lane (some commits landed, some not — 8 of the same 107)
    reads NOT_ANCESTOR and keeps, and every unreadable case reads UNKNOWN.
    `gc_scan` switches on this directly, where "landed as itself", "landed as
    content", "no" and "cannot see" all triage apart.

    Asks the TRUNK ref, never the local branch: "landed" means the fleet has
    it, and this predicate gates a branch deletion."""
    return vcs.backend(root).landed_state(root, branch, _trunk(root))


RETIRABLE = (vcs.ANCESTOR, vcs.PATCH_EQUIVALENT)


def _proof_word(state):
    """The AUDIT phrase for one landedness state, in ONE line.

    A silent reap is indistinguishable from data loss, so every retirement says
    which of the two proofs carried it — and every refusal says which of the
    two FAILURES it was, because "the work is not on the trunk" and "I could
    not finish the read" are different facts and only the first is about the
    work. Kept short on purpose: these render one-per-row on a board the owner
    reads, beside a triage line that already carries tip/age/ref."""
    return {
        vcs.ANCESTOR: "LANDED by ancestry (the tip itself is on the trunk)",
        vcs.PATCH_EQUIVALENT: "LANDED by patch identity (every commit is on "
                              "the trunk under a rebased sha; ancestry alone "
                              "says no)",
        vcs.NOT_ANCESTOR: "NOT landed by ancestry or patch identity",
        vcs.UNKNOWN: "landedness UNKNOWN (unreadable object, a commit `git "
                     "cherry` cannot speak for, or a range past the cap)",
    }.get(state, "landedness UNKNOWN")


def _merged(root, branch):
    """Boolean projection of `_merge_state` for callers whose only safe branch
    is the positive (release, envtidy's worktree/orphan rows): True is PROOF of
    containment BY OBJECT OR BY CONTENT; False is 'no proof' — a clean
    negative OR an unreadable one, and on those surfaces both read KEEP.
    UNKNOWN can never read as merged here, so it can never authorize a
    delete."""
    return _merge_state(root, branch) in RETIRABLE


RETIRED_NS = "refs/helm-retired/"


def _delete_lane_branch(root, branch, state=None):
    """THE branch-retirement actuator: [lines], one per outcome, always naming
    the proof. Every caller that deletes a lane branch comes through here.

    A PROOF THE DELETE CANNOT ACT ON IS NOT A FIX. `git branch -d` refuses on
    REACHABILITY, which is the same wrong question `landed_state` corrects — a
    rebased-and-landed branch fails `-d` forever. So the patch-identity proof
    spends `-D`.

    ANCESTRY AND PATCH IDENTITY ARE NOT THE SAME GRADE OF FACT, and this
    function is where that difference is paid for rather than argued about.
    Ancestry is git's own reachability: after `-d` the tip is still on the
    trunk, so nothing can be lost and nothing more is owed. Patch identity says
    every commit's DIFF is upstream — which preserves the bytes but not the
    commit objects, their messages or their authorship, and `-D` makes those
    unreachable and eventually collectable.

    So the patch-identity path DEMOTES rather than deletes: the tip is written
    to `refs/helm-retired/<branch>` FIRST, and the branch is removed only if
    that write succeeded. The objects stay permanently reachable and immune to
    `git gc`; the owner's sidebar (which renders `refs/heads/*`) is clean;
    `git for-each-ref refs/helm-retired/` is the audit trail; and restore is
    one command. That turns a destructive step into a reversible one, which is
    the only honest way to spend a weaker grade of evidence.

    ORDER IS THE WHOLE SAFETY PROPERTY: save, verify the save, then delete. A
    failed or unverifiable save KEEPS the branch and says so. Reversibility is
    a PRECONDITION of the forced delete, never a courtesy after it.

    Re-reads the state unless handed one, so a caller cannot spend a stale
    scan verdict on a branch that moved."""
    state = _merge_state(root, branch) if state is None else state
    v = vcs.backend(root)
    if state not in RETIRABLE:
        return ["KEPT branch %s — %s" % (branch, _proof_word(state))]
    if state == vcs.ANCESTOR:
        # Reachability already vouches: the tip is ON the trunk after this.
        rc, _out, err = v.delete_branch(root, branch)
        if rc == 0:
            return ["deleted branch %s — %s" % (branch, _proof_word(state))]
        return ["KEPT branch %s (safe -d refused: %s)" %
                (branch, err or "unknown error")]
    rc, sha, _err = v.text(root, "rev-parse", "--verify", "-q", branch)
    sha = (sha or "").strip()
    if rc != 0 or not sha:
        return ["KEPT branch %s — patch-identical to the trunk, but its tip "
                "sha is UNREADABLE, so it could not be preserved and no "
                "forced delete was attempted" % branch]
    keep_ref = RETIRED_NS + branch
    rc, _out, err = v.text(root, "update-ref", keep_ref, sha)
    if rc != 0:
        return ["KEPT branch %s — could not write the retirement ref %s (%s), "
                "so the forced delete was not attempted" %
                (branch, keep_ref, (err or "unknown error").strip()[:120])]
    rc, saved, _err = v.text(root, "rev-parse", "--verify", "-q", keep_ref)
    if rc != 0 or (saved or "").strip() != sha:
        return ["KEPT branch %s — the retirement ref %s did not read back as "
                "%s, so the tip is not provably preserved and nothing was "
                "deleted" % (branch, keep_ref, sha[:12])]
    # DELETE EXACTLY WHAT WAS PRESERVED, OR NOTHING. Every guard above is about
    # the retirement REF; none of them bound the DELETE to the sha they proved.
    # `-D` removes whatever the branch points at when it runs, so a branch that
    # advanced after the read-back on line above had its NEW tip deleted while
    # the ref preserved the OLD one — the commits in between survived nowhere.
    # `expect` makes this a compare-and-delete: it refuses the moved branch.
    rc, _out, err = v.delete_branch(root, branch, force=True, expect=sha)
    if rc != 0:
        return ["KEPT branch %s — the forced delete was REFUSED (%s). If the "
                "branch moved after %s was preserved, that refusal is the "
                "guard working: the new tip is still on the branch and the "
                "next pass re-reads it" %
                (branch, (err or "unknown error").strip()[:160], sha[:12])]
    return ["retired branch %s — %s; tip %s PRESERVED at %s — restore: "
            "git branch %s %s"
            % (branch, _proof_word(state), sha[:12], keep_ref, branch, sha)]


def _age_word(seconds):
    """Compact owner-facing age; precision beyond the largest useful unit only
    makes a triage row noisier, not truer."""
    seconds = max(0, int(seconds))
    for unit, width in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= width:
            return "%d%s" % (seconds // width, unit)
    return "%ds" % seconds


def _branch_triage(root, lane, branch, now=None, state=None):
    """Evidence for work Git cannot prove landed: name, tip, commit age, committer.

    This is reporting evidence only. A failed read never becomes permission to
    remove; the caller keeps the room and prints UNKNOWN fields instead.
    Note: git committer identity in shared seats is a shared git identity (%cn <%ce>),
    not a seat/agent provenance signal.

    `state` REPLACES the old blanket "landing unproven" with the specific
    verdict (`_proof_word`), so a keep row says whether the work is genuinely
    absent from the trunk or whether the read could not be finished. Passed by
    every caller that already computed it; without it the wording stays the
    conservative generic one, which is right for the callers that never asked."""
    rc, out, _err = vcs.backend(root).text(
        root, "show", "-s", "--format=%H%x00%ct%x00%cn <%ce>", branch)
    parts = out.split("\0", 2) if rc == 0 else []
    if len(parts) != 3:
        tip, age, committer = "unreadable", "unknown", "unknown"
    else:
        tip, stamp, committer = parts
        try:
            age = _age_word((time.time() if now is None else now) - int(stamp))
        except (TypeError, ValueError):
            age = "unknown"
    # NAME THE REF IT WAS MEASURED AGAINST. A remote-tracking ref is itself a
    # local snapshot, so "unproven" can mean "genuinely not landed" OR "this
    # checkout has not fetched since it landed" — and those read identically
    # without the ref and its sha. Printing them is what makes a stale verdict
    # diagnosable at a glance instead of a mystery that costs an hour.
    ref = _trunk(root)
    rc2, head, _e2 = vcs.backend(root).text(root, "rev-parse", "--short=12", ref)
    against = "%s @ %s" % (ref, head) if rc2 == 0 and head else "%s (UNREADABLE)" % ref
    # RE-DERIVED, not resolved to a side: trunk taught this line to say whether
    # the committer is a ROSTERED SEAT (a shared identity is not provenance),
    # and this lane taught it to report WHICH PROOF answered the landedness
    # question instead of the hardcoded "landing unproven". Both are live and
    # neither subsumes the other — taking either side whole silently reverts a
    # shipped feature, which is the whole reason this conflict is semantic.
    try:
        from .. import seats
        c_name = committer.split(" <", 1)[0].strip()
        is_seat = seats.is_rostered_name(c_name)
    except Exception:
        is_seat = False
    comm_text = ("git committer: %s" % committer) if is_seat else (
        "git committer, shared across seats: %s — not seat provenance" % committer)
    verdict = _proof_word(state) if state else "landing unproven"
    return ("TRIAGE %s (%s): tip %s, age %s (%s) — "
            "%s vs %s; worktree + branch kept" %
            (lane, branch, tip[:12], age, comm_text, verdict, against))


def refresh_trunk(root):
    """(ok, why) — refresh the remote-tracking ref BEFORE a destructive pass.

    `gc_scan` decides "landed" against a REMOTE-TRACKING ref, which is itself a
    local snapshot: a lane that landed an hour ago reads UNLANDED in a checkout
    that has not fetched since, and reads identically to one that genuinely
    never landed. That direction is safe on its own — a stale ref usually keeps
    branches — but the INVERSE is the destructive one: origin/main can also be
    stale in a way that makes an unlanded branch look contained, and `--apply`
    deletes on exactly that proof.

    vcs.py:405 already states the rule as prose — "callers that act
    destructively must fetch first" — and nothing did. This is that sentence,
    made mechanical.

    UNKNOWN REFUSES. A fetch that fails leaves the proof unrefreshed, and an
    unrefreshed proof must not authorize a deletion. Same law as `lr land`'s
    deletion guard: an unscannable deletion set is not a known-safe one.

    A trunk with no remote component (a local-only branch) has nothing to
    refresh and is not a failure."""
    ref = _trunk(root)
    if "/" not in (ref or ""):
        return True, "trunk %s is local — nothing to refresh" % (ref or "?")
    remote = ref.split("/", 1)[0]
    rc, _out, err = vcs.backend(root).text(root, "fetch", "--quiet", remote)
    if rc != 0:
        return False, "git fetch %s failed: %s" % (
            remote, (err or "").strip()[:160] or "rc %s" % rc)
    return True, "refreshed %s" % remote


def trunk_sync(root, enforcing):
    """([lines], post_error) — converge the shared checkout's OWN base branch
    onto the trunk ref, and NAME every state it cannot lawfully converge.

    THE INCIDENT (2026-08-03): the fleet's shared checkout sat FORKED from
    origin/main for ~80 minutes — a local-only commit raced a land, the ff
    silently stopped, and every seat kept measuring "trunk" on the stale
    side while the integrator announced a land none of them had. It was
    found by reflog archaeology, not by an instrument. `refresh_trunk`
    already makes the REMOTE-TRACKING ref honest before destructive passes;
    this leg makes the LOCAL base honest against it, on the same cadence,
    in the only direction that is lawful without judgment:

      IN SYNC  -> silence (gc's idiom: healthy states say nothing).
      BEHIND   -> fast-forward on --apply; the dry run names it. The ref
                  guard ADMITS an ff of the shared base structurally, and
                  the landlock check refuses one beside a staged merge —
                  a refusal is git's own words forwarded, never overridden
                  (NO integrator env here: this walks the front door).
      AHEAD    -> named, kept. Unpushed base commits are someone's mid-land
                  window or a stray direct commit; a PUSH completes them and
                  publishing is a person's call — gc never pushes.
      DIVERGED -> named, kept, POSTED to the room (--apply only). Whose
                  commit rides the repair is a judgment call, and automating
                  a judgment call is how a guard becomes the next incident.

    HEAD off the base branch, unreadable refs, or UNKNOWN ancestry are
    named and left alone: a sync that cannot prove what it is standing on
    must not move it."""
    v = vcs.backend(root)
    trunk = _trunk(root)
    if "/" not in (trunk or ""):
        return [], None            # no remote: local base IS the trunk
    base = trunk.split("/", 1)[1]
    rc, head_ref, _e = v.text(root, "symbolic-ref", "-q", "HEAD")
    head_ref = (head_ref or "").strip()
    if rc != 0 or head_ref != "refs/heads/" + base:
        return ["TRUNK-SYNC kept: shared checkout HEAD is %s, not '%s' — a "
                "sync that cannot prove what it stands on must not move it"
                % (head_ref or "detached/unreadable", base)], None
    rc_l, local, _el = v.text(root, "rev-parse", "--verify",
                              "refs/heads/" + base)
    rc_t, remote, _et = v.text(root, "rev-parse", "--verify", trunk)
    local, remote = (local or "").strip(), (remote or "").strip()
    if rc_l != 0 or rc_t != 0 or not local or not remote:
        return ["TRUNK-SYNC kept: cannot read '%s' vs %s — refs unreadable"
                % (base, trunk)], None
    if local == remote:
        return [], None
    fwd = v.ancestry(root, local, trunk)
    back = v.ancestry(root, remote, "refs/heads/" + base)
    if fwd == vcs.ANCESTOR:                                   # BEHIND
        if not enforcing:
            return ["TRUNK-BEHIND '%s' is behind %s (%s -> %s) — --apply "
                    "fast-forwards (a dry run does not fetch, so even this "
                    "verdict may be stale)"
                    % (base, trunk, local[:12], remote[:12])], None
        rc_m, _out, err = v.text(root, "merge", "--ff-only", trunk)
        if rc_m != 0:
            return ["TRUNK-BEHIND kept: fast-forward refused — %s"
                    % ((err or "").strip()[:200] or "rc %s" % rc_m)], None
        return ["TRUNK-SYNC fast-forwarded '%s': %s -> %s"
                % (base, local[:12], remote[:12])], None
    if back == vcs.ANCESTOR:                                  # AHEAD
        _rc, n, _e2 = v.text(root, "rev-list", "--count",
                             "%s..%s" % (trunk, local))
        _rc, tops, _e3 = v.text(root, "log", "--format=%h %s",
                                "--max-count=3", "%s..%s" % (trunk, local))
        return ["TRUNK-AHEAD '%s' carries %s unpushed commit(s) past %s: %s "
                "— a push or a land completes them; gc never publishes"
                % (base, (n or "?").strip(), trunk,
                   "; ".join((tops or "").strip().splitlines())
                   or "unreadable")], None
    if fwd == vcs.NOT_ANCESTOR and back == vcs.NOT_ANCESTOR:  # DIVERGED
        line = ("TRUNK-DIVERGED '%s' and %s have FORKED (local %s / trunk "
                "%s) — every seat in this checkout is measuring the wrong "
                "trunk. gc never repairs a fork; a deputy reconciles, then "
                "this cadence re-converges."
                % (base, trunk, local[:12], remote[:12]))
        return [line], (post_gc_summary(line) if enforcing else None)
    return ["TRUNK-SYNC kept: ancestry UNKNOWN between '%s' (%s) and %s "
            "(%s) — an unreadable answer moves nothing"
            % (base, local[:12], trunk, remote[:12])], None


def format_gc_summary(root, removed, kept, triage, planned=None, blocked=None):
    """The one-line estate verdict, and it must distinguish CLEAN from JAMMED.

    "removed=0 kept=40" is literally true of an estate with nothing to remove
    AND of one where every planned removal failed — and it reads as the first.
    Measured 2026-08-04: a run planned TWO removals, completed ZERO, printed
    `removed=0 kept=40 triage=31`, and real time was spent believing the estate
    was clean. The counts were all correct; the sentence was still false.

    `planned`/`blocked` are optional so the dry-run and the phantom/peek paths
    keep their existing shape — a summary only grows the clause when there is
    an unfinished intention to report."""
    line = "worktree gc %s: removed=%d" % (
        os.path.basename(root.rstrip(os.sep)), removed)
    if blocked:
        # NAME THE REASON, not just the count: "2 blocked" sends someone
        # hunting, "2 blocked: bound pane" is already the diagnosis.
        why = sorted(set(blocked))
        line += " (%d planned, %d blocked: %s)" % (
            planned if planned is not None else removed + len(blocked),
            len(blocked), "; ".join(why))
    return line + " kept=%d triage=%d" % (kept, triage)


def post_gc_summary(line):
    """One owner-visible room line per APPLY pass. Mutations are already
    complete, so a chat outage reports to the caller but never rolls them back."""
    try:
        from .. import chat
        chat.post(line)
        return None
    except Exception as exc:
        return "worktree gc: summary post failed — %s" % exc


def _live():
    """resource -> {holder, remaining}: a lockless TRUE read of the desk
    ledger (seats' own sweep semantics, ZERO writes — claims_list's GC-on-
    read leg stays on the surfaces that own it; a report/scan here, incl.
    the `helm gc` policy row, must never churn .claims.json)."""
    now = seats._now_mono()
    c = seats._sweep(pk.read_json(seats.claims_path(), {}) or {})
    return {r: {"holder": v.get("holder"),
                "remaining": int(v.get("exp_mono", now) - now)}
            for r, v in c.items() if r != "_fence" and isinstance(v, dict)}


def _head(path):
    """The room's HEAD sha, or None. Reported so a human inspecting a
    manual-only room knows where to look; it never authorizes a removal."""
    return vcs.backend(path).head_sha(path)


def _runtime_source_in(path):
    """Whether this running GC imported its own code from `path`.

    Cwd occupancy cannot see imported source. Removing that worktree mid-command
    leaves already-loaded functions alive but makes any later lazy import fail —
    exactly how self-reap completed its mutations and then lost the required chat
    summary. Unknown path relationships KEEP: a later GC launched from another
    checkout can retire the room safely."""
    try:
        room = os.path.realpath(path)
        source = os.path.realpath(__file__)
        return os.path.commonpath((source, room)) == room
    except (OSError, ValueError):
        return True


_OP_MARKERS = (("merge", "MERGE_HEAD"), ("rebase", "rebase-merge"),
               ("rebase", "rebase-apply"), ("cherry-pick", "CHERRY_PICK_HEAD"),
               ("revert", "REVERT_HEAD"), ("bisect", "BISECT_LOG"))


def _current_branch(path):
    """The branch the room is on right now, or None when detached/unreadable.

    Read at ENACT time, never trusted from the scan: the gap between judging a
    room and deleting it is exactly where a detach+commit hides."""
    rc, out, _e = vcs.backend(path).text(path, "symbolic-ref", "--quiet",
                                         "--short", "HEAD")
    return out.strip() if rc == 0 and out.strip() else None


def _operation_state(path):
    """Name of the git operation in progress, or None.

    A room mid-merge/rebase/cherry-pick/revert/bisect holds sequencer state
    that is not a commit and not a branch, so neither `-d` nor any reachability
    walk speaks for it. codex named this alongside the detached case and it
    gets the same answer: manual-only. Unreadable reads as IN AN OPERATION —
    the direction that keeps the room."""
    b = vcs.backend(path)
    rc, admin, _e = b.text(path, "rev-parse", "--absolute-git-dir")
    admin = (admin or "").strip()
    if rc != 0 or not admin:
        return "unreadable-gitdir"
    for name, marker in _OP_MARKERS:
        try:
            if os.path.exists(os.path.join(admin, marker)):
                return name
        except OSError:
            return "unreadable-gitdir"
    return None


def _wip_commit(path, msg):
    """The lost-and-found write: stage EVERYTHING, commit --no-verify onto
    the lane's own branch under the janitor identity. Strictly loss-reducing
    and reversible — the only authored-bytes write in this module."""
    return vcs.backend(path).wip_commit(path, msg)


def _removal_blocker(root, path, lane=None, stale_lease_ok=False,
                     disposable_ok=False, panes_ok=False):
    """Current-state refusal reason, or None when removal may proceed. Called
    at enact time (and again immediately before remove) so a scan cannot
    authorize deleting a newly leased/locked/occupied room. `disposable_ok` is
    used only before the narrow Orca shell retirement leg; UNKNOWN and every
    other process class remain blockers.

    `panes_ok` DEFERS the bound-pane refusal — it does not waive it. It exists
    for exactly one caller: the enact path asks once with it set, so it learns
    whether anything OTHER than a pane blocks removal before spending a close
    on a room it could not remove anyway. That path then closes the panes and
    asks again with NOTHING relaxed, and only a None from that second call
    removes. A pane that did not actually go away therefore still refuses,
    which keeps this a SATISFIED guard rather than a bypassed one."""
    cur = next((w for w in worktrees(root) if w["path"] == path), None)
    if cur is None:
        return "no longer a registered worktree"
    if _runtime_source_in(path):
        return "RUNNING this helm GC binary from the room"
    if lane:
        held = _live().get(resource(root, lane))
        if held:
            return "lease live — %s holds it" % held["holder"]
    occupied = _occupants(path)
    if occupied and not (disposable_ok and all(
            _disposable_worktree_occupant(pid) for pid in occupied)):
        return "OCCUPIED by cwd pid(s) %s" % ",".join(occupied)
    # A PANE OUTLIVES ITS SHELL, so /proc alone cannot clear a room. Deleting a
    # worktree the metaharness still has a terminal in leaves the operator a
    # pane whose cwd no longer exists — it renders as a bare command prompt,
    # and from the outside it is indistinguishable from someone having killed
    # the agent. That is the 2026-07-30 incident: the owner reported seats
    # "killed back to a CWD" repeatedly and was told the seats were fine,
    # because every instrument consulted was a /proc scan. No amount of
    # recovery is cheap here, so this leg refuses on EITHER a bound pane or an
    # unanswerable host — `disposable_ok` deliberately does NOT relax it, since
    # a disposable SHELL says nothing about the PANE wrapped around it.
    if not panes_ok:
        panes, pane_err = _panes_bound_to(path)
        if pane_err:
            return "cannot prove the room is pane-free — %s" % pane_err
        if panes:
            return ("metaharness pane(s) %s are bound to this room"
                    % ",".join(panes))
    if cur["locked"] and not (stale_lease_ok
                              and cur["reason"].startswith("lease:")):
        return "LOCKED: %s" % (cur["reason"] or "no reason")
    return None


def _retire_bound_panes(path):
    """Close the metaharness panes bound to `path` -> ([handles], error).

    THE DEADLOCK THIS BREAKS. The room cannot be removed because a pane is
    bound to it; the pane persists in the owner's sidebar because the room
    exists. helm could already hang up a disposable SHELL
    (_retire_disposable_occupants) but nothing could close the PANE wrapped
    around it, so neither side ever moved and the worktree count only grew.
    Measured 2026-08-04: a reaper run planned two removals, completed ZERO,
    and reported `removed=0` — both blocked on exactly this.

    THE GUARD IS NOT RELAXED, IT IS SATISFIED. _removal_blocker still refuses
    on a bound pane; this leg makes the pane genuinely go away first, and the
    caller then re-asks with NO relaxation. Deleting a worktree out from under
    a live pane leaves the operator a bare prompt indistinguishable from a
    killed agent — the 2026-07-30 incident — so the ordering (close, re-verify,
    THEN remove) is the whole safety property. Reversed, it reproduces that
    incident exactly.

    EVERY UNCERTAINTY KEEPS THE ROOM. A pane list that cannot be read, an
    adapter that will not close, a handle that survives, a close that cannot
    be re-verified: all return an error and the room stays. No SIGKILL, no
    escalation, no second attempt — cleanup never earns the right to guess
    harder, and the cost of a wrong delete here is an owner who cannot tell
    tidying from an agent being killed."""
    panes, err = _panes_bound_to(path)
    if err:
        return [], "cannot prove the room is pane-free — %s" % err
    if not panes:
        return [], None
    from .. import harness
    ad = harness.detect()
    if ad is None:
        # Panes were reported but no adapter answers now: the two readings
        # disagree, and a delete is not the way to resolve that.
        return [], ("metaharness reported pane(s) %s but no adapter is "
                    "available to close them" % ",".join(panes))
    closed = []
    for handle in panes:
        try:
            ad.stop(handle)
        except Exception as exc:          # HarnessError/OSError and anything
            return closed, ("could not close metaharness pane %s: %s: %s"
                            % (handle, type(exc).__name__, exc))
        closed.append(handle)
    # RE-VERIFY AGAINST THE ADAPTER, never against our own success. `stop`
    # returning cleanly says the command was accepted, not that the pane is
    # gone; only a fresh read of the binding proves the room is free.
    still, err2 = _panes_bound_to(path)
    if err2:
        return closed, ("closed pane(s) %s but cannot re-verify the room is "
                        "pane-free — %s" % (",".join(closed), err2))
    if still:
        return closed, ("pane(s) %s are STILL bound after close"
                        % ",".join(still))
    return closed, None


def _retire_disposable_occupants(path):
    """Stop the exact Orca shell-ready placeholder class, or return a refusal.

    SIGHUP models the terminal hangup Orca's interactive placeholder expects;
    Bash ignores SIGTERM in that state. A process that changes shape, gains a
    child, cannot be inspected, or survives the bounded wait is meaningful by
    default and keeps the room. No SIGKILL escalation: cleanup never earns the
    right to guess harder."""
    occupied = _occupants(path)
    if not occupied:
        return [], None
    if not all(_disposable_worktree_occupant(pid) for pid in occupied):
        return [], "OCCUPIED by non-disposable cwd pid(s) %s" % ",".join(occupied)
    stopped = []
    for pid in occupied:
        try:
            os.kill(int(pid), signal.SIGHUP)
            stopped.append(pid)
        except ProcessLookupError:
            continue
        except OSError as exc:
            return stopped, "could not stop disposable Orca shell %s: %s" % (pid, exc)
    pending = set(stopped)
    for _ in range(20):
        pending = {pid for pid in pending
                   if os.path.exists(os.path.join("/proc", pid, "cwd"))}
        if not pending:
            break
        time.sleep(0.05)
    if pending:
        return stopped, "disposable shell(s) %s ignored SIGHUP" % ",".join(sorted(pending))
    left = _occupants(path)  # one full census catches a newcomer after the wait
    if left:
        return stopped, "new occupant(s) %s" % ",".join(left)
    return stopped, None


# ---------------------------------------------------------------------------
# housekeeping — the four-row verdict table (the dumpster is not in it)
# ---------------------------------------------------------------------------

def gc_scan(root, registered=None):
    """One row per room, verdict ∈ keep|triage|rescue|remove.

    Removal requires the complete retirement proof: no live lease, no meaningful
    occupant, no out-of-band lock, an attached branch, a clean tree, and branch
    tip ancestry to the integration base. Unlanded work keeps BOTH its branch and
    worktree and carries owner-facing triage evidence. Dirty work is rescue-
    committed but also kept: the rescue itself makes the branch unlanded.

    Harness-minted rooms use the same proof. Seeing more never means doing more."""
    live = _live()
    rows = []
    rooms = lane_rows(root, registered=registered) \
        + auto_rows(root, registered=registered)
    for w in rooms:
        if _provably_absent(w["path"]):
            continue                  # registry residue rides the phantom pass
        lane = w["lane"]
        held = live.get(resource(root, lane))
        branch = (w["branch"] or "")[len("refs/heads/"):] or None
        occupied = _occupants(w["path"])
        disposable = [pid for pid in occupied if _disposable_worktree_occupant(pid)]
        blocking = [pid for pid in occupied if pid not in disposable]
        r = {"lane": lane, "path": w["path"], "branch": branch}
        if held:
            r.update(verdict="keep", why="lease live — %s holds it, %ds left"
                     % (held["holder"], held["remaining"]))
        elif _runtime_source_in(w["path"]):
            r.update(verdict="keep",
                     why="RUNNING this helm GC binary from the room — rerun "
                         "from another checkout")
        elif blocking:
            r.update(verdict="keep", why="OCCUPIED by cwd pid(s) %s — never remove"
                     % ",".join(blocking))
        elif w["locked"] and not w["reason"].startswith("lease:"):
            r.update(verdict="keep", why="locked out-of-band (%s)"
                     % (w["reason"] or "no reason"))
        else:
            # "Clean" answers only whether uncommitted bytes exist. An attached
            # branch gives Git a lossless authority (`branch -d`); a detached or
            # mid-operation room has no equivalent and remains manual-only.
            op = _operation_state(w["path"])
            if branch is None or op:
                head = _head(w["path"])
                r.update(verdict="keep", manual_only=True, head=head,
                         why=("DETACHED%s — helm cannot prove removing this "
                              "room is lossless (no branch for `-d` to protect), "
                              "so it is never auto-reaped. Inspect and remove by "
                              "hand: HEAD %s"
                              % ((" + mid-%s" % op) if op else "",
                                 (head or "unreadable")[:12])))
            else:
                dirty = _dirty(w["path"])
                if dirty:
                    facts = _branch_triage(root, lane, branch)
                    if occupied:
                        r.update(verdict="triage",
                                 why="idle Orca shell pid(s) %s kept because work "
                                     "is not reapable; %s" %
                                     (",".join(disposable), facts))
                    else:
                        # The rescue commit is necessarily unlanded, so the room
                        # remains too; no ancestry subprocess is needed here.
                        r.update(verdict="rescue",
                                 why="lease-less + DIRTY on %s — rescue-commit, "
                                     "then KEEP room + branch for integration; %s"
                                     % (branch, facts))
                else:
                    # FOUR states, not two. Landed-as-itself and landed-as-
                    # content both retire and SAY WHICH; UNKNOWN is not a clean
                    # negative and can never authorize deleting room or branch.
                    anc = _merge_state(root, branch)
                    if anc in RETIRABLE:
                        r.update(verdict="remove", proof=anc,
                                 why="lease-less + clean + %s — remove room + "
                                     "delete branch" % _proof_word(anc)
                                     + (" after stopping disposable Orca shell pid(s) %s"
                                        % ",".join(disposable) if disposable else ""))
                    else:
                        r.update(verdict="triage", proof=anc,
                                 why=_branch_triage(root, lane, branch,
                                                    state=anc))
        rows.append(r)
    return rows


def gc_enact(root, row):
    """Enforce ONE scan row -> [lines]. Only a fresh, complete LANDED proof
    removes. Rescue writes a commit and then KEEPS the newly-unlanded room.
    Triage rows are report-only. Every destructive premise is re-read."""
    if row["verdict"] in ("keep", "triage"):
        return []
    v = vcs.backend(root)
    if row["verdict"] == "rescue":
        blocked = _removal_blocker(root, row["path"], row["lane"],
                                   stale_lease_ok=True)
        if blocked:
            return ["SKIPPED %s (%s) — kept" % (row["path"], blocked)]
        rc, _out, err = _wip_commit(
            row["path"], "wip: rescued %s %s" % (row["lane"], pk.now_ts()))
        if rc != 0:
            return ["SKIPPED %s (rescue commit failed: %s) — room kept"
                    % (row["path"], err)]
        return ["rescued dirty work -> %s; room kept" % row["branch"],
                _branch_triage(root, row["lane"], row["branch"])]

    # ASK FIRST WITH THE PANE DEFERRED, so a room blocked for some OTHER reason
    # never costs a pane close. Nothing is waived here: the unrelaxed call
    # below is the one that authorizes the delete.
    blocked = _removal_blocker(root, row["path"], row["lane"],
                               stale_lease_ok=True, disposable_ok=True,
                               panes_ok=True)
    if blocked:
        return ["SKIPPED %s (%s) — kept" % (row["path"], blocked)]
    # PANE BEFORE SHELL, and both before any removal. The pane is the object
    # the owner SEES; hanging up the shell first would leave a live pane on a
    # room we are about to delete, which is the 2026-07-30 incident in the
    # order that causes it.
    closed, pane_error = _retire_bound_panes(row["path"])
    lines = (["closed metaharness pane(s) %s" % ",".join(closed)]
             if closed else [])
    if pane_error:
        return lines + ["SKIPPED %s (%s) — kept" % (row["path"], pane_error)]
    stopped, stop_error = _retire_disposable_occupants(row["path"])
    if stopped:
        lines.append("stopped disposable Orca shell pid(s) %s" % ",".join(stopped))
    if stop_error:
        return lines + ["SKIPPED %s (%s) — kept" % (row["path"], stop_error)]
    # NOTHING RELAXED. A pane that did not actually close, an occupant that
    # reappeared, a lease taken in the meantime — all refuse here, and this is
    # the only call whose None permits the delete.
    blocked = _removal_blocker(root, row["path"], row["lane"],
                               stale_lease_ok=True)
    if blocked:
        return lines + ["SKIPPED %s (%s) — kept" % (row["path"], blocked)]

    # Re-prove the exact scan premise. Current branch identity alone is not
    # enough: the branch can advance after scan, or the tree can become dirty.
    cur = _current_branch(row["path"])
    if cur != row.get("branch"):
        return lines + [
            "SKIPPED %s (was on %s at scan, now %s — the room moved under "
            "the scan) — kept" % (row["path"], row.get("branch"),
                                  cur or "DETACHED")]
    if _dirty(row["path"]):
        return lines + ["SKIPPED %s (became DIRTY after scan) — kept for triage"
                        % row["path"]]
    branch = row.get("branch")
    anc = _merge_state(root, branch) if branch else vcs.UNKNOWN
    if anc not in RETIRABLE:
        facts = _branch_triage(root, row["lane"], branch or "HEAD", state=anc)
        changed = ("landedness became UNKNOWN after scan" if anc == vcs.UNKNOWN
                   else "branch became unlanded after scan")
        return lines + ["SKIPPED %s (%s) — %s" %
                        (row["path"], changed, facts)]

    v.unlock_worktree(root, row["path"])            # stale lease tag, if any
    rc, _out, err = v.remove_worktree(root, row["path"])
    if rc != 0:
        return lines + ["SKIPPED %s (%s)" % (row["path"], err)]
    lines.append("removed " + row["path"])
    # The branch delete carries the FRESH state (`anc`), never the scan's — and
    # it names which of the two proofs retired the lane, so the decision is
    # auditable after the fact instead of an unexplained disappearance.
    return lines + _delete_lane_branch(root, row["branch"], anc)


def gc_orphans(path=None):
    """Lease-less lane rooms needing the sweep — `helm gc`'s report-row feed
    (report-only there; `helm work gc --apply` is the actuator). [] outside
    a repo; fail-open total — a janitor row must never error the janitor."""
    try:
        root = find_root(path)
        if not root:
            return []
        return [r["path"] for r in gc_scan(root) if r["verdict"] != "keep"]
    except Exception:
        return []


def _provably_absent(path):
    """True ONLY when the path is provably gone. Any other answer is False.

    codex-2's FIX on this lane (its r4 review round) named the hazard exactly:
    "unreadable live dirs prune". os.path.isdir() SWALLOWS OSError and answers
    False, so a directory that exists but cannot be read — a permission-denied
    tree, an NFS mount that is timing out, a disk that is failing — was
    indistinguishable from one that is gone, and the enact leg deleted its
    registry record. That is fail-open in the destructive direction, which is
    the one direction a prune may never fail open in.

    So the question is asked as a stat and every answer is classified:
      FileNotFoundError / NotADirectoryError -> provably absent, prunable
      any other OSError                      -> CANNOT SEE, never prunable
      stat succeeds                          -> something is there, never prunable
    The last line also narrows the old behaviour deliberately: a FILE sitting
    at a worktree path used to count as phantom. It no longer does. A prune is
    registry loss and a file there is a state nobody predicted — the
    conservative read costs manual triage, while the permissive one costs a
    record that a live tree needed."""
    try:
        os.stat(path)
    except (FileNotFoundError, NotADirectoryError):
        return True
    except OSError:
        return False
    return False


_PHANTOM_OWNER_KINDS = {
    "work": ("lane", "harness"),
    "estate": (None,),
}


def _is_phantom_record(root, row, owner):
    return managed_room_kind(root, row["path"]) in _PHANTOM_OWNER_KINDS[owner] \
        and not row["locked"] and _provably_absent(row["path"])


def phantom_scan(root, owner="work", registered=None):
    """(approved, excluded, error) from one registry snapshot for one owner.

    The excluded set is evidence too: apply verifies those records survived, so
    a future actuator cannot quietly reintroduce the global-prune failure while
    still reporting only the approved candidates."""
    if registered is None:
        registered, error = vcs.backend(root).worktrees(root)
        if error:
            return [], [], error
    owned = [r for r in registered
             if managed_room_kind(root, r["path"])
             in _PHANTOM_OWNER_KINDS[owner]]
    approved = [r["path"] for r in owned if _is_phantom_record(root, r, owner)]
    allowed = set(approved)
    return approved, [r["path"] for r in owned if r["path"] not in allowed], None


def phantom_records(root, registered=None):
    """Missing records owned by `helm work gc`: lane + harness rooms.

    A record with no directory is registry residue: Git keeps it indefinitely
    and metaharnesses render it as a ghost room. The cleanup-owner classifier is
    the boundary — unmanaged estate belongs to `helm worktree gc`, while peeks
    belong to `helm work peek --drop`."""
    return phantom_scan(root, registered=registered)[0]


def estate_phantom_records(root, registered=None):
    """Missing, unlocked records in the remaining unmanaged estate."""
    return phantom_scan(root, owner="estate", registered=registered)[0]


def prune_phantom_records(root, approved, excluded=None, owner="work"):
    """(removed_paths, error, unknown) for an approved path set.

    Every path is re-authorized against a fresh registry read immediately before
    the VCS seam removes that exact administrative record without touching the
    checkout path. A global `worktree prune` is never used: it would let one
    approved phantom authorize deletion of every record
    Git considers stale, including unreadable live records excluded by the scan.

    The final stat-to-record interval is not transactional, so a revival may
    require metadata repair; the record-only VCS operation makes that race
    incapable of deleting authored bytes. The final registry read is the
    evidence source."""
    approved = list(dict.fromkeys(approved or ()))
    excluded = list(dict.fromkeys(excluded or ()))
    if not approved:
        return [], None, False
    v, failures = vcs.backend(root), []
    for index, path in enumerate(approved, 1):
        rows, read_error = v.worktrees(root)
        if read_error:
            failures.append("registry re-read failed before approved record "
                            "#%d: %s" %
                            (index, str(read_error).strip()[:120]))
            break
        row = next((r for r in rows if r["path"] == path), None)
        if row is None:
            continue                         # a concurrent cleanup already won
        if not _is_phantom_record(root, row, owner):
            failures.append("approved record #%d no longer clears the removal "
                            "proof" % index)
            continue
        rc, _out, err = v.remove_worktree_record(root, path)
        if rc != 0:
            failures.append("approved record #%d: %s" %
                            (index, (err or "remove refused").strip()[:120]))
    rows, read_error = v.worktrees(root)
    if read_error:
        return [], ("targeted worktree removal completed but the registry "
                    "re-read failed, so removal is UNKNOWN: %s" %
                    str(read_error).strip()[:160]), True
    registered = {r["path"] for r in rows}
    gone = [p for p in approved if p not in registered]
    collateral = [p for p in excluded if p not in registered]
    remaining = len(approved) - len(gone)
    if collateral:
        failures.insert(0, "%d excluded record(s) disappeared during the "
                        "targeted pass" % len(collateral))
    if remaining or failures:
        detail = "; ".join(failures[:3])
        suffix = ("; " + detail) if detail else ""
        return gone, ("%d approved phantom record(s) remain registered%s" %
                      (remaining, suffix)), False
    return gone, None, False


# ---------------------------------------------------------------------------
# the room board + the rail
# ---------------------------------------------------------------------------

def held_lane_files(root, exclude=None, registered=None):
    """[(lane, [files])] — every ACTIVELY-HELD lane and the files it authored.

    THE DISCLOSURE HALF OF lane_overlaps, and it exists because that function
    cannot answer at the moment the question is asked. lane_overlaps COMPARES
    two lanes' authored diffs, so it needs both to have commits; a lane being
    CLAIMED has none, and asking there returns silence — a guard that says
    "clear" exactly when you consult it is worse than no guard.

    So the claim seam discloses instead of comparing: here are the lanes that
    are live and here is what they are in. The claimer knows what they are
    about to touch and this is the one fact they cannot get from the board.

    Measured need, 2026-08-04: THREE seats built the #183 tree-warning fix in
    parallel under three different lane labels — which-helm-warning-scope,
    treewarn-fires-only-in-a-helm-checkout, fix-183-tree-warning-scope — all in
    helm/cli.py. The label guard cannot see a shared FILE, and lane_overlaps
    could, but it lives in `gc`: it reports the collision to whoever runs
    housekeeping, after the duplicate work is written.

    Same held-lease filter as lane_overlaps (an unheld room is nobody racing
    you) and the same _authored fileset, deliberately reusing both so the two
    surfaces cannot drift into disagreeing about who is live."""
    v = vcs.backend(root)
    live = _live()
    trunk = _trunk(root)
    cache, out = {}, []
    for w in lane_rows(root, registered=registered):
        lane = w["lane"]
        if lane == exclude:
            continue
        branch = (w["branch"] or "")[len("refs/heads/"):]
        if not branch or not live.get(resource(root, lane)):
            continue
        # TRI-STATE, because `or ()` COLLAPSED AN UNKNOWN INTO A DENIAL.
        # _authored returns _EVERYTHING when it could not look — no trunk
        # resolvable, merge-base non-zero, diff non-zero — and _EVERYTHING is
        # an EMPTY frozenset subclass, so it is FALSEY. `sorted(x or ())` then
        # produced [], indistinguishable from a branch that genuinely authored
        # nothing, and the claim seam rendered "CLAIMED, nothing authored yet"
        # about a lane that may have authored plenty. A cap, a timeout or a
        # failed read must yield UNKNOWN and never a confident answer; this
        # one manufactured the confident answer out of the failure itself.
        # (@codex, in review.)
        authored = _authored(v, root, trunk, branch, cache)
        files = None if authored is _EVERYTHING else sorted(authored)
        # A LANE THAT HAS AUTHORED NOTHING IS STILL DISCLOSED, and it is the
        # one that matters most. The docstring above says a guard that answers
        # "clear" exactly when you consult it is worse than no guard — and
        # `if files:` reintroduced that silence on the OTHER side of the same
        # seam. The lane being CLAIMED has no commits, which is why this
        # function discloses instead of comparing; every lane it discloses had
        # to have commits, so two seats who both noticed one defect minutes
        # apart, neither having written a line, each got told the coast was
        # clear. That is not the edge case, it is the CENTRAL case: the
        # collision window is exactly the interval before either side writes
        # code. Measured 2026-08-05 on 13 live lanes — 12 disclosed, 1 dropped,
        # and the dropped one was an actively-worked lane with an open
        # dispatch (helm#1, and the cj/helm-claude duplicate claim that hour).
        out.append((lane, files))
    # ORDER BY URGENCY, three classes. UNKNOWN first — a lane whose history
    # could not be read is the one a claimer can conclude least about and so
    # must see. Then authored-nothing, whose bare NAME is the only signal that
    # exists before either side writes code. Files-bearing lanes last: they
    # carry the most information per row and a claimer can compare them.
    # The caller budgets PER CLASS rather than slicing this list, so a run of
    # one class can no longer starve another.
    out.sort(key=lambda lf: (0 if lf[1] is None else 1 if not lf[1] else 2,))
    return out


def moved_lane_targets(root, registered=None):
    """[(lane, [paths])] — files a LIVE lane authored that TRUNK NO LONGER HAS.

    THE SIBLING OF held_lane_files, ONE AXIS OVER. That one answers "who else
    is editing this file NOW"; this one answers "did the file I am editing
    MOVE OR DIE ON TRUNK SINCE I BRANCHED". Both are disclosures at the same
    seam and they deliberately share _authored, _trunk and the held-lease
    filter so the two cannot drift into disagreeing about who is live.

    MEASURED NEED, 2026-08-05: a lane edited helm/web.py:254; the web split
    landed and moved that code to helm/web_core.py, leaving web.py a 156-line
    facade. TWO OUTCOMES WERE AVAILABLE and only one is loud. The rebase
    conflicted, which is git working correctly. The SILENT branch is the
    expensive one: had the hunks not collided, the edit would have applied to
    a file that no longer holds the caller, the real caller would have kept
    calling a name the same commit deleted, and the owner's endpoint would
    have raised on first click — with a GREEN GATE the whole way, because the
    gate measured the OLD base. It was caught because an agent read a chat
    post carefully. That is a coincidence, not a mechanism.

    A CREATED FILE IS ABSENT FROM TRUNK BY DESIGN, so absence alone is the
    wrong test and would warn on every new module — this function's own lane
    added helm/tasks.py. The discriminator is the lane's MERGE-BASE: a path
    that existed where the lane branched and does NOT exist on trunk is a
    path trunk moved or deleted. A path absent from both is simply new.

    DISCLOSURE, NEVER REFUSAL. The legitimate answer is often "yes, I know, I
    am the one splitting it" — a refusal would have blocked the very split
    that motivated this. Say the thing; let the reader judge.

    CANNOT-LOOK IS NOT CLEAR, the same rule every guard here follows. No
    resolvable trunk, no merge-base, an unreadable diff, and an UNKNOWN
    authored set all yield SILENCE for that lane rather than an all-clear.

    THE `is _EVERYTHING` CHECK IS DEFENCE, NOT LOAD-BEARING TODAY, and an
    earlier draft of this docstring overstated it. _EVERYTHING is an EMPTY
    frozenset, so an UNKNOWN set already intersects to nothing and the lane is
    already skipped — a truthiness test would behave identically RIGHT NOW.
    What the identity check buys is (a) it survives the sentinel ever gaining
    members, which is the only reading under which the two differ, and (b) it
    skips a merge-base spawn. Measured by a reviewer who broke it three ways.

    IT DOES NOT TAKE `exclude`, UNLIKE ITS SIBLING, AND THAT IS DELIBERATE.
    held_lane_files answers "who ELSE is live" so it drops the claimer;
    this answers "did trunk move a file THIS lane is in", and the holder is
    exactly the person who can act on that. Re-claiming your own lane should
    name your own moved targets."""
    v = vcs.backend(root)
    trunk = _trunk(root)
    if not trunk:
        return []
    live = _live()
    cache, out = {}, []
    for w in lane_rows(root, registered=registered):
        lane = w["lane"]
        branch = (w["branch"] or "")[len("refs/heads/"):]
        if not branch or not live.get(resource(root, lane)):
            continue
        files = _authored(v, root, trunk, branch, cache)
        if files is _EVERYTHING:
            continue
        rc, base, _e = v.text(root, "merge-base", branch, trunk)
        base = base.strip()
        if rc != 0 or not base:
            continue
        # ONE SUBPROCESS PER LANE, NOT TWO PER AUTHORED PATH. The first cut
        # asked cat-file twice per file and cost ~600 spawns on a fifteen-lane
        # board — paid on EVERY claim, before the claimer's own success line.
        # --diff-filter=D over base..trunk is both halves of the discriminator
        # in one call: a path git reports DELETED existed at the branch point
        # (or it could not be deleted) and does not exist on trunk.
        #
        # --no-renames IS LOAD-BEARING. With rename detection on, git pairs the
        # delete with the add and the moved path VANISHES from the output —
        # the command returns empty for exactly the case this function exists
        # to catch. Measured both ways.
        # DELETION IS THE RARE CASE; DRAINAGE IS THE ONE THAT HAPPENS.
        # The first cut asked only --diff-filter=D and was SILENT on its own
        # founding incident: the real web split left helm/web.py in place as a
        # 156-line facade, so the path exists at BOTH refs and no delete is
        # ever reported. Measured on the real split pair: 3805 lines -> 156,
        # +36/-3685, --diff-filter=D empty. A guard blind to the case that
        # motivated it is decoration, and only pointing the predicate at the
        # REAL commit pair found that — my fixture had used `git mv`, a shape
        # production does not mint.
        #
        # NO THRESHOLD, AND NO COMPARISON EITHER. A later cut asked whether
        # trunk removed MORE than the lane changed; a reviewer named it as a
        # tuned threshold wearing a ratio of 1.0, which makes detection depend
        # on HOW MUCH THE LANE TYPED rather than on what happened to the file.
        # So this states the FACT and declines to judge it: trunk touched a
        # path this lane is also editing, and here is what it did. The reader
        # decides whether -3685 matters — the same disclosure discipline the
        # sibling follows. Naturally rare: it needs BOTH sides to have touched
        # the same path.
        #
        # TWO SUBPROCESSES PER LANE, NEITHER PER PATH. numstat carries every
        # changed path with its counts; the deletion probe is a second call
        # only because numstat CANNOT tell a deleted file from one emptied in
        # place — both read 0/N — and mislabelling that sends a reader hunting
        # a file that is still there. --no-renames is load-bearing on both:
        # with rename detection on, git pairs the delete with the add and the
        # moved path vanishes from the output entirely.
        rc_d, stat, _e4 = v.text(root, "diff", "--numstat", "--no-renames",
                                 base, trunk, "--")
        if rc_d != 0:
            continue
        rc_g, killed, _e6 = v.text(root, "diff", "--name-only",
                                   "--diff-filter=D", "--no-renames",
                                   base, trunk, "--")
        if rc_g != 0:
            # CANNOT-LOOK IS NOT CLEAR, and the first cut laundered it: an
            # unreadable deletion probe became `set()`, which is
            # indistinguishable from "nothing was deleted" and flows straight
            # into the drainage branch — so a genuinely DELETED file would be
            # reported as merely drained, with a label the evidence does not
            # support. This function's own docstring promises silence on an
            # unreadable input and this line broke that promise two calls
            # later. Skip the lane; an unlabelled event is worse than none.
            continue
        deleted_paths = set(killed.split("\n")) - {""}
        gone = ["%s (deleted on trunk)" % q
                for q in sorted(deleted_paths & set(files))]
        for row in stat.split("\n"):
            cols = row.split("\t")
            if len(cols) != 3 or cols[2] not in files:
                continue
            add, dele, path = cols
            if path in deleted_paths:
                continue                      # already named, once
            a = int(add) if add.isdigit() else 0
            d = int(dele) if dele.isdigit() else 0
            if not (a or d):
                continue
            gone.append("%s (trunk +%d/-%d since you branched)" % (path, a, d))
        if gone:
            out.append((lane, sorted(gone)))
    return out


def lane_overlaps(root, registered=None):
    """[(lane_a, lane_b, [shared files])] — open lanes whose OWN work touches
    the same file. The board's only answer to 'is someone else already on
    this?'

    A LEASE ANSWERS A NARROWER QUESTION THAN THE ONE PEOPLE ASK OF IT. It
    guards a worktree+branch, so it answers 'is anyone else editing this
    ROOM' — never 'is anyone else fixing this DEFECT'. Two seats can each hold
    a valid, uncontested lease on two different lanes aimed at one bug and the
    board reads perfectly healthy. That is not a hypothetical: 2026-07-30 it
    happened THREE times in one day, and every time a human reading chat
    caught it, never a surface.

      roster-test-owns-its-globals x roster-test-owns-the-cockpit-stamp
        -> both rewrote tests/test_seats.py for the same red; main stayed
           broken for hours while each seat deferred to the other
      context-recovery x the landed seat-identity fix
        -> both rebuilt the same fixture in tests/test_seat_identity.py; found
           only when the merge conflicted, after both were written and gated

    FILE OVERLAP IS A PROXY, NOT THE THING, and it is stated as one: two lanes
    can fix one defect in different files, and two can share a file
    legitimately. It is the strongest signal available from state the board
    ALREADY has, and it is checked against both controls — it fires on both
    real incidents above and stays silent on lr-land x never-track, which
    genuinely never collided.

    Diffs each tip against the PAIR'S OWN merge-base, never against the trunk:
    once a lane lands, merge-base(tip, trunk) IS the tip and its diff is empty,
    so a trunk-based probe reports zero overlap for every historical pair. That
    is how the first version of this check scored both known incidents clean.
    BOTH LANES MUST BE ACTIVELY HELD, and that filter is what makes this a
    signal instead of noise. Raw file overlap across every open room is O(N^2)
    and hub-dominated: measured on the live board it produced 26 pairs, most of
    them "we both touched tests/test_seats.py", which is true of nearly every
    lane and tells nobody anything. A PARKED lane is not racing you — its
    holder is gone. Two LIVE leases whose work has converged on one file is the
    shape that actually cost a day: both roster lanes were held by active
    seats, both mid-flight, both certain the other was handling it.

    THE LIMITATION, stated rather than papered over: a lane can only collide
    here with another LANE. The ctx-recovery duplicate collided with work that
    had already LANDED on the trunk, which no lease can represent, so this
    would not have caught it. It catches the live-vs-live case, which is 2 of
    today's 3.

    THE PAIR-BASE IS THE CANDIDATE SET, NOT THE ANSWER — a narrowing added
    2026-08-03 after the false-positive rate was measured rather than argued.
    When two lanes sit on DIFFERENT trunk points, merge-base(a, b) is the older
    of the two bases, so the fresher lane's diff against it includes every
    trunk commit it merely INHERITED by being rebased later. Measured on
    lane/gemini-window-750k against lane/worktree-reap-cadence: 30 files in its
    "own" diff, 29 of them landed trunk commits, 4 genuinely its own. Across
    the whole live board that produced 29 reported pairs of which 10 — 34% —
    shared nothing but inherited landings. It inflates worst exactly when the
    fleet lands fast, which is when the board is read most.

    SO EACH LANE'S FILES ARE INTERSECTED WITH ITS OWN DIFF AGAINST ITS OWN
    MERGE-BASE WITH TRUNK, which is the set it actually authored. Verified over
    all 29 live pairs: it removed all 10 false ones and kept all 19 real ones.

    AND THE FALLBACK IS THE POINT, NOT AN EDGE CASE. Once a lane LANDS,
    merge-base(tip, trunk) IS the tip and its own-diff is EMPTY — the exact
    reason the first version of this check was pair-based at all, and why a
    trunk-based probe scored both founding incidents clean. So an empty
    own-diff falls back to the pair-base set rather than erasing the lane.
    In practice only actively-held lanes reach here and a landed lane is not
    held, but the historical controls run on pairs that HAVE landed, and this
    is what keeps them green.
    """
    v = vcs.backend(root)
    live = _live()
    trunk = _trunk(root)
    files, own, order = {}, {}, []
    for w in lane_rows(root, registered=registered):
        branch = (w["branch"] or "")[len("refs/heads/"):]
        if not branch or not live.get(resource(root, w["lane"])):
            continue          # unheld room: nobody is racing you for it
        order.append((w["lane"], branch))
    out = []
    for i, (lane_a, br_a) in enumerate(order):
        for lane_b, br_b in order[i + 1:]:
            rc, base, _e = v.text(root, "merge-base", br_a, br_b)
            if rc != 0 or not base:
                continue          # unrelated histories: no shared question
            # keyed by (branch, base) because the SAME branch has a different
            # own-diff against a different pair-base — caching on branch alone
            # would answer the second pair with the first pair's files
            for br in (br_a, br_b):
                if (br, base) not in files:
                    rc2, out2, _e2 = v.text(root, "diff", "--name-only",
                                            base + ".." + br)
                    files[(br, base)] = (set(out2.split("\n")) - {""}
                                         if rc2 == 0 else set())
            a = _authored(v, root, trunk, br_a, own) & files[(br_a, base)]
            b = _authored(v, root, trunk, br_b, own) & files[(br_b, base)]
            shared = sorted(a & b)
            if shared:
                out.append((lane_a, lane_b, shared))
    return out


def _authored(v, root, trunk, branch, cache):
    """The files THIS branch actually wrote — its diff against its OWN
    merge-base with trunk.

    LOOKED-AND-FOUND-NOTHING IS NOT COULD-NOT-LOOK, and conflating them is
    what made the first cut of this narrowing WORSE than no narrowing. A
    branch with no commits of its own — freshly claimed, just rebased, work
    still uncommitted — has an empty own-diff, and the first version treated
    that as "cannot narrow" and handed back the un-narrowed pair-base set,
    which for such a branch is 100% inherited trunk landings. Measured: 29
    pairs / 10 false became 30 pairs / 12 false. The narrowing generated the
    exact noise it was written to remove.

    AN EMPTY OWN-DIFF HAS TWO CAUSES AND THEY NEED OPPOSITE ANSWERS. A branch
    that has authored NOTHING YET must drop out of every pair; a branch whose
    work has REACHED THE TRUNK while its lease is still held must NOT, because
    it still collides with an open lane on the same file — the whole point of
    `test_overlap_survives_one_lane_already_being_on_the_trunk`, and the
    merged-but-room-not-yet-reaped window is real.

    THE BRANCH'S OWN BIOGRAPHY SEPARATES THEM. Tip equality is only a moment,
    and trunk first-parent membership still includes a lane landed by fast-
    forward. The branch reflog answers the actual question: creation/rebase
    bookkeeping with no authored action returns EMPTY; commit/merge/cherry-
    pick/revert/rebase-pick evidence preserves the pair-base set. An unreadable,
    truncated, or unfamiliar biography is UNKNOWN and takes that same
    conservative preserve-overlap path.

    A FAILED COMPUTATION takes that same no-op path: no trunk resolvable,
    merge-base non-zero, diff non-zero. Same rule as every other guard here —
    a check that could not look must not answer as if it had — and un-narrowed
    is the safe side, because a missed collision cost a day and an extra pair
    costs a glance."""
    if branch in cache:
        return cache[branch]
    authored = _EVERYTHING
    if trunk:
        rc, base, _e = v.text(root, "merge-base", branch, trunk)
        if rc == 0 and base.strip():
            rc2, out2, _e2 = v.text(root, "diff", "--name-only",
                                    base.strip() + ".." + branch)
            if rc2 == 0:
                found = set(out2.split("\n")) - {""}
                if found:
                    authored = found
                elif _branch_authored(v, root, branch) is False:
                    authored = found
    cache[branch] = authored
    return authored


def _branch_authored(v, root, branch):
    """True/False/None from the branch's OWN reflog biography.

    An FF-landed lane and an untouched stale lane both sit on trunk's
    first-parent line. The branch reflog separates them: a lane-created commit,
    merge, cherry-pick, revert, or rebase pick is positive authorship evidence;
    creation/rebase bookkeeping alone proves no authored commit. Unknown
    actions, an unreadable log, or a log with no creation boundary return None
    and keep the conservative wide overlap set.
    """
    rc, out, _e = v.text(root, "reflog", "show", "--format=%gs", branch)
    if rc != 0:
        return None
    actions = [x.strip() for x in out.splitlines() if x.strip()]
    if not actions or not any(x.startswith("branch: Created from ")
                              for x in actions):
        return None
    authored = ("commit", "merge ", "merge:", "cherry-pick", "revert",
                "rebase (pick)")
    if any(x.startswith(authored) for x in actions):
        return True
    bookkeeping = ("branch: Created from ", "rebase (start)",
                   "rebase (finish)")
    return False if all(x.startswith(bookkeeping) for x in actions) else None


class _Everything(frozenset):
    """Intersects to a no-op. A sentinel rather than a flag because the call
    site is one expression and a flag would have to be threaded through it."""

    def __and__(self, other):
        return other

    def __rand__(self, other):
        return other


_EVERYTHING = _Everything()


def list_rows(root, registered=None):
    """The shadow board: registry ⋈ claims, computed — lane, holder,
    remaining, dirty, ahead/behind the base, and `lease` for the rows THIS
    seat holds (seats.own_leases — never another holder's; see its docstring
    for why that is a strand-fix and not a disclosure). The lease join is
    keyed on the stored resource name, so the token printed here is the exact
    token `helm work release` will accept."""
    # ahead/behind is an OWNER-FACING distance to the trunk, so it measures
    # against the trunk ref too — a lane reported "behind 6" against a stale
    # local main tells the owner to rebase onto something the fleet never had.
    base, v = _trunk(root), vcs.backend(root)
    own = seats.own_leases()
    # ONE READ FOR BOTH COLUMNS. `_live()` and `claims_list()` were two
    # separate reads, so the HOLDER on a row could come from one instant and
    # its LIVENESS from another — a row could show a holder that the
    # classification no longer knew about, or vice versa. And `stale_by` kept
    # only the stale ones, which FLATTENED UNKNOWN back into healthy on the
    # board: exactly the collapse this lane exists to end, surviving on the
    # one surface that lists lanes. @codex-2, non-blocking finding on review
    # row 18a49b86cf1f, fixed in the same pass as the producer cases. THE ROW
    # AND NOT THE REVIEWED SHA: that tip was a lane commit this lane has since
    # been rebased past, no branch contains it, and a fresh clone cannot
    # resolve it — a citation only the author's machine can follow is not
    # provenance. The ledger row outlives every rebase.
    claims = seats.claims_list()
    live = {c["resource"]: {"holder": c["holder"],
                            "remaining": c["remaining"]} for c in claims}
    liveness_by = {c["resource"]: c.get("liveness") for c in claims}
    rows = []
    for w in lane_rows(root, registered=registered):
        res = resource(root, w["lane"])
        held = live.get(res)
        branch = (w["branch"] or "")[len("refs/heads/"):]
        counts = v.ahead_behind(root, base, branch or "HEAD")
        behind, ahead = counts if counts else ("?", "?")
        rows.append({"lane": w["lane"], "branch": branch, "path": w["path"],
                     "holder": held["holder"] if held else None,
                     "remaining": held["remaining"] if held else None,
                     "lease": own.get(res) if held else None,
                     "liveness": liveness_by.get(res),
                     "stale": liveness_by.get(res) == "stale",
                     # ONE status call, then every field it answers — the board
                     # needs the detail, not just the boolean
                     **{k: v for k, v in _room_status(w["path"]).items()
                        if k in ("dirty", "conflicts", "operation",
                                 "dangling_conflict")},
                     "locked": w["locked"],
                     "ahead": ahead, "behind": behind})
    return rows
