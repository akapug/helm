"""helm work — the peek cluster: a READ-ONLY door to an arbitrary landed sha.

THE GAP THIS CLOSES (measured on this fleet, 2026-08-01): a REVIEWER had no
sanctioned way to stand at a point-in-time commit. The shared checkout's HEAD
rule refuses any departure from the trunk, `helm work claim` mints a LANE room
— a lease, a branch, a write intent, the wrong shape for a look — and the
integrator override is integrator-only. The gap even reproduced itself while
this module was being built: the build agent assigned to it was first refused
by that same guard when its harness tried to mint a scratch worktree.

A PEEK is the missing shape: `git worktree add --detach` at exactly the
resolved commit, under the dedicated container `<repo>-wt/peeks/<sha12>`.
No lease (nothing to hand back), no branch (nothing to protect or land), no
lock (disposable by declaration), and no env override — the ref guard admits
the birth STRUCTURALLY (see `_guard.REF_GUARD_HOOK`: detached-only, peek-area
only, never a branch ref). The room path is the pure function of the commit,
the `lane_path` law: peeking the same sha twice converges on one room.

Whole-object honesty: an unresolvable committish refuses with git's own
words; an uncreatable peek area refuses with the OSError; a room whose HEAD
moved off the sha its name declares refuses reuse rather than handing over a
room that is no longer what it says. Nothing here ever falls back silently.
"""
import os

from .. import eventledger, vcs
from ._common import PEEK_DIRNAME
from ._lanes import _occupants, _panes_bound_to, worktrees


def peek_area(root):
    """The one peek container — a sibling of the lane rooms, one level down
    so `lane_rows`/`_infer_lane`/gc structurally cannot mistake a peek for a
    lane (the SEAT_HOME_DIRNAME precedent, not a filter bolted on later)."""
    return root.rstrip(os.sep) + "-wt" + os.sep + PEEK_DIRNAME


def _activity_path(root):
    """Stable sibling-lock anchor serializing peek reuse with removal."""
    return os.path.join(peek_area(root), ".activity")


def resolve_commit(root, committish):
    """(full_sha, None) or (None, git's real error). `^{commit}` peels tags
    and refuses trees/blobs; `--end-of-options` keeps a `-`-leading argument
    an argument. The error is git's own words — a refusal that paraphrases
    away `unknown revision` costs the caller the diagnosis."""
    rc, out, err = vcs.backend(root).text(
        root, "rev-parse", "--verify", "--end-of-options",
        committish + "^{commit}")
    if rc != 0 or not out:
        return None, (err or "rev-parse said nothing").strip()
    return out.strip(), None


def peek_path(root, sha):
    """Deterministic room path — the pure function IS the registry key
    (the `lane_path` law). sha12 keeps the dir readable; the room's HEAD
    holds the full truth."""
    return os.path.join(peek_area(root), sha[:12])


def peek(root, committish):
    """(rc, payload). Mint (or converge on) the read-only room for
    `committish`. Success payload: {path, sha}; refusal payload: {error}.

    Idempotent ONLY while the room is still what its name declares: detached
    at exactly the sha. Reuse and removal share one activity lock, so a cadence
    pass cannot consume stale evidence after this call refreshes the room."""
    sha, err = resolve_commit(root, committish)
    if sha is None:
        return 1, {"error": "helm work: peek cannot resolve '%s' — %s"
                            % (committish, err)}
    try:
        os.makedirs(peek_area(root), exist_ok=True)
    except OSError as exc:
        return 1, {"error": "helm work: cannot create peek area %s — %s"
                            % (peek_area(root), exc)}
    with eventledger.locked(_activity_path(root)) as held:
        if not held:
            return 1, {"error": "helm work: peek activity lock unavailable"}
        return _peek_locked(root, sha)


def _peek_locked(root, sha):
    """Mint/reuse one resolved peek while holding the activity lock."""
    path = peek_path(root, sha)
    v = vcs.backend(root)
    row = next((w for w in worktrees(root)
                if os.path.abspath(w["path"]) == path), None)
    if row is not None:
        if row.get("branch") or v.head_sha(path) != sha:
            return 1, {"error":
                       "helm work: peek room %s exists but is no longer a "
                       "clean peek of %s (branch=%s HEAD=%s) — inspect it, or "
                       "`helm work peek --drop %s` first"
                       % (path, sha[:12], row.get("branch") or "detached",
                          (v.head_sha(path) or "unreadable")[:12], sha[:12])}
        # REUSE IS ACTIVITY. This touch and the drop-side TTL recheck share
        # `_activity_path`: whichever wins the lock authors the next state.
        try:
            os.utime(path, None)
        except OSError as exc:
            return 1, {"error": "helm work: cannot refresh peek activity %s — %s"
                                % (path, exc)}
        return 0, {"path": path, "sha": sha, "reused": True}
    # NO env override on this add — passing the ref guard without
    # HELM_WORK_INTEGRATOR/HELM_WORK_CLAIM is the entire point of the door;
    # if the guard refuses this, the guard is wrong and this must stay red.
    rc, _out, aerr = v.add_worktree_detached(root, path, sha)
    if rc != 0:
        return 1, {"error": "helm work: worktree add --detach refused — %s"
                            % ((aerr or "").strip() or "rc %s" % rc)}
    return 0, {"path": path, "sha": sha, "reused": False}


def peek_drop(root, target, stale_ttl_s=None, now=None):
    """(rc, [lines]). Retire one peek room.

    Explicit drops ignore age. Cadence drops pass ``stale_ttl_s`` and recheck
    the room's current mtime under the SAME activity lock reuse holds; refreshed
    evidence refuses before any occupant/pane/removal probe.
    """
    area = peek_area(root)
    cand = os.path.abspath(target)
    if os.path.dirname(cand) == os.path.abspath(area):
        path = cand
    else:
        sha, err = resolve_commit(root, target)
        if sha is None:
            return 1, ["helm work: peek --drop takes a peek room path or a "
                       "resolvable commit — %s" % err]
        path = peek_path(root, sha)
    if not os.path.isdir(area):
        # The CONTAINER is absent — no peek was ever cut in this estate.
        # "not a registered peek room" here would misdescribe the state: it
        # sends the caller checking one room's registration when the true
        # fact is that there are no peek rooms at all.
        return 1, ["helm work: no peek container at %s — no peek was ever "
                   "cut here, nothing to drop" % area]
    with eventledger.locked(_activity_path(root)) as held:
        if not held:
            return 1, ["helm work: peek activity lock unavailable — %s kept"
                       % path]
        return _peek_drop_locked(root, path, stale_ttl_s=stale_ttl_s,
                                 now=now)


def _peek_drop_locked(root, path, stale_ttl_s=None, now=None):
    """Remove one path while holding the activity lock."""
    if stale_ttl_s is not None:
        import time
        try:
            idle = (time.time() if now is None else now) \
                - os.stat(path).st_mtime
        except OSError:
            return 1, ["helm work: cannot refresh peek age for %s — kept" % path]
        if idle < stale_ttl_s:
            return 1, ["helm work: peek activity refreshed %s (%ds idle) — kept"
                       % (path, max(0, int(idle)))]
    registered = {os.path.abspath(w["path"]) for w in worktrees(root)}
    if path not in registered:
        return 1, ["helm work: %s is not a registered peek room — nothing "
                   "dropped" % path]
    occupied = _occupants(path)
    if occupied:
        return 1, ["helm work: %s is OCCUPIED by cwd pid(s) %s — move out "
                   "before dropping" % (path, ",".join(occupied))]
    panes, pane_err = _panes_bound_to(path)
    if pane_err:
        return 1, ["helm work: cannot prove %s is pane-free — %s"
                   % (path, pane_err)]
    if panes:
        return 1, ["helm work: metaharness pane(s) %s are bound to %s — "
                   "close them before dropping" % (",".join(panes), path)]
    rc, _out, err = vcs.backend(root).remove_worktree(root, path)
    if rc != 0:
        return 1, ["helm work: peek drop refused — %s"
                   % ((err or "").strip() or "rc %s" % rc)]
    return 0, ["helm work: peek room %s dropped" % path]


PEEK_TTL_S = 2 * 3600


def stale_peeks(root, ttl_s=PEEK_TTL_S, registered=None, now=None):
    """[(path, idle_s)] — peek rooms idle past the TTL, oldest first.

    THE RECURRENCE THE OWNER ASKED KILLED FIVE TIMES (2026-08-03): peeks are
    minted per review/gate and nothing retired them, so a metaharness sidebar
    accreted one ghost project per peek forever. A peek is read-only BY
    CONSTRUCTION, so age is the only signal needed HERE — every live-room
    refusal (cwd occupant, bound pane, dirty tree) already lives in
    peek_drop, which stays the ONLY remover. Idleness is the room dir's mtime,
    explicitly refreshed by `peek()` reuse. The scan is candidate evidence,
    never deletion authority: drop rechecks it under the shared activity lock,
    then independently refuses cwd occupants, panes, and dirty worktrees.

    `now` is injectable for tests; the default reads the wall clock at the
    call, never at import."""
    import time
    t = time.time() if now is None else now
    out = []
    for row in peek_rows(root, registered=registered):
        try:
            idle = t - os.stat(row["path"]).st_mtime
        except OSError:
            continue                  # vanished mid-scan — the phantom pass owns it
        if idle >= ttl_s:
            out.append((row["path"], int(idle)))
    out.sort(key=lambda pair: -pair[1])
    return out


def peek_rows(root, registered=None):
    """The registered rooms inside the peek container, for the board. Shape
    is deliberately NOT the lane-row shape: a peek has no lane, holder,
    lease, or ahead/behind — printing it in that table would invite exactly
    the claim/release verbs that must never aim here."""
    area = os.path.abspath(peek_area(root))
    rows = []
    for w in registered if registered is not None else worktrees(root):
        path = os.path.abspath(w["path"])
        if os.path.dirname(path) != area:
            continue
        rows.append({"name": os.path.basename(path), "path": path,
                     "branch": (w["branch"] or "")[len("refs/heads/"):] or None,
                     "locked": w["locked"]})
    return rows
