"""helm work — the lanes cluster: the two ledgers read from their native
systems, room paths/branches, occupancy, status, and the unguarded
Agent/Workflow inventory. Moved verbatim from the pre-split helm/work.py.
"""
import json
import os
import stat
import time

from .. import automap, vcs
from ._common import (
    _AGENT_ROOM_RE, _WORKFLOW_ROOM_RE, PEEK_DIRNAME, _claude_homes,
    _load_json_nofollow, _read_small_nofollow,
)


# ---------------------------------------------------------------------------
# the two ledgers, each read from its native system — the join is computed
# ---------------------------------------------------------------------------

def _git_bytes(where, *args, timeout=30, env=None):
    """Byte-preserving git call. Paths are filesystem bytes, not UTF-8 text;
    porcelain parsers must never inherit quoting or decode ambiguity.

    `env` OVERLAYS the ambient environment for this one call (never replaces
    it — git needs HOME/PATH/GIT_CONFIG_*). It is how the ref-guard is handed
    the single bit distinguishing the sanctioned branch creator from a
    forbidden one, scoped to the call rather than leaked into the process.

    The spawn itself lives behind the VCS seam (helm/vcs.py `run`); this stays
    the module's ONE named git boundary, so the porcelain readers below have a
    single point to be reasoned about (and injected at)."""
    return vcs.backend(where).run(where, *args, timeout=timeout, env=env)


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


def project_token(root):
    """The PROJECT component of a claims-lane resource — ONE owner for it.

    `dispatches._repo_project` reads this to decide whether a live claim belongs
    to a dispatched row's repository, and nothing else may derive it: two
    derivations of one lease key is a silent mismatch, and the day they disagree
    `progress_state` reads IDLE for a seat that is holding the lane while the row
    reads OVERDUE and the work is happening. That function's docstring carries
    the known non-injectivity and why the registry is NOT the cure for it."""
    return os.path.basename(root.rstrip(os.sep))


def resource(root, lane):
    """(project, lane) -> the claims-lane resource name."""
    return "worktree:%s:%s" % (project_token(root), lane)


def lane_path(root, lane):
    """Deterministic room path — the pure function IS the registry key.
    Rooms live in ONE sibling container (the human helm-wt convention,
    already automap-folded)."""
    return root.rstrip(os.sep) + "-wt" + os.sep + lane


def lane_branch(lane):
    return "lane/" + lane


def _worktree_records(root):
    """(rows, error) from git's NUL porcelain. `-z` disables C quoting and
    preserves spaces, newlines, backslashes, and non-UTF8 path bytes.

    THE BACKEND is the authority: it owns both the argv it issues and the
    format it parses (a jj backend answers from `jj workspace list`). Only the
    raw SPAWN is handed in — `read=_git_bytes` keeps this module's single git
    boundary in the path, so every spawn _lanes makes is still findable (and
    injectable) in one place."""
    return vcs.backend(root).worktrees(root, read=_git_bytes)


def worktrees(root):
    """The git-native registry. Callers that need to surface registry failure
    use `_worktree_records`; legacy safety callers retain [] on failure."""
    return _worktree_records(root)[0]


def _occupants_many(paths):
    """One /proc pass for many rooms -> ({path: [pids]}, census_complete).
    Cwd links are the positive proof; an unreadable census is explicit UNKNOWN.

    COMPLETE MEANT ONLY THAT /proc COULD BE LISTED, which is the smaller half
    of the question. Every per-pid cwd read could fail — a hidepid mount, a
    container, a namespace that shows the directory and refuses the links —
    and this still answered `True` beside an empty map: the census reported
    NOBODY IS ANYWHERE with the same two values it uses for a genuinely empty
    board. Readers act on that difference (the seam rung deletes every
    occupancy-live peer; `lane_rows` drops its unavailability note), so the
    two states need two answers.

    A SINGLE SUCCESSFUL READ IS ENOUGH TO CALL IT COMPLETE, and that threshold
    is deliberately the loosest one that discriminates: individual failures are
    ordinary (a process exits between the listing and the readlink), so only
    the TOTAL failure is evidence about the instrument. The scanning process
    can always read its own cwd, so a healthy box cannot reach the incomplete
    branch — the must-hit is built into the mechanism."""
    roots = {p: os.path.realpath(p).rstrip(os.sep) for p in paths}
    out = {p: [] for p in paths}
    try:
        names = os.listdir("/proc")
    except OSError:
        return {p: ["unknown"] for p in paths}, False
    read = attempted = 0
    for pid in names:
        if not pid.isdigit():
            continue
        attempted += 1
        try:
            cwd = os.path.realpath(os.path.join("/proc", pid, "cwd")).rstrip(os.sep)
        except OSError:
            continue
        read += 1
        for path, root in roots.items():
            if cwd == root or cwd.startswith(root + os.sep):
                out[path].append(pid)
    for p in out:
        out[p].sort(key=lambda n: int(n) if n.isdigit() else -1)
    return out, not (attempted and not read)


def _occupants(path):
    """Live PIDs whose cwd is `path` or beneath it; UNKNOWN fails closed."""
    return _occupants_many([path])[0][path]


def _disposable_worktree_occupant(pid, proc_root="/proc"):
    """Host-agnostic GC query; metaharness-specific evidence lives at the
    adapter seam rather than accumulating in worktree policy."""
    from .. import harness
    return harness.disposable_worktree_pid(pid, proc_root=proc_root)


def _panes_bound_to(path):
    """([handles], error) for panes the metaharness holds in `path`. Same seam
    discipline as the occupant query above — GC asks, the adapter knows."""
    from .. import harness
    return harness.worktree_panes(path)


def managed_room_kind(root, path):
    """The one cleanup-owner classifier: `lane`, `peek`, `harness`, or None.
    A peek room's owner is `helm work peek --drop` — naming the kind here is
    what keeps envtidy's stray-worktree sweep (which skips every managed
    kind) and the lane sweeps structurally blind to it, rather than each
    surface growing its own exclusion filter."""
    parent = os.path.dirname(os.path.abspath(path))
    if parent == os.path.abspath(root.rstrip(os.sep) + "-wt"):
        return "lane"
    if parent == os.path.join(os.path.abspath(root.rstrip(os.sep) + "-wt"),
                              PEEK_DIRNAME):
        return "peek"
    if parent == os.path.join(os.path.abspath(root.rstrip(os.sep)),
                              ".claude", "worktrees"):
        return "harness"
    return None


def lane_rows(root, registered=None):
    """The project's normal lane rooms: direct children of `<root>-wt/`."""
    rows = []
    for w in registered if registered is not None else worktrees(root):
        path = os.path.abspath(w["path"])
        if managed_room_kind(root, path) == "lane":
            row = dict(w)
            row["lane"] = os.path.basename(path)
            rows.append(row)
    return rows


def auto_rows(root, registered=None):
    """The HARNESS-MINTED rooms: direct children of `<root>/.claude/worktrees/`.

    Subagent (`agent-<id>`) and workflow (`wf_<id>`) worktrees, created per run
    by the harness and reaped by NOTHING. lane_rows deliberately matches only
    `<root>-wt/`, so every gc that consumed it was blind to these by
    construction — the sweep looked complete and covered less than half the
    tree.

    MEASURED 2026-07-28: 12 of 29 registered worktrees were harness-minted, all
    abandoned, TWO carrying uncommitted work from lanes whose agents had died.
    They are also the rows the OWNER sees: orca lists every worktree in its
    sidebar, so 12 unreadable `wf_c8548678-d78-1`-shaped entries sat between him
    and the 6 seats he actually talks to. He had to hunt for a live seat among
    them ("i see, he is here in the sidebar, hidden among all the random other
    ones"). Signal, not count, was the complaint.

    Returned in the SAME row shape as a lane so every existing verdict rule
    applies unchanged — occupancy still keeps, dirty still RESCUES to its own
    branch, merged still allows `-d`. This widens what the sweep can see; it
    weakens nothing about what the sweep may do.
    """
    rows = []
    for w in registered if registered is not None else worktrees(root):
        path = os.path.abspath(w["path"])
        if managed_room_kind(root, path) == "harness":
            row = dict(w)
            row["lane"] = os.path.basename(path)
            row["harness_minted"] = True
            rows.append(row)
    return rows


def _status_entries(raw):
    """Parse `git status --porcelain=v2 -z` into (path_bytes, submodule, kind).
    Rename/copy source paths are consumed but deliberately not timed: the
    destination is the uncommitted path. Any v1/human record is rejected.

    `kind` is the record letter git itself used (b"1" ordinary, b"2"
    rename/copy, b"u" UNMERGED, b"?" untracked). It is carried because the
    parser ALREADY distinguished an unmerged record and then threw that away:
    every caller collapsed it into plain "dirty", so a checkout sitting in a
    conflict was indistinguishable from one with edits in it. Extending the ONE
    parser rather than adding a second that re-counts conflicts — one question,
    one authority (the duplicate-`_helm_sources` lesson)."""
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
            entries.append((parts[8], parts[2], kind))
        elif kind == b"2":
            parts = field.split(b" ", 9)
            if len(parts) != 10 or not parts[9] or i >= len(fields) - 1 \
                    or not fields[i]:
                raise ValueError("malformed rename/copy record")
            entries.append((parts[9], parts[2], kind))
            i += 1  # exact original path, including newlines/NUL framing
        elif kind == b"u":
            parts = field.split(b" ", 10)
            if len(parts) != 11:
                raise ValueError("malformed unmerged record")
            entries.append((parts[10], parts[2], kind))
        elif field.startswith(b"? "):
            entries.append((field[2:], b"N...", b"?"))
        elif field.startswith(b"! ") or field.startswith(b"# "):
            continue
        else:
            raise ValueError("non-v2 or unknown status record")
    return entries


#: markers git leaves while an operation that CAN be continued or aborted runs
_OP_MARKERS = (("MERGE_HEAD", "merge"), ("CHERRY_PICK_HEAD", "cherry-pick"),
               ("REVERT_HEAD", "revert"), ("BISECT_LOG", "bisect"),
               ("rebase-merge", "rebase"), ("rebase-apply", "rebase"))


def _operation_in_progress(path):
    """The name of the git operation this checkout is mid-way through, "" for
    none, or None when we could not look.

    None is a THIRD answer on purpose: "I could not read the git dir" is not
    "there is no operation", and reporting the second for the first is the
    could-not-look-recorded-as-a-fact class this repo has now hit at the
    filesystem, argv, event-ledger and roster layers in one day."""
    rc, gitdir, _err = _git(path, "rev-parse", "--absolute-git-dir", timeout=5)
    if rc != 0 or not gitdir:
        return None
    try:
        for marker, name in _OP_MARKERS:
            if os.path.exists(os.path.join(gitdir, marker)):
                return name
    except OSError:
        return None
    return ""


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
                (os.fsdecode(err).strip() or "unknown error"),
                "conflicts": 0, "operation": None,
                "dangling_conflict": False}
    if not out:
        return {"dirty": False, "wrote_ago": None, "clock_skew": False,
                "unknown": None, "conflicts": 0, "operation": None,
                "dangling_conflict": False}
    try:
        entries = _status_entries(out)
    except ValueError as exc:
        return {"dirty": True, "wrote_ago": None, "clock_skew": False,
                "unknown": "unsafe status record: %s" % exc,
                "conflicts": 0, "operation": None,
                "dangling_conflict": False}
    newest, incomplete = None, []
    for rel_bytes, sub, _kind in entries:
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
    conflicts = sum(1 for _rel, _sub, kind in entries if kind == b"u")
    operation = _operation_in_progress(path) if conflicts else ""
    return {"dirty": True, "wrote_ago": age, "clock_skew": skew,
            "unknown": "; ".join(sorted(set(incomplete))) or None,
            "conflicts": conflicts, "operation": operation,
            # THE STATE NO AGENT CAN RECOVER FROM BY REFLEX, and the one that hit
            # this repo's shared checkout FIVE times in one day: conflict stages
            # in the index with NO operation in progress — the shape a conflicted
            # `git stash apply/pop` leaves. `git merge --abort` correctly refuses
            # (git sees no merge), so the natural next move is `reset --hard`,
            # which is exactly the move that would destroy a hand-resolution —
            # AND, because it is tree-wide while the conflict is per-path, every
            # unrelated uncommitted change in the room as well. The remedy the
            # CLI prints is therefore per-path `restore`, never a hard reset.
            # UNKNOWN operation is never treated as dangling: we did not look.
            "dangling_conflict": bool(conflicts) and operation == ""}


def _wrote_ago(path):
    """Compatibility projection of `_room_status`; None includes clean and
    timestamp-unknown dirty rooms, which callers must distinguish via dirty."""
    return _room_status(path)["wrote_ago"]


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


def _live_claude_sessions():
    try:
        from .. import session
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
        # the unsafe-checkout branch carries the conflict keys too: a caller
        # reading them must not KeyError on exactly the rooms we could not trust
        status_row = ({"dirty": True, "wrote_ago": None, "clock_skew": False,
                       "unknown": issue, "conflicts": 0, "operation": None,
                       "dangling_conflict": False}
                      if issue else _room_status(path))
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
                     "conflicts": status_row["conflicts"],
                     "operation": status_row["operation"],
                     "dangling_conflict": status_row["dangling_conflict"],
                     "harness": harness, "harness_note": harness_note,
                     "hard_unknown": "; ".join(dict.fromkeys(hard_unknown)) or None,
                     "unknown": "; ".join(dict.fromkeys(unknown)) or None})
    return rows, errors


def unguarded_rows(root):
    """Compatibility list projection; CLI uses `unguarded_inventory` so a
    registry/discovery failure is visible rather than looking empty."""
    return unguarded_inventory(root)[0]
