#!/usr/bin/env python3
"""helm work — in-cave git coordination: worktree lifecycle on the claims
lane (design: prd/2026-07-21-in-cave-git-coordination.md — the maintainer's
tree + the front desk). The shared checkout is the INTEGRATOR's tree; every
other seat works in a private room `<repo>-wt/<lane>` on branch
`lane/<lane>`, checked in and out at the desk.

Every leg is a REUSE — the anti-bureaucracy bar is what this module does NOT
add:
  * the desk     — seats.claim/release/claims_list, untouched: the lease
                   nonce is the room key, TTL the checkout deadline, expiry
                   monotonic; the stop-guard already refuses a session stop
                   with the key still in pocket.
  * the registry — `git worktree list --porcelain` ⋈ `.claims.json`, joined
                   at read time. ZERO new state files: registry drift is
                   unrepresentable.
  * do-not-disturb — `git worktree lock --reason lease:<id8>` (git-native:
                   even raw prune/remove refuses while locked).
  * housekeeping — `work gc`, dry-run default (gc.py culture). LOCKED or
                   OCCUPIED (any live process cwd) rooms are immune. The ONLY
                   write to authored bytes anywhere here is a RESCUE COMMIT
                   onto the lane's own branch — lost-and-found, never the
                   dumpster; no code path discards uncommitted work.
  * the rail     — composed reference-transaction + post-checkout hooks for
                   the ONE resource that needs determinism (the shared
                   checkout). The first refuses branch/HEAD mutation before
                   it happens and protects occupied worktree branches; the
                   second is a pointer-only safety net. Existing hooks remain
                   first-class participants, preserved byte-for-byte.

Tripwires (design §5): per-file claims, approval steps, a second registry,
queues/priorities on lanes, a daemon — any of these is the MC slide; stop.
"""
import fcntl
import os
import re
import shlex
import stat
import subprocess
import sys
import tempfile

from . import automap, pk, seats

DEFAULT_TTL = 4 * 3600          # a build lane, not a chat lock
LANE_RE = re.compile(r"[A-Za-z0-9._-]{1,64}$")
_VALUE_FLAGS = ("--repo", "--seat", "--lease", "--ttl")

MANAGED_HOOK_MARKER = "# helm work managed hook:"
LEGACY_HOOK_MARKERS = ("# helm work ref-guard", "# helm work guard")


REF_GUARD_HOOK = """#!/bin/sh
# helm work managed hook: reference-transaction v2
# Existing executable hook, when present, runs first with the exact same stdin.
base=%(base)s
user_hook=%(user_hook)s

helm_work_ref_guard() {
  [ "$1" = "prepared" ] || return 0
  [ "$HELM_WORK_INTEGRATOR" = "1" ] && return 0
  [ "$HELM_WORK_CLAIM" = "1" ] && return 0

  git_dir=$(git rev-parse --path-format=absolute --git-dir 2>/dev/null) || {
    echo "[helm work] REFUSED: cannot identify the invoking git directory" >&2
    return 1
  }
  common=$(git rev-parse --path-format=absolute --git-common-dir 2>/dev/null) || {
    echo "[helm work] REFUSED: cannot identify the common git directory" >&2
    return 1
  }
  current=$(git symbolic-ref -q HEAD 2>/dev/null || :)
  main_tree=0
  [ "$git_dir" = "$common" ] && main_tree=1
  zero=0000000000000000000000000000000000000000
  registered_ready=0

  while IFS=' ' read -r old new ref; do
    case "$ref" in
      HEAD)
        if [ "$main_tree" = "1" ] && [ "$new" != "ref:refs/heads/$base" ]; then
          echo "[helm work] REFUSED: shared checkout HEAD must stay on '$base'" >&2
          echo "[helm work] claim a private room instead: helm work claim <lane>" >&2
          echo "[helm work] integrator override: HELM_WORK_INTEGRATOR=1" >&2
          return 1
        fi ;;
      refs/heads/*)
        branch=${ref#refs/heads/}
        if [ "$ref" != "$current" ] || [ "$new" = "$zero" ]; then
          if [ "$registered_ready" = "0" ]; then
            registered=$(git worktree list --porcelain) || {
              echo "[helm work] REFUSED: cannot verify worktree occupancy for '$branch'" >&2
              return 1
            }
            registered_ready=1
          fi
          if printf '%%s\n' "$registered" | grep -Fqx "branch $ref"; then
            echo "[helm work] REFUSED: '$branch' is OCCUPIED by a registered worktree" >&2
            echo "[helm work] move/release that room before mutating its branch" >&2
            echo "[helm work] integrator override: HELM_WORK_INTEGRATOR=1" >&2
            return 1
          fi
        fi
        actual_old=$(git rev-parse --verify "$ref" 2>/dev/null || :)
        if [ "$main_tree" = "1" ] && [ -z "$actual_old" ] && \
           [ "$new" != "$zero" ] && [ "$ref" != "refs/heads/$base" ]; then
          echo "[helm work] REFUSED: '$branch' may not be created in the shared checkout" >&2
          echo "[helm work] claim its room: helm work claim $branch" >&2
          echo "[helm work] integrator override: HELM_WORK_INTEGRATOR=1" >&2
          return 1
        fi
        if [ "$main_tree" = "1" ] && [ "$ref" = "refs/heads/$base" ] && \
           [ -n "$actual_old" ] && [ "$new" != "$zero" ] && \
           [ "$actual_old" != "$new" ] && \
           ! git merge-base --is-ancestor "$actual_old" "$new"; then
          echo "[helm work] REFUSED: non-fast-forward update of shared '$base'" >&2
          echo "[helm work] integrator override: HELM_WORK_INTEGRATOR=1" >&2
          return 1
        fi ;;
    esac
  done
  return 0
}

if [ -x "$user_hook" ]; then
  umask 077
  input=$(mktemp "${TMPDIR:-/tmp}/helm-work-ref.XXXXXX") || {
    echo "[helm work] REFUSED: cannot capture reference transaction input" >&2
    exit 1
  }
  trap 'rm -f "$input"' 0 1 2 3 15
  cat >"$input" || exit 1
  "$user_hook" "$@" <"$input" || exit $?
  helm_work_ref_guard "$@" <"$input"
  exit $?
fi
helm_work_ref_guard "$@"
exit $?
"""


GUARD_HOOK = """#!/bin/sh
# helm work managed hook: post-checkout v2
# Existing executable hook, when present, runs first with the original args.
base=%(base)s
user_hook=%(user_hook)s

helm_work_post_guard() {
  [ "$HELM_WORK_INTEGRATOR" = "1" ] && return 0
  prev="$1"; new="$2"; flag="$3"
  [ "$flag" = "1" ] || return 0
  case "$prev" in 0000*) return 0 ;; esac
  git_dir=$(git rev-parse --path-format=absolute --git-dir 2>/dev/null) || {
    echo "[helm work] ALERT: cannot identify the invoking git directory" >&2
    return 0
  }
  common=$(git rev-parse --path-format=absolute --git-common-dir 2>/dev/null) || {
    echo "[helm work] ALERT: cannot identify the common git directory" >&2
    return 0
  }
  [ "$git_dir" = "$common" ] || return 0
  cur=$(git symbolic-ref -q HEAD 2>/dev/null || :)
  [ "$cur" = "refs/heads/$base" ] && return 0
  branch=${cur#refs/heads/}
  if [ "$prev" = "$new" ] && [ -n "$cur" ]; then
    git symbolic-ref HEAD "refs/heads/$base" || return 0
    echo "[helm work] shared checkout is the integrator's tree — healed back" >&2
    echo "[helm work] to $base; your branch '$branch' survives — work on it:" >&2
    echo "[helm work]   helm work claim $branch" >&2
    command -v helm >/dev/null 2>&1 && helm chat post \
      "@integrator guard healed 'checkout -b $branch' in the shared checkout" \
      >/dev/null 2>&1
  else
    echo "[helm work] ALERT: shared checkout left $base ($prev -> $new) —" >&2
    echo "[helm work] not healed (content switch); this is the integrator's tree." >&2
    command -v helm >/dev/null 2>&1 && helm chat post \
      "@integrator shared checkout switched off $base — inspect" \
      >/dev/null 2>&1
  fi
  return 0
}

user_rc=0
if [ -x "$user_hook" ]; then
  "$user_hook" "$@" || user_rc=$?
fi
helm_work_post_guard "$@"
[ "$user_rc" = "0" ] || exit "$user_rc"
exit $?
"""


# ---------------------------------------------------------------------------
# the two ledgers, each read from its native system — the join is computed
# ---------------------------------------------------------------------------

def _git(where, *args, timeout=30, env=None):
    """git under a path -> (rc, stdout, stderr). Spawn trouble reads as
    rc -1 — every caller fails toward its SAFE verdict (dirty, unmerged).

    `env` OVERLAYS the ambient environment for this one call (never replaces
    it — git needs HOME/PATH/GIT_CONFIG_*). Used to hand the ref-guard the
    single bit that distinguishes the sanctioned branch creator from a
    forbidden one, scoped to the call rather than leaked into the process."""
    try:
        r = subprocess.run(["git", "-C", where] + list(args),
                           capture_output=True, text=True, timeout=timeout,
                           env=dict(os.environ, **env) if env else None)
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except Exception as exc:
        return -1, "", str(exc)


def find_root(path=None):
    """The MAIN repo root from anywhere inside it — automap's resolver
    (worktrees fold via --git-common-dir), reused not rebuilt."""
    root = automap._git_root(path or os.getcwd())
    return automap._strip_worktree(root) if root else None


def resource(root, lane):
    """(project, lane) -> the claims-lane resource name."""
    return "worktree:%s:%s" % (os.path.basename(root.rstrip(os.sep)), lane)


def lane_path(root, lane):
    """Deterministic room path — the pure function IS the registry key.
    Rooms live in ONE sibling container (the human helm-wt convention,
    already automap-folded)."""
    return root.rstrip(os.sep) + "-wt" + os.sep + lane


def lane_branch(lane):
    return "lane/" + lane


def worktrees(root):
    """`git worktree list --porcelain`, parsed — THE registry, never
    mirrored into a file."""
    rc, out, _err = _git(root, "worktree", "list", "--porcelain")
    if rc != 0:
        return []
    rows, cur = [], None
    for ln in out.splitlines():
        if ln.startswith("worktree "):
            cur = {"path": ln[9:], "branch": None, "locked": False, "reason": ""}
            rows.append(cur)
        elif cur is None or not ln:
            continue
        elif ln.startswith("branch "):
            cur["branch"] = ln[7:]
        elif ln.split(" ", 1)[0] == "locked":
            cur["locked"], cur["reason"] = True, ln[7:]
    return rows


def _occupants(path):
    """Live PIDs whose cwd is `path` or beneath it. Linux /proc is the
    authoritative sibling-pane proof: deleting such a worktree strands that
    process at a `(deleted)` cwd and drops an interactive pane to a bare shell.
    If the census itself is unavailable, return an `unknown` sentinel so every
    removal path fails closed rather than guessing the room is empty."""
    root = os.path.realpath(path).rstrip(os.sep)
    try:
        names = os.listdir("/proc")
    except OSError:
        return ["unknown"]
    out = []
    for pid in names:
        if not pid.isdigit():
            continue
        try:
            cwd = os.path.realpath(os.path.join("/proc", pid, "cwd")).rstrip(os.sep)
        except OSError:
            continue
        if cwd == root or cwd.startswith(root + os.sep):
            out.append(pid)
    return sorted(out, key=lambda p: int(p) if p.isdigit() else -1)


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


def lane_rows(root):
    """The project's lane rooms: registry rows under `<root>-wt/`, +lane."""
    box = root.rstrip(os.sep) + "-wt" + os.sep
    rows = []
    for w in worktrees(root):
        if w["path"].startswith(box):
            w["lane"] = w["path"][len(box):].strip(os.sep)
            rows.append(w)
    return rows


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
                from . import chat
                chat.post("@integrator " + note)
            except Exception:
                pass
    return 0, lines


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

def list_rows(root):
    """The shadow board: registry ⋈ claims, computed — lane, holder,
    remaining, dirty, ahead/behind the base."""
    live, base = _live(), _base(root)
    rows = []
    for w in lane_rows(root):
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


def hook_path(root, name="post-checkout"):
    """The hook Git will actually execute, including a repo-local hooksPath."""
    rc, out, _err = _git(root, "rev-parse", "--path-format=absolute",
                         "--git-path", "hooks")
    if rc == 0 and out:
        return os.path.join(out, name)
    rc, out, _err = _git(root, "rev-parse", "--path-format=absolute",
                         "--git-common-dir")
    gitdir = out if rc == 0 and out else os.path.join(root, ".git")
    return os.path.join(gitdir, "hooks", name)


GUARD_HOOKS = (("reference-transaction", REF_GUARD_HOOK),
               ("post-checkout", GUARD_HOOK))


def _path_snapshot(path):
    """A restorable hook node. Symlinks and executable mode are semantic."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return ("absent", None, None)
    if stat.S_ISLNK(st.st_mode):
        return ("symlink", os.readlink(path), None)
    if stat.S_ISREG(st.st_mode):
        with open(path, "rb") as f:
            return ("file", f.read(), stat.S_IMODE(st.st_mode))
    return ("other", None, stat.S_IMODE(st.st_mode))


def _put_snapshot(path, snap):
    """Atomically put one file/symlink; chmod happens before visibility."""
    kind, value, mode = snap
    parent = os.path.dirname(path)
    os.makedirs(parent, exist_ok=True)
    if kind == "absent":
        if os.path.lexists(path):
            os.unlink(path)
        return
    fd, tmp = tempfile.mkstemp(prefix=".helm-work-hook-", dir=parent)
    try:
        if kind == "file":
            with os.fdopen(fd, "wb", closefd=False) as f:
                f.write(value)
                f.flush()
                os.fsync(f.fileno())
            os.fchmod(fd, mode)
        elif kind == "symlink":
            os.close(fd)
            fd = None
            os.unlink(tmp)
            os.symlink(value, tmp)
        else:
            raise OSError("unsupported hook node type at %s" % path)
        if fd is not None:
            os.close(fd)
            fd = None
        os.replace(tmp, path)
        dfd = os.open(parent, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    finally:
        if fd is not None:
            os.close(fd)
        if os.path.lexists(tmp):
            os.unlink(tmp)


def _owned_hook(snap):
    if snap[0] != "file":
        return False
    lines = snap[1].decode("utf-8", "replace").splitlines()[:8]
    return (any(line.startswith(MANAGED_HOOK_MARKER) for line in lines)
            or any(marker in lines for marker in LEGACY_HOOK_MARKERS))


def _hook_scope(root, targets):
    """Only mutate this repo's main checkout or common git directory."""
    rc, common, err = _git(root, "rev-parse", "--path-format=absolute",
                           "--git-common-dir")
    if rc != 0 or not common:
        return False, "cannot identify common git directory: " + err
    parents = {os.path.dirname(p) for p in targets}
    if len(parents) != 1:
        return False, "guard hooks resolve to different directories"
    parent = os.path.realpath(next(iter(parents)))
    anchors = (os.path.realpath(root), os.path.realpath(common))
    try:
        safe = any(os.path.commonpath((parent, a)) == a for a in anchors)
    except ValueError:
        safe = False
    if not safe:
        return False, ("effective hooksPath is outside this repo/common git dir: "
                       + parent)
    return True, parent


def _guard_plan(root):
    base = _base(root)
    plan = []
    for name, template in GUARD_HOOKS:
        target = hook_path(root, name)
        user = target + ".helm-user"
        subs = {"base": shlex.quote(base), "user_hook": shlex.quote(user)}
        script = template % subs
        plan.append({"name": name, "target": target, "user": user,
                     "script": script})
    return base, plan


def install_guard(root, apply=False):
    """Print or transactionally install the deterministic shared-tree rail.

    reference-transaction refuses branch creation/HEAD departure in the main
    checkout and mutations of branches occupied by another worktree. The
    post-checkout hook is a last-resort pointer-only heal. Existing hooks are
    preserved byte-for-byte as executable-mode-aware `.helm-user` companions
    and composed before Helm. Concurrent installers serialize on the hook dir;
    any write failure rolls every changed path back to its exact prior node."""
    base, plan = _guard_plan(root)
    targets = [p["target"] for p in plan]
    safe, scope = _hook_scope(root, targets)
    if not safe:
        return 1, ["helm work: REFUSED guard install — " + scope]
    if not apply:
        lines = []
        for p in plan:
            prior = _path_snapshot(p["target"])
            if prior[0] != "absent" and not _owned_hook(prior):
                lines.append("# preserves existing hook as %s" % p["user"])
            lines += ["# ---- %s ----" % p["target"],
                      p["script"].rstrip("\n"), ""]
        return 0, lines + [
            "helm work: DRY — would atomically install %d composed hooks "
            "(--apply installs; HELM_WORK_INTEGRATOR=1 is the override)"
            % len(plan)]

    rc, current, _err = _git(root, "symbolic-ref", "--short", "HEAD")
    if rc != 0 or current != base:
        return 1, ["helm work: REFUSED guard install — shared checkout HEAD is "
                   "%s, expected %s" % (current or "detached", base)]

    os.makedirs(scope, exist_ok=True)
    lockfd = os.open(scope, os.O_RDONLY)
    try:
        fcntl.flock(lockfd, fcntl.LOCK_EX)
        paths = targets + [p["user"] for p in plan]
        before = {path: _path_snapshot(path) for path in paths}
        desired = {}
        notes = []
        for p in plan:
            target, user = p["target"], p["user"]
            prior, preserved = before[target], before[user]
            if prior[0] == "other" or preserved[0] == "other":
                return 1, ["helm work: REFUSED guard install — unsupported hook "
                           "node at %s" % (target if prior[0] == "other" else user)]
            if not _owned_hook(prior) and prior[0] != "absent":
                if preserved[0] == "absent":
                    desired[user] = prior
                    notes.append("helm work: preserved existing %s as %s"
                                 % (target, user))
                elif prior != preserved:
                    return 1, ["helm work: REFUSED guard install — %s and its "
                               "preserved companion differ; nothing changed"
                               % target]
            desired[target] = ("file", p["script"].encode("utf-8"), 0o755)

        changed = []
        try:
            for path in [p["user"] for p in plan] + targets:
                want = desired.get(path)
                if want is None or before[path] == want:
                    continue
                changed.append(path)
                _put_snapshot(path, want)
        except Exception as exc:
            rollback = []
            for path in reversed(changed):
                try:
                    _put_snapshot(path, before[path])
                except Exception as restore_exc:
                    rollback.append("%s: %s" % (path, restore_exc))
            detail = "helm work: guard install failed and was rolled back — %s" % exc
            if rollback:
                detail += "; ROLLBACK FAILED: " + "; ".join(rollback)
            return 1, [detail]
        state = "updated" if changed else "already up to date"
        return 0, notes + ["helm work: guard rail %s in %s" % (state, scope),
                           "helm work: shared checkout branch creation/switch now "
                           "FAILS before mutation; occupied worktree branches are "
                           "protected (override: HELM_WORK_INTEGRATOR=1)"]
    finally:
        os.close(lockfd)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

USAGE = """usage: helm work <verb> [--repo PATH] [--seat S]
  claim <lane> [--ttl N] [--lease ID]   check in: lease + private room —
                                        prints path<TAB>branch<TAB>lease<TAB>ttl
  release [<lane>] --lease ID [--park]  check out: dirty refuses (--park
                                        WIP-commits), key surrendered, room removed
  gc [--apply]                          housekeeping: keep/remove/rescue table
                                        (dry-run default; rescue never discards)
  list                                  the room board: worktree registry x claims
  install-guard [--apply]               composed deterministic git guards (dry/apply)"""


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


def cmd_work(args):
    """work claim|release|gc|list|install-guard — worktree lifecycle on the
    claims lane: private room per lane, the shared checkout stays the
    integrator's, abandoned dirty work is rescued, never discarded."""
    args = list(args or [])
    if not args:
        print(USAGE, file=sys.stderr)
        return 2
    verb, rest = args[0], args[1:]
    repo = seats._flag(rest, "--repo")
    root = find_root(repo) if repo else find_root()
    if not root:
        print("helm work: not inside a git repo (--repo PATH names one)",
              file=sys.stderr)
        return 2
    seat = seats._flag(rest, "--seat") or seats.derive_seat(None)
    session = seats._env_session()
    if verb == "claim":
        pos = _positional(rest)
        if not pos or not LANE_RE.match(pos[0]):
            print("usage: helm work claim <lane>  (lane = [A-Za-z0-9._-]{1,64};"
                  " keep the printed lease id — it is the room key)",
                  file=sys.stderr)
            return 2
        ttl = seats._flag(rest, "--ttl")
        rc, line = claim(root, pos[0], seat,
                         ttl=int(ttl) if ttl else DEFAULT_TTL,
                         lease=seats._flag(rest, "--lease"), session=session)
        print(line, file=sys.stdout if rc == 0 else sys.stderr)
        return rc
    if verb == "release":
        pos = _positional(rest)
        lane = pos[0] if pos else _infer_lane(root)
        if not lane:
            print("usage: helm work release <lane> --lease ID [--park] "
                  "(lane infers only from inside its room)", file=sys.stderr)
            return 2
        rc, lines = release_lane(root, lane, seat,
                                 lease=seats._flag(rest, "--lease"),
                                 session=session, park="--park" in rest)
        for ln in lines:
            print(ln, file=sys.stdout if rc == 0 else sys.stderr)
        return rc
    if verb == "gc":
        rows = gc_scan(root)
        if not rows:
            print("helm work gc: no lane rooms under %s-wt/" % root)
            return 0
        enforcing = "--apply" in rest
        print("helm work gc — %d room%s under %s-wt/ (%s)" % (
            len(rows), "s"[:len(rows) != 1], root,
            "APPLYING" if enforcing else "dry-run; --apply enforces — dirty "
            "rooms are rescue-committed to their branch, never discarded"))
        w = max(len(r["lane"]) for r in rows)
        for r in rows:
            print("  %-7s %-*s  %s" % (r["verdict"].upper(), w, r["lane"], r["why"]))
            if enforcing:
                for ln in gc_enact(root, r):
                    print("        " + ln)
        return 0
    if verb == "list":
        rows = list_rows(root)
        if not rows:
            print("helm work: no lane rooms — `helm work claim <lane>` opens "
                  "one at %s-wt/<lane>" % root)
            return 0
        w = max(len(r["lane"]) for r in rows)
        for r in rows:
            hold = "%s %ds" % (r["holder"], r["remaining"]) if r["holder"] else "-"
            print("  %-*s  %-24s  %-5s  +%s/-%s%s  %s" % (
                w, r["lane"], hold, "dirty" if r["dirty"] else "clean",
                r["ahead"], r["behind"], "  locked" if r["locked"] else "",
                r["path"]))
        return 0
    if verb == "install-guard":
        rc, lines = install_guard(root, apply="--apply" in rest)
        for ln in lines:
            print(ln, file=sys.stdout if rc == 0 else sys.stderr)
        return rc
    print(USAGE, file=sys.stderr)
    return 2
