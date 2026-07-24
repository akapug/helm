"""helm work — the claims cluster: check-in/check-out at the desk plus the
CLI arg leaf helpers. Moved verbatim from the pre-split helm/work.py.
"""
import os

from .. import pk, seats
from ._common import DEFAULT_TTL, _VALUE_FLAGS
from ._lanes import (
    _git, _occupants, lane_branch, lane_path, resource, worktrees,
)
from ._gc import _base, _dirty, _has_branch, _merged, _wip_commit


# ---------------------------------------------------------------------------
# check-in / check-out
# ---------------------------------------------------------------------------

def claim(root, lane, seat, ttl=DEFAULT_TTL, lease=None, session=None):
    """(rc, line). Check-in: the lease FIRST (seats.claim, unmodified — held
    by someone else is the existing refusal), then the room. Idempotent: a
    registered room is reused, a parked lane branch re-opens; re-claim with
    --lease extends. A room that fails to mint hands the key straight back."""
    res = resource(root, lane)
    ok, msg, lease_id = seats.claim(res, seat, ttl=ttl, lease=lease,
                                    session=session)
    if not ok:
        return 1, "helm work: " + msg
    path, branch = lane_path(root, lane), lane_branch(lane)
    if path not in {w["path"] for w in worktrees(root)}:
        args = (["worktree", "add", path, branch] if _has_branch(root, branch)
                else ["worktree", "add", "-b", branch, path, _base(root)])
        # The ref-guard cannot tell this apart from a forbidden `checkout -b`
        # — `worktree add -b` runs it with cwd AND toplevel both equal to the
        # shared checkout. So the ONE sanctioned branch creator announces
        # itself, scoped to this single call rather than the process env.
        rc, _out, err = _git(root, *args, env={"HELM_WORK_CLAIM": "1"})
        if rc != 0:
            seats.release(res, seat, lease=lease_id, session=session)
            return 1, "helm work: worktree add failed — %s (lease returned)" % err
    _git(root, "worktree", "lock", path, "--reason", "lease:" + lease_id[:8])
    return 0, "%s\t%s\t%s\t%d" % (path, branch, lease_id, ttl)


def release_lane(root, lane, seat, lease=None, session=None, park=False):
    """(rc, [lines]). Checkout at the desk — inspect the room BEFORE the key
    changes hands: dirty REFUSES with exactly two exits (commit and re-run,
    or --park: WIP-commit onto the lane branch — nothing is ever discarded).
    The key surrender (seats.release — the composite-binding validator)
    precedes removal, so a caller with the wrong lease removes nothing; a
    removal that fails after a good release leaves a lease-less clean room
    the reaper sweeps. Merged branch tidied; unmerged stays, integrator told."""
    res, path, branch = resource(root, lane), lane_path(root, lane), lane_branch(lane)
    lines = []
    room = path in {w["path"] for w in worktrees(root)}
    occupied = _occupants(path) if room else []
    if occupied:
        return 1, ["helm work: %s is OCCUPIED by cwd pid(s) %s — room and "
                   "lease kept; move every live pane/process out before release"
                   % (path, ",".join(occupied))]
    if room and _dirty(path):
        if not park:
            return 1, ["helm work: %s is DIRTY — two exits, no third: commit "
                       "in the room and re-run, or --park (WIP-commits onto "
                       "%s; nothing is ever discarded)" % (path, branch)]
        rc, _out, err = _wip_commit(path, "wip: parked %s %s" % (lane, pk.now_ts()))
        if rc != 0:
            return 1, ["helm work: park commit failed — %s (room untouched, "
                       "lease kept)" % err]
        lines.append("helm work: parked WIP onto %s" % branch)
    ok, msg = seats.release(res, seat, lease=lease, session=session)
    if not ok:
        return 1, lines + ["helm work: " + msg]
    lines.append("helm work: " + msg)
    if room:
        _git(root, "worktree", "unlock", path)
        rc, _out, err = _git(root, "worktree", "remove", path)
        if rc != 0:
            return 1, lines + ["helm work: room stays (%s) — lease released; "
                               "`helm work gc` sweeps it" % err]
        lines.append("helm work: room %s removed" % path)
    if _has_branch(root, branch):
        if _merged(root, branch):
            _git(root, "branch", "-d", branch)
            lines.append("helm work: branch %s was merged — deleted" % branch)
        else:
            note = "lane %s released; branch %s awaits integration" % (lane, branch)
            lines.append("helm work: " + note)
            try:
                from .. import chat
                chat.post("@integrator " + note)
            except Exception:
                pass
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
    """Inside a room, release needs no lane argument."""
    box = root.rstrip(os.sep) + "-wt" + os.sep
    cwd = cwd or os.getcwd()
    return cwd[len(box):].split(os.sep)[0] if cwd.startswith(box) else None
