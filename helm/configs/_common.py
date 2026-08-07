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
# the harness vocabulary — physics.py owns it (it is what raises on an unknown
# one); a second literal here is how the two silently drift apart.
HARNESSES = physics.HARNESSES
# seat homes are only ever minted for these two (`helm seat add` writes
# <helm>/_global/seats/<family>/{claude,pi}) — codex seats carry no isolated home.
_SEAT_HARNESSES = ("claude", "pi")


def _rp(p):
    """realpath+expanduser — _classify._real's body, inlined because
    _classify imports FROM this module and the other direction would cycle."""
    return os.path.realpath(os.path.expanduser(os.fsdecode(p)))


def _typed_home_roots():
    """Every import-time credential home PAIRED WITH THE HARNESS THAT OWNS IT.

    THE PROVENANCE IS FREE HERE, AND IT WAS BEING THROWN AWAY. Each glob knows
    its harness BY CONSTRUCTION — `~/.codex-homes/*` is codex because of the
    directory it was globbed out of, not because the letters "codex" appear
    somewhere in the resulting string. The old code built this same list,
    flattened it to bare paths, and then had `harness_for` RECONSTRUCT the
    harness by parsing path spelling: a fact the code held two lines earlier,
    re-derived by guessing.

    That guess failed three times in a row, each fix correct and none final:
      1. `"pi" in hp` over the whole path — a random mkdtemp suffix decided the
         answer, reddening the full suite at a measured 0.497% of runs.
      2. a dotted-component rule borrowed from the leaf matcher — an unrelated
         dotted ANCESTOR captured it (`/tmp/.pi-cache/u/.claude` -> pi).
      3. the leaf matcher stripping a leading dot — the exact dotted pass
         REJECTED `.pi-cache`, then the fallback let it straight back in.
    Every round enumerated one more case; every round a reviewer found a case
    outside the enumeration. That is `per-case-handler-spiral` and the cure is
    not a fourth rule, it is not having to guess.
    """
    pairs = [(f"{HOME}/.claude", "claude"), (f"{HOME}/.codex", "codex"),
             (f"{HOME}/.pi/agent", "pi")]
    for h in HARNESSES:                       # per-account homes
        pairs += [(p, h) for p in sorted(glob.glob(f"{HOME}/.{h}-homes/*"))]
    seats = os.path.join(_HELM_HOME, "_global", "seats")
    for h in _SEAT_HARNESSES:                 # helm's own isolated seat homes
        pairs += [(p, h) for p in sorted(glob.glob(os.path.join(seats, "*", h)))]
    return pairs


def _home_root_tables():
    """(HOME_ROOTS, HOME_ROOT_HARNESS) for the CURRENT HOME/_HELM_HOME.

    ONE function builds both so they cannot drift, and it is re-runnable so a
    fixture can repoint HOME and rebuild THROUGH THIS CODE instead of
    hand-assembling an equivalent dict. That is not a convenience: a test that
    builds the table itself proves only that its own comprehension works, and
    a mutation to the real keying below stays invisible. Measured — keying by
    raw path instead of realpath SURVIVED a mutation round for exactly that
    reason, on a suite that was otherwise binding every rule here.
    """
    pairs = _typed_home_roots()
    # HOME_ROOTS STAYS A BARE LIST OF PATHS, and the reason is worth stating
    # because the obvious refactor is to retag it as pairs and that is WRONG.
    # It is an AUTHORIZATION list, read by gates that only ever ask "is this
    # path allowed": _classify's write gate, _io's 0o600-vs-0o644 mode choice,
    # `_under(rp, CWD_ROOTS + HOME_ROOTS)` concatenation, the "home_roots"
    # field SERIALIZED into the /api/configs response (a wire format), and ~20
    # rebind sites across seven test files. Exactly one consumer wants the
    # harness, so provenance goes in a PARALLEL table and the gate does not move.
    #
    # The table is keyed by REALPATH because callers ask with one: homes_configs
    # resolves `h = _real(raw)` before every lookup, so a raw-keyed table misses
    # precisely when a home is a symlink.
    return [p for p, _ in pairs], {_rp(p): h for p, h in pairs}


# credential-home roots (project-scoped configs never live here, but home/user-scope
# settings do: <home>/settings.json, <home>/.claude.json, <home>/config.toml, …).
HOME_ROOTS, HOME_ROOT_HARNESS = _home_root_tables()


def _seat_home_harness(rp):
    """The harness of a seat home minted AFTER import, or None.

    `helm seat add` creates <helm>/_global/seats/<family>/<harness> (and, for
    instances, <helm>/_global/seats/<family>/instances/<seat>/<harness>) IN
    THE SAME PROCESS that then reads configs, so the import-time glob cannot
    have seen it. The terminal component is the harness because the LAUNCHER
    WROTE IT THAT WAY — this is reading a tagged root, not parsing a spelling:
    the answer is rejected unless the whole enclosing shape matches too.

    Mirrors _classify._is_seat_home, which admits the same two shapes per call
    for the write gate. It differs in one deliberate way: that gate hardcodes
    `claude`, while provenance also answers for the `pi` seat homes the
    HOME_ROOTS glob has always collected.
    """
    leaf = os.path.basename(rp)
    if leaf not in _SEAT_HARNESSES:
        return None
    seats = _rp(os.path.join(_HELM_HOME, "_global", "seats"))
    up2 = os.path.dirname(os.path.dirname(rp))      # seats | instances
    if up2 == seats:                                # seats/<family>/<harness>
        return leaf
    return (leaf if os.path.basename(up2) == "instances"     # …/instances/<seat>/<harness>
            and os.path.dirname(os.path.dirname(up2)) == seats else None)


def harness_for(home_path):
    """The harness a home belongs to, or None when we genuinely do not know.

    A LOOKUP AGAINST TAGGED ROOTS. Never a guess about how the path is spelled,
    which is the whole point — see _typed_home_roots for the three rounds of
    increasingly clever spelling rules this replaces, and for why a fourth one
    was never going to be the last.

    Three sources, in order, and they are not heuristics — each is a set some
    other part of helm already treats as authoritative:

    1. HOME_ROOT_HARNESS — the import-time globs, each tagged by the glob that
       produced it.
    2. A direct child of ~/.claude-homes / ~/.codex-homes / ~/.pi-homes,
       computed PER CALL. This is exactly the dynamic shape _resolve._allowed_home
       admits for homes created after import, so provenance and authorization
       answer for the same set.
    3. A seat home (_seat_home_harness) — likewise per call, mirroring
       _classify._is_seat_home.

    ANYTHING ELSE RETURNS None, and callers must handle it. The old code
    returned "claude" for every unrecognized path, which is why a mkdtemp
    suffix could silently mislabel a home instead of failing: a default that
    is right most of the time hides the cases where it is wrong. Note that an
    honest None is NOT free — physics.physics_report RAISES on an unknown
    harness, so every caller screens against HARNESSES before passing it on.
    """
    if not home_path:
        return None                 # realpath("") is the CWD — never a home
    rp = _rp(home_path)
    hit = HOME_ROOT_HARNESS.get(rp)
    if hit:
        return hit
    parent = os.path.dirname(rp)
    for h in HARNESSES:
        if parent == _rp(f"{HOME}/.{h}-homes"):
            return h
    return _seat_home_harness(rp)


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
    ".pi/settings.json": ("json", "pi", "settings"),
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
# macOS spells the same dirfd-relative primitive renameatx_np(RENAME_SWAP)
# (atomic on APFS; an unsupporting volume errors and the writer's existing
# "atomic" error arm fires exactly as on a renameat2-less Linux). Darwin's
# RENAME_SWAP == 2 == Linux's RENAME_EXCHANGE, so call sites carry unchanged.
_RENAME_NOREPLACE = 1
_RENAME_EXCHANGE = 2
_LIBC = ctypes.CDLL(None, use_errno=True)
_RENAMEAT2 = getattr(_LIBC, "renameat2", None)
if _RENAMEAT2 is None:
    _RENAMEAT2 = getattr(_LIBC, "renameatx_np", None)
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
