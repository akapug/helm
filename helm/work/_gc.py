"""helm work — the gc cluster: git verdict helpers, the live-lease read,
the rescue commit, housekeeping (scan/enact/orphans), and the room board.
Moved verbatim from the pre-split helm/work.py.
"""
from .. import pk, seats
from ._lanes import (
    _git, _occupants, find_root, lane_rows, resource, worktrees,
)


def _dirty(path):
    """Uncommitted/untracked bytes in the room? An unreadable room reads as
    DIRTY — callers must fail toward rescue, never toward discard."""
    rc, out, _err = _git(path, "status", "--porcelain")
    return True if rc != 0 else bool(out)


def _base(root):
    """The integration branch: main when it exists, else the shared
    checkout's own HEAD."""
    if _git(root, "rev-parse", "--verify", "-q", "refs/heads/main")[0] == 0:
        return "main"
    rc, out, _err = _git(root, "symbolic-ref", "--short", "HEAD")
    return out if rc == 0 and out else "main"


def _has_branch(root, branch):
    return _git(root, "rev-parse", "--verify", "-q",
                "refs/heads/" + branch)[0] == 0


def _merged(root, branch):
    return _git(root, "merge-base", "--is-ancestor", branch, _base(root))[0] == 0


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


def _wip_commit(path, msg):
    """The lost-and-found write: stage EVERYTHING, commit --no-verify onto
    the lane's own branch under the janitor identity. Strictly loss-reducing
    and reversible — the only authored-bytes write in this module."""
    _git(path, "add", "-A")
    return _git(path, "-c", "user.name=helm-work",
                "-c", "user.email=helm-work@local",
                "commit", "--no-verify", "-m", msg)


def _removal_blocker(root, path, lane=None, stale_lease_ok=False):
    """Current-state refusal reason, or None when removal may proceed. Called
    at enact time (and again immediately before remove after any rescue commit)
    so a scan cannot authorize deleting a newly leased/locked/occupied room."""
    cur = next((w for w in worktrees(root) if w["path"] == path), None)
    if cur is None:
        return "no longer a registered worktree"
    if lane:
        held = _live().get(resource(root, lane))
        if held:
            return "lease live — %s holds it" % held["holder"]
    occupied = _occupants(path)
    if occupied:
        return "OCCUPIED by cwd pid(s) %s" % ",".join(occupied)
    if cur["locked"] and not (stale_lease_ok
                              and cur["reason"].startswith("lease:")):
        return "LOCKED: %s" % (cur["reason"] or "no reason")
    return None


# ---------------------------------------------------------------------------
# housekeeping — the four-row verdict table (the dumpster is not in it)
# ---------------------------------------------------------------------------

def gc_scan(root):
    """One row per lane room, verdict ∈ keep|remove|rescue. Live lease =
    guest in the room; an out-of-band lock = do-not-disturb; a `lease:` lock
    with no live lease is a STALE key tag and falls through to the sweep."""
    live = _live()
    rows = []
    for w in lane_rows(root):
        lane = w["lane"]
        held = live.get(resource(root, lane))
        branch = (w["branch"] or "")[len("refs/heads/"):] or None
        occupied = _occupants(w["path"])
        r = {"lane": lane, "path": w["path"], "branch": branch,
             "locked": w["locked"], "lock_reason": w["reason"],
             "occupied": occupied, "merged": False}
        if held:
            r.update(verdict="keep", why="lease live — %s holds it, %ds left"
                     % (held["holder"], held["remaining"]))
        elif occupied:
            r.update(verdict="keep", why="OCCUPIED by cwd pid(s) %s — never remove"
                     % ",".join(occupied))
        elif w["locked"] and not w["reason"].startswith("lease:"):
            r.update(verdict="keep", why="locked out-of-band (%s)"
                     % (w["reason"] or "no reason"))
        elif _dirty(w["path"]):
            r.update(verdict="rescue",
                     why="lease-less + DIRTY — wip-commit onto %s, then remove "
                         "(lost-and-found, never the dumpster)" % (branch or "?"))
        else:
            r["merged"] = bool(branch) and _merged(root, branch)
            r.update(verdict="remove",
                     why="lease-less + clean — remove"
                     + ("; branch merged, -d too" if r["merged"]
                        else "; branch %s stays for the integrator" % branch))
        rows.append(r)
    return rows


def gc_enact(root, row):
    """Enforce ONE non-keep row -> [lines]. Rescue-first: a failed rescue
    commit SKIPS the removal loudly — the room outlives any error. Re-check
    lease, lock and cwd occupancy at enact time: a safe scan can go stale before
    the destructive syscall, and a sibling pane may enter the room meanwhile."""
    if row["verdict"] == "keep":
        return []
    blocked = _removal_blocker(root, row["path"], row["lane"],
                               stale_lease_ok=True)
    if blocked:
        return ["SKIPPED %s (%s) — kept" % (row["path"], blocked)]
    lines = []
    if row["verdict"] == "rescue":
        rc, _out, err = _wip_commit(
            row["path"], "wip: rescued %s %s" % (row["lane"], pk.now_ts()))
        if rc != 0:
            return ["SKIPPED %s (rescue commit failed: %s) — room kept"
                    % (row["path"], err)]
        lines.append("rescued dirty work -> %s" % row["branch"])
    blocked = _removal_blocker(root, row["path"], row["lane"],
                               stale_lease_ok=True)
    if blocked:
        return lines + ["SKIPPED %s (%s) — kept" % (row["path"], blocked)]
    _git(root, "worktree", "unlock", row["path"])   # stale lease tag, if any
    rc, _out, err = _git(root, "worktree", "remove", row["path"])
    if rc != 0:
        return lines + ["SKIPPED %s (%s)" % (row["path"], err)]
    lines.append("removed " + row["path"])
    if row["merged"] and row["branch"]:
        _git(root, "branch", "-d", row["branch"])
        lines.append("deleted merged branch " + row["branch"])
    return lines


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


# ---------------------------------------------------------------------------
# the room board + the rail
# ---------------------------------------------------------------------------

def list_rows(root, registered=None):
    """The shadow board: registry ⋈ claims, computed — lane, holder,
    remaining, dirty, ahead/behind the base."""
    live, base = _live(), _base(root)
    rows = []
    for w in lane_rows(root, registered=registered):
        held = live.get(resource(root, w["lane"]))
        branch = (w["branch"] or "")[len("refs/heads/"):]
        behind = ahead = "?"
        rc, out, _err = _git(root, "rev-list", "--left-right", "--count",
                             "%s...%s" % (base, branch or "HEAD"))
        if rc == 0 and "\t" in out:
            behind, ahead = out.split("\t")
        rows.append({"lane": w["lane"], "branch": branch, "path": w["path"],
                     "holder": held["holder"] if held else None,
                     "remaining": held["remaining"] if held else None,
                     "dirty": _dirty(w["path"]), "locked": w["locked"],
                     "ahead": ahead, "behind": behind})
    return rows
