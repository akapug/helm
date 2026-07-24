"""helm work — the guard cluster: the composed deterministic shared-tree
rail (hook templates, snapshots, scope check, transactional install).
Moved verbatim from the pre-split helm/work.py.
"""
import fcntl
import os
import shlex
import stat
import tempfile

from ._lanes import _git
from ._gc import _base


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
