import os
import hashlib

from ._common import (
    physics, HOME, CWD_ROOTS, HOME_ROOTS, harness_for,
    _SKIP_DIRS, _PROJECT_FILES, _HOME_FILES, _HOME_SUBDIRS,
)
from ._classify import (
    _real, _lstat_regular, classify_path, _candidate, _root_match, _under,
)


# ── enumeration: the cwd tree with configs present ────────────────────────────

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
    rd = os.path.join(cwd, ".claude", "rules")
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
    for kind in ("skills", "commands", "agents"):
        d = os.path.join(cwd, ".claude", kind)
        try:
            names = sorted(e.name for e in os.scandir(d)
                           if not e.name.startswith(".") and not e.is_symlink())
        except OSError:
            names = []
        if names:
            out.append({"rel": f".claude/{kind}/", "path": d, "type": "dir",
                        "harness": "claude", "kind": kind, "editable": False,
                        "reason": "directory — manage entries individually",
                        "entries": names})
    return out


def _find_config_dirs(root, maxdepth=6):
    """Dirs under root (bounded, skip-noise) that hold a recognized project config."""
    root = _real(root)
    hits = set()
    root_depth = root.rstrip("/").count("/")
    for dirpath, dirnames, _ in os.walk(root):
        depth = dirpath.rstrip("/").count("/") - root_depth
        if depth >= maxdepth:
            dirnames[:] = []
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS and not d.startswith(".worktree")]
        if _project_files_at(dirpath):
            hits.add(dirpath)
    return hits


def tree(root=None, extra_cwds=None):
    """A nested cwd tree (under root) of directories that hold project configs, each
    node carrying its config files. extra_cwds (e.g. live session cwds) are folded in
    so a cwd with configs is shown even outside the scanned root."""
    requested = [root] if root else CWD_ROOTS
    if root:
        probe = os.path.join(root, ".helm-config-root-probe")
        rr = _real(root)
        if _root_match(probe) is None or not _under(rr, CWD_ROOTS):
            return {"roots": [], "count": 0, "config_roots": [],
                    "home_roots": HOME_ROOTS,
                    "error": "scan root is outside the allowlisted config roots",
                    "code": "refused"}
    roots, seen_roots = [], set()
    for r in requested:
        rr = _real(r)
        if rr not in seen_roots:
            roots.append(rr)
            seen_roots.add(rr)
    cfg_dirs = set()
    for r in roots:
        if os.path.isdir(r):
            cfg_dirs |= _find_config_dirs(r)
    for c in (extra_cwds or []):
        rc = _real(c) if c else None
        # only fold session cwds that live UNDER a config root — a cwd elsewhere
        # (e.g. the home dir itself) must never graft a node ABOVE the root.
        if rc and os.path.isdir(rc) and _under(rc, roots) and _project_files_at(rc):
            cfg_dirs.add(rc)

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
    for d in cfg_dirs:
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
    return {"roots": roots_out, "count": len(nodes),
            "config_roots": roots, "home_roots": HOME_ROOTS}


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
