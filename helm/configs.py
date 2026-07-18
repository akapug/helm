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
import glob
import json
import os
import shutil
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
# credential-home roots (project-scoped configs never live here, but home/user-scope
# settings do: <home>/settings.json, <home>/.claude.json, <home>/config.toml, …).
HOME_ROOTS = ([f"{HOME}/.claude", f"{HOME}/.codex"]
              + sorted(glob.glob(f"{HOME}/.claude-homes/*"))
              + sorted(glob.glob(f"{HOME}/.codex-homes/*")))

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


def _real(p):
    return os.path.realpath(os.path.expanduser(p))


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
    # home/user-scope: the recognized basename sits DIRECTLY in a home root
    if base in _HOME_FILES:
        parent = _real(os.path.dirname(rp))
        if any(parent == _real(h) for h in HOME_ROOTS):
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
    return "other"


def _under(path, roots):
    rp = _real(path)
    return any(rp == _real(r) or rp.startswith(_real(r).rstrip("/") + "/") for r in roots)


def _is_plugin_or_managed(path):
    rp = _real(path)
    if any(rp.startswith(_real(m)) for m in MANAGED_DIRS):
        return True
    # plugin-provided config lives under a home's plugins/ tree — read-only here.
    # Match the actual plugins install dir, not any directory merely NAMED 'plugins'
    # (a project's own plugins/ dir is a legit editable location).
    return any(rp.startswith(_real(os.path.join(h, "plugins")) + os.sep) for h in HOME_ROOTS)


def classify_path(path):
    """(type, editable, reason). editable iff a RECOGNIZED config file (by filename/
    rel-path, not just extension) under an allowlisted root, not a credential store,
    not plugin/managed. Non-existent files are still editable (create-if-absent) as
    long as the parent (or its parent) exists."""
    rp = _real(path)
    typ = _ext_type(rp)
    if os.path.basename(rp) in _DENY_FILES:
        return typ, False, "credential/token store — never editable"
    if not _is_recognized_config(rp):
        return typ, False, "not a recognized config file"
    if not _under(rp, CWD_ROOTS + HOME_ROOTS):
        return typ, False, "outside the allowlisted config roots"
    if _is_plugin_or_managed(rp):
        return typ, False, "plugin/managed-provided — read-only"
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
        if os.path.isfile(p):
            _, editable, reason = classify_path(p)
            out.append({"rel": rel, "path": p, "type": typ, "harness": harness,
                        "kind": kind, "editable": editable, "reason": reason})
    # .claude/rules/*.md and .claude/{skills,commands,agents}/ counts
    rules = sorted(glob.glob(os.path.join(cwd, ".claude", "rules", "*.md")))
    for r in rules:
        _, editable, reason = classify_path(r)
        out.append({"rel": os.path.relpath(r, cwd), "path": r, "type": "md",
                    "harness": "claude", "kind": "rule", "editable": editable, "reason": reason})
    for kind in ("skills", "commands", "agents"):
        d = os.path.join(cwd, ".claude", kind)
        if os.path.isdir(d):
            names = [e for e in sorted(os.listdir(d)) if not e.startswith(".")]
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
    for dirpath, dirnames, filenames in os.walk(root):
        depth = dirpath.rstrip("/").count("/") - root_depth
        if depth >= maxdepth:
            dirnames[:] = []
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS and not d.startswith(".worktree")]
        names = set(filenames)
        if names & {".mcp.json", "CLAUDE.md", "CLAUDE.local.md", "AGENTS.md"}:
            hits.add(dirpath)
        if ".claude" in dirnames and os.path.isdir(os.path.join(dirpath, ".claude")):
            cd = os.path.join(dirpath, ".claude")
            if any(os.path.exists(os.path.join(cd, x)) for x in
                   ("settings.json", "settings.local.json", "CLAUDE.md", "rules")):
                hits.add(dirpath)
    return hits


def tree(root=None, extra_cwds=None):
    """A nested cwd tree (under root) of directories that hold project configs, each
    node carrying its config files. extra_cwds (e.g. live session cwds) are folded in
    so a cwd with configs is shown even outside the scanned root."""
    roots = [_real(r) for r in ([root] if root else CWD_ROOTS)]
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
    """Home/user-scope config files that exist, per home dir — the top of the cascade."""
    out = []
    for h in HOME_ROOTS:
        if not os.path.isdir(h):
            continue
        files = []
        for rel, (typ, harness, kind) in _HOME_FILES.items():
            p = os.path.join(h, rel)
            if os.path.isfile(p):
                _, editable, reason = classify_path(p)
                files.append({"rel": rel, "path": p, "type": typ, "harness": harness,
                              "kind": kind, "editable": editable, "reason": reason})
        # sibling default state file: ~/.claude.json for the ~/.claude home
        if os.path.basename(h) == ".claude":
            sib = os.path.join(os.path.dirname(h), ".claude.json")
            if os.path.isfile(sib):
                _, editable, reason = classify_path(sib)
                files.append({"rel": "../.claude.json", "path": sib, "type": "json",
                              "harness": "claude", "kind": "state", "editable": editable,
                              "reason": reason})
        if files:
            out.append({"home": os.path.basename(h), "path": h,
                        "provider": "codex" if "codex" in h else "claude", "files": files})
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


def resolve(home_path, cwd, harness):
    """physics.py's resolved seat — what (home, cwd) WOULD load, with source +
    precedence, plus MCP winner/shadowed annotation. This IS the cascade view
    (root→…→cwd + the home/user layer)."""
    return _annotate_mcp_shadows(physics.physics_report(home_path, cwd or None, harness))


# ── read / edit (safety-first) ────────────────────────────────────────────────

def read_file(path):
    """Raw content + type + editability of one RECOGNIZED config file. Content is
    served ONLY for a recognized config under an allowlisted root — never an
    arbitrary file (the review's #1: an ungated read returned /etc/passwd,
    ~/.aws/…, credential stores). A plugin/managed recognized config is readable
    (view-only) but not editable."""
    rp = _real(path)
    typ, editable, reason = classify_path(rp)
    exists = os.path.isfile(rp)
    readable = (os.path.basename(rp) not in _DENY_FILES
                and _is_recognized_config(rp) and _under(rp, CWD_ROOTS + HOME_ROOTS))
    if not readable:
        return {"path": rp, "type": typ, "editable": False,
                "reason": reason or "not a recognized config file", "exists": exists,
                "content": "", "error": "refused: only recognized config files are readable"}
    content, err = "", None
    if exists:
        try:
            with open(rp, encoding="utf-8", errors="replace") as f:
                content = f.read(1_000_000)  # 1MB cap — config files are small
        except OSError as e:
            err = str(e)
    return {"path": rp, "type": typ, "editable": editable, "reason": reason,
            "exists": exists, "content": content, "error": err}


def _validate(typ, content):
    """(ok, error). Reject an edit that would make a parseable type unparseable."""
    if typ == "json":
        try:
            json.loads(content)
        except ValueError as e:
            return False, f"invalid JSON: {e}"
    elif typ == "toml":
        if tomllib is None:
            return True, "toml not validated (python < 3.11)"
        try:
            tomllib.loads(content)
        except Exception as e:
            return False, f"invalid TOML: {e.__class__.__name__}: {e}"
    return True, None


def _backup(rp):
    """Copy the current file to the backup dir with a sidecar recording its origin;
    return the backup path (or None if the file does not exist yet — a create has
    nothing to back up). The sidecar (not the filename) carries the orig path, so
    restore never has to reverse a fragile path-encoding, and the ns suffix makes
    same-second backups of one file distinct."""
    if not os.path.isfile(rp):
        return None
    os.makedirs(BACKUP_DIR, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime()) + f"-{time.time_ns() % 1_000_000_000:09d}"
    tag = os.path.basename(rp)
    dest = os.path.join(BACKUP_DIR, f"{stamp}__{tag}")
    shutil.copy2(rp, dest)
    with open(dest + ".orig", "w") as f:
        json.dump({"orig": rp, "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}, f)
    return dest


def write_file(path, content):
    """Backup → validate → atomic write. Refuses non-recognized / plugin / managed /
    out-of-root paths. Returns {ok, backup, note} or {error}."""
    rp = _real(path)
    typ, editable, reason = classify_path(rp)
    if not editable:
        return {"error": f"refused: {reason} ({rp})"}
    ok, verr = _validate(typ, content)
    if not ok:
        return {"error": f"refused: {verr} — no change written"}
    parent = os.path.dirname(rp)
    created_parent = False
    if not os.path.isdir(parent):
        try:
            os.makedirs(parent, exist_ok=True)
            created_parent = True
        except OSError as e:
            return {"error": f"could not create parent dir: {e}"}
    backup = _backup(rp)
    existed = os.path.isfile(rp)
    mode = 0o600 if os.path.basename(rp) in (".claude.json", ".credentials.json") else 0o644
    tmp = f"{rp}.helm-tmp.{os.getpid()}"
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())  # durable before the rename (no zero-length inode on power loss)
        os.replace(tmp, rp)
    except OSError as e:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return {"error": f"write failed: {e}"}
    note = ("created" if not existed else "updated") + (f" ({verr})" if verr else "")
    return {"ok": True, "path": rp, "backup": backup, "created_parent": created_parent,
            "note": note}


# ── structured entry ops (safer than raw-file editing for common toggles) ─────

def entry_op(action, path, kind, name, value=None):
    """Add / remove / toggle one MCP server (claude .mcp.json or .claude.json
    mcpServers) or one hook — structured, so the common case never hand-edits JSON.
    action: add|remove|enable|disable. Returns write_file's result."""
    rp = _real(path)
    typ, editable, reason = classify_path(rp)
    if not editable:
        return {"error": f"refused: {reason}"}
    if typ != "json":
        return {"error": "structured entry ops apply to JSON config files only"}
    data = {}
    if os.path.isfile(rp):
        try:
            with open(rp, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError) as e:
            return {"error": f"cannot parse {rp}: {e}"}
    if not isinstance(data, dict):
        return {"error": "config root is not a JSON object"}
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
    return write_file(rp, json.dumps(data, indent=2) + "\n")


# ── backups / undo ────────────────────────────────────────────────────────────

def _backup_orig(bp):
    """The original path a backup came from (from its sidecar)."""
    try:
        with open(bp + ".orig") as f:
            return json.load(f).get("orig")
    except (OSError, ValueError):
        return None


def list_backups():
    out = []
    try:
        for b in sorted(os.listdir(BACKUP_DIR), reverse=True):
            if b.endswith(".orig"):
                continue
            bp = os.path.join(BACKUP_DIR, b)
            if not os.path.isfile(bp):
                continue
            st = os.stat(bp)
            out.append({"backup": bp, "orig": _backup_orig(bp) or "(unknown)",
                        "size": st.st_size,
                        "at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.st_mtime))})
    except OSError:
        pass
    return out


def restore(backup):
    """Restore a backup to its original path (validated + re-backed-up first)."""
    bp = _real(backup)
    if not (bp.startswith(_real(BACKUP_DIR) + os.sep) and os.path.isfile(bp)):
        return {"error": "unknown backup"}
    orig = _backup_orig(bp)
    if not orig:
        return {"error": "backup has no recorded origin path"}
    try:
        with open(bp, encoding="utf-8") as f:
            content = f.read()
    except OSError as e:
        return {"error": f"cannot read backup: {e}"}
    return write_file(orig, content)


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
        print(json.dumps(resolve(home_p, cwd, harness), indent=2, ensure_ascii=False))
        return 0

    print("usage: helm configs [list|show <path>|cascade <cwd>]", file=sys.stderr)
    return 2
