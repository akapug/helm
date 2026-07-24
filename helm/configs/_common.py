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

from .. import physics

HOME = os.path.expanduser("~")
BACKUP_DIR = os.path.join(HOME, ".cache", "helm", "config-backups")

# cwd roots to browse for project-scoped configs (colon-separated env override;
# the same env convention catalog.py uses).
CWD_ROOTS = [r for r in os.environ.get(
    "HELM_CONFIG_ROOTS", f"{HOME}/dev").split(":") if r]
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
