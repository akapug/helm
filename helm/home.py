#!/usr/bin/env python3
"""The ~/.helm home resolver. HOME-anchored, never cwd-derived — a helm command
invoked from any worktree resolves the same root every time (the Q132/Q133
cwd-independence class).

Env transition law: HELM_* preferred, legacy MELD_* accepted as fallback (the
env2 new-with-old-fallback pattern). ~/.helm is the durable USER knowledge
corpus; engine runtime state (any tool's ~/.config/<tool>) stays out of it.
"""
import os

# Per-project concept-category chain (the buildr .local family, lifted to a
# user-level product-namespace home). Order is the organic dev cycle:
# priors art feeds prd, build happens in the repo, evals then journal then archive.
PROJECT_CATEGORIES = (
    "premises", "heuristics", "lexicon", "prd", "journal", "evals", "archive",
)

# Global-only categories: shared prior-art corpus, the one operator profile,
# cross-project truths, reflexes (fire regardless of project).
GLOBAL_CATEGORIES = (
    "priors", "know-your-user", "premises", "heuristics", "lexicon",
    "reflexes", "archive",
)

GLOBAL = "_global"


def env(name, default=None):
    """HELM_<name> preferred; legacy MELD_<name> accepted as fallback."""
    v = os.environ.get("HELM_" + name)
    if v is None:
        v = os.environ.get("MELD_" + name)
    return default if v is None else v


def env_pair(name, companion):
    """A value plus metadata selected atomically from one env namespace.
    A preferred HELM value must never inherit stale MELD provenance."""
    value = os.environ.get("HELM_" + name)
    if value is not None:
        return value, os.environ.get("HELM_" + companion)
    value = os.environ.get("MELD_" + name)
    return value, os.environ.get("MELD_" + companion)


# The harness session-id vars, in resolution order. CLAUDE_CODE_SESSION_ID is
# the REAL var Claude Code exports; CLAUDE_SESSION_ID is the legacy/hook-injected
# alias (the SessionStart join hook passes session_id explicitly, so it worked
# even while a bare CLI post fell through to the anon floor — owner-caught
# 2026-07-21: a manual `helm chat post` posted as 'agent', and the a2a per-session
# cursor silently no-op'd for every claude-code session). CODEX_SESSION_ID is the
# codex seat. One resolver so no call site misses the real var again.
_SESSION_ENV = ("CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID", "CODEX_SESSION_ID")


def session_id():
    """The current harness session id across every harness that sets one, or
    None so callers fall through to their auto-name/anon floor."""
    for k in _SESSION_ENV:
        v = os.environ.get(k)
        if v:
            return v
    return None


def helm_home():
    """The root. HELM_HOME env else ~/.helm — anchored on $HOME, never cwd."""
    override = env("HOME")
    if override:
        return os.path.abspath(os.path.expanduser(override))
    return os.path.join(os.path.expanduser("~"), ".helm")


def global_dir():
    return os.path.join(helm_home(), GLOBAL)


def project_dir(name):
    return os.path.join(helm_home(), name)


def registry_path():
    """The master project list (the auto-map output) — pure PROJECTION,
    rebuildable from a re-scan, safe to regenerate."""
    return os.path.join(global_dir(), "registry.json")


def authored_path():
    """The AUTHORED registry layer (notes/edges/aliases/external/retired),
    keyed by project name. Unrebuildable — registry.json can be wiped and
    re-synced, this file cannot."""
    return os.path.join(global_dir(), "registry-authored.json")


def adopted_memory_dir():
    """The already-live personal-knowledge store this user's agents write today:
    ~/.claude/projects/<slug-of-home>/memory (prior-*.md / lex-*.md / bulk).
    helm ADOPTS it in place — same files, one more resolver — so existing hooks
    and helm always see one store."""
    home = os.path.expanduser("~")
    import re
    slugged = re.sub(r"[^A-Za-z0-9-]", "-", home.replace("/", "-"))
    return os.path.join(home, ".claude", "projects", slugged, "memory")


def claude_memory_dir_for(path):
    """The claude per-project memory dir for an arbitrary project path (exists
    only if claude sessions ran there)."""
    import re
    slugged = re.sub(r"[^A-Za-z0-9-]", "-", path.replace("/", "-"))
    return os.path.join(os.path.expanduser("~"), ".claude", "projects", slugged, "memory")


def scaffold_global():
    """Ensure the _global chain exists. Idempotent, additive. Also seeds the
    shipped default reflex pack — a no-op for every id already present, so an
    operator edit or retire is never overwritten (reflex.seed_defaults law)."""
    g = global_dir()
    for c in GLOBAL_CATEGORIES:
        os.makedirs(os.path.join(g, c), exist_ok=True)
    from . import reflex
    reflex.seed_defaults()
    return g


def scaffold_project(name):
    """Ensure one project's chain exists. Idempotent, additive — never deletes.
    Returns the project dir. A symlinked project home (adoption of an existing
    external chain, e.g. mission-control -> ~/.mc/mission-control) is honored
    and never re-scaffolded inside."""
    p = project_dir(name)
    if os.path.islink(p):
        return p
    for c in PROJECT_CATEGORIES:
        os.makedirs(os.path.join(p, c), exist_ok=True)
    return p
