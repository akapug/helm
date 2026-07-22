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
  * the rail     — one ~25-line post-checkout hook for the ONE resource that
                   needs determinism (the shared checkout — the 07-20
                   `checkout -b` failure). install-guard prints it by
                   default, --apply installs; the heal is pointer-only and
                   provably a working-tree no-op (flag=1 ∧ prev==new).

Tripwires (design §5): per-file claims, approval steps, a second registry,
queues/priorities on lanes, a daemon — any of these is the MC slide; stop.
"""
import os
import re
import subprocess
import sys
import time

from . import automap, pk, seats

DEFAULT_TTL = 4 * 3600          # a build lane, not a chat lock
LANE_RE = re.compile(r"[A-Za-z0-9._-]{1,64}$")
_VALUE_FLAGS = ("--repo", "--seat", "--lease", "--ttl")

GUARD_HOOK = """#!/bin/sh
# helm work guard — the shared checkout is the integrator's tree (installed
# by `helm work install-guard`; design: in-cave git coordination §3d).
# post-checkout <prev> <new> <flag>. Escape hatch: HELM_WORK_INTEGRATOR=1.
[ "$HELM_WORK_INTEGRATOR" = "1" ] && exit 0
prev="$1"; new="$2"; flag="$3"
[ "$flag" = "1" ] || exit 0                       # file checkout: not ours
case "$prev" in 0000*) exit 0 ;; esac             # fresh checkout/worktree add
main_wt=$(git worktree list --porcelain | sed -n 's/^worktree //p' | head -n 1)
top=$(git rev-parse --show-toplevel 2>/dev/null)
[ "$top" = "$main_wt" ] || exit 0                 # lane rooms are unguarded
cur=$(git symbolic-ref -q HEAD)
[ "$cur" = "refs/heads/%(base)s" ] && exit 0      # still on the trunk: fine
branch="${cur#refs/heads/}"
if [ "$prev" = "$new" ]; then
  # `checkout -b` at the tip: pointer-only heal, working tree untouched
  git symbolic-ref HEAD "refs/heads/%(base)s"
  echo "[helm work] shared checkout is the integrator's tree — healed back" >&2
  echo "[helm work] to %(base)s; your branch '$branch' survives — work on it:" >&2
  echo "[helm work]   helm work claim $branch" >&2
  command -v helm >/dev/null 2>&1 && helm chat post \\
    "@integrator guard healed 'checkout -b $branch' in the shared checkout" \\
    >/dev/null 2>&1
else
  echo "[helm work] ALERT: shared checkout left %(base)s ($prev -> $new) —" >&2
  echo "[helm work] not healed (content switch); this is the integrator's tree." >&2
  command -v helm >/dev/null 2>&1 && helm chat post \\
    "@integrator shared checkout switched off %(base)s — inspect" \\
    >/dev/null 2>&1
fi
exit 0
"""


# ---------------------------------------------------------------------------
# the two ledgers, each read from its native system — the join is computed
# ---------------------------------------------------------------------------

def _git(where, *args, timeout=30):
    """git under a path -> (rc, stdout, stderr). Spawn trouble reads as
    rc -1 — every caller fails toward its SAFE verdict (dirty, unmerged)."""
    try:
        r = subprocess.run(["git", "-C", where] + list(args),
                           capture_output=True, text=True, timeout=timeout)
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


def unguarded_rows(root):
    """Registered worktrees that are NOT lane rooms — the shared checkout's
    own siblings, and above all the agent-spawned rooms under
    `.claude/worktrees/wf_*` that the Agent/Workflow `isolation: worktree`
    option mints directly through git.

    These are real, writable checkouts of this repo that no lease covers and
    no `helm work list` row mentioned, so occupancy existed only as chat prose
    — which is how three agents came to share one review room on 2026-07-22
    and a concurrent rebase destroyed a reviewer's uncommitted work. The
    claims guard was never missing (`claim` refuses a second holder outright);
    these rooms simply bypass it. Reporting them with their LIVE occupants
    turns 'nobody told me it was taken' into a question anyone can answer
    before they touch it."""
    box = root.rstrip(os.sep) + "-wt" + os.sep
    me = os.path.realpath(root).rstrip(os.sep)
    rows = []
    for w in worktrees(root):
        if w["path"].startswith(box):
            continue
        if os.path.realpath(w["path"]).rstrip(os.sep) == me:
            continue
        rows.append({"path": w["path"],
                     "branch": (w["branch"] or "")[len("refs/heads/"):],
                     "locked": w["locked"],
                     "occupants": _occupants(w["path"]),
                     "wrote_ago": _wrote_ago(w["path"]),
                     "dirty": _dirty(w["path"])})
    return rows


def _wrote_ago(path):
    """Seconds since the newest write among this room's UNCOMMITTED files, or
    None when nothing is in flight.

    The cwd census (`_occupants`) is necessary but NOT sufficient: a subagent
    reviewer edits a room through `git -C`/absolute paths without ever cwd-ing
    into it, so the room reads 'no live occupant' while a file in it was
    written seconds ago. That exact gap is what a waking seat saw before it
    rebased a live reviewer's tree out from under it. Scoped to changed files
    (`diff --name-only` + untracked), so a clean room costs one git call and a
    busy one stats only what is actually in flight."""
    rc, out, _err = _git(path, "status", "--porcelain")
    if rc != 0 or not out:
        return None
    newest = None
    for ln in out.splitlines():
        rel = ln[3:].strip().strip('"')
        rel = rel.split(" -> ")[-1]  # renames report old -> new
        try:
            m = os.stat(os.path.join(path, rel)).st_mtime
        except OSError:
            continue
        newest = m if newest is None else max(newest, m)
    return None if newest is None else max(0, int(time.time() - newest))


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
        rc, _out, err = _git(root, *args)
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


def hook_path(root):
    rc, out, _err = _git(root, "rev-parse", "--path-format=absolute",
                         "--git-common-dir")
    gitdir = out if rc == 0 and out else os.path.join(root, ".git")
    return os.path.join(gitdir, "hooks", "post-checkout")


def install_guard(root, apply=False):
    """(rc, [lines]). The shared-checkout rail: PRINT the post-checkout heal
    hook by default; --apply installs it into the main checkout's hooks dir
    (the integrator's coordinated step). Refuses to clobber a foreign hook."""
    script = GUARD_HOOK % {"base": _base(root)}
    target = hook_path(root)
    if not apply:
        return 0, [script.rstrip("\n"), "",
                   "helm work: DRY — would install the guard at %s (--apply "
                   "installs; the integrator's own env sets "
                   "HELM_WORK_INTEGRATOR=1)" % target]
    try:
        with open(target) as f:
            prior = f.read()
    except OSError:
        prior = None
    if prior is not None and "helm work guard" not in prior:
        return 1, ["helm work: %s exists and is not ours — not overwriting "
                   "(merge by hand)" % target]
    os.makedirs(os.path.dirname(target), exist_ok=True)
    pk.atomic_write(target, script)
    os.chmod(target, 0o755)
    return 0, ["helm work: guard installed at %s (escape hatch: "
               "HELM_WORK_INTEGRATOR=1 in the integrator's env)" % target]


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
  install-guard [--apply]               the shared-checkout heal hook (print/install)"""


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
        loose = unguarded_rows(root)
        if not rows and not loose:
            print("helm work: no lane rooms — `helm work claim <lane>` opens "
                  "one at %s-wt/<lane>" % root)
            return 0
        if rows:
            w = max(len(r["lane"]) for r in rows)
            for r in rows:
                hold = ("%s %ds" % (r["holder"], r["remaining"])
                        if r["holder"] else "-")
                print("  %-*s  %-24s  %-5s  +%s/-%s%s  %s" % (
                    w, r["lane"], hold, "dirty" if r["dirty"] else "clean",
                    r["ahead"], r["behind"], "  locked" if r["locked"] else "",
                    r["path"]))
        if loose:
            # Occupancy that no lease covers. Printed as a WARNING, never as a
            # lane row: these rooms are outside the claims system, so `claim`
            # cannot refuse a second writer in them and `gc` will not rescue
            # them. Anyone about to rebase one can now see who is standing in
            # it first.
            print("⚠ %d UNGUARDED room%s — outside the claims lane, so NO "
                  "lease can refuse a second writer here. Verify occupancy "
                  "before touching one; prefer `helm work claim <lane>`:"
                  % (len(loose), "s"[:len(loose) != 1]))
            for r in loose:
                if r["occupants"]:
                    who = "OCCUPIED by pid " + ",".join(r["occupants"])
                elif r["wrote_ago"] is not None and r["wrote_ago"] < 900:
                    # No cwd, but bytes landed just now — a subagent writer.
                    who = "WRITTEN %ds AGO — assume live" % r["wrote_ago"]
                elif r["wrote_ago"] is not None:
                    who = "work in flight, last write %dm ago" % (
                        r["wrote_ago"] // 60)
                else:
                    who = "no live occupant"
                print("    %-6s %-11s %-34s %s  %s" % (
                    "dirty" if r["dirty"] else "clean",
                    "locked" if r["locked"] else "unlocked",
                    who, r["branch"] or "(detached)", r["path"]))
        return 0
    if verb == "install-guard":
        rc, lines = install_guard(root, apply="--apply" in rest)
        for ln in lines:
            print(ln, file=sys.stdout if rc == 0 else sys.stderr)
        return rc
    print(USAGE, file=sys.stderr)
    return 2
