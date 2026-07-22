#!/usr/bin/env python3
"""configs.py — the config-management model.
helm-native (see ATTRIBUTION.md for lineage)
dissolve-into-helm law. Design record: the predecessor repo's CONFIGS_DESIGN.md.

One place to SEE and safely EDIT every local claude/codex config — MCPs, hooks,
skills, rules, memory (CLAUDE.md/AGENTS.md), settings — across all homes and cwds,
with the cascade (root → /dev → project).

Two halves:
  * READ / RESOLVE — reuses physics.py (the ground-truthed cascade resolver) for
    "what a seat loads", and enumerates which config files exist across a cwd tree.
  * EDIT — a safety-first editor: every write is backup → validate → atomic rename,
    restricted to RECOGNIZED config files under allowlisted roots, never a
    plugin/managed file. A bad write must never brick an agent's launch.

Stdlib only. Never reads or writes credential/token contents (config files only).
"""
import ctypes
import errno
import glob
import hashlib
import json
import os
import secrets
import stat
import time

try:
    import tomllib  # py3.11+; validation of config.toml degrades to a warning without it
except ImportError:  # pragma: no cover
    tomllib = None

from . import physics

HOME = os.path.expanduser("~")
BACKUP_DIR = os.path.join(HOME, ".cache", "helm", "config-backups")

# cwd roots to browse for project-scoped configs (colon-separated env override;
# HELM_* preferred, legacy SESH_* accepted — the env2 pattern catalog.py uses).
CWD_ROOTS = [r for r in os.environ.get(
    "HELM_CONFIG_ROOTS", os.environ.get("SESH_CONFIG_ROOTS", f"{HOME}/dev")).split(":") if r]
# helm seats are full isolated claude config homes living under the helm home
# (<helm_home>/_global/seats/<family>/claude — settings.json, .claude.json,
# sessions). helm_home honors HELM_HOME/MELD_HOME. The delivery-lane wiring
# writes settings.json here through the same gated path as any home; creds stay
# denied (auth.json/.credentials.json) and the proxy config.yaml is unrecognized.
_HELM_HOME = os.path.abspath(os.path.expanduser(
    os.environ.get("HELM_HOME") or os.environ.get("MELD_HOME") or f"{HOME}/.helm"))
# credential-home roots (project-scoped configs never live here, but home/user-scope
# settings do: <home>/settings.json, <home>/.claude.json, <home>/config.toml, …).
HOME_ROOTS = ([f"{HOME}/.claude", f"{HOME}/.codex"]
              + sorted(glob.glob(f"{HOME}/.claude-homes/*"))
              + sorted(glob.glob(f"{HOME}/.codex-homes/*"))
              + sorted(glob.glob(os.path.join(_HELM_HOME, "_global", "seats", "*", "claude"))))

MANAGED_DIRS = ("/etc/claude-code", "/Library/Application Support/ClaudeCode")
# dirs we never descend into when scanning a cwd tree (noise + huge).
_SKIP_DIRS = {"node_modules", ".git", "target", ".venv", "venv", "__pycache__",
              "dist", "build", ".next", ".cache", "vendor", ".worktrees"}

# recognized project-scoped config files (relative to a cwd) → (type, harness, kind)
# kind drives the UI grouping; type drives validation.
_PROJECT_FILES = {
    ".mcp.json": ("json", "claude", "mcp"),
    "CLAUDE.md": ("md", "claude", "memory"),
    "CLAUDE.local.md": ("md", "claude", "memory"),
    ".claude/CLAUDE.md": ("md", "claude", "memory"),
    ".claude/settings.json": ("json", "claude", "settings"),
    ".claude/settings.local.json": ("json", "claude", "settings"),
    "AGENTS.md": ("md", "codex", "memory"),
}
# recognized home/user-scope files (relative to a home dir)
_HOME_FILES = {
    "settings.json": ("json", "claude", "settings"),
    ".claude.json": ("json", "claude", "state"),
    "config.toml": ("toml", "codex", "settings"),
    "hooks.json": ("json", "codex", "hooks"),
    "AGENTS.md": ("md", "codex", "memory"),
    "CLAUDE.md": ("md", "claude", "memory"),
}


import re

# credential/token stores — NEVER readable or editable through the config layer,
# even though they are JSON under a home root (the review's #2/#5 root cause).
_DENY_FILES = {".credentials.json", "auth.json"}
_RULE_RE = re.compile(r"/\.claude/rules/[^/]+\.md$")
# DECLARATIVE config that lives one level down inside a config home, not
# directly in it. The owner's report was "poking around my configs UI proves
# it, but mostly they are uneditable from there" — measured: of 500 files
# under the config homes, 457 were unrecognized, and the great majority of
# those SHOULD be (48 credential stores, 108 .bak copies, 104 runtime state
# files like history.jsonl / goals_1.sqlite / *-snapshot.json, 32 plugin
# metadata blobs). Stripping those left exactly two categories of real
# human-authored config that the gate simply had no pattern for:
#   commands/<name>.md   — the owner's own slash commands
#   rules/<name>.rules   — codex rule files
# Both are declarative, validatable, and carry the same trust surface as
# CLAUDE.md, which has always been editable here.
_HOME_SUBDIRS = {
    "commands": (".md", "md", "claude", "command"),
    "rules": (".rules", "text", "codex", "rule"),
}
_HOME_SUBDIR_RE = re.compile(r"/(commands/[^/]+\.md|rules/[^/]+\.rules)$")
_MAX_CONFIG_BYTES = 1_000_000
_MISSING_REVISION = "missing"

# Linux gives us an atomic exchange primitive. It is what lets the writer inspect
# the exact inode displaced by the save and exchange it back if another process
# replaced or changed the file in the final check-to-rename window.
_RENAME_NOREPLACE = 1
_RENAME_EXCHANGE = 2
_LIBC = ctypes.CDLL(None, use_errno=True)
_RENAMEAT2 = getattr(_LIBC, "renameat2", None)
if _RENAMEAT2 is not None:
    _RENAMEAT2.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int,
                           ctypes.c_char_p, ctypes.c_uint)
    _RENAMEAT2.restype = ctypes.c_int

# DELIBERATELY STILL EXCLUDED — this is a security boundary, not an oversight.
# The remaining 23 unrecognized-but-config-shaped files are EXECUTABLES:
# <home>/*.py and <home>/*.sh hook and statusline scripts. Making those
# editable would turn the config editor into a remote-code-execution surface —
# the web UI writes them, the next hook invocation runs them as the owner. A
# JSON/MD/TOML config can only misconfigure; a .sh hook can do anything. Edit
# those with a real editor, where the act of doing so is explicit.


def _real(p):
    return os.path.realpath(os.path.expanduser(p))


def _error(code, message):
    """A class-only failure safe to return through HTTP/CLI.

    OSError strings and refused caller paths are intentionally not reflected:
    either can disclose an unrelated path from outside the owner config surface.
    """
    return {"error": message, "code": code}


def _absolute(path):
    if not isinstance(path, str) or not path or "\0" in path:
        return None
    return os.path.abspath(os.path.expanduser(path))


def _candidate(path):
    """Resolve the parent, never the leaf.

    realpath(path) silently turns a symlink file into its target and loses the
    fact that the owner selected an alias. Keeping the leaf unresolved lets
    lstat/open(O_NOFOLLOW)/fstat reject aliases and devices explicitly.
    """
    ap = _absolute(path)
    if ap is None:
        return None
    return os.path.join(_real(os.path.dirname(ap)), os.path.basename(ap))


def _path_below(path, root):
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def _root_match(path):
    """Return the canonical allowlisted root matching the caller's lexical path.

    Both the lexical and resolved-parent paths must remain in the same root.
    This denies /tmp/alias -> ~/.claude escapes while still supporting a root
    which is itself configured as a symlink. Symlink components below a root
    are denied; discovery emits canonical paths, so aliases never become a
    second identity for one file.
    """
    ap, rp = _absolute(path), _candidate(path)
    if ap is None or rp is None:
        return None
    roots = list(CWD_ROOTS) + list(HOME_ROOTS) + [_HELM_HOME]
    for root in roots:
        raw, real = _absolute(root), _real(root)
        if raw is None:
            continue
        base = raw if _path_below(ap, raw) else real if _path_below(ap, real) else None
        if base is None or not _path_below(rp, real):
            continue
        rel = os.path.relpath(ap, base)
        cur = base
        for part in rel.split(os.sep)[:-1]:
            if part in ("", "."):
                continue
            cur = os.path.join(cur, part)
            try:
                if stat.S_ISLNK(os.lstat(cur).st_mode):
                    return None
            except FileNotFoundError:
                break
            except OSError:
                return None
        return real
    holder = _home_holder(rp)
    if _is_seat_home(holder):
        real = _real(holder)
        if _path_below(ap, real) and _path_below(rp, real):
            cur = real
            for part in os.path.relpath(ap, real).split(os.sep)[:-1]:
                if part in ("", "."):
                    continue
                cur = os.path.join(cur, part)
                try:
                    if stat.S_ISLNK(os.lstat(cur).st_mode):
                        return None
                except FileNotFoundError:
                    break
                except OSError:
                    return None
            return real
    return None


def _lstat_regular(path):
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError:
        return False
    return st if stat.S_ISREG(st.st_mode) else False


def _home_holder(path):
    parent = os.path.dirname(path)
    if os.path.basename(parent) in _HOME_SUBDIRS:
        return os.path.dirname(parent)
    return parent


def _is_seat_home(parent):
    """True iff `parent` is a seat's isolated claude config dir —
    <helm_home>/_global/seats/<family>/claude, OR (slice 6) an instance's
    <helm_home>/_global/seats/<family>/instances/<seat>/claude. Computed per
    call (env-honoring, NOT the import-time glob) so a seat minted after
    import (`helm seat add` wires its delivery hooks in the same process) is
    recognized by the write gate. Same trust surface as the HOME_ROOTS glob
    that catches pre-existing seats; creds beside it stay denied by name."""
    from . import home
    rp = _real(parent)
    if os.path.basename(rp) != "claude":
        return False
    sroot = _real(os.path.join(home.global_dir(), "seats"))
    up1 = os.path.dirname(rp)                             # <family> | <seat>
    up2 = os.path.dirname(up1)                            # seats | instances
    if up2 == sroot:
        return True                                       # seats/<family>/claude
    # instance: seats/<family>/instances/<seat>/claude
    return (os.path.basename(up2) == "instances"
            and os.path.dirname(os.path.dirname(up2)) == sroot)


def _is_recognized_config(rp):
    """True iff rp is a RECOGNIZED config file — the single gate for both reading
    content and editing. Extension alone is NOT enough (a package.json, a random
    settings.json, or a credential store must never qualify): a project file must
    match a known rel-path pattern, a home file must sit directly in a home root.
    This is the fix for the review's arbitrary-read (#1) + arbitrary-write (#2)."""
    base = os.path.basename(rp)
    if base in _DENY_FILES:
        return False
    # project-scoped: the path ends with a known rel pattern (at any cwd depth)
    if any(rp.endswith("/" + rel) for rel in _PROJECT_FILES):
        return True
    if _RULE_RE.search(rp):
        return True
    # home-scope DECLARATIVE config one level down (commands/, rules/): the
    # subdir must sit directly in a recognized home root, so a stray
    # commands/foo.md anywhere else on disk still fails the gate.
    m = _HOME_SUBDIR_RE.search(rp)
    if m:
        holder = _real(os.path.dirname(os.path.dirname(rp)))
        if any(holder == _real(h) for h in HOME_ROOTS) or _is_seat_home(holder):
            return True
    # home/user-scope: the recognized basename sits DIRECTLY in a home root
    if base in _HOME_FILES:
        parent = _real(os.path.dirname(rp))
        if any(parent == _real(h) for h in HOME_ROOTS) or _is_seat_home(parent):
            return True
        if base == ".claude.json" and parent == _real(HOME):  # the ~/.claude.json sibling
            return True
    return False


def _ext_type(path):
    """Validation type for a path by its recognized shape, else 'other'."""
    base = os.path.basename(path)
    if base.endswith((".mcp.json",)) or base in (".claude.json", "settings.json",
                                                 "settings.local.json", "hooks.json"):
        return "json"
    if base.endswith(".json"):
        return "json"
    if base.endswith(".toml"):
        return "toml"
    if base.endswith(".md"):
        return "md"
    if base.endswith(".rules"):
        # No parser to validate against, so it rides the text path — but it
        # IS recognized, which is what makes it editable at all.
        return "text"
    return "other"


def _under(path, roots):
    rp = _real(path)
    return any(rp == _real(r) or rp.startswith(_real(r).rstrip("/") + "/") for r in roots)


def _is_plugin_or_managed(path):
    rp = _real(path)
    if any(_path_below(rp, _real(m)) for m in MANAGED_DIRS):
        return True
    # plugin-provided config lives under a home's plugins/ tree — read-only here.
    # Match the actual plugins install dir, not any directory merely NAMED 'plugins'
    # (a project's own plugins/ dir is a legit editable location).
    return any(rp.startswith(_real(os.path.join(h, "plugins")) + os.sep) for h in HOME_ROOTS)


def classify_path(path):
    """(type, editable, reason) for one leaf without following that leaf.

    Existing entries must be regular, owner-writable files. Missing recognized
    leaves may be created only below a real allowlisted parent (or one direct
    config subdirectory below it). All callers share this classification gate.
    """
    rp = _candidate(path)
    typ = _ext_type(rp or "")
    if rp is None:
        return typ, False, "invalid path"
    if os.path.basename(rp) in _DENY_FILES:
        return typ, False, "credential/token store — never editable"
    if not _is_recognized_config(rp):
        return typ, False, "not a recognized config file"
    if _root_match(path) is None:
        return typ, False, "outside the allowlisted config roots"
    holder = _home_holder(rp)
    if not (_under(rp, CWD_ROOTS + HOME_ROOTS) or _is_seat_home(holder)):
        return typ, False, "outside the allowlisted config roots"
    if _is_plugin_or_managed(rp):
        return typ, False, "plugin/managed-provided — read-only"
    st = _lstat_regular(rp)
    if st is False:
        return typ, False, "not a regular file"
    if st is not None:
        if st.st_size > _MAX_CONFIG_BYTES:
            return typ, False, "file exceeds the editor size limit"
        if not (st.st_mode & stat.S_IWUSR):
            return typ, False, "owner read-only"
        return typ, True, "ok"
    parent = os.path.dirname(rp)
    if not os.path.isdir(parent) and not os.path.isdir(os.path.dirname(parent)):
        return typ, False, "parent directory does not exist"
    return typ, True, "ok"


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
            out.append({"id": hashlib.sha256(os.fsencode(h)).hexdigest()[:16],
                        "home": os.path.basename(h), "path": h,
                        "provider": "codex" if "codex" in h else "claude",
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
                                   _real(f"{HOME}/.codex-homes"))


def resolve(home_path, cwd, harness):
    """physics.py's resolved seat — what (home, cwd) WOULD load, with source +
    precedence, plus MCP winner/shadowed annotation. This IS the cascade view
    (root→…→cwd + the home/user layer). The home must be a recognized cred
    home (allowlist, same posture as read_file) — never an arbitrary dir."""
    if not _allowed_home(home_path):
        return {"error": "home is not a recognized cred home", "code": "refused"}
    return _annotate_mcp_shadows(physics.physics_report(home_path, cwd or None, harness))


# ── read / edit (safety-first) ────────────────────────────────────────────────

class _ConfigIOError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def _stat_identity(st):
    return st.st_dev, st.st_ino


def _revision(st, data):
    """Opaque edit identity stable across our own atomic rename."""
    h = hashlib.sha256()
    h.update(("%d:%d:%d:%d:%d:%d:%d:" % (
        st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns,
        stat.S_IMODE(st.st_mode), st.st_uid, st.st_gid)).encode())
    h.update(data)
    return h.hexdigest()


def _same_displaced_snapshot(a, b):
    """Compare the inode exchanged out of place with the pre-save snapshot.

    rename/exchange itself updates ctime on Linux, so ctime cannot participate
    here. Identity, bytes, mtime, ownership and mode still catch replacement,
    content changes and metadata changes in the final race window.
    """
    sa, sb = a["stat"], b["stat"]
    return (a["data"] == b["data"] and _stat_identity(sa) == _stat_identity(sb)
            and sa.st_size == sb.st_size and sa.st_mtime_ns == sb.st_mtime_ns
            and stat.S_IMODE(sa.st_mode) == stat.S_IMODE(sb.st_mode)
            and sa.st_uid == sb.st_uid and sa.st_gid == sb.st_gid)


def _snapshot_at(dirfd, name):
    """Open one regular leaf without following it and return a stable snapshot."""
    try:
        before = os.stat(name, dir_fd=dirfd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError:
        raise _ConfigIOError("io", "config file could not be inspected")
    if not stat.S_ISREG(before.st_mode):
        raise _ConfigIOError("not-regular", "config entry is not a regular file")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(name, flags, dir_fd=dirfd)
    except OSError:
        raise _ConfigIOError("open", "config file could not be opened safely")
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode) or _stat_identity(opened) != _stat_identity(before):
            raise _ConfigIOError("conflict", "config file changed concurrently")
        chunks, total = [], 0
        while True:
            chunk = os.read(fd, min(65536, _MAX_CONFIG_BYTES + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > _MAX_CONFIG_BYTES:
                raise _ConfigIOError("too-large", "config file exceeds the editor size limit")
        after = os.fstat(fd)
        stable = (opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns,
                  _stat_identity(opened))
        if stable != (after.st_size, after.st_mtime_ns, after.st_ctime_ns,
                      _stat_identity(after)):
            raise _ConfigIOError("conflict", "config file changed concurrently")
        data = b"".join(chunks)
        return {"data": data, "stat": after, "revision": _revision(after, data)}
    finally:
        os.close(fd)


def _snapshot(path):
    parent, name = os.path.dirname(path), os.path.basename(path)
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0) \
        | getattr(os, "O_NOFOLLOW", 0)
    try:
        dfd = os.open(parent, flags)
    except FileNotFoundError:
        return None
    except OSError:
        raise _ConfigIOError("parent", "config parent could not be opened safely")
    try:
        return _snapshot_at(dfd, name)
    finally:
        os.close(dfd)


def _decode(data):
    encoding = "utf-8-sig" if data.startswith(b"\xef\xbb\xbf") else "utf-8"
    try:
        return data.decode(encoding), encoding
    except UnicodeDecodeError:
        raise _ConfigIOError("encoding", "config file is not valid UTF-8")


def _newline_style(content):
    crlf = content.count("\r\n")
    rest = content.replace("\r\n", "")
    kinds = sum(bool(n) for n in (crlf, rest.count("\n"), rest.count("\r")))
    if kinds > 1:
        return "mixed"
    if crlf:
        return "crlf"
    if "\n" in rest:
        return "lf"
    if "\r" in rest:
        return "cr"
    return "none"


def _encode(content, encoding, newline):
    if newline == "mixed":
        raise _ConfigIOError("newline", "mixed newline styles are read-only")
    normalized = content.replace("\r\n", "\n").replace("\r", "\n")
    if newline == "crlf":
        normalized = normalized.replace("\n", "\r\n")
    elif newline == "cr":
        normalized = normalized.replace("\n", "\r")
    return normalized.encode(encoding)


def _readable(path, rp):
    holder = _home_holder(rp)
    return (os.path.basename(rp) not in _DENY_FILES
            and _is_recognized_config(rp)
            and _root_match(path) is not None
            and (_under(rp, CWD_ROOTS + HOME_ROOTS) or _is_seat_home(holder)))


def read_file(path):
    """Content plus an opaque revision for one safely-opened config file."""
    rp = _candidate(path)
    typ, editable, reason = classify_path(path)
    if rp is None or not _readable(path, rp):
        return {"path": "", "type": typ, "editable": False, "reason": reason,
                "exists": False, "content": "", "revision": None,
                **_error("refused", "only recognized config files are readable")}
    st = _lstat_regular(rp)
    exists = st is not None and st is not False
    if st is False:
        return {"path": rp, "type": typ, "editable": False,
                "reason": "not a regular file", "exists": True, "content": "",
                "revision": None, **_error("not-regular", "config entry is not a regular file")}
    if not exists:
        return {"path": rp, "type": typ, "editable": editable, "reason": reason,
                "exists": False, "content": "", "revision": _MISSING_REVISION,
                "encoding": "utf-8", "newline": "none", "error": None}
    try:
        snap = _snapshot(rp)
        content, encoding = _decode(snap["data"])
    except _ConfigIOError as e:
        return {"path": rp, "type": typ, "editable": False, "reason": e.message,
                "exists": True, "content": "", "revision": None,
                **_error(e.code, e.message)}
    newline = _newline_style(content)
    if newline == "mixed":
        editable, reason = False, "mixed newline styles are read-only"
    return {"path": rp, "type": typ, "editable": editable, "reason": reason,
            "exists": True, "content": content, "revision": snap["revision"],
            "encoding": encoding, "newline": newline, "error": None}


def _validate(typ, content):
    """(ok, error). Reject an edit that would make a parseable type unparseable."""
    if typ == "json":
        try:
            json.loads(content)
        except ValueError as e:
            return False, f"invalid JSON at line {getattr(e, 'lineno', '?')} column {getattr(e, 'colno', '?')}"
    elif typ == "toml":
        if tomllib is None:
            return True, "toml not validated (python < 3.11)"
        try:
            tomllib.loads(content)
        except Exception as e:
            return False, f"invalid TOML ({e.__class__.__name__})"
    return True, None


def _write_all(fd, data):
    view = memoryview(data)
    while view:
        n = os.write(fd, view)
        if n <= 0:
            raise OSError(errno.EIO, "short write")
        view = view[n:]


def _backup_snapshot(rp, snap, encoding, newline):
    """Durably record the exact bytes about to be displaced."""
    try:
        os.makedirs(BACKUP_DIR, mode=0o700, exist_ok=True)
        os.chmod(BACKUP_DIR, 0o700)
        dfd = os.open(BACKUP_DIR, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                      | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        raise _ConfigIOError("backup", "config backup could not be created")
    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime()) \
        + f"-{time.time_ns() % 1_000_000_000:09d}"
    tag = hashlib.sha256(os.fsencode(rp)).hexdigest()[:20]
    name = f"{stamp}__{tag}"
    meta = json.dumps({"orig": rp, "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                       "encoding": encoding, "newline": newline}, ensure_ascii=False).encode()
    made = []
    try:
        for leaf, data in ((name, snap["data"]), (name + ".orig", meta)):
            fd = os.open(leaf, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                         | getattr(os, "O_CLOEXEC", 0), 0o600, dir_fd=dfd)
            try:
                os.fchmod(fd, 0o600)
                _write_all(fd, data)
                os.fsync(fd)
            finally:
                os.close(fd)
            made.append(leaf)
        os.fsync(dfd)
        return os.path.join(BACKUP_DIR, name)
    except OSError:
        for leaf in made:
            try:
                os.unlink(leaf, dir_fd=dfd)
            except OSError:
                pass
        raise _ConfigIOError("backup", "config backup could not be created")
    finally:
        os.close(dfd)


def _renameat2(dfd, old, new, flags):
    if _RENAMEAT2 is None:
        raise OSError(errno.ENOTSUP, "renameat2 unavailable")
    if _RENAMEAT2(dfd, os.fsencode(old), dfd, os.fsencode(new), flags) != 0:
        e = ctypes.get_errno()
        raise OSError(e, os.strerror(e))


def _remove_quiet(dfd, name):
    try:
        os.unlink(name, dir_fd=dfd)
    except OSError:
        pass


def _write_file_impl(path, content, expected_revision=None, encoding_override=None,
                     newline_override=None):
    """Validated, conflict-detecting, durable atomic edit of one regular file.

    Existing saves use renameat2(RENAME_EXCHANGE): the displaced inode remains
    staged until its identity and bytes match the editor revision and the parent
    directory is fsynced. Any mismatch or durability failure exchanges it back.
    Creates use an atomic no-clobber hard link. No arbitrary path is opened by
    name before the shared recognition/containment/lstat gate accepts it.
    """
    rp = _candidate(path)
    typ, editable, reason = classify_path(path)
    if rp is None or not editable:
        return _error("refused", "refused: " + reason)
    if not isinstance(content, str):
        return _error("content", "refused: content must be text")
    ok, verr = _validate(typ, content)
    if not ok:
        return _error("validation", f"refused: {verr} — no change written")
    parent, name = os.path.dirname(rp), os.path.basename(rp)
    created_parent = False
    if not os.path.isdir(parent):
        if expected_revision not in (None, _MISSING_REVISION):
            return _error("conflict", "config file changed concurrently; reload before saving")
        if os.path.basename(parent) not in (".claude", "commands", "rules"):
            return _error("parent", "config parent does not exist")
        try:
            os.mkdir(parent, 0o700)
            created_parent = True
        except OSError:
            return _error("parent", "config parent could not be created safely")
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0) \
        | getattr(os, "O_NOFOLLOW", 0)
    try:
        dfd = os.open(parent, flags)
    except OSError:
        return _error("parent", "config parent could not be opened safely")
    tmp = f".helm-config-{os.getpid()}-{secrets.token_hex(8)}"
    backup = None
    try:
        try:
            current = _snapshot_at(dfd, name)
        except _ConfigIOError as e:
            return _error(e.code, e.message)
        current_revision = current["revision"] if current else _MISSING_REVISION
        if expected_revision is not None and expected_revision != current_revision:
            return _error("conflict", "config file changed concurrently; reload before saving")
        if current:
            try:
                current_content, current_encoding = _decode(current["data"])
            except _ConfigIOError as e:
                return _error(e.code, e.message)
            current_newline = _newline_style(current_content)
        else:
            current_encoding, current_newline = "utf-8", "lf"
        encoding = encoding_override or current_encoding
        newline = newline_override or current_newline
        try:
            data = _encode(content, encoding, newline)
        except (_ConfigIOError, UnicodeEncodeError) as e:
            code = e.code if isinstance(e, _ConfigIOError) else "encoding"
            message = e.message if isinstance(e, _ConfigIOError) else "content cannot use the original encoding"
            return _error(code, message)
        if len(data) > _MAX_CONFIG_BYTES:
            return _error("too-large", "config content exceeds the editor size limit")
        if current:
            try:
                backup = _backup_snapshot(rp, current, current_encoding, current_newline)
            except _ConfigIOError as e:
                return _error(e.code, e.message)
            mode = stat.S_IMODE(current["stat"].st_mode)
            uid, gid = current["stat"].st_uid, current["stat"].st_gid
        else:
            holder = _home_holder(rp)
            mode = 0o600 if (_under(rp, HOME_ROOTS) or _is_seat_home(holder)) else 0o644
            uid, gid = os.geteuid(), os.getegid()
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                         | getattr(os, "O_CLOEXEC", 0), mode, dir_fd=dfd)
            try:
                os.fchown(fd, uid, gid)
                os.fchmod(fd, mode)
                _write_all(fd, data)
                os.fsync(fd)
                staged_stat = os.fstat(fd)
            finally:
                os.close(fd)
        except OSError:
            _remove_quiet(dfd, tmp)
            return _error("stage", "config update could not be staged safely")
        if current:
            try:
                _renameat2(dfd, tmp, name, _RENAME_EXCHANGE)
            except OSError:
                _remove_quiet(dfd, tmp)
                return _error("atomic", "atomic config exchange is unavailable")
            try:
                displaced = _snapshot_at(dfd, tmp)
                if displaced is None or not _same_displaced_snapshot(displaced, current):
                    raise _ConfigIOError("conflict", "config file changed concurrently; reload before saving")
                os.fsync(dfd)
            except (_ConfigIOError, OSError) as e:
                try:
                    _renameat2(dfd, tmp, name, _RENAME_EXCHANGE)
                    os.fsync(dfd)
                except OSError:
                    return _error("rollback", "config update failed and rollback could not be confirmed")
                _remove_quiet(dfd, tmp)
                if isinstance(e, _ConfigIOError):
                    return _error(e.code, e.message)
                return _error("durability", "config update was rolled back after a durability failure")
            try:
                os.unlink(tmp, dir_fd=dfd)
            except OSError:
                try:
                    _renameat2(dfd, tmp, name, _RENAME_EXCHANGE)
                    os.fsync(dfd)
                except OSError:
                    return _error("rollback", "config cleanup failed and rollback could not be confirmed")
                _remove_quiet(dfd, tmp)
                return _error("cleanup", "config update was rolled back after cleanup failed")
            try:
                os.fsync(dfd)
            except OSError:
                pass  # target rename was already durably fsynced; only temp cleanup may replay
        else:
            try:
                os.link(tmp, name, src_dir_fd=dfd, dst_dir_fd=dfd, follow_symlinks=False)
                os.fsync(dfd)
            except FileExistsError:
                _remove_quiet(dfd, tmp)
                return _error("conflict", "config file appeared concurrently; reload before saving")
            except OSError:
                _remove_quiet(dfd, tmp)
                return _error("atomic", "config create could not be committed atomically")
            try:
                os.unlink(tmp, dir_fd=dfd)
            except OSError:
                try:
                    os.unlink(name, dir_fd=dfd)
                    os.fsync(dfd)
                except OSError:
                    return _error("rollback", "config create failed and rollback could not be confirmed")
                _remove_quiet(dfd, tmp)
                return _error("cleanup", "config create was rolled back after cleanup failed")
            try:
                os.fsync(dfd)
            except OSError:
                pass  # the linked target was already durably fsynced
    finally:
        os.close(dfd)
    note = ("created" if current is None else "updated") + (f" ({verr})" if verr else "")
    return {"ok": True, "path": rp, "backup": backup, "created_parent": created_parent,
            "revision": _revision(staged_stat, data), "note": note}


def write_file(path, content, expected_revision=None):
    """Public writer contract; private format overrides are restore-only."""
    return _write_file_impl(path, content, expected_revision=expected_revision)


# ── structured entry ops (safer than raw-file editing for common toggles) ─────

def entry_op(action, path, kind, name, value=None):
    """Add/remove one MCP entry against the exact safely-read revision."""
    typ, editable, reason = classify_path(path)
    if not editable:
        return _error("refused", "refused: " + reason)
    if typ != "json":
        return _error("type", "structured entry ops apply to JSON config files only")
    got = read_file(path)
    if got.get("error"):
        return _error(got.get("code") or "read", got["error"])
    try:
        data = json.loads(got["content"]) if got["exists"] else {}
    except ValueError as e:
        return _error("validation", f"cannot parse config JSON: {e}")
    if not isinstance(data, dict):
        return _error("shape", "config root is not a JSON object")
    if kind == "mcp":
        servers = data.setdefault("mcpServers", {})
        if not isinstance(servers, dict):
            return {"error": "mcpServers is not an object"}
        if action == "add":
            if not isinstance(value, dict):
                return {"error": "add needs a server config object"}
            servers[name] = value
        elif action == "remove":
            servers.pop(name, None)
        else:
            return {"error": f"unsupported action {action!r} for mcp"}
    else:
        return {"error": f"unsupported kind {kind!r} (mcp only, for now)"}
    return write_file(path, json.dumps(data, indent=2) + "\n",
                      expected_revision=got["revision"])


# ── backups / undo ────────────────────────────────────────────────────────────

def _backup_meta(bp):
    """Validated sidecar for one regular backup leaf."""
    try:
        if not _lstat_regular(bp):
            return None
        snap = _snapshot(bp + ".orig")
        if snap is None:
            return None
        raw, _ = _decode(snap["data"])
        data = json.loads(raw)
        orig = data.get("orig")
        rp = _candidate(orig)
        if rp is None or not _readable(orig, rp):
            return None
        return data
    except (OSError, ValueError, TypeError, _ConfigIOError):
        return None


def _backup_orig(bp):
    meta = _backup_meta(bp)
    return meta.get("orig") if meta else None


def list_backups():
    out = []
    try:
        for b in sorted(os.listdir(BACKUP_DIR), reverse=True):
            if b.endswith(".orig"):
                continue
            bp = os.path.join(BACKUP_DIR, b)
            st = _lstat_regular(bp)
            meta = _backup_meta(bp)
            if not st or not meta:
                continue
            out.append({"backup": bp, "orig": meta["orig"], "size": st.st_size,
                        "at": meta.get("at") or time.strftime(
                            "%Y-%m-%d %H:%M:%S", time.localtime(st.st_mtime))})
    except OSError:
        pass
    return out


def restore(backup):
    """Restore exact backup text/newline encoding through the normal write gate."""
    ap = _absolute(backup)
    root = _real(BACKUP_DIR)
    if ap is None or not _path_below(ap, root) or _candidate(ap) != ap:
        return _error("backup", "unknown backup")
    meta = _backup_meta(ap)
    if not meta:
        return _error("backup", "unknown backup")
    try:
        snap = _snapshot(ap)
        if snap is None:
            raise _ConfigIOError("backup", "backup disappeared concurrently")
        content, detected = _decode(snap["data"])
    except _ConfigIOError as e:
        return _error(e.code, "backup could not be read safely")
    encoding = meta.get("encoding") if meta.get("encoding") in ("utf-8", "utf-8-sig") else detected
    newline = meta.get("newline") if meta.get("newline") in ("none", "lf", "crlf", "cr") \
        else _newline_style(content)
    return _write_file_impl(meta["orig"], content, encoding_override=encoding,
                            newline_override=newline)


# ── CLI (read-only surface over the same model) ───────────────────────────────

def _print_files(files, indent="    "):
    import sys
    for f in files:
        extra = ""
        if f.get("entries") is not None:
            extra = "  (%d entries)" % len(f["entries"])
        elif not f["editable"]:
            extra = "  [read-only: %s]" % f["reason"]
        print("%s%-32s %s%s" % (indent, f["rel"], f["kind"], extra))


def cmd_configs(args):
    """configs [list|show <path>|cascade <cwd> [--harness claude|codex] [--home DIR]]
    — read-only surface over the config model. list = every discovered config file
    grouped by scope; show = one recognized file's content; cascade = what a seat
    at <cwd> loads (via physics)."""
    import sys
    args = list(args or [])
    verb = args.pop(0) if args else "list"

    if verb == "list":
        homes = homes_configs()
        print("helm configs — home/user scope:")
        if not homes:
            print("  (none)")
        for h in homes:
            print("  %s  [%s]" % (h["path"], h["provider"]))
            _print_files(h["files"])
        t = tree()
        print("project scope (roots: %s):" % ", ".join(t["config_roots"]))
        def _walk(node):
            if node["files"]:
                print("  " + node["path"])
                _print_files(node["files"])
            for c in node["children"]:
                _walk(c)
        for r in t["roots"]:
            _walk(r)
        return 0

    if verb == "show":
        if not args:
            print("usage: helm configs show <path>", file=sys.stderr)
            return 2
        r = read_file(args[0])
        if r.get("error"):
            print("helm configs: %s" % r["error"], file=sys.stderr)
            return 1
        print("# %s  [%s%s]" % (r["path"], r["type"],
                                "" if r["editable"] else "; read-only: " + r["reason"]),
              file=sys.stderr)
        sys.stdout.write(r["content"])
        return 0

    if verb == "cascade":
        cwd, harness, home_p = None, "claude", None
        while args:
            a = args.pop(0)
            if a == "--harness" and args:
                harness = args.pop(0)
            elif a == "--home" and args:
                home_p = args.pop(0)
            elif not a.startswith("-") and cwd is None:
                cwd = a
            else:
                print("usage: helm configs cascade <cwd> [--harness claude|codex] "
                      "[--home DIR]", file=sys.stderr)
                return 2
        if not cwd or harness not in ("claude", "codex"):
            print("usage: helm configs cascade <cwd> [--harness claude|codex] "
                  "[--home DIR]", file=sys.stderr)
            return 2
        home_p = home_p or os.path.join(HOME, ".codex" if harness == "codex" else ".claude")
        res = resolve(home_p, cwd, harness)
        if res.get("error"):
            print("helm configs cascade: %s" % res["error"], file=sys.stderr)
            return 1
        print(json.dumps(res, indent=2, ensure_ascii=False))
        return 0

    if verb == "edit":
        # edit <path>  (new content on stdin) — backup -> validate -> atomic
        if not args:
            print("usage: helm configs edit <path>   (new content on stdin)", file=sys.stderr)
            return 2
        if sys.stdin.isatty():
            print("helm configs edit: pipe the new content on stdin "
                  "(refusing an interactive empty write)", file=sys.stderr)
            return 2
        r = write_file(args[0], sys.stdin.read())
        if r.get("error"):
            print("helm configs edit: " + r["error"], file=sys.stderr)
            return 1
        print("helm configs: wrote %s (backup: %s)" % (r["path"], r.get("backup") or "none — new file"))
        return 0

    if verb == "backups":
        bs = list_backups()
        if not bs:
            print("helm configs: no backups yet.")
            return 0
        print("helm configs backups (%d, newest first):" % len(bs))
        for b in bs[:30]:
            print("  %s  <- %s" % (b.get("backup", "?"), b.get("orig") or "?"))
        return 0

    if verb == "restore":
        if not args:
            print("usage: helm configs restore <backup-path>", file=sys.stderr)
            return 2
        r = restore(args[0])
        if r.get("error"):
            print("helm configs restore: " + r["error"], file=sys.stderr)
            return 1
        print("helm configs: restored %s (pre-restore backup: %s)"
              % (r["path"], r.get("backup") or "none"))
        return 0

    print("usage: helm configs [list|show <path>|cascade <cwd>|edit <path>|"
          "backups|restore <backup>]", file=sys.stderr)
    return 2
