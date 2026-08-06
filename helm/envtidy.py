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
skillsync.canonical(): HELM_HOOKS_CANONICAL, HELM_MCPS_CANONICAL.

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
    ("PostToolUse",          None, "record --hook-json",          5),
    ("PostToolUseFailure",   None, "record --hook-json",          5),
    ("PostToolUse",          "*",  "chat deliver --hook-json",    2),
    ("SessionStart",         "*",  "chat join --hook-json",       5),
    ("Stop",                 None, "chat stop-guard --hook-json", 5),
    ("PreCompact",           None, "handoff check --hook-json",   5),
    ("SessionEnd",           None, "handoff check --hook-json",   5),
)

# The seat subset: a launched codex/kimi/… seat receives fleet chat and cannot
# idle past a NEW row of it — bounded by the pending-fingerprint latch and by
# the stop-active continuation that skips the rung entirely (the complete
# escape set: tests/test_delivery_promise_escapes) — but never ground-injects,
# records tool outcomes, or writes
# handoffs (survey: the 4 seats deliberately carry only 3 of the 8). Derived by
# arg-set so a HELM_HOOKS_CANONICAL override still yields the right lean subset.
_LEAN_ARGS = frozenset((
    "chat deliver --hook-json", "chat join --hook-json",
    "chat stop-guard --hook-json"))


def _load_hook_override():
    """HELM_HOOKS_CANONICAL: a path to a JSON file of
    [{event, matcher?, args, timeout?}] that REPLACES CANONICAL_HOOKS — the
    exact skillsync.canonical() env-override seam, so an operator can pin the
    estate's hook canon without editing helm. Fail-CLOSED: a set path that
    will not parse raises, rather than silently reverting to the default."""
    p = _home.env("HOOKS_CANONICAL")
    if not p:
        return None
    with open(os.path.expanduser(p), encoding="utf-8") as f:
        spec = json.load(f)
    out = []
    for e in spec:
        out.append((e["event"], e.get("matcher"), e["args"],
                    int(e.get("timeout", 5))))
    return tuple(out)


def canonical_hooks():
    """The canonical hook set for credhomes + the default ~/.claude
    (HELM_HOOKS_CANONICAL overrides). Mirrors skillsync.canonical()."""
    return _load_hook_override() or CANONICAL_HOOKS


def hooks_for(label):
    """(kind, canonical-hooks) for one config dir. Seats get the lean delivery
    subset of whatever the full canonical set is; homes get the full set."""
    full = canonical_hooks()
    if label.startswith("seat:"):
        return "seat", tuple(h for h in full if h[2] in _LEAN_ARGS)
    return "home", full


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


def canonical_mcps():
    """The canonical MCP names every home should reach — EMPTY by default (no
    host-specific MCP ships in code). Resolution: HELM_MCPS_CANONICAL (a JSON
    file {name: config|null}), else the host's authored `canonical_mcps`
    (registry-authored.json `host` block). Mirrors skillsync.canonical(); like
    it, REFUSES (propagates registry.AuthoredUnreadable) rather than returning
    empty when the authored layer exists but is unreadable."""
    p = _home.env("MCPS_CANONICAL")
    if p:
        with open(os.path.expanduser(p), encoding="utf-8") as f:
            d = json.load(f)
        if not isinstance(d, dict):
            raise ValueError("HELM_MCPS_CANONICAL must be a JSON object {name: config|null}")
        return d
    h = registry.authored_host().get("canonical_mcps")
    return dict(h) if isinstance(h, dict) else {}


# ---------------------------------------------------------------------------
# reading — every read fails toward its SAFE verdict (skip, report), never a
# silent partial mutate
# ---------------------------------------------------------------------------

def _read_json(path):
    """(data, error). A missing file is {} with no error (nothing to read);
    an unparseable one is (None, msg) — the fail-closed signal every caller
    checks before it would mutate."""
    try:
        with open(path, encoding="utf-8") as f:
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
    that names no real spec (a HELM_HOOKS_CANONICAL entry, or `record`, which
    lives in the tuple list alone).

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
    event, matcher, args, timeout = tup
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
    # a plugin `<name>@<source>` provides the `<name>` MCP (survey map).
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
        row["helm_hooks"] = sorted("%s %s" % (e, a) for e, a in present)
        row["strays"] = strays
        row["missing"] = ["%s %s" % (e, a) for e, a in sorted(want_ids - present)]
        row["mcps"] = home_mcps(cdir)
        homes.append(row)
        for m in row["missing"]:
            missing_map.setdefault(m, []).append(label)
        for s in strays:
            stray_map.setdefault(label, []).append("%s: %s" % (s["event"], s["command"]))

    # mcp variance: universal intersection + per-home effective + gaps
    eff_sets = [set(h.get("mcps", {}).get("effective", []))
                for h in homes if h["kind"] != "seat" and "mcps" in h]
    universal = sorted(set.intersection(*eff_sets)) if eff_sets else []
    canon_mcp = canonical_mcps()
    mcp_missing = {}
    for h in homes:
        if h["kind"] == "seat" or "mcps" not in h:
            continue
        gap = [n for n in canon_mcp if n not in h["mcps"]["effective"]]
        if gap:
            mcp_missing[h["label"]] = gap

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
            "missing_by_home": mcp_missing},
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
    shelf = os.path.join(backup_root, label)
    os.makedirs(shelf, exist_ok=True)
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
    _kindname, want = hooks_for(label)
    out = json.loads(json.dumps(cur))
    actions = {}
    for tup in want:
        spec = _spec(tup)
        actions[tup[2]] = _hooks._merge_event(out, spec)
    return out, actions, want


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
    return {"label": label, "kind": kind, "path": cdir,
            "verdict": "ok" if not changed else "change", "actions": actions,
            "strays": strays, "new_raw": new_raw, "diff": diff}


def _readraw(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read(), None
    except FileNotFoundError:
        return "", None
    except OSError as e:
        return "", str(e)


def apply_hooks_home(plan, backup_root):
    """Re-read and re-derive ONE hook plan through bounded exact-revision CAS."""
    from . import configs
    sp = os.path.join(plan["path"], "settings.json")
    label = plan["label"]

    def transform(cur):
        out, actions, want = _derive_hooks(label, cur)
        return out, {"actions": actions, "want": want}

    def verify(candidate, before, _metadata):
        expected, _actions, want = _derive_hooks(label, before)
        cmds = [c for _e, _m, c in _all_hooks(candidate)]
        return candidate == expected and all(
            _hooks.spec_command(_spec(tup)) in cmds for tup in want)

    res = configs.transform_json_file(sp, transform, verify=verify)
    if not res.get("ok"):
        return "FAIL", res["error"]
    if not res.get("wrote"):
        return "ok", "already current after CAS re-read"
    backup = res.get("backup")
    _log(backup_root, "%s hooks sync (backup: %s)" % (label, backup))
    return "applied", "backup: %s; CAS attempts: %d" % (
        backup or "none — new file", res["attempts"])


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

def plan_mcp_home(label, cdir):
    """Read-only MCP plan for ONE home -> dict. For each canonical name missing
    from the home's EFFECTIVE set: add (config in hand) | surface (no config /
    provided-by-plugin path unknown). Seats are skipped (no MCPs by design)."""
    kind = _kind(label)
    if kind == "seat":
        return {"label": label, "kind": kind, "path": cdir, "verdict": "skip",
                "detail": "seats carry no MCPs (delivery lane only)", "adds": [], "surface": []}
    canon = canonical_mcps()
    eff = home_mcps(cdir)
    adds, surface = [], []
    for name, cfg in canon.items():
        if name in eff["effective"]:
            continue
        if isinstance(cfg, dict):
            adds.append(name)
        else:
            surface.append(name)
    verdict = "ok" if not adds and not surface else "change"
    return {"label": label, "kind": kind, "path": cdir, "verdict": verdict,
            "adds": adds, "surface": surface, "effective": eff["effective"]}


def apply_mcp_home(plan, backup_root):
    """Enact ONE mcp plan's adds: write the missing servers into the home's
    .claude.json mcpServers, backup-first, SUPERSET-preserving (an existing
    server is never dropped) with rollback. -> (verdict, detail)."""
    if not plan["adds"]:
        return "ok", "nothing to add (surface-only)"
    canon = canonical_mcps()
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
        pk.atomic_write(statefile, json.dumps(out, indent=2) + "\n")
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
    """Reconcile canonical MCP servers into every home. Additive + fail-closed:
    a server is added only with a concrete config in hand, else surfaced;
    dry-run by default; idempotent."""
    if dirs is None:
        dirs = skillsync.config_dirs()
    backup_root = backup_root or BACKUP_ROOT
    plans, changed, failed, steady = [], [], [], 0
    for label, cdir in dirs:
        p = plan_mcp_home(label, cdir)
        plans.append(p)
        if p["verdict"] in ("ok", "skip"):
            steady += 1
            continue
        if apply and p["adds"]:
            v, detail = apply_mcp_home(p, backup_root)
            p["verdict"], p["detail"] = v, detail
            (failed if v == "FAIL" else changed).append(p)
        else:
            changed.append(p)
    return {"backup_root": backup_root, "apply": apply, "plans": plans,
            "changed": changed, "failed": failed, "steady": steady}


# ---------------------------------------------------------------------------
# 4. worktree gc — compose `helm work gc`, sweep orphan stubs + stray worktrees
# ---------------------------------------------------------------------------

def _worktree_rows(root, base, phantoms=None, registered=None, protected=()):
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
        merged = bool(branch) and work._merged(root, branch)
        occupied = work._occupants(w["path"])
        r = {"path": w["path"], "branch": branch, "locked": w["locked"],
             "occupied": occupied, "dirty": dirty, "merged": merged,
             "rescue": False, "remove": False}
        if w["locked"]:
            r["verdict"], r["why"] = "keep", "LOCKED (active review) — never touch"
        elif occupied:
            r["verdict"], r["why"] = "keep", \
                "OCCUPIED by cwd pid(s) %s — never remove" % ",".join(occupied)
        elif merged and not dirty:
            r.update(verdict="remove", remove=True,
                     why="clean + merged — remove worktree + delete branch")
        elif merged and dirty:
            r.update(verdict="rescue+remove", rescue=True, remove=True,
                     why="merged but dirty — rescue-commit to branch, then remove")
        elif dirty:
            r.update(verdict="rescue+keep", rescue=True,
                     why="dirty + unmerged — rescue-commit to branch, then KEEP "
                         "(ahead>0 needs land/review)")
        else:
            r.update(verdict="keep",
                     why="unmerged (ahead>0) — blocked; land or review before removal")
        rows.append(r)
    return rows


def _orphan_branches(root, base, pattern=None):
    """Cleanup-owned branches with NO registered worktree — the commit IS the
    work. Landed (by ancestry OR by patch identity) -> delete; anything else,
    including every unreadable case -> KEEP.

    THE SURFACE THE OWNER IS ACTUALLY LOOKING AT. Rooms are reaped by
    `work.gc_scan`; branches OUTLIVE their rooms — a live box once carried 107
    `lane/*` branches against 24 branch-holding worktrees — so most of what a
    sidebar shows has no room left to reap. This row asked ancestry alone,
    which is sha identity, while lands here are REBASED; it therefore answered
    "not merged" truthfully and kept them all.

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
        state = work._merge_state(root, b)
        merged = state in work.RETIRABLE
        rows.append({"branch": b, "merged": merged, "state": state,
                     "verdict": "delete" if merged else "keep",
                     "why": work._proof_word(state)})
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
            return ["SKIPPED %s (rescue commit failed: %s) — kept" % (r["path"], err)]
        lines.append("rescued dirty work -> %s" % (r["branch"] or "?"))
    elif r["rescue"]:
        lines.append("would rescue-commit dirty work -> %s" % (r["branch"] or "?"))
    if r["remove"]:
        if apply:
            blocked = work._removal_blocker(root, r["path"])
            if blocked:
                return lines + ["SKIPPED %s (%s) — kept" % (r["path"], blocked)]
            work._git(root, "worktree", "unlock", r["path"])
            rc, _o, err = work._git(root, "worktree", "remove", r["path"])
            if rc != 0:
                return lines + ["SKIPPED %s (%s)" % (r["path"], err)]
            lines.append("removed " + r["path"])
            if r["merged"] and r["branch"]:
                # Through the ONE branch-retirement actuator: it re-reads the
                # proof, picks -d vs the patch-identity -D, and prints the
                # restore line. The old inline `branch -d` also swallowed its
                # rc, so a refusal here was silent AND mislabelled "deleted".
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
    base = work._base(root)
    # (a) managed rooms — DELEGATE to work.gc (its lease-aware rescue logic,
    # plus lane/harness phantom ownership, reused exactly once).
    lane_rows = work.gc_scan(root, registered=registered)
    for row in lane_rows:
        if row["path"] in protected:
            row.update(verdict="keep", why="TARGET named by --repo — never remove")
    lane_lines = {}
    if apply:
        for lr in lane_rows:
            out = work.gc_enact(root, lr)
            if out:
                lane_lines[lr["lane"]] = out
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
    wt_rows = _worktree_rows(root, base, estate_phantoms,
                             registered=registered, protected=protected)
    wt_lines = {}
    for r in wt_rows:
        out = _enact_worktree(root, r, apply)
        if out:
            wt_lines[r["path"]] = out
    # (c) orphan branch stubs
    orphans = _orphan_branches(root, base)
    orphan_lines = {}
    for o in orphans:
        if o["verdict"] == "delete":
            if apply:
                # NO state handed in: the scan's verdict is re-proven here, one
                # call, because the branch can advance between the two and a
                # scan verdict is not a permission slip.
                orphan_lines[o["branch"]] = work._delete_lane_branch(
                    root, o["branch"])
            else:
                orphan_lines[o["branch"]] = ["would delete — " + o["why"]]
    return {"root": root, "base": base, "apply": apply,
            "lane_rows": lane_rows, "lane_lines": lane_lines,
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
    """Census + all three reconcilers (hooks, mcp, worktree), dry-run by
    default. --apply runs them all backup-first. One consolidated report."""
    if dirs is None:
        dirs = skillsync.config_dirs()
    backup_root = backup_root or BACKUP_ROOT
    return {
        "apply": apply, "backup_root": backup_root,
        "census": census(dirs=dirs, root=root),
        "hooks": hooks_sync(dirs=dirs, backup_root=backup_root, apply=apply),
        "mcp": mcp_sync(dirs=dirs, backup_root=backup_root, apply=apply),
        "worktree": worktree_gc(root=root, apply=apply),
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _print_census(r, out=sys.stdout):
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
                p("  %-26s %s" % (label, s))
    m = r["mcp_variance"]
    p("mcp universal (every non-seat home reaches): %s"
      % (", ".join(m["universal_effective"]) or "-"))
    if m["missing_by_home"]:
        p("mcp gaps (canonical %s):" % ", ".join(m["canonical_names"]))
        for label, gap in sorted(m["missing_by_home"].items()):
            p("  %-26s missing %s" % (label, ", ".join(gap)))
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
        canon_names = sorted(canonical_mcps())
    except registry.AuthoredUnreadable as e:
        print("helm mcp sync: authored layer unreadable (%s) — refusing; the "
              "canonical MCP set is unknown and a partial sync could shadow real "
              "servers. Config is recoverable from its .corrupt backup." % e,
              file=sys.stderr)
        return 2
    mode = "APPLIED" if apply else "dry-run (--apply to execute)"
    print("helm mcp sync [%s] — canonical names: %s (HELM_MCPS_CANONICAL overrides)"
          % (mode, ", ".join(canon_names)))
    for p in r["changed"]:
        bits = []
        if p["adds"]:
            bits.append(p.get("detail") or ("add %s" % ", ".join(p["adds"])))
        if p.get("surface"):
            bits.append("surface to owner (no config): %s" % ", ".join(p["surface"]))
        print("  %-26s %s" % (p["label"], "; ".join(bits)))
    print("helm mcp sync: %d home(s) with gaps, %d already canonical, %d failed"
          % (len(r["changed"]), r["steady"], len(r["failed"])))
    for p in r["failed"]:
        print("  FAIL %-26s %s" % (p["label"], p.get("detail", "")), file=sys.stderr)
    if apply and not r["failed"]:
        print("backups: " + r["backup_root"])
    return 1 if r["failed"] else 0


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
    paths = [row["path"] for row in r["lane_rows"] + r["worktree_rows"]]
    room_removed = sum(not os.path.exists(path) for path in paths)
    branch_removed = sum(row["verdict"] == "delete" and
                         not work._has_branch(r["root"], row["branch"])
                         for row in r["orphans"])
    phantom_removed = (len(r["managed_phantom_removed"])
                       + len(r["estate_phantom_removed"]))
    removed = room_removed + branch_removed + phantom_removed
    phantom_kept = (len(r["managed_phantoms"]) + len(r["estate_phantoms"])
                    - phantom_removed)
    triage = (sum(row["verdict"] in ("triage", "rescue")
                   for row in r["lane_rows"])
              + sum(row["verdict"] in ("keep", "rescue+keep") and
                    (not row.get("merged") or row.get("dirty"))
                    for row in r["worktree_rows"])
              + sum(row["verdict"] == "keep" for row in r["orphans"])
              + phantom_kept)
    total = (len(paths) + len(r["orphans"]) + len(r["managed_phantoms"])
             + len(r["estate_phantoms"]))
    line = work.format_gc_summary(r["root"], removed, total - removed, triage)
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
    sync + worktree gc, all dry-run by default. One consolidated 'here is
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
    print("\n[1/4] CENSUS")
    _print_census(r["census"])
    print("\n[2/4] HOOKS SYNC")
    h = r["hooks"]
    if not h["changed"] and not h["failed"]:
        print("  every home carries its full canonical hook set (0 changes)")
    for p in h["changed"]:
        adds = [a for a, act in p["actions"].items() if act == "add"]
        upds = [a for a, act in p["actions"].items() if act == "update"]
        print("  %-26s %s%s" % (p["label"],
              "+%s " % ", ".join(adds) if adds else "",
              "~%s" % ", ".join(upds) if upds else p.get("detail", "")))
    for p in h["failed"]:
        print("  FAIL %-26s %s" % (p["label"], p.get("detail", "")))
    print("\n[3/4] MCP SYNC")
    m = r["mcp"]
    if not m["changed"] and not m["failed"]:
        print("  every home reaches the canonical MCP set (0 changes)")
    for p in m["changed"]:
        bits = []
        if p["adds"]:
            bits.append("add %s" % ", ".join(p["adds"]))
        if p.get("surface"):
            bits.append("surface (no config): %s" % ", ".join(p["surface"]))
        print("  %-26s %s" % (p["label"], "; ".join(bits)))
    print("\n[4/4] WORKTREE GC")
    _print_worktree(r["worktree"])
    print("\n" + "=" * 72)
    fails = len(h["failed"]) + len(m["failed"])
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
