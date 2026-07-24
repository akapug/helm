#!/usr/bin/env python3
"""The ~/.helm home resolver. HOME-anchored, never cwd-derived — a helm command
invoked from any worktree resolves the same root every time (the
cwd-independence class).

Env transition law: HELM_* preferred, legacy MELD_* accepted as fallback (the
env2 new-with-old-fallback pattern). ~/.helm is the durable USER knowledge
corpus; engine runtime state (any tool's ~/.config/<tool>) stays out of it.
"""
import os
import re

# A legitimate seat name is an IDENTIFIER: codex-2, reviewer-1, ds4pro —
# [A-Za-z0-9._-], bounded. HELM_CHAT_NAME is the one unvalidated join seam every
# chat surface trusts (roster keys, chat from/tfrom/rfrom, hook pane names,
# todos, codex capacity, …); it is validated HERE, at the source, exactly once,
# so a control-char name never enters the system rather than being laundered at
# each of a dozen sinks forever (the ESC/bidi display-launder class, closed at
# the owner layer — decision-spirit #15).
_SEAT_NAME_RE = re.compile(r"\A[A-Za-z0-9._-]{1,64}\Z")


class SeatNameError(ValueError):
    """A hostile HELM_CHAT_NAME reached the join seam. The message names the
    offending bytes SAFELY (ASCII-escaped via _safe_name) — the raw ESC/bidi
    payload never rides the error onward into a terminal or log."""


def _safe_name(raw):
    """The offending name rendered as pure printable ASCII — ESC becomes \\x1b,
    a bidi override U+202E becomes \\u202e — so the rejection message itself
    can never carry the control/format payload it is reporting on. Bounded, so
    a pathologically long name cannot flood the error."""
    return str(raw)[:80].encode("unicode_escape").decode("ascii")


def chat_name():
    """The seat identity from HELM_CHAT_NAME (legacy MELD_CHAT_NAME) — THE one
    validated ingestion seam for the seat name. Every os.environ read of this
    var routes here (chat.whoname, seats.derive_seat, human.operator_name,
    launch); no other module reads it raw (tests/test_display_launder_tripwire
    enforces that with a source grep).

    Returns the name when it is a legitimate seat identifier ([A-Za-z0-9._-],
    like codex-2 / reviewer-1 / ds4pro); None when unset OR empty (callers
    fall through to their auto-name floor, preserving the old `if name:` /
    `or "owner"` semantics); and RAISES SeatNameError when the name carries ESC
    / C0-C1 controls / Unicode bidi overrides (U+202A-E, U+2066-9) / any other
    format-Cf. A control-char seat name is never legitimate, so it is REJECTED
    at the source — it never becomes a roster key, a chat from-field, a hook
    pane name, a todo row, or any future sink."""
    raw = env("CHAT_NAME")
    if not raw:                     # unset or explicitly empty -> fall through
        return None
    if not _SEAT_NAME_RE.match(raw):
        raise SeatNameError(
            "HELM_CHAT_NAME is not a legitimate seat name: '%s' "
            "(a seat name is [A-Za-z0-9._-], like codex-2) — refusing to join "
            "or post under it" % _safe_name(raw))
    return raw


def validate_seat_arg(raw):
    """The SECOND seat-name ingestion beside the env seam: a name supplied as a
    CLI arg (helm launch/spawn --seat). Same rule as chat_name — None when empty
    (caller falls through), the name when a legit identifier, SeatNameError on
    ESC/control/bidi — so a hostile --seat can never be exported as the child's
    HELM_CHAT_NAME or written as a roster key (the source-grep tripwire guards
    env reads only; this closes the arg path)."""
    if not raw:
        return None
    if not _SEAT_NAME_RE.match(raw):
        raise SeatNameError(
            "--seat is not a legitimate seat name: '%s' "
            "(a seat name is [A-Za-z0-9._-], like codex-2) — refusing to "
            "launch under it" % _safe_name(raw))
    return raw


# Per-project concept-category chain (a .local product-namespace family, lifted
# to a user-level home). Order is the organic dev cycle:
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


def seat_claude_roots():
    """Every minted fleet seat's Claude projects root, family and instance.
    These are harness transcript homes, not a second store."""
    root = os.path.join(global_dir(), "seats")
    out = []
    try:
        families = os.listdir(root)
    except OSError:
        return out
    for family in families:
        d = os.path.join(root, family)
        p = os.path.join(d, "claude", "projects")
        if os.path.isdir(p):
            out.append(os.path.realpath(p))
        inst = os.path.join(d, "instances")
        try:
            names = os.listdir(inst)
        except OSError:
            continue
        for name in names:
            p = os.path.join(inst, name, "claude", "projects")
            if os.path.isdir(p):
                out.append(os.path.realpath(p))
    return sorted(set(out))


def cv_env(base=None):
    """Environment for any cv subprocess: preserve caller overrides and add all
    fleet seat transcript roots through CV's generic multi-root contract."""
    e = dict(os.environ if base is None else base)
    roots = [p for p in e.get("CLUSTERVISION_CLAUDE_ROOTS", "").split(os.pathsep)
             if p]
    roots.extend(seat_claude_roots())
    if roots:
        e["CLUSTERVISION_CLAUDE_ROOTS"] = os.pathsep.join(dict.fromkeys(roots))
    return e


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
    external chain via symlink) is honored and never re-scaffolded inside."""
    p = project_dir(name)
    if os.path.islink(p):
        return p
    for c in PROJECT_CATEGORIES:
        os.makedirs(os.path.join(p, c), exist_ok=True)
    return p
