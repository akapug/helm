"""helm work — the lanes cluster: the two ledgers read from their native
systems, room paths/branches, occupancy, status, and the unguarded
Agent/Workflow inventory. Moved verbatim from the pre-split helm/work.py.
"""
import json
import os
import re
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


def _occupants_many(paths, proc_root="/proc"):
    """One /proc pass for many rooms -> ({path: [pids]}, census_complete).
    Cwd links are the positive proof; an unreadable census is explicit UNKNOWN.
    `proc_root` is the process table read, so a fixture can plant one.

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
    branch — the must-hit is built into the mechanism.

    THE LINK IS READ WITH readlink, AND ONLY ITS TARGET IS RESOLVED.
    `os.path.realpath` does not raise on a cwd link it cannot read: it answers
    with the link's own path, which is inside no room, so every unreadable pid
    would count as a read that proved it absent and the incomplete branch
    could never run. A blind pass answers each room with the unlistable-table
    shape, because `_occupants` hands gc, reap, release and drop a list with
    no completeness flag beside it, and an empty one reads as nobody there."""
    roots = {p: os.path.realpath(p).rstrip(os.sep) for p in paths}
    out = {p: [] for p in paths}
    try:
        names = os.listdir(proc_root)
    except OSError:
        return {p: ["unknown"] for p in paths}, False
    read = attempted = 0
    for pid in names:
        if not pid.isdigit():
            continue
        attempted += 1
        try:
            cwd = os.path.realpath(os.readlink(
                os.path.join(proc_root, pid, "cwd"))).rstrip(os.sep)
        except OSError:
            continue
        read += 1
        for path, root in roots.items():
            if cwd == root or cwd.startswith(root + os.sep):
                out[path].append(pid)
    if attempted and not read:
        return {p: ["unknown"] for p in paths}, False
    for p in out:
        out[p] = [n for n in out[p] if not _git_housekeeping(n, proc_root)]
        out[p].sort(key=lambda n: int(n) if n.isdigit() else -1)
    return out, True


def _git_housekeeping(pid, proc_root="/proc"):
    """True when `pid` is GIT'S OWN auto-housekeeping, which is nobody's live
    work: `git maintenance run` (git 2.51+ forks `git maintenance run --auto
    --quiet --detach` into the background after every commit, cwd in the
    room), `git gc --auto`, or a git process those run (`git repack`,
    `git pack-objects`), each of which exits by itself. Counted as an
    occupant, it refused a release made right after a commit and was named
    as someone's live pane.

    EXACT ARGV, NEVER A PATTERN: the program is `git` and its subcommand is
    `maintenance run`, or `gc` with `--auto`. A descendant counts only while
    every hop up to that run is itself `git`, so a person's `git gc`, a git
    verb under a shell, a hook script and an editor on these words all stay
    occupants."""
    hop = int(pid) if str(pid).isdigit() else 0
    for _ in range(_ANCESTRY_HOPS):
        if hop <= 1:
            return False
        argv = _proc_argv(hop, proc_root)
        if not argv or os.path.basename(argv[0]) != "git":
            return False
        if argv[1:3] == ["maintenance", "run"] or \
                (argv[1:2] == ["gc"] and "--auto" in argv[2:]):
            return True
        hop = _proc_ppid(hop, proc_root)
    return False


def _occupants(path):
    """Live PIDs whose cwd is `path` or beneath it; UNKNOWN fails closed."""
    return _occupants_many([path])[0][path]


# ---------------------------------------------------------------------------
# who is in a room — what an OCCUPIED refusal names, so nobody guesses
# ---------------------------------------------------------------------------
#
# THE FAILURE MODE: a refusal of the shape "OCCUPIED by cwd pid(s) N — room
# and lease kept; move every live pane/process out before release" reads as
# permission to kill N, and a local model read it exactly that way when N
# was helm's OWN detached `python -m helm.findingspass`, which exits by itself.
# It runs with its cwd in the checkout helm runs from, so a lane room
# supplying the running helm is exactly where one sits. A pid list plus an
# imperative names no owner, so the reader supplies one, and "move a process
# out" means a signal.
#
# So the refusal names each occupant by pid AND command line, and says WHOSE
# it is: helm's own worker that ends by itself (wait), helm's own daemon (its
# stop verb), an idle harness placeholder (the release stops it), or somebody
# else's live work (its owner moves it). No line suggests a signal.

# HELM'S OWN WORKERS THAT END BY THEMSELVES: every helm spawn of a
# `python -m helm...` process (`git grep -n "python -m helm\."` and the
# `[sys.executable, "-m", "helm", ...]` Popen sites) that is bounded, plus the
# gate's child scripts, which it runs by path. Matched by argv PREFIX.
_HELM_SELF_EXITING = (
    (("helm.findingspass",),
     "helm's findings pass, a local-model read of one review row"),
    (("helm", "gate", "run"), "a helm gate run"),
    (("helm", "relevance", "score-turn"), "helm's per-turn relevance scorer"),
    (("helm", "proxywatch"), "a helm proxywatch pass"),
    (("helm/gatechild.py",), "a helm gate's test child"),
    (("helm/gateshard.py",), "a helm gate shard"),
    (("helm/gateslice.py",), "a helm gate slice runner"),
)
# helm's own DAEMONS: they do not end by themselves, and each has a stop verb.
_HELM_DAEMONS = (
    (("helm", "router", "run"), "helm's model router daemon", "helm router down"),
)
_CMDLINE_CAP = 160
_HELM_SCRIPT = re.compile(r"(?:^|/)(helm/[a-z_]+\.py)$")
_PY_INTERP = re.compile(r"(?:python|graalpy|pypy)[\d.]*\Z")
# The wrappers that FORK and stay in the process table above the program they
# run: `timeout` (these options take a value) and the findings pass's
# `sh -c 'exec "$@" &' NAME`, whose program is "$@". An exec-style wrapper
# (env, nice, nohup) replaces itself, so its argv is never read.
_TIMEOUT_VALUED = ("-k", "-s", "--kill-after", "--signal")
_RUNS_ITS_ARGS = re.compile(r'exec "\$@"(?:\s*&)?')
_ANCESTRY_HOPS = 32


def _proc_argv(pid, proc_root="/proc"):
    try:
        with open(os.path.join(proc_root, str(pid), "cmdline"), "rb") as f:
            return [os.fsdecode(a) for a in f.read(65536).split(b"\0") if a]
    except OSError:
        return []


def _proc_ppid(pid, proc_root="/proc"):
    """The parent pid from /proc/<pid>/stat, or 0. The comm field may itself
    hold spaces and parentheses, so the fields are read after its LAST ')'."""
    try:
        with open(os.path.join(proc_root, str(pid), "stat"), "rb") as f:
            return int(f.read(4096).rsplit(b")", 1)[1].split()[1])
    except (OSError, IndexError, ValueError):
        return 0


def _python_runs(args):
    """What a Python interpreter runs, from the argv AFTER the interpreter:
    ("m", module, rest), ("script", path, rest), or None for a `-c` program,
    stdin, or no program. CPython's own option grammar: short options cluster
    (`-Bm x`), and `-c`, `-m`, `-W`, `-X` take the rest of their token or the
    next one. Everything after the program is the program's data."""
    i = 0
    while i < len(args):
        a = args[i]
        i += 1
        if a == "--check-hash-based-pycs":
            i += 1
        elif a == "--":
            return ("script", args[i], args[i + 1:]) if i < len(args) else None
        elif a.startswith("--"):
            continue
        elif a == "-":
            return None
        elif not a.startswith("-"):
            return "script", a, args[i:]
        else:
            for j, c in enumerate(a[1:], 2):
                if c in "cmWX":
                    val = a[j:]
                    if not val:
                        val = args[i] if i < len(args) else ""
                        i += 1
                    if c == "c":
                        return None
                    if c == "m":
                        return "m", val, args[i:]
                    break
    return None


def _program(argv):
    """The argv of the program a process runs: argv[0] and its arguments, or,
    under a wrapper that forks (`timeout`, `sh -c 'exec "$@" &' NAME`), the
    command that wrapper's own grammar says it runs. Nothing later in an argv
    is ever a program."""
    while argv:
        name = os.path.basename(argv[0])
        if name == "timeout":
            i = 1
            while i < len(argv) and argv[i].startswith("-"):
                i += 2 if argv[i] in _TIMEOUT_VALUED else 1
            argv = argv[i + 1:]                 # past the DURATION
        elif name in ("sh", "bash", "dash") and argv[1:2] == ["-c"] \
                and len(argv) > 2 and _RUNS_ITS_ARGS.fullmatch(argv[2].strip()):
            argv = argv[4:]                     # past the script and its $0
        else:
            return argv
    return argv


def _helm_entry(argv):
    """What helm entry an argv runs, as a tuple the tables above match by
    prefix: ("helm", verb, sub) for the package or its bin/helm script,
    ("helm.<module>",) for a module, ("helm/<script>.py",) for a helm script
    run by path (a gate child, a seat supervisor), () for anything else.

    THE PROGRAM DECIDES, NEVER A TOKEN ANYWHERE. A process runs helm when its
    program (_program: argv[0], or what a `timeout` or the findings pass's
    `sh -c 'exec "$@" &'` above it runs) is the helm launcher, or is a Python
    interpreter running `-m helm...` or a helm script. An editor, pager or
    git on helm's own sources, a `-c` program or a script handed `-m helm.x`
    as arguments, and a pytest marker all carry a helm entry as DATA; matched
    anywhere, each read as helm's own, and an editor on helm/gatechild.py
    read "exits by itself: WAIT". An interpreter's NAME is data too when it
    is not the program: `vim +/python3 helm/gatechild.py`, `less -p python3`,
    `git log -S python3 --`. What descends from a real helm worker is still
    helm's, by ancestry (describe_occupants)."""
    argv = _program(argv)
    name = os.path.basename(argv[0]) if argv else ""
    if name == "helm":
        return ("helm",) + tuple(argv[1:3])
    runs = _python_runs(argv[1:]) if _PY_INTERP.match(name) else None
    if not runs:
        return ()
    kind, what, rest = runs
    if kind == "m":
        return ("helm",) + tuple(rest[:2]) if what == "helm" else \
            (what,) if what.startswith("helm.") else ()
    path = what.replace(os.sep, "/")
    if os.path.basename(path) == "helm":
        return ("helm",) + tuple(rest[:2])
    script = _HELM_SCRIPT.search(path)
    return (script.group(1),) if script else ()


def _helm_role(argv):
    """(kind, what, stop verb) for helm's own entries, or None."""
    entry = _helm_entry(argv)
    for prefix, what in _HELM_SELF_EXITING:
        if entry[:len(prefix)] == prefix:
            return "self-exiting", what, None
    for prefix, what, verb in _HELM_DAEMONS:
        if entry[:len(prefix)] == prefix:
            return "daemon", what, verb
    return None


def describe_occupants(pids, disposable=(), proc_root="/proc"):
    """One line per room occupant: its pid, its command line (truncated), and
    what the reader does about it — which is never to signal it. A process
    that is not itself a helm entry but descends from a self-exiting one (a
    gate's unittest child) is helm's too, by its ancestry."""
    out = []
    for pid in pids:
        if not str(pid).isdigit():
            out.append("  pid %s  — the process table could not be read, so "
                       "occupancy is UNKNOWN; the room is kept until a census "
                       "can be made" % pid)
            continue
        argv = _proc_argv(pid, proc_root)
        # ONE LINE PER OCCUPANT: an argv may carry newlines (a `sh -c` script)
        # or control bytes, and either would forge the next line of the list.
        cmd = re.sub(r"[\s\x00-\x1f\x7f]+", " ", " ".join(
            [os.path.basename(argv[0])] + argv[1:])).strip() if argv else ""
        if len(cmd) > _CMDLINE_CAP:
            cmd = cmd[:_CMDLINE_CAP - 1] + "…"
        shown = "`%s`" % cmd if cmd else "(command line unreadable; it may have exited)"
        role, via, hop = _helm_role(argv) if argv else None, None, int(pid)
        for _ in range(_ANCESTRY_HOPS):
            if role or hop <= 1:
                break
            hop = _proc_ppid(hop, proc_root)
            role = _helm_role(_proc_argv(hop, proc_root)) if hop > 1 else None
            via = hop
        part = "part of %s (pid %d)" % (role[1], via) if role and via else \
            role[1] if role else ""
        if str(pid) in disposable:
            why = ("an idle harness shell placeholder — the release stops it "
                   "itself once the room holds nothing else")
        elif role and role[0] == "self-exiting":
            why = ("HELM'S OWN, exits by itself — %s: WAIT for it to finish, "
                   "then run the release again" % part)
        elif role:
            why = ("HELM'S OWN daemon, it does not exit by itself — %s: stop "
                   "it with `%s` if it should not run here" % (part, role[2]))
        elif _helm_entry(argv):
            why = ("a helm command a seat or person is running, not a helm "
                   "worker — only whoever started it ends it or moves out")
        else:
            why = ("NOT helm's — someone's live pane, seat, shell or editor: "
                   "only its owner moves it out (cd elsewhere, or close the "
                   "pane)")
        out.append("  pid %s  %s  — %s" % (pid, shown, why))
    return out


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
