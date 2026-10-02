#!/usr/bin/env python3
"""helm tidy — the estate janitor: census the whole claude-config estate,
reconcile every home's hooks + MCP servers to a NAMED canonical set, and gc
the git worktrees — one umbrella verb, dry-run by DEFAULT everywhere.

This is skillsync.py's shape, widened from skills to the rest of the estate.
The laws it reuses verbatim (see skillsync's module docstring for the why):

  * CENSUS via skillsync.config_dirs() — the ONE discovery of every claude
    config dir on this host (credhomes deduped on realpath, the default
    ~/.claude, seats + seat-instances, smoke dirs skipped). No second census.
  * DRY-RUN PURITY — a plan touches nothing; only --apply mutates.
  * BACKUP-FIRST + ROLLBACK — every mutate copies the pre-image into
    ~/.env-premerge-backup FIRST (the backup IS the original), writes the new
    bytes atomically (tmp+rename, pk.atomic_write), then re-reads and proves
    the result is a SUPERSET of what was there before — a foreign hook, an
    existing MCP server, anything — or restores the backup. Never lose a config.
  * IDEMPOTENT — a clean estate reports zero changes; a second --apply is a
    no-op.
  * FAIL-CLOSED — a settings.json that will not parse is REPORTED and SKIPPED,
    never mutated; one unreadable home never turns into a partial destructive
    apply across the rest.

The reconcile is ADDITIVE, never subtractive: hooks sync ADDS every missing
canonical hook and REPORTS strays (repo-hygiene, herdr, orca — legit on the
host home) without ever removing them; mcp sync ADDS a missing canonical
server only when a concrete config is in hand, else reports the gap. The
canonical sets are named constants with env overrides, exactly like
skillsync.canonical(): HELM_HOOKS_CANONICAL, HELM_MCPS_CANONICAL (then the
private file MCPS_PRIVATE, refused unless only its owner can reach it).

A SEAT IS A FULL AGENT (task/3089). Every seat config dir is reconciled to the
same hook set as a home, planned for the same MCP servers (a seat whose launch
runs a non-claude harness is EXCLUDED and counted as excluded, never as
canonical; an unsure seat is planned), and linked to
the host's global instructions (skillsync.link_instructions). The seat mint
door runs the same pieces for a new dir (seat_launch_assets._write_launch_assets),
so a seat is born full and tidy only has to keep it so.

Worktree gc COMPOSES `helm work gc` for the lease-aware lane rooms (never
re-implements its rescue logic) and adds the estate-wide sweep the lane gc
does not cover: orphan `worktree-*` / `lane/*` branch stubs and stray registered
worktrees (wf_*, agent-*). RESCUE-DIRTY-FIRST (commit --no-verify onto the
worktree's own branch before any removal), NEVER touch a LOCKED or OCCUPIED
(any live process cwd) worktree, and NEVER remove work that is ahead of the
base (unmerged unique commits) — ahead>0 is blocked, not prunable.
"""
import difflib
import glob
import json
import os
import re
import shutil
import sys
import time

from . import home as _home
from . import hooks as _hooks
from . import pk
from . import registry
from . import skillsync
from . import vcs

BACKUP_ROOT = os.path.join(os.path.expanduser("~"), ".env-premerge-backup")


# ---------------------------------------------------------------------------
# the canonical hook set — the survey's proposed_canonical_hooks, NAMED
# ---------------------------------------------------------------------------
# One tuple per canonical hook: (event, matcher, helm-args, timeout). The
# helm-args string is BOTH the command helm runs and the entry's own-marker
# (unique within its event group, so a PostToolUse `record` and a PostToolUse
# `chat deliver` never collide). matcher None = no matcher key (fires on every
# invocation of the event); "*" = the all-tools matcher the delivery lane rides.
CANONICAL_HOOKS = (
    ("UserPromptSubmit",     None, "inject --hook-json",         10),
    # Installed recorder budgets, not record.HOOK_SPECS' logical dispatcher 5.
    # Sync must preserve deployed_spec's 10s shell / 15s native deadlines.
    ("PostToolUse",          None, "record --hook-json",         10),
    ("PostToolUseFailure",   None, "record --hook-json",         10),
    ("PostToolUse",          "*",  "chat deliver --hook-json",    2),
    ("SessionStart",         "*",  "chat join --hook-json",       5),
    # 20, NOT 5, AND `hooks.SPECS` IS WHY — see the derivation recorded there.
    # This tuple and SPECS are two representations of one fact, and `sync
    # --apply` installs from THIS one, so a budget corrected only in SPECS is
    # silently rewritten back on the next sync. A parity arm in
    # tests/test_envtidy.py now fails when the two disagree.
    ("Stop",                 None, "chat stop-guard --hook-json", 20),
    ("PreCompact",           None, "handoff check --hook-json",   5),
    ("SessionEnd",           None, "handoff check --hook-json",   5),
)

# NO SEAT SUBSET. A seat is a full agent (owner ruling, task/3089), treated
# like any other agent on the host: every config dir, home or seat, is
# reconciled to the one canonical set. A delivery-only subset here would
# disagree with the mint door — hooks.SEAT_SPECS is hooks.SPECS, so every seat
# is BORN with inject and both handoff producers — and would read a seat that
# lacks the recorder legs as complete.


def _load_hook_override():
    """HELM_HOOKS_CANONICAL: a path to a JSON file of
    [{event, matcher?, args, timeout?}] that REPLACES CANONICAL_HOOKS — the
    exact skillsync.canonical() env-override seam, so an operator can pin the
    estate's hook canon without editing helm. Fail-CLOSED: a set path that
    will not parse raises, rather than silently reverting to the default."""
    p = _home.env("HOOKS_CANONICAL")
    if not p:
        return None
    with pk.open_regular(os.path.expanduser(p), encoding="utf-8") as f:
        spec = json.load(f)
    out = []
    for e in spec:
        out.append((e["event"], e.get("matcher"), e["args"],
                    int(e.get("timeout", 5))))
    return tuple(out)


def canonical_hooks():
    """The canonical hook set for every config dir — credhomes, the default
    ~/.claude and every seat (HELM_HOOKS_CANONICAL overrides). Mirrors
    skillsync.canonical()."""
    return _load_hook_override() or CANONICAL_HOOKS


def hooks_for(label):
    """(kind, canonical-hooks) for one config dir. The kind is reported; the
    set is the SAME full canonical set for a seat as for a home."""
    return ("seat" if label.startswith("seat:") else "home"), canonical_hooks()


# ---------------------------------------------------------------------------
# the canonical MCP set
# ---------------------------------------------------------------------------
# name -> server config (or None = report-only). EMPTY by default: the public
# tree names no host-specific MCPs. On a live host the canonical set is whatever
# that deployment's agents already reach via ENABLED PLUGINS (survey
# mcp_variance) — minting a RAW mcpServers entry for one would create the exact
# duplicate-shadow the survey warns about. mcp sync therefore only ADDS a raw
# server when (a) the name is missing from the home's EFFECTIVE set (not provided
# by any plugin or raw entry) AND (b) a concrete config is in hand. The canonical
# names are supplied HOST-LOCAL, never shipped (see canonical_mcps). Anything
# else is surfaced to the owner, never guessed (fail-closed).
#
# THE CONCRETE CONFIGS LIVE IN A PRIVATE FILE, never in the registry. A server
# config can carry a credential (an Authorization header, an env key), and the
# authored registry is shared state that other tools read and print. So the
# host's configs sit in MCPS_PRIVATE, a file only its owner may read, and a
# file anyone else can reach is REFUSED by name before one byte is parsed.
MCPS_PRIVATE = os.path.join(os.path.expanduser("~"), ".config", "helm",
                            "mcps-canonical.json")
_OFF = ("off", "0", "no", "false")

# WHICH SEATS TAKE THE SERVERS IS A HARNESS QUESTION, NOT A FAMILY ONE. A
# family names the MODEL behind a seat; the servers are read by the HARNESS
# the launch runs. MEASURED (task/3089): every codex-family seat's launch.sh
# runs the claude harness (HELM_AGENT_HARNESS=claude, CLAUDE_CONFIG_DIR = the
# seat's own claude dir), so a family-name exclusion kept Claude Code sessions
# without their servers. A seat is excluded only when its own launch script
# STAMPS a non-claude harness; no script, no stamp, an unreadable script or
# disagreeing stamps are UNSURE, and an unsure seat is planned.
_HARNESS_STAMP = re.compile(
    r"(?<![A-Za-z0-9_])HELM_AGENT_HARNESS=[\'\"]?([A-Za-z0-9._-]{1,64})")
_LAUNCH_READ_BYTES = 1 << 20
# The names the seat launch line stamps (seat_launch_assets.launch_line), read
# exactly: a bare MAX_CONTEXT_TOKENS matched a fixture and never a real seat.
_WINDOW_KNOB = "CLAUDE_CODE_MAX_CONTEXT_TOKENS"
_OUTPUT_KNOB = "CLAUDE_CODE_MAX_OUTPUT_TOKENS"


def _stamp_pattern(name):
    """The exact `NAME=<digits>` stamp, never NAME inside a longer word."""
    return re.compile(r"(?<![A-Za-z0-9_])%s=[\'\"]?([0-9]{1,9})(?![0-9])"
                      % re.escape(name))

# A SEAT'S SERVERS ARE PAID FOR OUT OF ITS WINDOW. Behind a proxy base URL
# Claude Code loads every MCP tool schema up front (no tool search), and the
# canonical set measured about 32k tokens on a resume: 28% of a 115k local
# seat's window gone before its first read, and that seat compacted on the
# spot. A seat whose stamped window cannot carry the set within this share
# takes only the FLOOR: every seat can search the web whatever its window
# (owner: "they all must be able to AT LEAST search the web"). A seat served
# from the operator's own box takes the floor whatever its window
# (`_own_box`): there every schema is also prefill paid on every request. So
# does a seat whose family launches on the lite profile (`_profile_floor`,
# task/3253), wherever it is served.
MCP_SCHEMA_TOKENS = 32000
MCP_WINDOW_SHARE = 0.15
MCP_FLOOR = ("exa",)

# SERVERS A FAMILY'S API REFUSES BY SCHEMA, with the refusal. A server here is
# never written into that family's seats. A withheld server already present
# (refused, over a seat's window, or on a seat served from the operator's own
# box) is surfaced for removal, never removed:
# the sync only ever adds. Drop a row once the proxy normalizes that server's
# schemas for the family. Empty today: gemini's row for polyana (an array
# with no items was HTTP 400) went when the CLIProxyAPI gemini translator
# started filling `items` (fork 7.2.110-helm.14, measured 200 on polyana's
# own schemas).
MCP_FAMILY_REFUSES = {}


class MCPSourceRefused(Exception):
    """A canonical MCP file that exists but must not be used: someone other
    than its owner can read or write it, or it belongs to another user. The
    message names the file and its mode, never its content."""


def private_mcps_path():
    """The private canonical MCP file, or None when HELM_MCPS_PRIVATE is `off`.
    Unset or empty is the default path."""
    p = _home.env("MCPS_PRIVATE")
    if p and p.strip().lower() in _OFF:
        return None
    return os.path.expanduser(p) if p else MCPS_PRIVATE


def _read_private_json(path, what):
    """{name: config|null} from a file only its owner can reach.

    THE MODE IS CHECKED ON THE OPEN DESCRIPTOR, before json.load, so the file
    judged is the file read and a refused file's content is never parsed. Any
    group or other bit refuses, not only the read bits: a file others can
    WRITE is a file others can put a server `command` into, which every seat
    then runs. Raises FileNotFoundError for a missing file (the caller's
    'not configured'), MCPSourceRefused for an unsafe one."""
    import stat
    with pk.open_regular(path, encoding="utf-8") as f:
        st = os.fstat(f.fileno())
        if st.st_mode & 0o077 or st.st_uid != os.getuid():
            raise MCPSourceRefused(
                "%s %s is %s (owner uid %d) — REFUSED: a canonical MCP file "
                "can carry credentials, so helm reads it only when its owner "
                "alone can reach it; run `chmod 600 %s` as its owner"
                % (what, path, stat.filemode(st.st_mode), st.st_uid, path))
        d = json.load(f)
    if not isinstance(d, dict):
        raise ValueError("%s must be a JSON object {name: config|null}" % what)
    return d


def canonical_mcps():
    """The canonical MCP names every config dir should reach — EMPTY by
    default (no host-specific MCP ships in code). Resolution, first found wins:
    HELM_MCPS_CANONICAL (a JSON file {name: config|null}), else the private
    file (private_mcps_path(): ~/.config/helm/mcps-canonical.json unless
    HELM_MCPS_PRIVATE moves it or turns it `off`), else the host's authored
    `canonical_mcps` (registry-authored.json `host` block). Both files are
    REFUSED (MCPSourceRefused) when anyone but their owner can reach them.
    Mirrors skillsync.canonical(); like it, REFUSES (propagates
    registry.AuthoredUnreadable) rather than returning empty when the authored
    layer exists but is unreadable."""
    p = _home.env("MCPS_CANONICAL")
    if p:
        return _read_private_json(os.path.expanduser(p), "HELM_MCPS_CANONICAL")
    priv = private_mcps_path()
    if priv:
        try:
            return _read_private_json(priv, "the private canonical MCP file")
        except FileNotFoundError:
            pass
    h = registry.authored_host().get("canonical_mcps")
    return dict(h) if isinstance(h, dict) else {}


def seat_harness(cdir):
    """The harness a seat's launch runs, read from the HELM_AGENT_HARNESS
    stamp in its launch script (`<seat dir>/launch.sh`, the seat dir being
    the config dir's parent), casefolded — or None when UNSURE: no script, an
    unreadable one, no stamp, or stamps that disagree. Read-only; the script
    is read, never printed (its text names the seat's token file)."""
    text = _launch_text(cdir)
    if text is None:
        return None
    found = {m.lower() for m in _HARNESS_STAMP.findall(text)}
    return found.pop() if len(found) == 1 else None


def seat_window(cdir):
    """The context window a seat runs with (CLAUDE_CODE_MAX_CONTEXT_TOKENS),
    as an int, read by `seat_stamp` — or None when UNSURE."""
    return seat_stamp(cdir, _WINDOW_KNOB)[0]


def seat_output(cdir):
    """The output cap a seat runs with (CLAUDE_CODE_MAX_OUTPUT_TOKENS), as an
    int, read by `seat_stamp` — or None when UNSURE or not stamped."""
    return seat_stamp(cdir, _OUTPUT_KNOB)[0]


def seat_stamp(cdir, name):
    """(value, source) for one token-count knob the seat's Claude Code runs
    with: source is "settings.json" or "launch.sh"; value is None when UNSURE.

    THE SEAT'S OWN settings.json `env` OUTRANKS THE LAUNCH STAMP: Claude Code
    applies that block over the launch environment (MEASURED on a live local
    seat whose window was pinned there, task/3184). So a pin there is the
    effective value, and a pin that is not a token count is UNSURE rather than
    a silent fall back to the stamp it was written to replace. A settings file
    that does not parse is UNSURE too: nobody can say which value Claude Code
    took. Without a pin, the launch stamp decides; no script, an unreadable
    one, no stamp, or stamps that disagree are UNSURE. Read-only; neither
    file's text is printed (the launch script names the seat's token file)."""
    settings, err = _read_json(os.path.join(cdir, "settings.json"))
    if err is not None or not isinstance(settings, dict):
        return None, "settings.json"
    env = settings.get("env")
    if isinstance(env, dict) and name in env:
        pin = env[name]
        if isinstance(pin, bool):
            return None, "settings.json"
        if isinstance(pin, int) and pin > 0:
            return pin, "settings.json"
        if isinstance(pin, str) and re.fullmatch(r"[1-9][0-9]{0,8}", pin):
            return int(pin), "settings.json"
        return None, "settings.json"
    text = _launch_text(cdir)
    if text is None:
        return None, "launch.sh"
    found = {int(m) for m in _stamp_pattern(name).findall(text)}
    return (found.pop() if len(found) == 1 else None), "launch.sh"


def _launch_text(cdir):
    """The seat's `<seat dir>/launch.sh` text (the seat dir being the config
    dir's parent), bounded, or None when it cannot be read. Never printed:
    its text names the seat's token file."""
    path = os.path.join(os.path.dirname(os.path.abspath(cdir)), "launch.sh")
    try:
        with pk.open_regular(path, encoding="utf-8", errors="replace") as f:
            return f.read(_LAUNCH_READ_BYTES)
    except (OSError, ValueError):
        return None


def seat_family(label):
    """`seat:gemini` -> gemini, `seat:codex/helm-codex` -> codex; None for a
    home."""
    if not label.startswith("seat:"):
        return None
    return label[len("seat:"):].split("/", 1)[0]


def mcp_exclusion(label, cdir):
    """The reason this config dir takes no servers from mcp sync, or None.
    Only a seat whose launch stamps a NON-claude harness is excluded; a home
    and an unsure seat are planned."""
    if not label.startswith("seat:"):
        return None
    harness = seat_harness(cdir)
    if harness is not None and harness != "claude":
        return ("its launch runs the %s harness, not Claude Code, so it reads "
                "no Claude Code MCP config; this sync writes no server here"
                % harness)
    return None


def _own_box(family):
    """True when the family's default pool row is served from the operator's
    own box (seat_catalog.own_box, the one predicate for a local family). A
    WINDOW SHARE UNDERCOUNTS THERE: the schemas cost prefill on a
    prefill-bound GPU on every request, whatever the window."""
    from . import seat, seat_catalog
    return seat_catalog.own_box(seat.FAMILIES.get(family) or {})


def _profile_floor(family):
    """True when the family's launch profile takes the floor whatever its
    window or its box (seat_catalog PROFILES, `mcp_floor`: the lite
    profile)."""
    from . import seat, seat_catalog     # noqa: F401 — seat seeds the impl
    return bool(seat_catalog.launch_profile(family).get("mcp_floor"))


def mcp_withheld(label, cdir, canon):
    """{server: why} for the canonical servers this dir must NOT be given:
    the ones its family's API refuses, and on a seat served from the
    operator's own GPU, on a lite-profile seat, or on a seat whose window
    cannot carry the whole set, everything above the floor. {} for a home."""
    if not label.startswith("seat:"):
        return {}
    family = seat_family(label)
    held = dict(MCP_FAMILY_REFUSES.get(family) or {})
    window = seat_window(cdir)
    why = None
    if _own_box(family):
        why = ("it is served from the operator's own GPU, where every schema "
               "is prefill paid on every request, main and subagents alike, "
               "so it takes the floor (%s) only" % ", ".join(MCP_FLOOR))
    elif _profile_floor(family):
        why = ("its family launches on the lite profile (seat_catalog "
               "PROFILES), which takes the floor (%s) only"
               % ", ".join(MCP_FLOOR))
    elif window is not None and MCP_SCHEMA_TOKENS > window * MCP_WINDOW_SHARE:
        why = ("its window is %d tokens and the whole set's schemas load up "
               "front at about %d (%d%%), over the %d%% a seat spends on "
               "them, so it takes the floor (%s) only"
               % (window, MCP_SCHEMA_TOKENS,
                  round(100.0 * MCP_SCHEMA_TOKENS / window),
                  round(100 * MCP_WINDOW_SHARE), ", ".join(MCP_FLOOR)))
    if why:
        for name in canon:
            if name not in MCP_FLOOR:
                held.setdefault(name, why)
    return held


# ---------------------------------------------------------------------------
# reading — every read fails toward its SAFE verdict (skip, report), never a
# silent partial mutate
# ---------------------------------------------------------------------------

def _read_json(path):
    """(data, error). A missing file is {} with no error (nothing to read);
    an unparseable one is (None, msg) — the fail-closed signal every caller
    checks before it would mutate."""
    try:
        with pk.open_regular(path, encoding="utf-8") as f:
            return json.load(f), None
    except FileNotFoundError:
        return {}, None
    except (OSError, ValueError) as e:
        return None, str(e)


def _all_hooks(settings):
    """[(event, matcher, command)] across every event group in a settings dict
    (shape-tolerant — a malformed sub-branch is skipped, never raised)."""
    out = []
    hooks = settings.get("hooks") if isinstance(settings, dict) else None
    if not isinstance(hooks, dict):
        return out
    for event, groups in hooks.items():
        for g in groups if isinstance(groups, list) else []:
            if not isinstance(g, dict):
                continue
            matcher = g.get("matcher")
            for h in g.get("hooks") or []:
                if isinstance(h, dict) and h.get("command"):
                    out.append((event, matcher, str(h["command"])))
    return out


def _helm_args(command):
    """The helm subcommand a hook command runs ('inject --hook-json',
    'chat deliver --hook-json'), or None if the command does not call helm.
    Strips the `timeout N <bin>` prefix and ANY shell tail so the identity is
    mechanism-independent (a hand-wired `helm inject` matches an installer one).

    The tail is cut at the first `;` or `||`, not at `|| true` alone. A GATED
    spec's command ends `; rc=$?; [ "$rc" = 2 ] && exit 2; exit 0`, and the
    old `||`-only strip left all of that inside the identity — so every home
    whose stop guard was correctly gated read as MISSING the stop-guard hook in
    the census, for as long as the gate has existed. A hook's identity is the
    helm subcommand it runs; how its exit code is handled afterwards is
    mechanism, which is exactly what this function exists to discard."""
    import re
    import shlex
    try:
        toks = shlex.split(re.split(r";|\|\|", str(command), maxsplit=1)[0])
    except ValueError:
        return None
    for i, t in enumerate(toks):
        if os.path.basename(t) == "helm" and i + 1 < len(toks):
            rest = toks[i + 1:]
            return " ".join(rest) if rest else None
    return None


def _spec(tup):
    """A hooks.py-shaped spec dict for a canonical tuple — RESOLVED from
    hooks.SPECS by (event, args), and rebuilt from the tuple ONLY for a tuple
    that names no real spec (a custom HELM_HOOKS_CANONICAL entry). Recorder
    ownership comes from record.deployed_spec, not its logical dispatcher spec.

    It used to rebuild unconditionally, and that silently dropped every spec
    field the tuple does not carry. One of those fields is `gate`, which is what
    makes the stop guard's exit-2 refusal reach the harness instead of being
    rewritten to success. So `hooks sync` computed the FAIL-OPEN `|| true`
    command as canonical, reported 10 of 12 homes as drifted from a correct
    estate, and `--apply` would have re-disarmed the gate in every home where it
    had just been fixed — a repair verb as the regression vector.

    The tuple list and SPECS are two representations of ONE fact. The tuple owns
    cadence (timeout) and placement (matcher) because an override must be able
    to set them; everything else, including any safety flag added later, comes
    from the richer representation and can no longer be lost in this mapping."""
    from . import record
    event, matcher, args, timeout = tup
    if event in record.HOOK_EVENTS and args == "record --hook-json":
        return dict(record.deployed_spec(event), timeout=timeout, matcher=matcher)
    for sp in _hooks.SPECS:
        if sp["event"] == event and sp["args"] == args:
            return dict(sp, timeout=timeout, matcher=matcher)
    return {"name": "%s/%s" % (event, args), "event": event, "args": args,
            "timeout": timeout, "matcher": matcher, "own": (args,)}


def home_mcps(cdir):
    """One home's effective MCP picture: raw mcpServers (from the home's
    .claude.json state file), enabled plugins, and the effective NAME set
    (raw ∪ plugin-basename). Read-only; fail-closed ({} on trouble)."""
    from . import physics
    state, _path, _warn = physics._claude_state_file(cdir)
    raw = sorted((state.get("mcpServers") or {}).keys()) \
        if isinstance(state, dict) else []
    settings, _err = _read_json(os.path.join(cdir, "settings.json"))
    plugins = []
    ep = settings.get("enabledPlugins") if isinstance(settings, dict) else None
    if isinstance(ep, dict):
        plugins = sorted(k for k, v in ep.items() if v)
    # a plugin `<server>@<marketplace>` provides the `<server>` MCP (survey map).
    plugin_mcps = [p.split("@", 1)[0] for p in plugins]
    return {"raw": raw, "plugins": plugins,
            "effective": sorted(set(raw) | set(plugin_mcps))}


def _kind(label):
    if label == "default-claude":
        return "default"
    if label.startswith("seat:"):
        return "seat"
    return "credhome"


# ---------------------------------------------------------------------------
# 1. census — READ-ONLY: the whole picture (skillsync.config_dirs discovery)
# ---------------------------------------------------------------------------

def census(dirs=None, root=None):
    """The whole estate, read-only: every config dir's hooks + MCPs, the
    hook/MCP variance vs canonical, and the orphan-worktree picture. Pure
    read — probes nothing, mutates nothing."""
    from . import posttool
    if dirs is None:
        dirs = skillsync.config_dirs()
    canon_hooks = canonical_hooks()
    homes, missing_map, stray_map = [], {}, {}
    for label, cdir in dirs:
        kind, want = hooks_for(label)
        want_ids = {(e, a) for e, _m, a, _t in want}   # (event, args) identity
        settings, err = _read_json(os.path.join(cdir, "settings.json"))
        row = {"label": label, "kind": kind, "path": cdir}
        if settings is None:
            row["error"] = "settings.json unreadable: %s" % err
            row["helm_hooks"] = []
            row["strays"] = []
            row["missing"] = ["%s %s" % (e, a) for e, a in sorted(want_ids)]
            homes.append(row)
            continue
        present, strays = set(), []
        for event, matcher, cmd in _all_hooks(settings):
            a = _helm_args(cmd)
            if a is None:
                strays.append({"event": event, "command": cmd})
            else:
                present.add((event, a))
        covered = posttool.covered_members(settings, executable=_hooks.helm_bin())
        present.update((s["event"], s["args"]) for s in posttool.installed_specs()
                       if s["name"] in covered)
        live = {(t[0], t[2]) for t in want if _hooks._lane_live(settings, _spec(t))}
        row["helm_hooks"] = sorted("%s %s" % (e, a) for e, a in present)
        row["strays"] = strays
        row["posttool_refusals"] = posttool.refusals(settings)
        row["refusal_detail"] = posttool.refusal_detail(cdir, row["posttool_refusals"])
        row["missing"] = ["%s %s" % (e, a) for e, a in sorted(want_ids - live)]
        row["mcps"] = home_mcps(cdir)
        homes.append(row)
        for m in row["missing"]:
            missing_map.setdefault(m, []).append(label)
        for s in strays:
            stray_map.setdefault(label, []).append("%s: %s" % (s["event"], s["command"]))
    for h in homes:
        # every row, the unreadable-settings ones too: instructions and the
        # MCP exclusion do not depend on settings.json parsing
        h["mcp_excluded"] = mcp_exclusion(h["label"], h["path"])
        if h["kind"] == "seat":
            ins = skillsync.link_instructions(h["path"], apply=False)
            action, detail = ins.action, ins.detail
        else:   # instructions_sync plans seats only; the default home is the source
            action, detail = "not planned", "not a seat"
        h["instructions"] = {"action": action, "detail": detail,
                             "claude_md": skillsync.claude_md_state(h["path"])}

    # mcp variance: universal intersection + per-home effective + gaps. Seats
    # are planned like homes now; an EXCLUDED seat is named, never a gap-free row.
    eff_sets = [set(h.get("mcps", {}).get("effective", []))
                for h in homes if h["kind"] != "seat" and "mcps" in h]
    universal = sorted(set.intersection(*eff_sets)) if eff_sets else []
    canon_error = None
    try:
        canon_mcp = canonical_mcps()
    except (MCPSourceRefused, registry.AuthoredUnreadable, OSError,
            ValueError) as e:
        canon_mcp, canon_error = {}, str(e)
    mcp_missing, mcp_excluded, mcp_loose, mcp_withheld_by = {}, {}, {}, {}
    for h in homes:
        if "mcps" in h:
            # FLAGGED ON EVERY DIR, excluded ones too: a server config others
            # can read is a credential others can read, whoever plans it
            h["mcp_state_loose"] = _loose_state(h["path"],
                                                h["mcps"].get("raw") or [])
            if h["mcp_state_loose"]:
                mcp_loose[h["label"]] = h["mcp_state_loose"]
        if h["mcp_excluded"]:
            mcp_excluded[h["label"]] = h["mcp_excluded"]
            continue
        if "mcps" not in h:
            continue
        # A WITHHELD SERVER IS NOT A GAP: the sync keeps it off this dir on
        # purpose, so listing it as missing would ask the operator to add
        # back what the sync says to remove by hand.
        held = mcp_withheld(h["label"], h["path"], canon_mcp)
        absent = [n for n in canon_mcp if n not in h["mcps"]["effective"]]
        h["mcp_missing"] = [n for n in absent if n not in held]
        if h["mcp_missing"]:
            mcp_missing[h["label"]] = h["mcp_missing"]
        if any(n in held for n in absent):
            mcp_withheld_by[h["label"]] = [n for n in absent if n in held]

    return {
        "homes": homes,
        "hooks_canonical": [
            {"event": e, "matcher": m, "args": a} for e, m, a, _t in canon_hooks],
        "hooks_variance": {
            "missing_by_hook": missing_map,
            "strays_by_home": stray_map},
        "mcp_variance": {
            "universal_effective": universal,
            "canonical_names": sorted(canon_mcp),
            "canonical_error": canon_error,
            "missing_by_home": mcp_missing,
            "excluded_by_home": mcp_excluded,
            "withheld_by_home": mcp_withheld_by,
            "loose_state_by_home": mcp_loose},
        "worktrees": _worktree_census(root),
    }


def _worktree_census(root):
    """Read-only worktree/branch snapshot for the census (no gc, no mutate)."""
    try:
        from . import work
        root = root or work.find_root()
        if not root:
            return {"note": "not inside a git repo"}
        base = work._base(root)
        wts = _worktree_rows(root, base)
        orphans = _orphan_branches(root, base)
        lanes = work.gc_scan(root)
        return {
            "root": root, "base": base,
            "registered": [{k: r[k] for k in
                            ("path", "branch", "locked", "occupied", "dirty",
                             "merged", "verdict", "why")} for r in wts],
            "orphan_branches": orphans,
            "lane_rooms": [{"lane": r["lane"], "verdict": r["verdict"],
                            "why": r["why"]} for r in lanes]}
    except Exception as e:
        return {"error": "worktree census failed: %s" % e}


# ---------------------------------------------------------------------------
# backup + atomic write + rollback — the mutate substrate (skillsync's law)
# ---------------------------------------------------------------------------

def _backup(backup_root, label, path):
    """Copy the pre-image into the backup root FIRST (the backup IS the
    original) -> the backup path, or None when there is nothing to back up."""
    if not os.path.isfile(path):
        return None
    # PRIVATE SHELVES. A pre-image can carry credentials (a seat's
    # .claude.json holds its servers' headers), and a copy keeps its own
    # mode, so a loose 0644 pre-image under a 0755 shelf was world-readable
    # by mode. The copy keeps that mode, so a rollback restores the original
    # bytes and mode exactly; the 0700 root and shelf are what keep it private.
    shelf = os.path.join(backup_root, label)
    os.makedirs(shelf, mode=0o700, exist_ok=True)
    for d in (backup_root, shelf):
        os.chmod(d, 0o700)
    dst = os.path.join(shelf, "%s-%d" % (os.path.basename(path), int(time.time() * 1000)))
    shutil.copy2(path, dst)
    return dst


def _restore(backup, path):
    """Restore the exact pre-image. A missing backup means the file did not
    exist before mutation, so rollback removes any failed after-image."""
    if backup and os.path.isfile(backup):
        shutil.copy2(backup, path)
        return
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


def _log(backup_root, line):
    os.makedirs(backup_root, exist_ok=True)
    with open(os.path.join(backup_root, "tidy-log.txt"), "a") as f:
        f.write("%s %s\n" % (time.strftime("%Y-%m-%dT%H:%M:%S"), line))


# ---------------------------------------------------------------------------
# 2. hooks sync — reconcile every home to the canonical hook set (additive)
# ---------------------------------------------------------------------------

def _derive_hooks(label, cur):
    from . import posttool
    _kindname, want = hooks_for(label)
    specs = tuple(_spec(tup) for tup in want)
    out, remaining, covered = posttool.prepare(
        cur, specs, executable=_hooks.helm_bin())
    actions = {s["args"]: covered[s["name"]] for s in specs
               if s["name"] in covered}
    if out != cur and not covered:
        actions["posttool"] = "update"
    for spec in remaining:
        # Both recorder events share args; an unchanged failure leg must not
        # erase the success leg's consolidation from the plan's change report.
        actions[spec["args"]] = _hooks._agg({
            "prior": actions.get(spec["args"], "ok"),
            "current": _hooks._merge_event(out, spec)})
    converted, _remaining, covered = posttool.prepare(out, specs, executable=_hooks.helm_bin())
    if converted != out:
        actions.update({s["args"]: "update" for s in specs if s["name"] in covered}
                       or {"posttool": "update"})
    return converted, actions, want


def plan_hooks_home(label, cdir):
    """Read-only plan for ONE config dir -> dict. actions per canonical hook
    (ok|add|update), the strays we would PRESERVE, and the after-image + diff.
    Never mutates; raises nothing (a bad shape becomes verdict=FAIL)."""
    kind, want = hooks_for(label)
    sp = os.path.join(cdir, "settings.json")
    raw, err = _readraw(sp)
    if err:
        return {"label": label, "kind": kind, "path": cdir, "verdict": "FAIL",
                "detail": "settings.json unreadable (%s) — refusing to touch it" % err,
                "actions": {}, "strays": []}
    try:
        cur = json.loads(raw) if raw.strip() else {}
    except ValueError as e:
        return {"label": label, "kind": kind, "path": cdir, "verdict": "FAIL",
                "detail": "settings.json unparseable (%s) — refusing to touch it" % e,
                "actions": {}, "strays": []}
    if not isinstance(cur, dict):
        return {"label": label, "kind": kind, "path": cdir, "verdict": "FAIL",
                "detail": "settings.json root is not an object", "actions": {}, "strays": []}
    strays = [c for _e, _m, c in _all_hooks(cur) if _helm_args(c) is None]
    try:
        out, actions, _want = _derive_hooks(label, cur)
    except ValueError as e:
        return {"label": label, "kind": kind, "path": cdir, "verdict": "FAIL",
                "detail": str(e), "actions": {}, "strays": strays}
    changed = any(a != "ok" for a in actions.values())
    new_raw = json.dumps(out, indent=2) + "\n"
    diff = "" if not changed else "\n".join(difflib.unified_diff(
        raw.splitlines(), new_raw.splitlines(), sp, sp + " (after sync)", lineterm=""))
    from . import posttool
    refusals = posttool.refusals(out)
    return {"label": label, "kind": kind, "path": cdir,
            "verdict": "ok" if not changed else "change", "actions": actions,
            "strays": strays, "new_raw": new_raw, "diff": diff,
            "refusals": refusals, "refusal_detail": posttool.refusal_detail(cdir, refusals)}


def _readraw(path):
    try:
        with pk.open_regular(path, encoding="utf-8") as f:
            return f.read(), None
    except FileNotFoundError:
        return "", None
    except OSError as e:
        return "", str(e)


def apply_hooks_home(plan, backup_root):
    """Re-read and re-derive ONE hook plan through bounded exact-revision CAS."""
    from . import configs, posttool
    sp = os.path.join(plan["path"], "settings.json")
    label = plan["label"]

    def transform(cur):
        out, actions, want = _derive_hooks(label, cur)
        return out, {"actions": actions, "want": want,
                     "posttool_refusals": posttool.refusals(out)}

    def verify(candidate, before, _metadata):
        expected, _actions, want = _derive_hooks(label, before)
        return candidate == expected and all(
            _hooks._lane_live(candidate, _spec(tup)) for tup in want)

    res = configs.transform_json_file(sp, transform, verify=verify)
    if not res.get("ok"):
        return "FAIL", res["error"]
    plan["refusals"] = (res.get("metadata") or {}).get("posttool_refusals") or ()
    plan["refusal_detail"] = posttool.refusal_detail(plan["path"], plan["refusals"])
    note = ("; " + plan["refusal_detail"]) if plan["refusal_detail"] else ""
    if not res.get("wrote"):
        return "ok", "already current after CAS re-read" + note
    backup = res.get("backup")
    _log(backup_root, "%s hooks sync (backup: %s)" % (label, backup))
    return "applied", "backup: %s; CAS attempts: %d%s" % (
        backup or "none — new file", res["attempts"], note)


def hooks_sync(dirs=None, backup_root=None, apply=False):
    """Reconcile every config dir's hooks to the canonical set. Additive:
    missing canonical hooks are added, strays preserved; dry-run by default.
    Idempotent — a fully-wired estate reports zero changes."""
    if dirs is None:
        dirs = skillsync.config_dirs()
    backup_root = backup_root or BACKUP_ROOT
    plans, changed, failed, steady = [], [], [], 0
    for label, cdir in dirs:
        p = plan_hooks_home(label, cdir)
        plans.append(p)
        if p["verdict"] == "FAIL":
            failed.append(p)
        elif p["verdict"] == "ok":
            steady += 1
        else:
            if apply:
                v, detail = apply_hooks_home(p, backup_root)
                p["verdict"], p["detail"] = v, detail
                if v == "FAIL":
                    failed.append(p)
                elif v == "ok":
                    steady += 1
                else:
                    changed.append(p)
            else:
                changed.append(p)
    return {"backup_root": backup_root, "apply": apply, "plans": plans,
            "changed": changed, "failed": failed, "steady": steady}


# ---------------------------------------------------------------------------
# 3. mcp sync — reconcile the canonical MCP servers (additive, fail-closed)
# ---------------------------------------------------------------------------

def _loose_state(cdir, raw):
    """The `.claude.json` mode string when it carries MCP servers AND someone
    other than its owner can reach it, else None. A server config can carry a
    credential, so the file that holds it is private — the mirror of the
    refusal on the canonical file. Read-only; a link or an unreadable stat is
    not ours to judge (None)."""
    import stat
    if not raw:
        return None
    p = os.path.join(cdir, ".claude.json")
    try:
        st = os.lstat(p)
    except OSError:
        return None
    if not stat.S_ISREG(st.st_mode) or not st.st_mode & 0o077:
        return None
    return stat.filemode(st.st_mode)


def plan_mcp_home(label, cdir, canon=None):
    """Read-only MCP plan for ONE config dir -> dict. A SEAT IS PLANNED LIKE A
    HOME (task/3089: a seat is a full agent). For each canonical name missing
    from the dir's EFFECTIVE set: add (config in hand) | surface (no config /
    provided-by-plugin path unknown). A dir whose `.claude.json` holds servers
    but is reachable by others is planned `tighten`. A seat whose launch runs
    a non-claude harness (mcp_exclusion) gets verdict `excluded` with its
    reason — its own
    counted answer, never `ok`. `canon` lets a caller resolve the canonical
    set once for a whole sweep."""
    kind = _kind(label)
    why = mcp_exclusion(label, cdir)
    if why:
        return {"label": label, "kind": kind, "path": cdir,
                "verdict": "excluded", "detail": why, "adds": [],
                "surface": [], "tighten": None, "effective": []}
    canon = canonical_mcps() if canon is None else canon
    eff = home_mcps(cdir)
    held = mcp_withheld(label, cdir, canon)
    adds, surface = [], []
    for name, cfg in canon.items():
        if name in eff["effective"] or name in held:
            continue
        if isinstance(cfg, dict):
            adds.append(name)
        else:
            surface.append(name)
    refused = [n for n in eff["effective"] if n in held]
    tighten = _loose_state(cdir, eff["raw"] or adds)
    verdict = "ok" if not (adds or surface or tighten or refused) \
        else "change"
    return {"label": label, "kind": kind, "path": cdir, "verdict": verdict,
            "adds": adds, "surface": surface, "tighten": tighten,
            "effective": eff["effective"],
            "withheld": [(n, held[n]) for n in canon if n in held],
            "refused": [(n, held[n]) for n in refused]}


def _tighten(plan, backup_root):
    """chmod a server-carrying `.claude.json` to 0600 — the whole change, and
    re-read to prove it held. No backup: the bytes do not move."""
    import stat
    statefile = os.path.join(plan["path"], ".claude.json")
    try:
        os.chmod(statefile, 0o600)
        mode = stat.S_IMODE(os.lstat(statefile).st_mode)
    except OSError as e:
        return "FAIL", "could not make %s private (%s)" % (statefile, e)
    if mode & 0o077:
        return "FAIL", "%s is still %o after chmod 600" % (statefile, mode)
    _log(backup_root, "%s mcp sync tightened .claude.json %s -> 0600"
         % (plan["label"], plan["tighten"]))
    return "applied", "made .claude.json private (%s -> -rw-------)" % plan["tighten"]


def apply_mcp_home(plan, backup_root, canon=None):
    """Enact ONE mcp plan's adds: write the missing servers into the dir's
    .claude.json mcpServers, backup-first, SUPERSET-preserving (an existing
    server is never dropped) with rollback. The written file is 0600: it now
    holds server configs, and those can carry credentials. A plan with no
    adds but a `tighten` makes the existing file private. -> (verdict,
    detail)."""
    if not plan["adds"]:
        if plan.get("tighten"):
            return _tighten(plan, backup_root)
        return "ok", "nothing to add (surface-only)"
    canon = canonical_mcps() if canon is None else canon
    statefile = os.path.join(plan["path"], ".claude.json")
    data, err = _read_json(statefile)
    if err:
        return "FAIL", ".claude.json unreadable (%s) — refusing to touch it" % err
    if not isinstance(data, dict):
        data = {}
    servers = data.get("mcpServers")
    if servers is not None and not isinstance(servers, dict):
        return "FAIL", "mcpServers is not an object — refusing to touch it"
    before = set((servers or {}).keys())
    out = json.loads(json.dumps(data))
    srv = out.setdefault("mcpServers", {})
    for name in plan["adds"]:
        srv[name] = canon[name]
    backup = _backup(backup_root, plan["label"], statefile)
    try:
        pk.atomic_write(statefile, json.dumps(out, indent=2) + "\n", mode=0o600)
    except OSError as e:
        _restore(backup, statefile)
        return "FAIL", "write failed: %s — pre-image restored" % e
    got, gerr = _read_json(statefile)
    after = set((got.get("mcpServers") or {}).keys()) if isinstance(got, dict) else set()
    if gerr or not before <= after or not set(plan["adds"]) <= after:
        _restore(backup, statefile)
        return "FAIL", "post-write superset check failed — backup restored (%s)" % (backup or "none")
    _log(backup_root, "%s mcp sync +%s (backup: %s)"
         % (plan["label"], ",".join(plan["adds"]), backup))
    return "applied", "added %s (backup: %s)" % (", ".join(plan["adds"]), backup or "none")


def mcp_sync(dirs=None, backup_root=None, apply=False):
    """Reconcile canonical MCP servers into every config dir, seats included.
    Additive + fail-closed: a server is added only with a concrete config in
    hand, else surfaced; dry-run by default; idempotent.

    `steady` COUNTS ONLY DIRS THAT ARE CANONICAL. An excluded dir is its own
    list, reported beside the count: folding a skipped dir into 'already
    canonical' is how four seats with ZERO servers read as a clean sweep.
    Raises MCPSourceRefused / registry.AuthoredUnreadable before touching
    anything when the canonical set cannot be read safely."""
    if dirs is None:
        dirs = skillsync.config_dirs()
    backup_root = backup_root or BACKUP_ROOT
    canon = canonical_mcps()
    plans, changed, failed, excluded, steady = [], [], [], [], 0
    for label, cdir in dirs:
        p = plan_mcp_home(label, cdir, canon=canon)
        plans.append(p)
        if p["verdict"] == "excluded":
            excluded.append(p)
            continue
        if p["verdict"] == "ok":
            steady += 1
            continue
        if apply and (p["adds"] or p["tighten"]):
            v, detail = apply_mcp_home(p, backup_root, canon=canon)
            p["verdict"], p["detail"] = v, detail
            (failed if v == "FAIL" else changed).append(p)
        else:
            changed.append(p)
    return {"backup_root": backup_root, "apply": apply, "plans": plans,
            "changed": changed, "failed": failed, "excluded": excluded,
            "steady": steady, "canonical": sorted(canon)}


# ---------------------------------------------------------------------------
# 3b. instructions sync — every seat links the host's global instructions
# ---------------------------------------------------------------------------

def instructions_sync(dirs=None, apply=False):
    """Give every SEAT config dir its global-instructions link
    (skillsync.link_instructions — the same primitive seat mint calls), dry-run
    by default. The owner's own homes are NOT planned by this lane and are
    counted as such (`not_planned`), never as canonical; a seat left unlinked
    because there is no source (distribution off, or the default home has no
    CLAUDE.md) is `unsourced`, also never canonical. `surfaced` holds the
    answers a human must act on (a real file or a blocked rules dir kept, a
    named source missing); they count as failures of the sweep."""
    if dirs is None:
        dirs = skillsync.config_dirs()
    plans, changed, surfaced, unsourced, not_planned = [], [], [], [], []
    steady = 0
    for label, cdir in dirs:
        if not label.startswith("seat:"):
            not_planned.append({"label": label, "path": cdir,
                                "detail": "not a seat (owner homes are not "
                                          "planned by this sync)"})
            continue
        res = skillsync.link_instructions(cdir, apply=apply)
        p = {"label": label, "path": cdir, "action": res.action,
             "detail": res.detail,
             "claude_md": skillsync.claude_md_state(cdir)}
        plans.append(p)
        if res.action == "ok":
            steady += 1
        elif res.action == "none":
            unsourced.append(p)
        elif res.action in ("linked", "would-link", "relinked", "would-relink"):
            changed.append(p)
        else:
            surfaced.append(p)
    return {"apply": apply, "plans": plans, "changed": changed,
            "surfaced": surfaced, "unsourced": unsourced,
            "not_planned": not_planned, "steady": steady}


# ---------------------------------------------------------------------------
# 4. worktree gc — compose `helm work gc`, sweep orphan stubs + stray worktrees
# ---------------------------------------------------------------------------

def _worktree_rows(root, base, phantoms=None, registered=None, protected=(),
                   memo=None):
    """Live remaining-estate worktrees, each classified.

    Lane/harness rooms belong to `helm work gc`, peeks belong to `work peek`,
    and unmanaged missing records ride the explicit phantom pass. Keeping those
    out here prevents a nonexistent checkout from being misread as DIRTY work."""
    from . import work
    wts = registered if registered is not None else work.worktrees(root)
    main = wts[0]["path"] if wts else None
    phantoms = set(phantoms or ())
    protected = set(protected or ())
    rows = []
    for w in wts:
        if w["path"] == main or work.managed_room_kind(root, w["path"]) \
                or w["path"] in phantoms:
            continue
        branch = (w["branch"] or "")[len("refs/heads/"):] or None
        if w["path"] in protected:
            rows.append({"path": w["path"], "branch": branch,
                         "locked": w["locked"], "occupied": [],
                         "dirty": False, "merged": False,
                         "rescue": False, "remove": False,
                         "verdict": "keep",
                         "why": "TARGET named by --repo — never remove"})
            continue
        dirty = work._dirty(w["path"])
        # `helm work gc`'s OWN QUESTION, not `_merged`: ancestry alone reads a
        # room minted at the trunk a minute ago as merged (task/3428), and this
        # sweep removed it, branch and all, before its builder's first commit.
        state, said = (work._sweep_state(root, w["path"], branch, memo=memo)
                       if branch else (None, None))
        merged = state in work.RETIRABLE
        occupied = work._occupants(w["path"])
        r = {"path": w["path"], "branch": branch, "locked": w["locked"],
             "occupied": occupied, "dirty": dirty, "merged": merged,
             "unstarted": state == work.UNSTARTED,
             "rescue": False, "remove": False}
        if w["locked"]:
            r["verdict"], r["why"] = "keep", "LOCKED (active review) — never touch"
        elif occupied:
            r["verdict"], r["why"] = "keep", \
                "OCCUPIED by cwd pid(s) %s — never remove" % ",".join(occupied)
        elif merged and not dirty:
            r.update(verdict="remove", remove=True,
                     why="clean + merged — remove worktree + delete branch"
                         + (" (%s)" % said if said else ""))
        elif merged and dirty:
            r.update(verdict="rescue+remove", rescue=True, remove=True,
                     why="merged but dirty — rescue-commit to branch, then remove")
        elif r["unstarted"]:
            r.update(verdict="rescue+keep" if dirty else "keep", rescue=dirty,
                     why=("dirty + %s — rescue-commit to branch, then KEEP"
                          if dirty else "clean + %s — KEEP") % said)
        elif dirty:
            r.update(verdict="rescue+keep", rescue=True,
                     why="dirty + unmerged — rescue-commit to branch, then KEEP "
                         "(%s)" % (said or "ahead>0 needs land/review"))
        else:
            # `said` IS THE SENTENCE THAT KEPT IT when the tip itself is on the
            # trunk: a commit only a reflog records is off it (task/3436), and
            # "ahead>0" would send the reader looking at the wrong branch.
            r.update(verdict="keep",
                     why="unmerged — blocked; land or review before removal "
                         "(%s)" % (said or "ahead>0"))
        rows.append(r)
    return rows


def _orphan_branches(root, base, pattern=None, memo=None):
    """Cleanup-owned branches with NO registered worktree — the commit IS the
    work. Landed (by ancestry OR by patch identity) -> delete; anything else,
    including every unreadable case -> KEEP.

    THE SURFACE THE OWNER IS ACTUALLY LOOKING AT. Rooms are reaped by
    `work.gc_scan`; branches OUTLIVE their rooms, and on 2026-08-03 this box
    carried 107 `lane/*` branches against 24 branch-holding worktrees — so most
    of what a sidebar shows has no room left to reap. This row asked ancestry
    alone, which is sha identity, while our lands are REBASED; it therefore
    answered "not merged" truthfully and kept them all.

    Each row carries its `state` and the audit phrase for it. A KEEP now says
    WHICH failure it was: content genuinely absent from the trunk, versus a
    landedness read that could not be completed. Those are different facts and
    only the first one is about the work.

    Defaults cover legacy `worktree-*` rooms and current `lane/*` rooms;
    HELM_WORKTREE_PRUNE_GLOB narrows to an operator-supplied single pattern."""
    from . import work
    override = pattern or _home.env("WORKTREE_PRUNE_GLOB")
    patterns = (override,) if override else ("worktree-*", "lane/*")
    found = []
    for glob in patterns:
        rc, out, _err = work._git(
            root, "for-each-ref", "--format=%(refname:short)",
            "refs/heads/" + glob)
        if rc != 0:
            return []
        found.extend(out.splitlines())
    wt_branches = {(w["branch"] or "")[len("refs/heads/"):]
                   for w in work.worktrees(root)}
    rows = []
    for b in dict.fromkeys(found):
        b = b.strip()
        if not b or b in wt_branches:
            continue
        # NO ROOM, SO NO GRACE — but the branch's reflog may be the only
        # place a commit it wrote and reset away still lives (task/3428), and
        # `_sweep_state` is the one reader that judges that commit.
        state, said = work._sweep_state(root, None, b, memo=memo)
        merged = state in work.RETIRABLE
        rows.append({"branch": b, "merged": merged, "state": state,
                     "verdict": "delete" if merged else "keep",
                     "why": said or work._proof_word(state)})
    return rows


def _enact_worktree(root, r, apply):
    """Enforce ONE registered-worktree row. Rescue-first: a failed rescue
    SKIPS the removal loudly — the worktree outlives any error. Apply re-checks
    lock + cwd occupancy so a pane entering after the scan is still immune."""
    from . import work
    if not (r["rescue"] or r["remove"]):
        return []
    if apply:
        blocked = work._removal_blocker(root, r["path"])
        if blocked:
            return ["SKIPPED %s (%s) — kept" % (r["path"], blocked)]
    lines = []
    if r["rescue"] and apply:
        rc, _o, err = work._wip_commit(
            r["path"], "wip: rescue before gc %s" % pk.now_ts())
        if rc != 0:
            # NOT NECESSARILY A FAILURE — `_wip_commit` refuses by design here
            # (live author, shared branch, dangling conflict), and calling a
            # deliberate refusal "rescue commit failed" sends an operator
            # looking for a broken git. The refusals are whole sentences; pass
            # them through rather than wrapping them in a diagnosis.
            return ["SKIPPED %s (%s) — kept" % (r["path"], err)]
        dest = _o if isinstance(_o, str) and _o.startswith("refs/") else (r["branch"] or "?")
        lines.append("rescued dirty work -> %s" % dest)
    elif r["rescue"]:
        lines.append("would rescue-commit dirty work -> %s" % (r["branch"] or "?"))
    if r["remove"]:
        if apply:
            blocked = work._removal_blocker(root, r["path"])
            if blocked:
                return lines + ["SKIPPED %s (%s) — kept" % (r["path"], blocked)]
            # WHAT THE ROOM HOLDS NOW, after any rescue and before every
            # re-read below: the last read before the removal compares against
            # it, so a detour made after the verdict keeps the room (task/3436
            # F4), as `gc_enact` does.
            seen = work._room_fingerprint(r["path"])
            # THE SCAN'S BRANCH IS NOT NECESSARILY WHAT THE ROOM HOLDS NOW. A
            # room that detached and committed after the scan passes every
            # blocker above, and the branch the scan judged still reads
            # merged; removing it orphans the new commit (task/3125).
            moved = work._moved_under_scan(r["path"], r["branch"])
            if moved:
                return lines + ["SKIPPED %s (%s) — kept" % (r["path"], moved)]
            # THE SCAN'S LANDEDNESS IS RE-ASKED TOO, as `gc_enact` re-asks it:
            # a room judged old and unstarted, or landed, can be switched or
            # committed in before this line. A rescued room cannot be asked
            # that — its own rescue commit makes its branch read unlanded, and
            # the branch delete below re-reads that on its own — so it is asked
            # what removing the ROOM alone would lose: a commit only its HEAD
            # reflog records (a detour made after the scan, task/3436 F2).
            state, said = (work._room_state(root, r["path"], (r["branch"],))
                           if r["rescue"] else
                           work._sweep_state(root, r["path"], r["branch"]))
            if state is not None and state not in work.RETIRABLE:
                return lines + ["SKIPPED %s (%s) — kept" % (
                    r["path"], said or work._proof_word(state))]
            work._git(root, "worktree", "unlock", r["path"])
            moved = work._moved_since(r["path"], seen)
            if moved:
                return lines + ["SKIPPED %s (%s) — kept" % (r["path"], moved)]
            rc, _o, err = work._git(root, "worktree", "remove", r["path"])
            if rc != 0:
                return lines + ["SKIPPED %s (%s)" % (r["path"], err)]
            lines.append("removed " + r["path"])
            if r["merged"] and r["branch"]:
                # Through the ONE branch-retirement actuator: it re-reads the
                # proof (every commit the branch's reflog alone records must be
                # on the trunk), picks -d vs the patch-identity -D, and prints
                # the restore line. The old inline `branch -d` also swallowed
                # its rc, so a refusal here was silent AND mislabelled
                # "deleted".
                lines += work._delete_lane_branch(root, r["branch"])
        else:
            lines.append("would remove " + r["path"]
                         + ("; delete branch " + r["branch"] if r["merged"] and r["branch"] else ""))
    return lines


def worktree_gc(root=None, apply=False):
    """The estate worktree sweep. COMPOSES `helm work gc` for lane rooms, adds
    the orphan-stub + stray-worktree sweep the lane gc does not cover. Dry-run
    by default; rescue-dirty-first; locked/occupied-immune; unmerged-blocked."""
    from . import work
    requested = os.path.realpath(os.path.abspath(root)) if root else None
    root = work.find_root(root)
    if not root:
        return {"error": "not inside a git repo (--repo PATH names one)"}
    registered, registry_error = vcs.backend(root).worktrees(root)
    if registry_error:
        return {"error": "worktree registry unavailable: %s" % registry_error}
    protected = {w["path"] for w in registered if requested and
                 (requested == os.path.realpath(w["path"]) or
                  requested.startswith(os.path.realpath(w["path"]) + os.sep))}
    # FETCH BEFORE THE SCAN WHEN THIS RUN DELETES, as `helm work gc --apply`
    # does: every "landed" below is asked of a remote-tracking ref, and an
    # unrefreshed proof must not authorize a branch deletion. A failed fetch
    # refuses the whole apply; the dry run never touches the network.
    if apply:
        ok, why = work.refresh_trunk(root)
        if not ok:
            return {"error": "REFUSING to apply — %s, so 'landed' is "
                             "UNPROVEN; re-run when the remote is reachable, "
                             "or use the dry run" % why}
    base = work._base(root)
    # ONE MEMO FOR EVERY SCAN BELOW, saved once (task/4061): the keep verdicts
    # of lanes, stray rooms and orphan branches whose tips and trunk did not
    # change are not re-derived. Each scan runs in its own projection scope;
    # every enact re-asks outside both.
    from . import projscope
    memo = work.LandedMemo(root)
    # (a) managed rooms — DELEGATE to work.gc (its lease-aware rescue logic,
    # plus lane/harness phantom ownership, reused exactly once).
    lane_rows = work.gc_scan(root, registered=registered, memo=memo)
    for row in lane_rows:
        if row["path"] in protected:
            row.update(verdict="keep", why="TARGET named by --repo — never remove")
    lane_lines, reclassified_lanes = {}, set()
    if apply:
        for lr in lane_rows:
            out = work.gc_enact(root, lr)
            if out:
                lane_lines[lr["lane"]] = out
            # THE SECOND CONSUMER, and it had the same divergence. envtidy
            # collected the enact lines and then derived triage purely from
            # SCAN verdicts, so a room enact reclassified was printed as
            # triage detail and excluded from envtidy's own count — the
            # identical bug one file over, which a fix scoped to _cli.py
            # would have left standing (a review's composition addendum).
            if work.was_reclassified(out):
                reclassified_lanes.add(lr["lane"])
    managed_phantoms, managed_excluded, _ = work.phantom_scan(
        root, registered=registered)
    managed_removed, managed_error, managed_unknown = ([], None, False)
    if apply:
        managed_removed, managed_error, managed_unknown = \
            work.prune_phantom_records(
                root, managed_phantoms, excluded=managed_excluded)

    # (b) remaining unmanaged estate. Missing records are their own class: a
    # nonexistent checkout cannot be classified by dirty/merged/occupied reads.
    estate_phantoms, estate_excluded, _ = work.phantom_scan(
        root, owner="estate", registered=registered)
    estate_removed, estate_error, estate_unknown = ([], None, False)
    if apply:
        estate_removed, estate_error, estate_unknown = \
            work.prune_phantom_records(
                root, estate_phantoms, excluded=estate_excluded, owner="estate")
    with projscope.scope():
        wt_rows = _worktree_rows(root, base, estate_phantoms,
                                 registered=registered, protected=protected,
                                 memo=memo)
    wt_lines = {}
    for r in wt_rows:
        out = _enact_worktree(root, r, apply)
        if out:
            wt_lines[r["path"]] = out
    # (c) orphan branch stubs
    with projscope.scope():
        orphans = _orphan_branches(root, base, memo=memo)
    memo.save()
    orphan_lines = {}
    for o in orphans:
        if o["verdict"] == "delete":
            if apply:
                # A FRESH state, never the scan's: the verdict is re-proven
                # here by the same question the scan asked, because the branch
                # can advance (or reset a commit away) between the two and a
                # scan verdict is not a permission slip.
                orphan_lines[o["branch"]] = work._delete_lane_branch(
                    root, o["branch"],
                    *work._sweep_state(root, None, o["branch"]))
            else:
                orphan_lines[o["branch"]] = ["would delete — " + o["why"]]
    return {"root": root, "base": base, "apply": apply,
            "lane_rows": lane_rows, "lane_lines": lane_lines,
            "reclassified_lanes": reclassified_lanes,
            "managed_phantoms": managed_phantoms,
            "managed_phantom_removed": managed_removed,
            "managed_phantom_error": managed_error,
            "managed_phantom_unknown": managed_unknown,
            "estate_phantoms": estate_phantoms,
            "estate_phantom_removed": estate_removed,
            "estate_phantom_error": estate_error,
            "estate_phantom_unknown": estate_unknown,
            "worktree_rows": wt_rows, "worktree_lines": wt_lines,
            "orphans": orphans, "orphan_lines": orphan_lines}


# ---------------------------------------------------------------------------
# 5. tidy — the umbrella
# ---------------------------------------------------------------------------

def tidy(dirs=None, backup_root=None, root=None, apply=False):
    """Census + all four reconcilers (hooks, mcp, instructions, worktree),
    dry-run by default. --apply runs them all backup-first. One consolidated
    report. An MCP leg whose canonical set cannot be read safely is REPORTED
    as that leg's error (nothing written by it), never allowed to abort the
    other legs or read as a clean sweep."""
    if dirs is None:
        dirs = skillsync.config_dirs()
    backup_root = backup_root or BACKUP_ROOT
    out = {"apply": apply, "backup_root": backup_root,
           "census": census(dirs=dirs, root=root),
           "hooks": hooks_sync(dirs=dirs, backup_root=backup_root, apply=apply)}
    try:
        out["mcp"] = mcp_sync(dirs=dirs, backup_root=backup_root, apply=apply)
    except (MCPSourceRefused, registry.AuthoredUnreadable, OSError,
            ValueError) as e:
        out["mcp"] = {"error": str(e), "backup_root": backup_root,
                      "apply": apply, "plans": [], "changed": [], "failed": [],
                      "excluded": [], "steady": 0, "canonical": []}
    out["instructions"] = instructions_sync(dirs=dirs, apply=apply)
    out["worktree"] = worktree_gc(root=root, apply=apply)
    return out


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _print_census(r, out=sys.stdout):
    from . import posttool
    p = lambda *a: print(*a, file=out)
    p("helm env census — %d config dir(s)" % len(r["homes"]))
    p("  %-26s %-9s %-5s %-4s %s" % ("home", "kind", "hooks", "miss", "strays"))
    for h in r["homes"]:
        if "error" in h:
            p("  %-26s %-9s  ERROR: %s" % (h["label"], h["kind"], h["error"]))
            continue
        p("  %-26s %-9s %-5d %-4d %d" % (
            h["label"], h["kind"], len(h["helm_hooks"]), len(h["missing"]),
            len(h["strays"])))
        if h.get("refusal_detail"):
            p("  " + h["refusal_detail"])
    v = r["hooks_variance"]
    if v["missing_by_hook"]:
        p("hook gaps (canonical hook -> homes missing it):")
        for a, homes in sorted(v["missing_by_hook"].items()):
            p("  %-26s %s" % (a, ", ".join(homes)))
    else:
        p("hooks: every home carries its full canonical set")
    if v["strays_by_home"]:
        p("stray (non-helm) hooks — PRESERVED, never removed:")
        for label, lst in sorted(v["strays_by_home"].items()):
            for s in lst:
                if not (s.startswith(posttool.EVENT + ": ")
                        and posttool.protected(s[len(posttool.EVENT) + 2:])):
                    p("  %-26s %s" % (label, s))
    m = r["mcp_variance"]
    p("mcp universal (every non-seat home reaches): %s"
      % (", ".join(m["universal_effective"]) or "-"))
    if m.get("canonical_error"):
        p("mcp canonical set UNREADABLE — no gap below can be trusted: %s"
          % m["canonical_error"])
    if m["missing_by_home"]:
        p("mcp gaps (canonical %s):" % ", ".join(m["canonical_names"]))
        for label, gap in sorted(m["missing_by_home"].items()):
            p("  %-26s missing %s" % (label, ", ".join(gap)))
    for label, why in sorted((m.get("excluded_by_home") or {}).items()):
        p("  %-26s EXCLUDED from mcp sync: %s" % (label, why))
    for label, names in sorted((m.get("withheld_by_home") or {}).items()):
        p("  %-26s withheld by the sync, not a gap: %s"
          % (label, ", ".join(names)))
    loose = m.get("loose_state_by_home") or {}
    if loose:
        paths = {h["label"]: h["path"] for h in r["homes"]}
        p("mcp CREDENTIAL EXPOSURE — .claude.json holds MCP servers and is "
          "group/world-accessible (`helm mcp sync --apply` makes it 0600):")
        for label, mode in sorted(loose.items()):
            # an excluded dir is not planned, so the sync will not repair it
            p("  LOOSE %-26s %s%s" % (
                label, mode,
                "  (excluded from mcp sync: chmod 600 %s)"
                % os.path.join(paths.get(label, "?"), ".claude.json")
                if label in (m.get("excluded_by_home") or {}) else ""))
    _print_seat_parity(r, m, out)
    w = r["worktrees"]
    if "error" in w or "note" in w:
        p("worktrees: %s" % (w.get("error") or w.get("note")))
    else:
        reg, orph = w["registered"], w["orphan_branches"]
        p("worktrees @ %s (base %s): %d registered, %d orphan-branch stub(s), %d lane room(s)"
          % (w["root"], w["base"], len(reg), len(orph), len(w["lane_rooms"])))
        for r2 in reg:
            p("  %-10s %-40s %s" % (r2["verdict"], r2.get("branch") or "-", r2["why"]))
        for o in orph:
            p("  %-10s %-40s %s" % (o["verdict"], o["branch"], o["why"]))


def _print_seat_parity(r, m, out):
    """One line per SEAT: its servers, helm hooks and global instructions
    against canonical — the task/3089 question, 'is this seat a full agent?',
    answered per seat instead of folded into an estate total."""
    seats = [h for h in r["homes"] if h["kind"] == "seat"]
    if not seats:
        return
    p = lambda *a: print(*a, file=out)
    canon = m["canonical_names"]
    p("seat parity (servers / helm hooks / global instructions vs canonical):")
    for h in seats:
        if h.get("mcp_excluded"):
            servers = "EXCLUDED"
        elif "mcps" not in h:
            servers = "unknown"
        else:
            miss = h.get("mcp_missing") or []
            servers = "%d/%d%s" % (len(canon) - len(miss), len(canon),
                                   " missing " + ",".join(miss) if miss else "")
        if h.get("mcp_state_loose"):
            servers += " LOOSE " + h["mcp_state_loose"]
        hooks = "%d missing" % len(h["missing"]) if h["missing"] else "full"
        ins = h.get("instructions") or {}
        p("  %-26s servers %-16s hooks %-10s instructions %s (own CLAUDE.md: %s)"
          % (h["label"], servers, hooks, ins.get("action", "unknown"),
             ins.get("claude_md", "unknown")))


def cmd_env(args):
    """env census [--json] — READ-ONLY: the whole estate picture (every home's
    hooks + MCPs, the variance vs canonical, orphan worktrees)."""
    args = list(args or [])
    sub = args[0] if args and not args[0].startswith("-") else "census"
    if sub not in ("census",):
        print("usage: helm env census [--json]", file=sys.stderr)
        return 2
    from .cli import guard_tail
    rc = guard_tail("helm env census", args[1:] if args and args[0] == sub
                    else args, flags=("--json",), valued=("--repo",),
                    usage="env census [--json] [--repo PATH]")
    if rc is not None:
        return rc
    from . import seats
    r = census(root=seats._flag(args, "--repo"))
    if "--json" in args:
        print(json.dumps(r, indent=2, ensure_ascii=False))
        return 0
    _print_census(r)
    return 0


def cmd_hooks_sync(args):
    """hooks sync [--apply] — reconcile every home's hooks to the canonical set
    (dry-run default; additive — strays preserved, backup-first, superset-refusal)."""
    from .cli import guard_tail
    rc = guard_tail("helm hooks sync", args or [], flags=("--apply",),
                    usage="hooks sync [--apply]")
    if rc is not None:
        return rc
    apply = "--apply" in (args or [])
    r = hooks_sync(apply=apply)
    mode = "APPLIED" if apply else "dry-run (--apply to execute)"
    print("helm hooks sync [%s] — canonical: %d hooks (HELM_HOOKS_CANONICAL overrides)"
          % (mode, len(canonical_hooks())))
    for p in r["changed"]:
        adds = [a for a, act in p["actions"].items() if act == "add"]
        upds = [a for a, act in p["actions"].items() if act == "update"]
        tag = p.get("detail") or ("+%s" % ", ".join(adds) if adds else "") \
            + (" ~%s" % ", ".join(upds) if upds else "")
        print("  %-26s %s" % (p["label"], tag.strip() or "change"))
    for p in r.get("plans", ()):
        if p.get("refusal_detail") and (p["verdict"] == "ok" or not p.get("detail")):
            print("  " + p["refusal_detail"])
    print("helm hooks sync: %d changed, %d already canonical, %d failed"
          % (len(r["changed"]), r["steady"], len(r["failed"])))
    for p in r["failed"]:
        print("  FAIL %-26s %s" % (p["label"], p.get("detail", "")), file=sys.stderr)
    if apply and not r["failed"]:
        print("backups: " + r["backup_root"])
    return 1 if r["failed"] else 0


def cmd_mcp(args):
    """mcp sync [--apply] — reconcile canonical MCP servers into every home
    (dry-run default; additive + fail-closed; backup-first, superset-refusal)."""
    args = list(args or [])
    if not args or args[0] != "sync":
        print("usage: helm mcp sync [--apply]", file=sys.stderr)
        return 2
    from .cli import guard_tail
    rc = guard_tail("helm mcp sync", args[1:], flags=("--apply",),
                    usage="mcp sync [--apply]")
    if rc is not None:
        return rc
    apply = "--apply" in args
    try:
        r = mcp_sync(apply=apply)
    except registry.AuthoredUnreadable as e:
        print("helm mcp sync: authored layer unreadable (%s) — refusing; the "
              "canonical MCP set is unknown and a partial sync could shadow real "
              "servers. Config is recoverable from its .corrupt backup." % e,
              file=sys.stderr)
        return 2
    except MCPSourceRefused as e:
        print("helm mcp sync: %s — nothing was planned or written" % e,
              file=sys.stderr)
        return 2
    mode = "APPLIED" if apply else "dry-run (--apply to execute)"
    print("helm mcp sync [%s] — canonical names: %s (HELM_MCPS_CANONICAL, else "
          "the private file %s, else the host block)"
          % (mode, ", ".join(r.get("canonical") or []),
             private_mcps_path() or "(off)"))
    for p in r["changed"]:
        print("  %-26s %s" % (p["label"], _mcp_bits(p)))
    canon = r.get("canonical") or []
    for p in r.get("plans") or ():
        if p.get("kind") == "seat" and p["verdict"] != "excluded":
            held = dict(p.get("withheld") or ())
            have = [n for n in canon if n in p.get("effective", ())]
            miss = [n for n in canon if n not in have and n not in held]
            print("  %-26s seat had %d/%d canonical server(s)%s%s"
                  % (p["label"], len(have), len(canon),
                     "; missing " + ", ".join(miss) if miss else "",
                     "".join("; withheld %s (%s)" % kv
                             for kv in sorted(held.items()))))
    for p in r.get("excluded") or ():
        print("  %-26s EXCLUDED: %s" % (p["label"], p["detail"]))
    print("helm mcp sync: %d dir(s) with gaps, %d already canonical, %d "
          "excluded, %d failed" % (len(r["changed"]), r["steady"],
                                    len(r.get("excluded") or ()), len(r["failed"])))
    for p in r["failed"]:
        print("  FAIL %-26s %s" % (p["label"], p.get("detail", "")), file=sys.stderr)
    if apply and not r["failed"]:
        print("backups: " + r["backup_root"])
    return 1 if r["failed"] else 0


def _mcp_bits(p):
    """The one rendering of a changed MCP plan, shared by `mcp sync` and
    `tidy` so the two reports cannot describe one plan two ways."""
    bits = []
    if p.get("adds"):
        bits.append(p.get("detail") or ("add %s" % ", ".join(p["adds"])))
    elif p.get("tighten"):
        bits.append(p.get("detail") or ("make .claude.json private (%s)"
                                        % p["tighten"]))
    if p.get("surface"):
        bits.append("surface to owner (no config): %s" % ", ".join(p["surface"]))
    for name, why in p.get("refused") or ():
        bits.append("REMOVE %s by hand: %s" % (name, why))
    return "; ".join(bits)


def _print_worktree(r, out=sys.stdout):
    p = lambda *a: print(*a, file=out)
    if "error" in r:
        p("helm worktree gc: " + r["error"])
        return
    mode = "APPLYING" if r["apply"] else "dry-run; --apply enforces"
    p("helm worktree gc [%s] @ %s (base %s)" % (mode, r["root"], r["base"]))
    p("  lane rooms (delegated to `helm work gc`): %d" % len(r["lane_rows"]))
    for lr in r["lane_rows"]:
        p("    %-8s %-20s %s" % (lr["verdict"].upper(), lr["lane"], lr["why"]))
        for ln in r["lane_lines"].get(lr["lane"], []):
            p("        " + ln)
    p("  managed phantom records (lane/harness): %d" %
      len(r["managed_phantoms"]))
    managed_removed = set(r["managed_phantom_removed"])
    for path in r["managed_phantoms"]:
        state = "removed" if path in managed_removed else \
            ("unknown" if r["managed_phantom_unknown"] and r["apply"] else
             "kept" if r["apply"] else "would remove")
        p("    %-12s %s" % (state.upper(), path))
    if r["managed_phantom_error"]:
        p("    ERROR " + r["managed_phantom_error"])
    p("  remaining-estate phantom records: %d" % len(r["estate_phantoms"]))
    estate_removed = set(r["estate_phantom_removed"])
    for path in r["estate_phantoms"]:
        state = "removed" if path in estate_removed else \
            ("unknown" if r["estate_phantom_unknown"] and r["apply"] else
             "kept" if r["apply"] else "would remove")
        p("    %-12s %s" % (state.upper(), path))
    if r["estate_phantom_error"]:
        p("    ERROR " + r["estate_phantom_error"])
    p("  stray worktrees: %d" % len(r["worktree_rows"]))
    for wr in r["worktree_rows"]:
        p("    %-14s %-36s %s" % (wr["verdict"], wr.get("branch") or "-", wr["why"]))
        for ln in r["worktree_lines"].get(wr["path"], []):
            p("        " + ln)
    p("  orphan branch stubs: %d" % len(r["orphans"]))
    for o in r["orphans"]:
        p("    %-8s %-36s %s" % (o["verdict"].upper(), o["branch"], o["why"]))
        for ln in r["orphan_lines"].get(o["branch"], []):
            p("        " + ln)


def _post_worktree_summary(r):
    from . import work
    if r["managed_phantom_unknown"] or r["estate_phantom_unknown"]:
        error = "worktree gc: phantom removal UNKNOWN — summary not posted"
        print(error, file=sys.stderr)
        return error
    rooms = r["lane_rows"] + r["worktree_rows"]
    # A PARKED CHECKOUT IS GONE WITH ITS BRANCH KEPT (task/4061): its own
    # count, still triage, never a removal.
    parked = sum(row["verdict"] == "park" and not os.path.exists(row["path"])
                 for row in r["lane_rows"])
    room_removed = sum(not os.path.exists(row["path"]) for row in rooms
                       if row["verdict"] != "park")
    branch_removed = sum(row["verdict"] == "delete" and
                         not work._has_branch(r["root"], row["branch"])
                         for row in r["orphans"])
    phantom_removed = (len(r["managed_phantom_removed"])
                       + len(r["estate_phantom_removed"]))
    removed = room_removed + branch_removed + phantom_removed
    phantom_kept = (len(r["managed_phantoms"]) + len(r["estate_phantoms"])
                    - phantom_removed)
    # ENACT-TIME TRUTH HERE TOO. A lane enact reclassified is triage NOW even
    # though the scan said remove; counting only the scan verdict is what made
    # this summary disagree with the detail it prints beside.
    triage = (sum(row["verdict"] in ("triage", "rescue", "park")
                   or row["lane"] in r.get("reclassified_lanes", ())
                   for row in r["lane_rows"])
              # AN UNSTARTED ROOM IS NOT TRIAGE: it is not merged because it
              # holds nothing to merge, and nobody owes it an integration.
              + sum(row["verdict"] in ("keep", "rescue+keep") and
                    ((not row.get("merged") and not row.get("unstarted"))
                     or row.get("dirty"))
                    for row in r["worktree_rows"])
              + sum(row["verdict"] == "keep" for row in r["orphans"])
              + phantom_kept)
    total = (len(rooms) + len(r["orphans"]) + len(r["managed_phantoms"])
             + len(r["estate_phantoms"]))
    line = work.format_gc_summary(r["root"], removed,
                                  total - removed - parked, triage,
                                  parked=parked)
    print(line)
    error = work.post_gc_summary(line)
    if error:
        print("helm " + error, file=sys.stderr)
    return error


def cmd_worktree(args):
    """worktree gc [--apply] — prune orphan worktree-*/lane/* branches + landed
    worktrees (dry-run default; rescue-dirty-first, locked/occupied-immune,
    unmerged-blocked; composes `helm work gc` for lane rooms)."""
    args = list(args or [])
    if not args or args[0] != "gc":
        print("usage: helm worktree gc [--apply]", file=sys.stderr)
        return 2
    from .cli import guard_tail
    rc = guard_tail("helm worktree gc", args[1:], flags=("--apply",),
                    valued=("--repo",), usage="worktree gc [--apply] [--repo PATH]")
    if rc is not None:
        return rc
    from . import seats
    apply = "--apply" in args
    r = worktree_gc(root=seats._flag(args, "--repo"), apply=apply)
    _print_worktree(r)
    if "error" in r:
        return 1
    failed = r["managed_phantom_error"] or r["estate_phantom_error"]
    summary_error = _post_worktree_summary(r) if apply else None
    return 1 if failed or summary_error else 0


def cmd_tidy(args):
    """tidy [--apply] [--repo PATH] — the umbrella: census + hooks sync + mcp
    sync + global-instructions links + worktree gc, all dry-run by default. One consolidated 'here is
    everything that would change' report; --apply runs them all backup-first;
    --repo points the census + worktree-gc legs at another repo root."""
    args = list(args or [])
    # guard_tail owns the whole parse contract: unknown junk, a MISSING or
    # flag-shaped --repo value (`tidy --repo --apply` once APPLIED against
    # root='--apply'), and a duplicate --repo all refuse before any work.
    from .cli import guard_tail
    rc = guard_tail("helm tidy", args, flags=("--apply",), valued=("--repo",),
                    usage="tidy [--apply] [--repo PATH]")
    if rc is not None:
        return rc
    apply = "--apply" in args
    root = args[args.index("--repo") + 1] if "--repo" in args else None
    r = tidy(root=root, apply=apply)
    mode = "APPLY" if apply else "DRY-RUN — here is everything that would change"
    print("=" * 72)
    print("helm tidy [%s]" % mode)
    print("=" * 72)
    print("\n[1/5] CENSUS")
    _print_census(r["census"])
    print("\n[2/5] HOOKS SYNC")
    h = r["hooks"]
    if not h["changed"] and not h["failed"]:
        print("  every config dir carries its full canonical hook set (0 changes)")
    for p in h["changed"]:
        adds = [a for a, act in p["actions"].items() if act == "add"]
        upds = [a for a, act in p["actions"].items() if act == "update"]
        print("  %-26s %s%s" % (p["label"],
              "+%s " % ", ".join(adds) if adds else "",
              "~%s" % ", ".join(upds) if upds else p.get("detail", "")))
    for p in h["failed"]:
        print("  FAIL %-26s %s" % (p["label"], p.get("detail", "")))
    print("\n[3/5] MCP SYNC")
    m = r["mcp"]
    if m.get("error"):
        print("  REFUSED — nothing planned or written: %s" % m["error"])
    elif not m["changed"] and not m["failed"]:
        print("  every planned config dir reaches the canonical MCP set "
              "(0 changes; %d already canonical)" % m["steady"])
    for p in m["changed"]:
        print("  %-26s %s" % (p["label"], _mcp_bits(p)))
    for p in m.get("excluded") or ():
        print("  %-26s EXCLUDED: %s" % (p["label"], p["detail"]))
    for p in m["failed"]:
        print("  FAIL %-26s %s" % (p["label"], p.get("detail", "")))
    print("\n[4/5] GLOBAL INSTRUCTIONS")
    ins = r.get("instructions") or {}
    for p in ins.get("changed") or ():
        print("  %-26s %s %s" % (p["label"], p["action"], p["detail"]))
    for p in ins.get("surfaced") or ():
        print("  SURFACE %-26s %s: %s" % (p["label"], p["action"], p["detail"]))
    for p in (ins.get("unsourced") or ())[:1]:
        print("  %d seat(s) NOT linked — no source: %s"
              % (len(ins["unsourced"]), p["detail"]))
    print("  %d seat(s) changed, %d already canonical, %d surfaced, %d not "
          "linked (no source), %d not planned (not a seat)"
          % (len(ins.get("changed") or ()), ins.get("steady", 0),
             len(ins.get("surfaced") or ()), len(ins.get("unsourced") or ()),
             len(ins.get("not_planned") or ())))
    print("\n[5/5] WORKTREE GC")
    _print_worktree(r["worktree"])
    print("\n" + "=" * 72)
    fails = (len(h["failed"]) + len(m["failed"]) + bool(m.get("error"))
             + len(ins.get("surfaced") or ()))
    w = r["worktree"]
    if "error" in w:
        fails += 1
    else:
        fails += bool(w["managed_phantom_error"])
        fails += bool(w["estate_phantom_error"])
        summary_error = _post_worktree_summary(w) if apply else None
        if summary_error and not (
                w["managed_phantom_unknown"] or w["estate_phantom_unknown"]):
            fails += 1
    print("helm tidy: %s%s" % (
        "APPLIED (backups: %s)" % r["backup_root"] if apply
        else "dry-run complete — `helm tidy --apply` executes",
        " — %d FAILED" % fails if fails else ""))
    return 1 if fails else 0
