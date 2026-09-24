import copy
import itertools
import os
import hashlib
import threading
import time

from ._common import (
    physics, HOME, CWD_ROOTS, HOME_ROOTS, harness_for,
    _SKIP_DIRS, _PROJECT_FILES, _HOME_FILES, _HOME_SUBDIRS,
)
from ._classify import (
    _real, _lstat_regular, classify_path, _candidate, _root_match, _under,
    resolving,
)


# ── enumeration: the cwd tree with configs present ────────────────────────────

# The one config SUBTREE `_project_files_at` reads beyond the `_PROJECT_FILES`
# table — .claude/rules/*.md, and the entry counts for .claude/{skills,commands,
# agents}. They are named here, not spelled inline below, so that the walk's
# candidate filter can DERIVE which directory entries matter from the same
# values the prober itself uses. A second hand-written list of names would
# drift, and the drift would be silent: the walk would simply stop finding a
# whole class of config dir, with nothing to say so.
_CONFIG_SUBDIR = ".claude"
_COUNTED_SUBDIRS = ("skills", "commands", "agents")


def _project_files_at(cwd):
    """Recognized project-scoped config files that EXIST directly at cwd."""
    out = []
    for rel, (typ, harness, kind) in _PROJECT_FILES.items():
        p = os.path.join(cwd, rel)
        if _lstat_regular(p):
            _, editable, reason = classify_path(p)
            out.append({"rel": rel, "path": p, "type": typ, "harness": harness,
                        "kind": kind, "editable": editable, "reason": reason})
    # .claude/rules/*.md and .claude/{skills,commands,agents}/ counts. Never
    # follow file or directory aliases: a symlink must not import an unrelated
    # tree into the owner config surface.
    rd = os.path.join(cwd, _CONFIG_SUBDIR, "rules")
    try:
        rules = sorted(e.path for e in os.scandir(rd)
                       if not e.name.startswith(".") and e.name.endswith(".md")
                       and e.is_file(follow_symlinks=False))
    except OSError:
        rules = []
    for r in rules:
        _, editable, reason = classify_path(r)
        out.append({"rel": os.path.relpath(r, cwd), "path": r, "type": "md",
                    "harness": "claude", "kind": "rule", "editable": editable, "reason": reason})
    for kind in _COUNTED_SUBDIRS:
        d = os.path.join(cwd, _CONFIG_SUBDIR, kind)
        try:
            names = sorted(e.name for e in os.scandir(d)
                           if not e.name.startswith(".") and not e.is_symlink())
        except OSError:
            names = []
        if names:
            out.append({"rel": "%s/%s/" % (_CONFIG_SUBDIR, kind), "path": d, "type": "dir",
                        "harness": "claude", "kind": kind, "editable": False,
                        "reason": "directory — manage entries individually",
                        "entries": names})
    return out


def _candidate_names():
    """Every directory entry name that can make a directory a config dir.

    DERIVED FROM WHAT THE PROBER READS, never transcribed beside it. Every path
    `_project_files_at` opens begins with either the first component of a
    `_PROJECT_FILES` key or `_CONFIG_SUBDIR`, so a directory holding none of
    these names cannot produce a single row.
    """
    return frozenset({rel.split("/", 1)[0] for rel in _PROJECT_FILES}
                     | {_CONFIG_SUBDIR})




def _find_config_dirs(root, maxdepth=6):
    """Dirs under root (bounded, skip-noise) that hold a recognized project config.

    NOTHING IS SUMMARIZED, COLLAPSED OR SHOWN BY ROOT ONLY. A git linked
    worktree and a submodule are both walked and both listed, the same as any
    other directory: THE OWNER'S SURFACE NEVER SHRINKS. Speed comes from asking
    fewer questions about the same tree, never from answering about less of it.

    THE QUESTION IS ASKED ONLY WHERE IT CAN BE ANSWERED YES. os.walk has
    already read each directory and hands back its subdirectory and file names
    as the second and third tuple elements — paid for, whether or not we look.
    A directory whose names miss `_candidate_names()` entirely cannot hold a
    recognized config, so `_project_files_at` — eight lstats and four scandirs,
    plus a `classify_path` per hit — never runs there. The set returned is
    exactly the set the unfiltered probe-everything walk returns.
    """
    root = _real(root)
    hits = set()
    # Derived once per walk, never at import: a snapshot taken at module load
    # is a second copy of the recognized names, free to drift from the prober.
    candidates = _candidate_names()
    root_depth = root.rstrip("/").count("/")
    for dirpath, dirnames, filenames in os.walk(root):
        # Candidacy is read from the names as os.walk found them, BEFORE the
        # prune below rewrites dirnames for the descent.
        if (not candidates.isdisjoint(dirnames)
                or not candidates.isdisjoint(filenames)) \
                and _project_files_at(dirpath):
            hits.add(dirpath)
        depth = dirpath.rstrip("/").count("/") - root_depth
        if depth >= maxdepth:
            dirnames[:] = []
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS and not d.startswith(".worktree")]
    return hits


# ── the two caches ────────────────────────────────────────────────────────────
# THE WALK and THE PAYLOAD BUILT FROM IT are cached separately, because a write
# invalidates them at different rates and the owner's tab hangs on both.
#
# THE WALK's key is its whole input and nothing else: the canonical scan roots,
# CWD_ROOTS and HOME_ROOTS — the two module-level lists fixtures and the home
# lifecycle rebind, and which `classify_path` reads for every file the walk
# recognizes. They are COPIED into tuples: a key holding the live list would be
# a key that changes under the dict it is stored in.
#
# extra_cwds (live session cwds) are DELIBERATELY NOT IN THE WALK KEY. They are
# not walked — each one is a handful of stats applied to the cached walk per
# call. A walk key holding them fails twice over against the only production
# caller, which folds a MOVING set of live session cwds into every request:
# every request misses, and every miss is left behind in the cache.
#
# THE BUILT PAYLOAD is keyed on the walk GENERATION plus exactly that moving
# set, because the payload genuinely depends on it. Rebuilding one costs a
# `_project_files_at` and a `classify_path` cascade for every node in the tree
# and every session cwd probed on top — the measured cost of a warm GET before
# this cache existed, on a request that learned nothing new.
#
# The walk TTL is deliberately short — a config file planted a minute ago must
# show up without anyone restarting the server — and the ↻ rescan button
# bypasses it outright, which is what an owner who just edited a tree clicks.
# The built TTL is shorter still: it is the bound on how long an edit made
# OUTSIDE helm's own write doors can stay invisible, and helm's doors do not
# wait for it (see `invalidate_built`).
_TREE_TTL = 60.0
# past the TTL a reader gets the LAST GOOD walk immediately and one background
# thread refreshes it. Past this, staleness stops being a kindness and the
# caller waits for a real answer.
_TREE_MAX_STALE = 600.0
# distinct roots are few (CWD_ROOTS, plus whatever ?root= the owner types), so
# this cap is a fence against unbounded growth, not a working-set estimate.
_TREE_CACHE_MAX = 32
_BUILT_TTL = 3.0
_BUILT_CACHE_MAX = 16

_tree_registry_lock = threading.Lock()   # guards the dict + each entry's flags
_tree_cache = {}                         # key → _Walk
# A generation per stored walk, unique across keys, so the built-payload cache
# can name the walk it was built from in one integer. itertools.count's __next__
# is a single bytecode-level step, which is what makes it safe to call from the
# refresh thread and a request thread at once.
_walk_seq = itertools.count(1)

_built_lock = threading.Lock()
_built_cache = {}                        # (generation, frozenset(cwds)) → (payload, at)


class _Walk:
    """One cached walk, with its OWN lock.

    A single global lock across the walk made every key wait on every other and
    turned one owner's ↻ rescan into a stall for every reader. Single-flight is
    a property of a key, so the lock lives on the key.
    """
    __slots__ = ("lock", "state", "refreshing", "thread", "used_at")

    def __init__(self):
        self.lock = threading.Lock()
        # (config dirs, walked_at, generation) — ONE assignment, replaced whole.
        # Three separate attributes can be read ACROSS a replacement, and a
        # lock-free reader would then pair one walk's directory set with another
        # walk's timestamp and generation: a fresh answer stamped stale, or a
        # stale answer published under a generation the payload cache believes.
        self.state = None
        self.refreshing = False
        self.thread = None       # the live refresh thread — tests join it
        self.used_at = 0.0


def _store(entry, dirs):
    """Publish a finished walk as one indivisible (dirs, stamp, generation)."""
    entry.state = (dirs, time.monotonic(), next(_walk_seq))
    return entry.state


def invalidate_built():
    """Drop the built payloads, KEEPING the walk.

    WHAT A SAVE CHANGES IS A FILE'S CONTENT, NOT WHICH DIRECTORIES HOLD
    CONFIGS. Saving over a file the tree already lists leaves the walk exactly
    right and the built payload — which carries that file's size-derived and
    classification-derived row — possibly wrong, so this is the whole cost such
    a write should pay. Re-walking the owner's cwd root on every keystroke-save
    is the hang this lane exists to cure, and a write door that did it would be
    re-introducing it one save at a time.
    """
    with _built_lock:
        _built_cache.clear()


def clear_tree_cache():
    """Drop every cached walk AND every payload built from one.

    For tests, and for any caller that has just changed which DIRECTORIES hold
    configs — helm's own config writers do, when a write creates a file where
    there was none.
    """
    with _tree_registry_lock:
        _tree_cache.clear()
    invalidate_built()


def _tree_key(roots):
    """What the walk actually depends on, copied out of the live lists.

    CWD_ROOTS and HOME_ROOTS are module-level lists the fixtures rebind and
    `helm homes` grows, and `classify_path` — which the walk calls for every
    recognized file — reads BOTH. A key made only of the requested root would
    serve one suite's tree to the next one's identical call, and an answer
    computed under one home list would keep being served under another.
    """
    return (tuple(roots), tuple(CWD_ROOTS), tuple(HOME_ROOTS))


def _walk_entry(key):
    """The _Walk for this key, created if new, marked used, cache bounded."""
    now = time.monotonic()
    with _tree_registry_lock:
        entry = _tree_cache.get(key)
        if entry is None:
            entry = _tree_cache[key] = _Walk()
        entry.used_at = now
        if len(_tree_cache) > _TREE_CACHE_MAX:
            stale = sorted((k for k in _tree_cache if k != key),
                           key=lambda k: _tree_cache[k].used_at)
            for k in stale[:len(_tree_cache) - _TREE_CACHE_MAX]:
                del _tree_cache[k]
        return entry


def _walk_roots(roots):
    """The walk itself, over every root, under one resolution memo."""
    dirs = set()
    with resolving():
        for r in roots:
            if os.path.isdir(r):
                dirs |= _find_config_dirs(r)
    return frozenset(dirs)


def _refresh_walk(entry, roots):
    """Re-walk in the background for a reader that was served a stale answer.

    A FAILED REFRESH MUST NOT POISON OR WEDGE THE ENTRY. The last good value
    stays (a walk that raised knows nothing that beats it), and `refreshing`
    clears in `finally` — otherwise one exception would leave the flag set and
    every later reader would be served the same stale answer forever, with
    nothing ever starting the refresh that would replace it.
    """
    try:
        with entry.lock:
            _store(entry, _walk_roots(roots))
    except Exception:
        pass
    finally:
        with _tree_registry_lock:
            entry.refreshing = False


def _cached_walk(roots, rescan):
    """(config dirs, generation) for these roots, walking only when it must.

    Three ways out, in order:
      * FRESH — inside the TTL, hand back the value.
      * STALE — past the TTL but inside the staleness bound: hand back the LAST
        GOOD value at once and refresh it once, in the background. A reader who
        opens the tab must not pay for a walk that only the next reader needs.
      * COLD or TOO STALE or rescan — take this key's lock and walk. The losers
        of a concurrent race re-check under the lock and take the winner's
        result; a rescan always walks and always replaces.
    """
    entry = _walk_entry(_tree_key(roots))
    state = entry.state
    if not rescan and state is not None:
        age = time.monotonic() - state[1]
        if age < _TREE_TTL:
            return state[0], state[2]
        if age < _TREE_MAX_STALE:
            with _tree_registry_lock:
                spawn = not entry.refreshing
                if spawn:
                    entry.refreshing = True
                    entry.thread = threading.Thread(
                        target=_refresh_walk, args=(entry, roots),
                        name="helm-configs-rescan", daemon=True)
            if spawn:
                entry.thread.start()
            return state[0], state[2]
    with entry.lock:
        state = entry.state
        if not rescan and state is not None \
                and time.monotonic() - state[1] < _TREE_TTL:
            return state[0], state[2]           # a racing caller already walked
        state = _store(entry, _walk_roots(roots))
        return state[0], state[2]


def _scan_roots(root):
    """The canonical roots one tree() call walks: the requested root, or every
    CWD_ROOT — realpath'd and deduped, caller order preserved."""
    out, seen = [], set()
    for r in ([root] if root else CWD_ROOTS):
        rr = _real(r)
        if rr not in seen:
            seen.add(rr)
            out.append(rr)
    return out


def _refused_root(root):
    """The refusal for a scan root outside the allowlist, or None.

    A refusal is a verdict about the ARGUMENT, not a scan. It is answered
    before either cache is ever consulted, so a root allowlisted a moment later
    is never met with a stored "refused".
    """
    if not root:
        return None
    probe = os.path.join(root, ".helm-config-root-probe")
    if _root_match(probe) is not None and _under(_real(root), CWD_ROOTS):
        return None
    return {"roots": [], "count": 0, "config_roots": [],
            "home_roots": list(HOME_ROOTS),
            "error": "scan root is outside the allowlisted config roots",
            "code": "refused"}


def tree(root=None, extra_cwds=None, rescan=False):
    """A nested cwd tree (under root) of directories that hold project configs, each
    node carrying its config files. extra_cwds (e.g. live session cwds) are folded in
    so a cwd with configs is shown even outside the scanned root.

    The WALK is served from a short-TTL, per-key, stale-while-revalidate cache
    and the PAYLOAD from a shorter-TTL cache keyed on that walk plus this
    call's session cwds; rescan=True walks, replaces, and rebuilds.
    """
    refused = _refused_root(root)
    if refused is not None:
        return refused
    roots = _scan_roots(root)
    cfg_dirs, generation = _cached_walk(roots, rescan)
    return _built(roots, cfg_dirs, generation, extra_cwds)


def _built(roots, cfg_dirs, generation, extra_cwds):
    """The payload for this walk generation and this call's session cwds.

    A WARM GET IS A DICT LOOKUP AND A COPY. Without this, every open of the
    configs tab re-ran `_project_files_at` and the `classify_path` cascade for
    every node in the tree and for every live session cwd probed on top — on
    the owner's box, about a thousand extra probes per request and most of a
    second of lstats, to rebuild an answer identical to the last one.

    WHAT COMES BACK IS ALWAYS THIS CALLER'S OWN. The cached payload is never
    handed out: a caller that sorts or extends what it received would otherwise
    be editing the next reader's answer, and `config_roots`/`home_roots` inside
    it would be helm's own authorization lists travelling out through JSON.
    """
    # MATERIALIZED ONCE, BEFORE THE KEY IS TAKEN FROM IT. Building the key
    # ITERATES the argument, so a caller handing over a generator — the natural
    # spelling for "the cwds of the live sessions" — would have it drained here
    # and pass an exhausted one to the build, folding in nothing and saying
    # nothing about it.
    probes = tuple(extra_cwds or ())
    key = (generation, frozenset(probes))
    now = time.monotonic()
    with _built_lock:
        hit = _built_cache.get(key)
        if hit is not None and now - hit[1] < _BUILT_TTL:
            return copy.deepcopy(hit[0])
    with resolving():
        payload = _build_tree(roots, cfg_dirs, probes)
    with _built_lock:
        stamp = time.monotonic()
        # A payload past its TTL can never be served again, so it goes now
        # and not when sixteen newer keys happen to push it out.
        for k in [k for k, v in _built_cache.items()
                  if stamp - v[1] >= _BUILT_TTL]:
            del _built_cache[k]
        _built_cache[key] = (payload, stamp)
        if len(_built_cache) > _BUILT_CACHE_MAX:
            drop = sorted((k for k in _built_cache if k != key),
                          key=lambda k: _built_cache[k][1])
            for k in drop[:len(_built_cache) - _BUILT_CACHE_MAX]:
                del _built_cache[k]
    return copy.deepcopy(payload)


def _build_tree(roots, cfg_dirs, extra_cwds):
    """The cached walk plus this call's session cwds, as the nested payload."""
    dirs = set(cfg_dirs)          # a COPY — the cached set outlives this call
    for c in (extra_cwds or []):
        rc = _real(c) if c else None
        # only fold session cwds that live UNDER a config root — a cwd elsewhere
        # (e.g. the home dir itself) must never graft a node ABOVE the root.
        if rc and os.path.isdir(rc) and _under(rc, roots) and _project_files_at(rc):
            dirs.add(rc)

    def _base_for(path):
        return next((b for b in roots if path == b or path.startswith(b.rstrip("/") + "/")), None)

    # one tree per config root; each config dir contributes the chain base→…→dir
    # (walking DOWN from the base, so the base is always the single top — the cascade
    # path root→…→leaf is exactly the ancestor chain).
    nodes = {}
    def _node(p):
        if p not in nodes:
            nodes[p] = {"path": p, "name": os.path.basename(p) or p,
                        "files": _project_files_at(p), "children": []}
        return nodes[p]
    for b in roots:
        if os.path.isdir(b):
            _node(b)
    for d in dirs:
        b = _base_for(d)
        if not b:
            continue
        rel = os.path.relpath(d, b)
        cur = b
        for part in ([] if rel == "." else rel.split(os.sep)):
            cur = os.path.join(cur, part)
            _node(cur)
    for path, node in nodes.items():
        parent = os.path.dirname(path)
        if parent in nodes and parent != path and _base_for(path) and path not in roots:
            nodes[parent]["children"].append(node)
    for n in nodes.values():
        n["children"].sort(key=lambda c: c["name"])
    roots_out = sorted((nodes[b] for b in roots if b in nodes), key=lambda c: c["path"])
    # config_roots / home_roots are COPIES: the live module lists must never
    # travel out through a JSON payload a caller might sort or extend.
    return {"roots": roots_out, "count": len(nodes),
            "config_roots": list(roots), "home_roots": list(HOME_ROOTS)}

def homes_configs():
    """Home/user-scope files, including direct commands and rules.

    Canonical-home dedup makes symlink aliases one deterministic identity. The
    first HOME_ROOTS occurrence wins precedence, matching the resolver's input
    order. Only visible direct regular children of commands/ and rules/ enter
    the surface; nested/generated state, devices and aliases do not.
    """
    out, seen, seen_files = [], set(), set()
    for precedence, raw in enumerate(HOME_ROOTS):
        h = _real(raw)
        if h in seen or not os.path.isdir(h):
            continue
        seen.add(h)
        files = []

        def add(rel, p, typ, harness, kind):
            cp = _candidate(p)
            if cp is None or cp in seen_files or not _lstat_regular(p):
                return
            seen_files.add(cp)
            _, editable, reason = classify_path(cp)
            files.append({"rel": rel, "path": cp, "type": typ, "harness": harness,
                          "kind": kind, "editable": editable, "reason": reason})

        for rel, (typ, harness, kind) in _HOME_FILES.items():
            add(rel, os.path.join(h, rel), typ, harness, kind)
        for subdir, (suffix, typ, harness, kind) in _HOME_SUBDIRS.items():
            d = os.path.join(h, subdir)
            if os.path.islink(d):
                continue
            try:
                entries = sorted((e for e in os.scandir(d)), key=lambda e: e.name)
            except OSError:
                entries = []
            for e in entries:
                if e.name.startswith(".") or not e.name.endswith(suffix) \
                        or not e.is_file(follow_symlinks=False):
                    continue
                add(f"{subdir}/{e.name}", os.path.join(d, e.name), typ, harness, kind)
        # sibling default state file: ~/.claude.json for the ~/.claude home
        if os.path.basename(h) == ".claude":
            sib = os.path.join(os.path.dirname(h), ".claude.json")
            add("../.claude.json", sib, "json", "claude", "state")
        if files:
            provider = harness_for(h)
            out.append({"id": hashlib.sha256(os.fsencode(h)).hexdigest()[:16],
                        "home": os.path.basename(h), "path": h,
                        "provider": provider,
                        "precedence": precedence, "files": files})
    return out


# ── resolve (reuse physics.py — the cascade authority) ────────────────────────

# claude MCP scope precedence (higher wins on a name collision): managed (enterprise)
# > local (home-project-approval) > project (.mcp.json) > user (home-global); plugins
# are namespaced so rarely collide. Best-effort per claude-code's documented order —
# used only to LABEL which of several same-named servers wins; it never changes what
# physics resolved.
_MCP_RANK = {"managed": 4, "home-project-approval": 3, "project-mcpjson": 2, "home-global": 1}


def _annotate_mcp_shadows(report):
    """Mark same-named MCP servers as winner / shadowed across sources, so the UI can
    show that a server is OVERRIDDEN (e.g. a project .mcp.json 'cv' shadows the user
    one). physics already marks .mcp.json depth-shadows; this adds the cross-tier
    case. Additive: shadowed entries stay in the list, flagged."""
    servers = report.get("mcpServers")
    if not isinstance(servers, list):
        return report

    def rank(e):
        if e.get("shadowed"):
            return -2
        src = e.get("source") or ""
        return _MCP_RANK.get(src, -1 if src.startswith("plugin") else 0)

    groups = {}
    for e in servers:
        groups.setdefault(e.get("name"), []).append(e)
    for group in groups.values():
        if len(group) < 2:
            continue
        winner = max(group, key=rank)
        for e in group:
            if e is not winner and not e.get("shadowed"):
                e["shadowed"] = True
                e["shadowed_by"] = winner.get("source")
    return report


def _allowed_home(path):
    """True iff path is a recognized cred home: an entry of HOME_ROOTS (tests
    patch this list), or a direct child of ~/.claude-homes / ~/.codex-homes
    (homes created after import — HOME_ROOTS globs once). The resolve read
    path must never walk an arbitrary directory's config-shaped files: the
    web layer exposes it on unauthenticated GET (?home=), and physics_report
    returns hook commands, mcpServers and settings layers for whatever home
    it is pointed at."""
    rp = _real(path)
    if any(rp == _real(h) for h in HOME_ROOTS):
        return True
    return os.path.dirname(rp) in (_real(f"{HOME}/.claude-homes"),
                                   _real(f"{HOME}/.codex-homes"),
                                   _real(f"{HOME}/.pi-homes"))


def resolve(home_path, cwd, harness):
    """physics.py's resolved seat — what (home, cwd) WOULD load, with source +
    precedence, plus MCP winner/shadowed annotation. This IS the cascade view
    (root→…→cwd + the home/user layer). The home must be a recognized cred
    home (allowlist, same posture as read_file) — never an arbitrary dir."""
    if not _allowed_home(home_path):
        return {"error": "home is not a recognized cred home", "code": "refused"}
    return _annotate_mcp_shadows(physics.physics_report(home_path, cwd or None, harness))
