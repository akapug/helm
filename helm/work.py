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
import json
import os
import re
import shlex
import stat
import subprocess
import sys
import tempfile
import time

from . import automap, pk, seats

DEFAULT_TTL = 4 * 3600          # a build lane, not a chat lock
RECENT_WRITE_SECONDS = 15 * 60  # display bucket only; never a reap authorization
LANE_RE = re.compile(r"[A-Za-z0-9._-]{1,64}$")
_AGENT_ROOM_RE = re.compile(r"agent-[A-Za-z0-9_-]{8,64}$")
_WORKFLOW_ROOM_RE = re.compile(r"(wf_[A-Za-z0-9_-]{3,64})-([1-9][0-9]*)$")
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

def _git_bytes(where, *args, timeout=30, env=None):
    """Byte-preserving git call. Paths are filesystem bytes, not UTF-8 text;
    porcelain parsers must never inherit quoting or decode ambiguity.

    `env` OVERLAYS the ambient environment for this one call (never replaces
    it — git needs HOME/PATH/GIT_CONFIG_*). It is how the ref-guard is handed
    the single bit distinguishing the sanctioned branch creator from a
    forbidden one, scoped to the call rather than leaked into the process."""
    try:
        r = subprocess.run(["git", "-C", where] + list(args),
                           capture_output=True, timeout=timeout,
                           env=dict(os.environ, **env) if env else None)
        return r.returncode, r.stdout, r.stderr
    except Exception as exc:
        return -1, b"", os.fsencode(str(exc))


def _git(where, *args, timeout=30, env=None):
    """Text git call for outputs that are not path records. Spawn trouble reads
    as rc -1 — every caller fails toward its SAFE verdict. `env` forwards to
    _git_bytes so the ref-guard's sanctioned-creator bit reaches git through
    the same seam every other call uses."""
    rc, out, err = _git_bytes(where, *args, timeout=timeout, env=env)
    return rc, os.fsdecode(out).strip(), os.fsdecode(err).strip()


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


def _worktree_records(root):
    """(rows, error) from git's NUL porcelain. `-z` disables C quoting and
    preserves spaces, newlines, backslashes, and non-UTF8 path bytes."""
    rc, out, err = _git_bytes(root, "worktree", "list", "--porcelain", "-z",
                              timeout=10)
    if rc != 0:
        return [], os.fsdecode(err) or "git worktree list failed"
    if out and not out.endswith(b"\0"):
        return [], "truncated git worktree porcelain"
    rows, cur = [], None
    try:
        for field in out.split(b"\0"):
            if not field:
                cur = None
            elif field.startswith(b"worktree "):
                cur = {"path": os.fsdecode(field[9:]), "branch": None,
                       "locked": False, "reason": ""}
                rows.append(cur)
            elif cur is None:
                raise ValueError("worktree field before record")
            elif field.startswith(b"branch "):
                cur["branch"] = os.fsdecode(field[7:])
            elif field == b"locked" or field.startswith(b"locked "):
                cur["locked"] = True
                cur["reason"] = os.fsdecode(field[7:]) if len(field) > 7 else ""
    except (ValueError, UnicodeError) as exc:
        return [], "invalid git worktree porcelain: %s" % exc
    return rows, None


def worktrees(root):
    """The git-native registry. Callers that need to surface registry failure
    use `_worktree_records`; legacy safety callers retain [] on failure."""
    return _worktree_records(root)[0]


def _occupants_many(paths):
    """One /proc pass for many rooms -> ({path: [pids]}, census_complete).
    Cwd links are the positive proof; an unreadable census is explicit UNKNOWN."""
    roots = {p: os.path.realpath(p).rstrip(os.sep) for p in paths}
    out = {p: [] for p in paths}
    try:
        names = os.listdir("/proc")
    except OSError:
        return {p: ["unknown"] for p in paths}, False
    for pid in names:
        if not pid.isdigit():
            continue
        try:
            cwd = os.path.realpath(os.path.join("/proc", pid, "cwd")).rstrip(os.sep)
        except OSError:
            continue
        for path, root in roots.items():
            if cwd == root or cwd.startswith(root + os.sep):
                out[path].append(pid)
    for p in out:
        out[p].sort(key=lambda n: int(n) if n.isdigit() else -1)
    return out, True


def _occupants(path):
    """Live PIDs whose cwd is `path` or beneath it; UNKNOWN fails closed."""
    return _occupants_many([path])[0][path]


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


def lane_rows(root, registered=None):
    """The project's normal lane rooms: direct children of `<root>-wt/`."""
    box = os.path.abspath(root.rstrip(os.sep) + "-wt")
    rows = []
    for w in registered if registered is not None else worktrees(root):
        path = os.path.abspath(w["path"])
        if os.path.dirname(path) == box:
            row = dict(w)
            row["lane"] = os.path.basename(path)
            rows.append(row)
    return rows


def _status_entries(raw):
    """Parse `git status --porcelain=v2 -z` into (path_bytes, submodule).
    Rename/copy source paths are consumed but deliberately not timed: the
    destination is the uncommitted path. Any v1/human record is rejected."""
    if raw and not raw.endswith(b"\0"):
        raise ValueError("truncated porcelain v2 record")
    fields, entries, i = raw.split(b"\0"), [], 0
    while i < len(fields) - 1:
        field, i = fields[i], i + 1
        if not field:
            continue
        kind = field[:1]
        if kind == b"1":
            parts = field.split(b" ", 8)
            if len(parts) != 9:
                raise ValueError("malformed ordinary record")
            entries.append((parts[8], parts[2]))
        elif kind == b"2":
            parts = field.split(b" ", 9)
            if len(parts) != 10 or not parts[9] or i >= len(fields) - 1 \
                    or not fields[i]:
                raise ValueError("malformed rename/copy record")
            entries.append((parts[9], parts[2]))
            i += 1  # exact original path, including newlines/NUL framing
        elif kind == b"u":
            parts = field.split(b" ", 10)
            if len(parts) != 11:
                raise ValueError("malformed unmerged record")
            entries.append((parts[10], parts[2]))
        elif field.startswith(b"? "):
            entries.append((field[2:], b"N..."))
        elif field.startswith(b"! ") or field.startswith(b"# "):
            continue
        else:
            raise ValueError("non-v2 or unknown status record")
    return entries


def _room_status(path, now=None):
    """One status subprocess, then metadata-only lstat of changed paths.
    Returns dirty/write age plus an uncertainty reason. No file contents and no
    symlink targets are read. Missing/deleted paths and dirty submodules cannot
    provide a trustworthy write clock, so they stay UNKNOWN rather than clean."""
    rc, out, err = _git_bytes(
        path, "status", "--porcelain=v2", "-z", "--untracked-files=all",
        "--ignore-submodules=none", timeout=5)
    if rc != 0:
        return {"dirty": True, "wrote_ago": None, "clock_skew": False,
                "unknown": "git status failed: %s" %
                (os.fsdecode(err).strip() or "unknown error")}
    if not out:
        return {"dirty": False, "wrote_ago": None, "clock_skew": False,
                "unknown": None}
    try:
        entries = _status_entries(out)
    except ValueError as exc:
        return {"dirty": True, "wrote_ago": None, "clock_skew": False,
                "unknown": "unsafe status record: %s" % exc}
    newest, incomplete = None, []
    for rel_bytes, sub in entries:
        rel = os.fsdecode(rel_bytes)
        if os.path.isabs(rel) or os.pardir in rel.split(os.sep):
            incomplete.append("path escaped checkout")
            continue
        try:
            st = os.lstat(os.path.join(path, rel))
        except OSError:
            incomplete.append("deleted or raced path")
            continue
        newest = st.st_mtime if newest is None else max(newest, st.st_mtime)
        if sub.startswith(b"S") and sub != b"N...":
            incomplete.append("dirty submodule has no bounded file clock")
    now = time.time() if now is None else now
    skew = newest is not None and newest > now
    age = None if newest is None else max(0, int(now - newest))
    return {"dirty": True, "wrote_ago": age, "clock_skew": skew,
            "unknown": "; ".join(sorted(set(incomplete))) or None}


def _wrote_ago(path):
    """Compatibility projection of `_room_status`; None includes clean and
    timestamp-unknown dirty rooms, which callers must distinguish via dirty."""
    return _room_status(path)["wrote_ago"]


def _read_small_nofollow(path, limit=4096):
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        data = os.read(fd, limit + 1)
    finally:
        os.close(fd)
    if len(data) > limit:
        raise OSError("metadata file too large")
    return data


def _load_json_nofollow(path, limit=2 * 1024 * 1024):
    return json.loads(_read_small_nofollow(path, limit=limit))


def _checkout_issue(path, common_dir):
    """Validate a registered worktree before `git -C`: no path/.git symlink,
    no stale replacement by another repo, and admin metadata points back here."""
    try:
        st = os.lstat(path)
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
            return "checkout path is missing or a symlink"
        dotgit = os.path.join(path, ".git")
        st = os.lstat(dotgit)
        if not stat.S_ISREG(st.st_mode):
            return "checkout .git is not a regular worktree link"
        line = os.fsdecode(_read_small_nofollow(dotgit)).strip()
        if not line.startswith("gitdir: "):
            return "checkout .git link is malformed"
        admin = os.path.realpath(os.path.join(path, line[8:]))
        if os.path.dirname(admin) != os.path.join(common_dir, "worktrees"):
            return "checkout belongs to another repository"
        backlink = os.fsdecode(_read_small_nofollow(
            os.path.join(admin, "gitdir"))).strip()
        if os.path.abspath(backlink) != os.path.abspath(dotgit):
            return "stale or aliased worktree metadata"
    except OSError as exc:
        return "unreadable worktree metadata: %s" % exc
    return None


def _claude_homes():
    try:
        from . import skillsync
        return [p for _label, p in skillsync.config_dirs()]
    except Exception:
        return None


def _live_claude_sessions():
    try:
        from . import session
        return set(session.live_sids())
    except Exception:
        return None


def _last_agent_terminal(path):
    """True only for a direct-agent transcript whose latest assistant record
    is a final text end-turn; false means incomplete/active/indeterminate."""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 512 * 1024))
            lines = f.read().splitlines()
    except OSError:
        return False
    for raw in reversed(lines):
        try:
            row = json.loads(raw)
        except (ValueError, UnicodeError):
            continue
        msg = row.get("message") if row.get("type") == "assistant" else None
        if not isinstance(msg, dict):
            continue
        content = msg.get("content") or []
        has_text = any(isinstance(c, dict) and c.get("type") == "text"
                       for c in content)
        return msg.get("stop_reason") == "end_turn" and has_text
    return False


def _harness_state(root, path, homes, live_sessions):
    """Existing Claude Agent/Workflow metadata + exact parent-session liveness.
    This is the clean-but-live signal: no argv substring guessing and no new
    claim registry. Missing/corrupt evidence is UNKNOWN, never absent."""
    room = os.path.basename(path)
    agent = _AGENT_ROOM_RE.fullmatch(room)
    workflow = _WORKFLOW_ROOM_RE.fullmatch(room)
    if homes is None or live_sessions is None:
        return "unknown", "harness/process census unavailable"
    slug = root.replace(os.sep, "-").replace(".", "-")
    found, uncertain = False, False
    for home in homes:
        project = os.path.join(home, "projects", slug)
        try:
            sessions = [e for e in os.scandir(project) if e.is_dir(follow_symlinks=False)]
        except OSError:
            continue
        for entry in sessions:
            sid = entry.name
            if agent:
                meta = os.path.join(entry.path, "subagents", room + ".meta.json")
                if not os.path.isfile(meta):
                    continue
                found = True
                try:
                    recorded = _load_json_nofollow(meta).get("worktreePath")
                except (OSError, ValueError, AttributeError, UnicodeError):
                    uncertain = True
                    continue
                if not recorded or os.path.realpath(recorded) != os.path.realpath(path):
                    uncertain = True
                    continue
                transcript = os.path.join(entry.path, "subagents", room + ".jsonl")
                if _last_agent_terminal(transcript):
                    continue
                if sid in live_sessions:
                    return "live", "direct Agent active in session %s" % sid[:8]
                uncertain = True
            elif workflow:
                meta = os.path.join(entry.path, "workflows", workflow.group(1) + ".json")
                if not os.path.isfile(meta):
                    continue
                found = True
                try:
                    status_value = str(
                        _load_json_nofollow(meta).get("status") or "").lower()
                except (OSError, ValueError, AttributeError, UnicodeError):
                    uncertain = True
                    continue
                if status_value in {"completed", "failed", "cancelled", "canceled", "stopped"}:
                    continue
                if status_value in {"running", "pending", "queued", "in_progress"} \
                        and sid in live_sessions:
                    return "live", "Workflow active in session %s" % sid[:8]
                uncertain = True
    if found and not uncertain:
        return "inactive", "harness metadata terminal"
    return "unknown", ("harness metadata incomplete" if found
                       else "no matching harness metadata")


def unguarded_inventory(root, registered=None, registry_error=None):
    """Report only direct Claude Agent/Workflow isolation rooms. Normal lanes,
    the main checkout, arbitrary sibling worktrees, aliases, and path escapes
    are not silently reclassified as adoptable lanes."""
    if registered is None:
        registered, registry_error = _worktree_records(root)
    errors = [registry_error] if registry_error else []
    root_abs = os.path.abspath(root)
    expected = os.path.join(root_abs, ".claude", "worktrees")
    root_real, expected_real = os.path.realpath(root_abs), os.path.realpath(expected)
    try:
        container_issue = None if os.path.commonpath(
            [root_real, expected_real]) == root_real else \
            "Agent/Workflow container escapes repository through a symlink"
    except ValueError:
        container_issue = "Agent/Workflow container is on another filesystem root"
    rc, common, err = _git(root, "rev-parse", "--path-format=absolute",
                           "--git-common-dir", timeout=5)
    common = os.path.realpath(common) if rc == 0 else None
    if common is None:
        errors.append("cannot validate worktree repository: %s" % err)
    candidates, seen = [], set()
    for w in registered:
        path = os.path.abspath(w["path"])
        room = os.path.basename(path)
        if os.path.dirname(path) != expected:
            continue
        if not (_AGENT_ROOM_RE.fullmatch(room) or _WORKFLOW_ROOM_RE.fullmatch(room)):
            continue
        real = os.path.realpath(path)
        if real in seen:
            errors.append("duplicate Agent/Workflow worktree alias: %s" % ascii(path))
            continue
        seen.add(real)
        issue = container_issue or \
            ("cannot validate repository identity" if common is None
             else _checkout_issue(path, common))
        candidates.append((w, path, room, issue))
    safe_paths = [p for _w, p, _room, issue in candidates if issue is None]
    occupant_map, census_ok = _occupants_many(safe_paths)
    homes, live_sessions = _claude_homes(), _live_claude_sessions()
    rows = []
    for w, path, room, issue in candidates:
        status_row = ({"dirty": True, "wrote_ago": None, "clock_skew": False,
                       "unknown": issue} if issue else _room_status(path))
        if issue:
            harness, harness_note = "unknown", "unsafe checkout not inspected"
            occupants = []
        else:
            harness, harness_note = _harness_state(
                os.path.abspath(root), path, homes, live_sessions)
            occupants = occupant_map[path]
        hard_unknown = [x for x in (issue, status_row["unknown"],
                                    None if census_ok else "cwd census unavailable") if x]
        unknown = hard_unknown + ([harness_note] if harness == "unknown" else [])
        branch_ref = w.get("branch") or ""
        branch = branch_ref[len("refs/heads/"):] \
            if branch_ref.startswith("refs/heads/") else branch_ref
        rows.append({"id": room, "path": path, "branch": branch,
                     "locked": w["locked"], "occupants": occupants,
                     "dirty": status_row["dirty"],
                     "wrote_ago": status_row["wrote_ago"],
                     "clock_skew": status_row["clock_skew"],
                     "harness": harness, "harness_note": harness_note,
                     "hard_unknown": "; ".join(dict.fromkeys(hard_unknown)) or None,
                     "unknown": "; ".join(dict.fromkeys(unknown)) or None})
    return rows, errors


def unguarded_rows(root):
    """Compatibility list projection; CLI uses `unguarded_inventory` so a
    registry/discovery failure is visible rather than looking empty."""
    return unguarded_inventory(root)[0]


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
        registered, registry_error = _worktree_records(root)
        rows = list_rows(root, registered=registered)
        loose, discovery_errors = unguarded_inventory(
            root, registered=registered, registry_error=registry_error)
        if not rows and not loose and not discovery_errors:
            print("helm work: no lane rooms — `helm work claim <lane>` opens "
                  "one at %s-wt/<lane>" % root)
            return 0
        if rows:
            print("GUARDED lane rooms — claims and leases apply:")
            w = max(len(r["lane"]) for r in rows)
            for r in rows:
                hold = ("%s %ds" % (r["holder"], r["remaining"])
                        if r["holder"] else "-")
                print("  GUARDED %-*s  %-24s  %-5s  +%s/-%s%s  path=%s" % (
                    w, r["lane"], hold, "dirty" if r["dirty"] else "clean",
                    r["ahead"], r["behind"], "  locked" if r["locked"] else "",
                    ascii(r["path"])))
        for error in discovery_errors:
            print("WARNING: UNGUARDED discovery UNKNOWN — %s; retain and inspect "
                  "manually." % ascii(error))
        if loose:
            print("WARNING: %d UNGUARDED Agent/Workflow room%s — visibility and "
                  "advisory evidence only. No claim/lease protects these rooms, "
                  "and this output never authorizes cleanup:" %
                  (len(loose), "s"[:len(loose) != 1]))
            for r in loose:
                if r["occupants"]:
                    state = "OCCUPIED"
                    evidence = "cwd pid(s) " + ",".join(r["occupants"])
                elif r["harness"] == "live":
                    state, evidence = "LIVE-HARNESS", r["harness_note"]
                elif r["hard_unknown"]:
                    state, evidence = "UNKNOWN", r["unknown"]
                elif r["wrote_ago"] is not None \
                        and r["wrote_ago"] <= RECENT_WRITE_SECONDS:
                    state = "RECENT-WRITE"
                    evidence = "%ds ago%s; advisory, not ownership proof" % (
                        r["wrote_ago"], " (future mtime/clock skew)"
                        if r["clock_skew"] else "")
                elif r["dirty"]:
                    state = "DIRTY"
                    evidence = "uncommitted work; last timestamp %s" % (
                        "%dm ago" % (r["wrote_ago"] // 60)
                        if r["wrote_ago"] is not None else "unknown")
                elif r["unknown"]:
                    state, evidence = "UNKNOWN", r["unknown"]
                else:
                    state = "CLEAN"
                    evidence = ("no cwd or live harness evidence; this is not "
                                "proof that no writer will resume")
                print("  UNGUARDED %-16s id=%-30s tree=%-8s lock=%-8s "
                      "branch=%s path=%s evidence=%s" % (
                          state, r["id"], "DIRTY" if r["dirty"] else "CLEAN",
                          "LOCKED" if r["locked"] else "UNLOCKED",
                          ascii(r["branch"] or "(detached)"), ascii(r["path"]),
                          ascii(evidence)))
        return 0
    if verb == "install-guard":
        rc, lines = install_guard(root, apply="--apply" in rest)
        for ln in lines:
            print(ln, file=sys.stdout if rc == 0 else sys.stderr)
        return rc
    print(USAGE, file=sys.stderr)
    return 2
