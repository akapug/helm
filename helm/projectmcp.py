"""A seat home's approval of its project's `.mcp.json` servers (stdlib only).
helm-native

WHY THIS EXISTS (task/2698). A seat never ran the servers its project's
`.mcp.json` declares, although its cwd was right. Claude Code runs a project
server only once it is APPROVED in the config dir's state file, under
`projects[<project key>].enabledMcpjsonServers` (or the settings key
`enableAllProjectMcpServers`, which helm never sets). An interactive session
asks; a seat nobody watches never answers, so the servers stay off. Seat
launch seeded the trust flag in the same `projects` entry
(seat_launch_assets._seed_onboarding) and never the approval.

WHAT CLAUDE CODE READS, which this module mirrors (physics.py encodes the same
facts for its report):
  * the `.mcp.json` files of the session's cwd AND every ancestor, nearest
    first (a nearer file wins a name). A file BELOW the cwd is never read.
  * the approval, under the project key it resolves for the session. Versions
    differ on that key: the cwd, the git top level, or the canonical root (a
    linked worktree's main checkout). `approval_keys` names every one of them
    and the approval lands under each, so no version misses it.

A SEAT LAUNCHED ABOVE ITS GIT ROOT (task/2670): the cwd is a parent of the
repository. Claude Code resolves the project as that cwd and never reads the
repository's `.mcp.json`, and no approval can change that. `resolve` names the
repository when exactly one child of the cwd is a git root holding a
`.mcp.json`; the approval is written for THAT root (so a session launched there
has it), and the launch says, in one line, that a session in the parent cwd
will not load the servers and names the root to launch from. helm never moves a
launch's cwd: the cwd names the seat and derives its room.

ADDITIVE AND IDEMPOTENT: names are appended to `enabledMcpjsonServers`, a name
the home lists under `disabledMcpjsonServers` (a rejection someone made) is
never approved, and a home that already carries every name is not written. The
state file keeps its key order, its indent, its trailing newline and its mode
(`update_state`, a compare-and-swap); a symlinked or unreadable one is
left untouched and named.
Nothing here spawns git: the repository is found by walking for `.git`.
"""
import json
import os
import stat

from . import pk

MCPJSON = ".mcp.json"
STATE = ".claude.json"


# ------------------------------------------------------------ the state file

def read_state(path):
    """(body, raw, error) of a config dir's state file. A missing file is
    ({}, None, None): a home before its first login has none yet. A symlink,
    a non-regular file, unparseable JSON or a non-object is (None, None, why),
    and the caller leaves the file alone."""
    if not os.path.lexists(path):
        return {}, None, None
    if os.path.islink(path):
        return None, None, "%s is a symlink (a rewrite would cut it)" % path
    try:
        with pk.open_regular(path, encoding="utf-8") as f:
            raw = f.read()
        body = json.loads(raw)
    except (OSError, ValueError) as e:
        return None, None, "%s is unreadable (%s)" % (path, e)
    if not isinstance(body, dict):
        return None, None, "%s is not a JSON object" % path
    return body, raw, None


def _indent(raw):
    """The indent the file was written with: None for a one-line file, else
    the leading spaces of its first indented line (Claude Code writes 2)."""
    for line in raw.splitlines()[1:]:
        stripped = line.lstrip(" ")
        if stripped:
            return len(line) - len(stripped) or None
    return None


#: how many times a write re-reads a state file that changed under it before
#: it refuses, writing nothing
CAS_TRIES = 5


class StateChanged(OSError):
    """The state file kept changing under every compare-and-swap try."""


class HomeHeld(OSError):
    """A live Claude Code runs on the config dir: it keeps the state in
    memory and rewrites the whole file, so no write here is safe."""


def holder(cdir):
    """None when no live Claude Code process runs on config dir `cdir`, else
    one clause naming why the home is HELD (task/2698). A process runs on the
    dir its environ's CLAUDE_CONFIG_DIR names, or the default home when it
    names none. FAIL CLOSED: a census that cannot be read, or a claude
    process whose environ cannot be read, counts as a holder, because a
    running session keeps this file in memory and rewrites all of it, and a
    compare-and-swap protects neither side against that."""
    from . import beacons, homes, orcaadopt
    try:
        procs, blind = orcaadopt.claude_processes()
    except Exception as e:                  # noqa: BLE001 — unread is held
        return "the claude process census could not be read (%s: %s)" % (
            e.__class__.__name__, e)
    if blind:
        return "claude pid %s could not be read" % ", ".join(
            str(int(x)) for x in blind)
    want = os.path.realpath(cdir)
    for proc in procs:
        pid = int(proc["pid"])
        env = beacons.proc_env(pid)
        if env is None:
            return "claude pid %d's environment could not be read" % pid
        runs = env.get("CLAUDE_CONFIG_DIR") or homes.DEFAULTS["claude"]
        if os.path.realpath(os.path.expanduser(runs)) == want:
            return "claude pid %d%s runs on it" % (
                pid, " (seat %s)" % proc["seat"] if proc.get("seat") else "")
    return None


def _fingerprint(path):
    """(inode, size, mtime_ns) of the state file, or None when it is absent."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return None
    return (st.st_ino, st.st_size, st.st_mtime_ns)


def _before_replace(path):
    """The window between our read and our replace, where a running Claude
    Code writes its own state. A no-op; the tests put a writer here."""


def _text(body, raw):
    """`body` in the SHAPE `raw` had (law 5): key order as loaded, the same
    indent, non-ASCII kept as written, the trailing newline only when the
    original ended with one."""
    if raw is None:
        return json.dumps(body, indent=2, ensure_ascii=False) + "\n"
    text = json.dumps(body, indent=_indent(raw), ensure_ascii=False)
    return text + "\n" if raw.endswith("\n") else text


def update_state(path, change, before_write=None):
    """THE ONE WRITER of a config dir's state file, a COMPARE-AND-SWAP
    (task/2698). A running Claude Code rewrites this file itself; a plain
    read-then-replace would drop whatever it wrote in between. So each try
    reads the file and its (inode, size, mtime_ns), applies `change(body)`,
    writes the new bytes to a temporary beside it, and re-states the file
    immediately before `os.replace`: a file that changed is re-read and the
    change recomputed on what is there now. After CAS_TRIES changed reads it
    raises StateChanged, and nothing is written.

    `change(body)` edits `body` in place and returns a truthy result, or a
    falsy one when there is nothing to write. `before_write(path)` runs on
    each try that writes, BEFORE the last unchanged-check (a backup beside the
    file, so it is of the bytes the replace takes). The file keeps its
    mode; a new one is private (0600): it can hold server configs, and those
    can carry credentials. -> (result, wrote). Raises ValueError for a file
    that cannot be read (symlink, unparseable, not an object), OSError for a
    write that fails."""
    for _try in range(CAS_TRIES):
        seen = _fingerprint(path)
        body, raw, err = read_state(path)
        if err:
            raise ValueError(err)
        if _fingerprint(path) != seen:
            continue                     # it moved while we read it
        result = change(body)
        if not result:
            return result, False
        held = holder(os.path.dirname(path))
        if held:
            raise HomeHeld(
                "%s not written: %s, and a running session rewrites the whole "
                "file from memory — it lands at the next launch with no "
                "holder, or on `helm homes provision --apply` when the home is "
                "idle" % (path, held))
        mode = 0o600 if raw is None else stat.S_IMODE(os.stat(path).st_mode)
        tmp = "%s.%d.cas.tmp" % (path, os.getpid())
        with open(tmp, "w", encoding="utf-8",
                  opener=lambda n, f: os.open(n, f, mode)) as fh:
            fh.write(_text(body, raw))
        os.chmod(tmp, mode)              # the umask must not relax a kept mode
        # the backup comes BEFORE the last check, so a write that lands while
        # it copies is seen by that check and never replaced away
        if before_write is not None:
            before_write(path)
        _before_replace(path)
        if _fingerprint(path) != seen:
            os.unlink(tmp)
            continue
        os.replace(tmp, path)
        return result, True
    raise StateChanged("%s changed under each of %d reads — a running session "
                       "is writing it; nothing was written, re-run when it "
                       "is idle" % (path, CAS_TRIES))


# ------------------------------------------------------ what Claude Code reads

def git_roots(path):
    """(top level, canonical root) of the repository holding `path`, found by
    walking up for `.git`, or (None, None). A linked worktree's `.git` file
    names its gitdir; that dir's `commondir` names the main repository, whose
    checkout is the canonical root. A read that fails keeps the top level."""
    d = os.path.realpath(path)
    while True:
        dot = os.path.join(d, ".git")
        if os.path.isdir(dot):
            return d, d
        if os.path.isfile(dot):
            return d, _canonical(d, dot)
        parent = os.path.dirname(d)
        if parent == d:
            return None, None
        d = parent


def _canonical(top, dotgit):
    try:
        with pk.open_regular(dotgit, encoding="utf-8") as f:
            line = f.readline().strip()
        if not line.startswith("gitdir:"):
            return top
        gitdir = os.path.join(top, line[len("gitdir:"):].strip())
        with pk.open_regular(os.path.join(gitdir, "commondir"),
                             encoding="utf-8") as f:
            common = os.path.realpath(os.path.join(gitdir, f.read().strip()))
    except (OSError, ValueError):
        return top
    return os.path.dirname(common) if os.path.basename(common) == ".git" else top


def _child_root(cwd):
    """The ONE child of `cwd` that is a git root holding a `.mcp.json`, or
    None when there is none or more than one (helm never guesses)."""
    try:
        names = sorted(os.listdir(cwd))
    except OSError:
        return None
    hits = [os.path.join(cwd, n) for n in names
            if os.path.lexists(os.path.join(cwd, n, ".git"))
            and os.path.isfile(os.path.join(cwd, n, MCPJSON))]
    return os.path.realpath(hits[0]) if len(hits) == 1 else None


def resolve(cwd, probe_below=True):
    """-> (root, keys, above). `root` is the directory whose `.mcp.json` walk
    the approval is for; `keys` are the `projects` keys to approve under, in
    order; `above` is True when the cwd is a parent of `root` (a session in
    the cwd will NOT load root's servers). `probe_below=False` skips looking
    for a child repository (the backfill of recorded projects never guesses
    one)."""
    cwd = os.path.realpath(cwd)
    top, canon = git_roots(cwd)
    if top:
        return cwd, _uniq([cwd, top, canon]), False
    child = _child_root(cwd) if probe_below else None
    if child:
        top, canon = git_roots(child)
        return child, _uniq([child, top, canon]), True
    return cwd, [cwd], False


def _uniq(paths):
    out = []
    for p in paths:
        if p and p not in out:
            out.append(p)
    return out


def mcpjson_names(cwd, warnings):
    """Server names in the project's own `.mcp.json` files: `cwd` and each
    ancestor UP TO its git top level, nearest first, each name once (task/2698
    trust: a file above the repository is not this project's, and helm never
    approves it; with no repository only the cwd's own file). A file that
    cannot be read adds ONE line to `warnings` and contributes nothing."""
    names, d = [], os.path.realpath(cwd)
    top = git_roots(d)[0] or d
    while True:
        p = os.path.join(d, MCPJSON)
        if os.path.lexists(p):
            try:
                with pk.open_regular(p, encoding="utf-8") as f:
                    body = json.load(f)
                servers = body.get("mcpServers") if isinstance(body, dict) else None
                if not isinstance(servers, dict):
                    raise ValueError("no mcpServers object")
            except (OSError, ValueError) as e:
                warnings.append("%s unreadable (%s) — its servers are not "
                                "approved" % (p, e))
            else:
                names.extend(n for n in servers if n not in names)
        parent = os.path.dirname(d)
        if d == top or parent == d:
            return names
        d = parent


# ------------------------------------------------------------ plan and write

def _rejected(projects, keys):
    """Every name any of `keys` lists under `disabledMcpjsonServers`: a name
    rejected under one key the project resolves to stays off under all of
    them. Raises ValueError for an entry helm cannot read."""
    out = set()
    for key in keys:
        entry = projects.get(key, {})
        if not isinstance(entry, dict):
            raise ValueError("projects[%s] is not an object" % key)
        rejected = entry.get("disabledMcpjsonServers", [])
        if not isinstance(rejected, list):
            raise ValueError("projects[%s] approval lists are not lists" % key)
        out.update(rejected)
    return out


def _entry_gap(projects, key, names, rejected=()):
    """The names `projects[key]` does not approve yet, minus any rejected
    (`rejected`, across every key of the project, and its own list). Raises
    ValueError for an entry helm cannot extend without replacing it."""
    entry = projects.get(key, {})
    if not isinstance(entry, dict):
        raise ValueError("projects[%s] is not an object" % key)
    have = entry.get("enabledMcpjsonServers", [])
    own = entry.get("disabledMcpjsonServers", [])
    if not isinstance(have, list) or not isinstance(own, list):
        raise ValueError("projects[%s] approval lists are not lists" % key)
    return [n for n in names if n not in have and n not in own
            and n not in rejected]


def plan(cdir, cwd, probe_below=True):
    """Read-only: what approving `cwd`'s project servers in `cdir` would add.
    -> {path, root, above, names, adds: [(key, [names])], warnings, error}.
    `error` set means the state file cannot be extended and nothing will be
    written; `warnings` are the unreadable `.mcp.json` lines."""
    warnings = []
    root, keys, above = resolve(cwd, probe_below)
    # A launch ABOVE the git root names the child root but approves nothing:
    # the trust bar approves only the launched cwd's own project, and a
    # session here would not load that root's servers anyway.
    names = [] if above else mcpjson_names(root, warnings)
    path = os.path.join(cdir, STATE)
    out = {"path": path, "cwd": os.path.realpath(cwd), "root": root,
           "above": above, "names": names, "keys": keys, "adds": [],
           "warnings": warnings, "error": None}
    if not names:
        return out
    body, _raw, err = read_state(path)
    projects = body.get("projects", {}) if body is not None else None
    if err or not isinstance(projects, dict):
        out["error"] = err or "%s: projects is not an object" % path
        return out
    try:
        rejected = _rejected(projects, keys)
        out["adds"] = [(k, g) for k in keys
                       for g in [_entry_gap(projects, k, names, rejected)] if g]
    except ValueError as e:
        out["error"] = "%s: %s" % (path, e)
    return out


def apply(p):
    """Write one plan's adds through the compare-and-swap writer, recomputed
    on the file as it is at the write, so a file that changed since the plan
    (or during the write) is extended as it is now. -> (verdict, detail):
    `ok` nothing to do, `applied`, or `FAIL` with the reason; never an
    exception."""
    if p["error"]:
        return "FAIL", p["error"]
    if not p["adds"]:
        return "ok", "nothing to approve"

    def change(body):
        projects = body.setdefault("projects", {})
        if not isinstance(projects, dict):
            raise ValueError("%s: projects is not an object" % p["path"])
        rejected = _rejected(projects, p["keys"])
        added = []
        for key, _names in p["adds"]:
            gap = _entry_gap(projects, key, p["names"], rejected)
            if gap:
                entry = projects.setdefault(key, {})
                entry["enabledMcpjsonServers"] = \
                    list(entry.get("enabledMcpjsonServers", [])) + gap
                added.append((key, gap))
        return added

    try:
        added, _wrote = update_state(p["path"], change)
    except ValueError as e:
        return "FAIL", "%s" % e
    except HomeHeld as e:
        return "FAIL", "%s" % e
    except OSError as e:
        return "FAIL", "%s not written (%s)" % (p["path"], e)
    if not added:
        return "ok", "nothing to approve"
    return "applied", "; ".join("%s (%s)" % (k, ", ".join(g)) for k, g in added)


def above_line(p):
    """The one line a launch above its git root says, or None."""
    if not p["above"]:
        return None
    return ("cwd %s is ABOVE the git root %s: Claude Code reads .mcp.json from "
            "the cwd upward only, so that root's servers do not load in this "
            "session and are not approved here; launch from %s to approve "
            "and run them" % (p["cwd"], p["root"], p["root"]))


def registered(p):
    """None when the plan's project is a REGISTERED helm project (the project
    registry names its checkout, git top level, or, for a linked worktree,
    its main checkout), else why not. A launch approves servers only for a
    project helm registered and launched a seat in on purpose (task/2698):
    an arbitrary cwd's `.mcp.json` is not one. An unreadable registry is
    not a registered project."""
    from . import foldcompose
    for key in p["keys"]:
        state, _name = foldcompose.project_state(key)
        if state == "registered":
            return None
        if state == "unknown":
            return "the project registry could not be read"
    return "%s is not a registered helm project" % p["root"]


def reconcile(cdir, cwd):
    """THE LAUNCH-TIME CALL: approve `cwd`'s project servers in `cdir`, when
    that project is registered with helm (`registered`).
    -> [lines], each one honest sentence (an approval made, a server file
    that could not be read, a state file left alone, the above-root note);
    an already-approved project says nothing. Never raises."""
    try:
        p = plan(cdir, cwd)
        refused = registered(p) if p["adds"] else None
        verdict, detail = ("FAIL", refused) if refused else apply(p)
    except Exception as e:           # graceful degrade: a line, never a traceback
        return ["project MCP servers NOT approved in %s (%s: %s)"
                % (cdir, e.__class__.__name__, e)]
    lines = list(p["warnings"])
    if verdict == "applied":
        lines.append("project MCP servers approved in %s: %s"
                     % (os.path.join(cdir, STATE), detail))
    elif verdict == "FAIL":
        lines.append("project MCP servers NOT approved (%s) — left untouched"
                     % detail)
    note = above_line(p)
    if note:
        lines.append(note)
    return lines


# --------------------------------------------------------- a home's backfill

def recorded_plans(cdir):
    """One plan per project the home has already TRUSTED (its state file's
    `projects[<dir>].hasTrustDialogAccepted`) and whose dir still exists —
    what a home made before approvals existed is missing. No child probe: the
    backfill approves what a recorded project reads, never a guess. Raises
    ValueError when the state file cannot be read (the drift report's
    cannot-tell)."""
    body, _raw, err = read_state(os.path.join(cdir, STATE))
    if err:
        raise ValueError(err)
    projects = body.get("projects", {})
    if not isinstance(projects, dict):
        raise ValueError("%s: projects is not an object"
                         % os.path.join(cdir, STATE))
    out = []
    for key, entry in projects.items():
        if isinstance(entry, dict) and entry.get("hasTrustDialogAccepted") is True \
                and os.path.isdir(key):
            out.append(plan(cdir, key, probe_below=False))
    return out
