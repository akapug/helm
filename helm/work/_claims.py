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
    _merge_state, _retire_disposable_occupants, _runtime_source_in, _wip_commit,
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
    for an hour. @codex-3 claimed a lane, inherited it for free, and caught it
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

def claim(root, lane, seat, ttl=DEFAULT_TTL, lease=None, session=None):
    """(rc, line). Check-in: the lease FIRST (seats.claim, unmodified — held
    by someone else is the existing refusal), then the room. Idempotent: a
    registered room is reused, a parked lane branch re-opens; re-claim with
    --lease extends. A room that fails to mint hands the lease straight back."""
    res, v = resource(root, lane), vcs.backend(root)
    ok, msg, lease_id = seats.claim(res, seat, ttl=ttl, lease=lease,
                                    session=session)
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
        rc, _out, err = v.add_worktree(
            root, path, branch,
            base=_base(root) if fresh else None,
            env={"HELM_WORK_CLAIM": "1"})
        if rc != 0:
            seats.release(res, seat, lease=lease_id, session=session)
            return 1, "helm work: worktree add failed — %s (lease returned)" % err
    v.lock_worktree(root, path, "lease:" + lease_id[:8])
    return 0, "%s\t%s\t%s\t%d" % (path, branch, lease_id, ttl)



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


def release_lane(root, lane, seat, lease=None, session=None, park=False):
    """(rc, [lines]). Surrender the lease, but retire the room only when Git
    proves the lane landed. A wrong or expired confirmation token mutates
    nothing: a park commit first strictly refreshes the existing grant; a clean
    release performs the single authoritative ledger write. Unlanded work keeps
    BOTH branch and worktree and emits triage evidence. A holder who lost their
    token reads it back off `helm work list` (seats.own_leases)."""
    res, path, branch = resource(root, lane), lane_path(root, lane), lane_branch(lane)
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
        rc, _out, err = _wip_commit(path, "wip: parked %s %s" % (lane, pk.now_ts()))
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
    if room:
        if merged and disposable:
            stopped, stop_error = _retire_disposable_occupants(path)
            if stopped:
                lines.append("helm work: stopped disposable Orca shell pid(s) %s" %
                             ",".join(stopped))
            if stop_error:
                return 1, lines + ["helm work: room stays (%s) — lease released; "
                                   "`helm work gc` retries only after a fresh "
                                   "landedness proof" % stop_error]
        v.unlock_worktree(root, path)
        if merged:
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
            note = _branch_triage(root, lane, branch, state=state)
            lines.append("helm work: " + note)
            try:
                from .. import chat
                chat.post("@integrator " + note)
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
