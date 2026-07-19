#!/usr/bin/env python3
"""The helm registry — the master project list + per-project overlay pointers.

Two files under the helm home, composed at read time:
  registry.json           the PROJECTION of observed reality (harness stores +
                          disk) — rebuildable from a re-scan, safe to regenerate.
  registry-authored.json  the AUTHORED layer (AUTHORED_FIELDS: what a human or
                          agent wrote) — keyed by project name, path-stamped,
                          unrebuildable, never regenerated.
load() returns the merged view (authored fields overlay their path-matched
projection record; external anchors materialize even with no projection record)
so every caller sees the pre-split shape; save() splits the merged view back
out. A mixed-era registry.json migrates its authored fields out ONCE, at load,
losslessly. Sync law: additive and idempotent — a re-sync refreshes
observations, never deletes a known project, never touches authored fields.

Adoption law: where a project already has a knowledge home (mission-control's
~/.mc/mission-control), ~/.helm/<name> becomes a SYMLINK to it — one chain,
never a second copy.

Also home of the PROJECTION REGISTRY (projections() + projection_survey()):
constitution laws 2+3 as an executable manifest — every on-disk store helm
reads or writes declares its class, and every projection its source + rebuild.
`helm projections` is the read surface; doctor.check_projection_registry
enforces it.
"""
import fnmatch
import os
import shutil
import time

from . import automap, home, pk

# Authored fields on a project record that a re-sync must never clobber.
AUTHORED_FIELDS = ("edges", "notes", "aliases", "external", "retired")

# Existing knowledge homes helm adopts by symlink instead of scaffolding.
ADOPTED_HOMES = {
    "mission-control": os.path.join(os.path.expanduser("~"), ".mc", "mission-control"),
}


def _authored_load():
    """The authored layer, with a corruption net: an unparseable file is backed
    up beside itself BEFORE any caller can save over it — authored content is
    unrebuildable, so a garbled byte must never cascade into an empty rewrite
    (cross-family review finding, 2026-07-19)."""
    path = home.authored_path()
    val = pk.read_json(path)
    if isinstance(val, dict):
        return val
    if os.path.exists(path):
        bak = path + ".corrupt-" + pk.now_ts().replace(":", "")
        if not os.path.exists(bak):
            shutil.copy2(path, bak)
    return {"version": 1, "projects": {}}


def _qualified(name, path):
    """The collision key for a same-name entry authored against a different
    path — both survive; the path stamp decides which one a project reads."""
    import hashlib
    return "%s@%s" % (name, hashlib.sha1((path or "").encode()).hexdigest()[:8])


def _authored_for(entries, name, path):
    """The authored entry for (name, path): the name key when its stamp
    matches, else the path-qualified key. None when neither matches."""
    entry = entries.get(name)
    if entry is not None and entry.get("path", path) == path:
        return entry
    return entries.get(_qualified(name, path))


def load():
    """The merged view. Migration: authored fields found inline in a mixed-era
    registry.json move to the authored file ONCE (idempotent; an already-
    authored value outranks a stale mixed copy; a same-name entry authored
    against a different path is never grafted onto — the collision class that
    once let a project inherit another's edges)."""
    reg = pk.read_json(home.registry_path(), {"version": 1, "projects": {}})
    projects = reg.setdefault("projects", {})
    auth = _authored_load()
    entries = auth.setdefault("projects", {})
    moved = False
    for name, rec in projects.items():
        found = [k for k in AUTHORED_FIELDS if k in rec]
        if not found:
            continue
        entry = entries.setdefault(name, {"path": rec.get("path", "")})
        if entry.get("path", rec.get("path")) != rec.get("path"):
            continue
        for k in found:
            entry.setdefault(k, rec.pop(k))
        moved = True
    if moved:
        pk.write_json(home.authored_path(), auth)
        pk.write_json(home.registry_path(), reg)
    for name, rec in projects.items():
        entry = _authored_for(entries, name, rec.get("path"))
        if entry:
            rec.update({k: entry[k] for k in AUTHORED_FIELDS if k in entry})
    for name, entry in entries.items():  # external anchors outlive a projection wipe
        if name in projects or not entry.get("external"):
            continue
        rec = {"name": name, "path": entry.get("path", ""), "kind": "external",
               "status": "external", "sessions": {}, "last_seen": None}
        rec.update({k: entry[k] for k in AUTHORED_FIELDS if k in entry})
        projects[name] = rec
    return reg


def save(reg):
    """Split write: authored fields -> registry-authored.json (per project,
    path-stamped), everything else -> registry.json (pure projection). Entries
    for projects absent from reg are left alone — a partial save never deletes
    authored content."""
    reg["generated_ts"] = pk.now_ts()
    auth = _authored_load()
    entries = auth.setdefault("projects", {})
    proj = dict(reg)
    proj["projects"] = {}
    for name, rec in reg.get("projects", {}).items():
        keep = {k: rec[k] for k in AUTHORED_FIELDS if k in rec}
        if keep:
            keep["path"] = rec.get("path", "")
            cur = entries.get(name)
            if cur is not None and cur.get("path", keep["path"]) != keep["path"]:
                # a same-name entry authored against a DIFFERENT path: never
                # clobber it (unrebuildable) — the newcomer's authored fields
                # land under the path-qualified key; both survive, load()
                # resolves by path stamp (cross-family review finding)
                entries[_qualified(name, keep["path"])] = keep
            else:
                entries[name] = keep
        proj["projects"][name] = {k: v for k, v in rec.items() if k not in AUTHORED_FIELDS}
    pk.write_json(home.authored_path(), auth)
    pk.write_json(home.registry_path(), proj)


def _overlay_pointers(rec):
    """The read-only pointers a project record carries to its authoritative
    stores: repo (truth), per-project memory dir (claude projection), cv scope
    (recall). helm references these — it never copies them."""
    mem = None
    for cwd in [rec["path"]] + rec.get("cwds", []):
        d = home.claude_memory_dir_for(cwd)
        if os.path.isdir(d):
            mem = d
            break
    rec["memory_dir"] = mem
    # cv prefix-matches on recorded cwds; sibling-dir worktrees don't share the
    # canonical root's prefix, so the scope carries every observed cwd too.
    rec["cv_scope"] = {"cwd_prefix": rec["path"],
                       "cwd_prefixes": sorted({rec["path"], *rec.get("cwds", [])})}
    rec["home"] = home.project_dir(rec["name"])
    return rec


def sync(observations=None):
    """Run the auto-map, merge into the persistent registry, scaffold homes.
    Returns (registry, report) where report = {"new": [...], "updated": [...]}."""
    reg = load()
    known = reg.setdefault("projects", {})
    mapped = automap.build_map(observations=observations)
    report = {"new": [], "updated": []}

    # PATH is identity. Match on path first (a display-name rename must not
    # duplicate a project). Only merge into an existing record when its path
    # matches; a same-BASENAME newcomer at a different path must get a fresh
    # non-colliding name, never inherit the incumbent's authored edges/notes.
    by_path = {p["path"]: n for n, p in known.items()}
    for name, rec in sorted(mapped.items()):
        _overlay_pointers(rec)
        cur_name = by_path.get(rec["path"])
        if cur_name is None and name in known and known[name].get("path") != rec["path"]:
            # basename collision with a DIFFERENT project — mint a distinct name
            taken = {n: p.get("path", "") for n, p in known.items()}
            cur_name = automap._name_for(rec["path"], taken, os.path.expanduser("~"))
            if cur_name in known:  # last-resort disambiguation
                cur_name = cur_name + "-" + rec["path"].strip("/").replace("/", "-")[-24:]
        cur = known.get(cur_name) if cur_name else None
        if cur is None:
            cur_name = cur_name or name
            rec["name"] = cur_name
            rec["first_seen"] = rec["last_seen"] or time.time()
            known[cur_name] = rec
            by_path[rec["path"]] = cur_name
            report["new"].append(cur_name)
        else:
            preserved = {k: cur[k] for k in AUTHORED_FIELDS if k in cur}
            preserved["first_seen"] = cur.get("first_seen", rec["last_seen"])
            rec["name"] = cur_name
            cur.update(rec)
            cur.update(preserved)
            report["updated"].append(cur_name)

    # second discovery tier: on-disk git repos with no observed agent activity
    # register as SHELF nodes — lineage/archive-report substrate, no home
    # scaffold, hidden from the default project list. An observed project is
    # never demoted to shelf (observation outranks presence).
    by_path = {p["path"]: n for n, p in known.items()}
    for path, name in automap.scan_repos().items():
        if path in by_path:
            continue
        shelf_name = name if name not in known else automap._name_for(path, {n: p["path"] for n, p in known.items()}, os.path.expanduser("~"))
        known[shelf_name] = {
            "name": shelf_name, "path": path, "kind": "git", "status": "shelf",
            "sessions": {}, "first_seen": time.time(), "last_seen": None,
        }
        by_path[path] = shelf_name
        report.setdefault("shelved", []).append(shelf_name)

    home.scaffold_global()
    for name, rec in known.items():
        if rec.get("external") or rec.get("retired") or rec.get("status") == "shelf":
            continue
        _adopt_or_scaffold(name)
    save(reg)
    reg = load()  # re-compose: a re-discovered project picks its authored fields back up
    _write_project_registries(reg)
    return reg, report


def _adopt_or_scaffold(name):
    p = home.project_dir(name)
    adopted = ADOPTED_HOMES.get(name)
    if adopted and os.path.isdir(adopted):
        if os.path.islink(p):
            return
        if not os.path.exists(p):
            os.symlink(adopted, p)
            return
        # a real dir already exists where the adoption symlink belongs — keep it
        # (never destroy user data); the doctor surfaces the conflict.
        return
    home.scaffold_project(name)


def _write_project_registries(reg):
    """Each project home mirrors its own record — the chain travels as one unit."""
    for name, rec in reg["projects"].items():
        p = home.project_dir(name)
        if not os.path.isdir(p) or os.path.islink(p):
            continue
        pk.write_json(os.path.join(p, "registry.json"), rec)


def get(name):
    return load()["projects"].get(name)


def add_external(name, path, note=""):
    """Register a read-only external node (vendored/reference/upstream clone).
    External nodes anchor lineage and are never cleanup candidates."""
    reg = load()
    if name not in reg["projects"]:
        reg["projects"][name] = {
            "name": name, "path": path, "kind": "external", "status": "external",
            "external": True, "notes": note, "sessions": {}, "edges": [],
            "first_seen": time.time(), "last_seen": None,
        }
        save(reg)
    return reg["projects"][name]


def add_edge(src, rel, dst, note="", confirmed=True):
    """Lineage edge on the source project: rel in
    forked-from | composes | supersedes | launched-as (+ free-form)."""
    reg = load()
    p = reg["projects"].get(src)
    if p is None:
        return None, "unknown project '%s' (helm sync first, or add-external)" % src
    edges = p.setdefault("edges", [])
    for e in edges:
        if e["rel"] == rel and e["to"] == dst:
            e.update({"note": note or e.get("note", ""), "confirmed": confirmed})
            save(reg)
            return e, None
    e = {"rel": rel, "to": dst, "note": note, "confirmed": confirmed, "ts": pk.now_ts()}
    edges.append(e)
    save(reg)
    return e, None


# ---------------------------------------------------------------------------
# projection registry — constitution laws 2+3 as an executable manifest
# ---------------------------------------------------------------------------

# Every on-disk store helm reads or writes, classified:
#   authored     source of truth — unrebuildable, never regenerated, ships
#   projection   regenerable view — source + rebuild REQUIRED; a projection
#                that cannot name its rebuild cannot be safely wiped or
#                gitignored (the ship-pull derived/authored split rests here)
#   events       append-only receipts — lossy-by-design, never truth
#   state        host-local runtime — safe to lose, readers fail open
#   backup       recovery copies of authored/config content
# doctor walks the manifest READ-ONLY (it reports; rebuilds stay with their
# legs: sync/sessions/inject/drift); rebuild-and-converge is pinned by the
# test suite, not run in doctor.
KINDS = ("authored", "projection", "events", "state", "backup")


def cache_root():
    """~/.cache/helm — the second classified root. HELM_CACHE_DIR overrides
    (inject._cache_file's law), which is how tests point it at a tmp dir."""
    return home.env("CACHE_DIR") or os.path.join(os.path.expanduser("~"), ".cache", "helm")


def projections():
    """The executable manifest: one row per declared on-disk store —
    {name, kind, root, globs, source, sources, rebuild, fresh_days,
    mutable: False}. globs are root-relative, fnmatch semantics (* crosses
    /); `sources` are the authoritative paths a projection re-derives from
    (at least one must exist while the projection does); `fresh_days` is the
    declared staleness horizon (None = self-invalidating: sig-keyed or TTL).
    Any file under either root that NO row names is an unclassified
    squatter — the ~/.remember rot class, flagged by doctor."""
    from . import store
    u = os.path.expanduser("~")
    scans = tuple(r for r in (home.env("SCAN_ROOTS") or "").split(":") if r)
    harness_roots = (os.path.join(u, ".claude", "projects"),
                     os.path.join(u, ".codex", "sessions"),
                     os.path.join(u, ".local", "share", "opencode")) + scans
    store_roots = (store.adopted_dir(), home.global_dir())
    master = home.registry_path()
    g_cats = sorted(set(home.GLOBAL_CATEGORIES) | set(store.TYPE_SUBDIR.values()))
    # + reflexes: mentor-taught project reflexes are authored (provenanced)
    p_cats = sorted(set(home.PROJECT_CATEGORIES) | set(store.TYPE_SUBDIR.values())
                    | {"reflexes"})

    def row(name, kind, root, globs, source="", sources=(), rebuild=None, fresh_days=None):
        return {"name": name, "kind": kind, "root": root, "globs": tuple(globs),
                "source": source, "sources": tuple(sources), "rebuild": rebuild,
                "fresh_days": fresh_days, "mutable": False}

    return (
        row("registry", "projection", "home", ("_global/registry.json",),
            source="harness session stores + disk repo scan",
            sources=harness_roots, rebuild="helm sync", fresh_days=30),
        row("project-registry", "projection", "home", ("*/registry.json",),
            source="_global/registry.json (+ authored layer), re-mirrored per project",
            sources=(master,), rebuild="helm sync", fresh_days=30),
        row("registry-authored", "authored", "home", ("_global/registry-authored.json",)),
        row("store-global", "authored", "home",
            tuple("_global/%s/*" % c for c in g_cats)),
        row("store-project", "authored", "home",
            tuple("*/%s/*" % c for c in p_cats)),
        row("host-blocks", "projection", "home", ("_global/hosts/*",),
            source="this host's registry.json observations",
            sources=(master,), rebuild="helm ship --apply"),
        row("gitignore", "authored", "home", (".gitignore",)),
        row("events-journal", "events", "home", ("_global/.state/events.jsonl*",)),
        row("inject-ledger", "events", "home", ("_global/.state/inject-ledger.jsonl*",)),
        row("attest-queue", "state", "home", ("_global/.state/attest-queue.jsonl",)),
        row("inject-seen", "state", "home", ("_global/.state/inject-seen/*",)),
        row("coinages", "state", "home", ("_global/.state/coinages.json",)),
        row("drift-snapshot", "projection", "home",
            ("_global/.state/drift-snapshot*.json",),
            source="the typed store's prior confidences",
            sources=store_roots, rebuild="helm drift"),
        row("now", "state", "home", ("_global/now.md",)),
        row("chat-node", "state", "home", ("_global/.state/chat-node.json",)),
        row("cells", "state", "home", ("_global/.state/cells.json",)),
        row("reflex-state", "state", "home", ("_global/.state/reflex-state/*",)),
        row("seats", "state", "home", ("_global/seats/*",)),
        row("catalog-cache", "projection", "cache", ("catalog-cache.json",),
            source="local claude/codex transcripts (cv ls, or the scanner)",
            sources=harness_roots, rebuild="helm sessions"),
        row("syn-cache", "projection", "cache", ("syn-cache.json",),
            source="transcript first-bytes (synthetic-session peek)",
            sources=harness_roots, rebuild="helm sessions"),
        row("store-cache", "projection", "cache", ("store-cache-*.json",),
            source="the typed store roots (stat-signature keyed)",
            sources=store_roots, rebuild="helm inject"),
        row("cwd-overrides", "authored", "cache", ("cwd-overrides.json",)),
        row("mints", "events", "cache", ("mints.jsonl",)),
        row("keepalive-log", "events", "cache", ("keepalive-log.jsonl",)),
        row("usage-history", "events", "cache", ("native-usage-history.jsonl",)),
        row("backups", "backup", "cache",
            ("config-backups/*", "settings-backups/*", "skills-backups/*",
             "skills-trash/*")),
        row("scratch", "state", "cache", ("*.lock", "*.tmp")),
    )


def _walk_root(root_dir):
    """Every regular file under root_dir as /-relative paths. .git pruned and
    symlinks never crossed or listed — a symlink is a pointer into someone
    else's estate (the adoption law), not a store to classify."""
    out = []
    for dirpath, dirnames, filenames in os.walk(root_dir):
        dirnames[:] = [d for d in dirnames if d != ".git"
                       and not os.path.islink(os.path.join(dirpath, d))]
        for n in filenames:
            p = os.path.join(dirpath, n)
            if not os.path.islink(p):
                out.append(os.path.relpath(p, root_dir))
    return sorted(out)


def projection_survey():
    """The manifest joined to disk: (rows, squatters) where each row copies
    its manifest row + `files` (matched, root-relative) and squatters is
    {"home": [...], "cache": [...]} — every file no row names. Overlapping
    rows both collect a file (classification, not ownership). Read-only;
    an absent root reads as empty."""
    rows = [dict(r, files=[]) for r in projections()]
    squat = {}
    for root_key, root_dir in (("home", home.helm_home()), ("cache", cache_root())):
        files = _walk_root(root_dir) if os.path.isdir(root_dir) else []
        mine = [r for r in rows if r["root"] == root_key]
        squat[root_key] = []
        for f in files:
            hit = False
            for r in mine:
                if any(fnmatch.fnmatchcase(f, g) for g in r["globs"]):
                    r["files"].append(f)
                    hit = True
            if not hit:
                squat[root_key].append(f)
    return rows, squat


def cmd_projections(args):
    """projections [--json] — the projection registry: every on-disk store
    helm writes, classified (authored/projection/events/state/backup), each
    projection naming its source + rebuild. Laws 2+3's read surface;
    `helm doctor` enforces it (source present, rebuild declared, staleness,
    squatters)."""
    rows, squat = projection_survey()
    if "--json" in args:
        import json
        print(json.dumps({"rows": rows, "squatters": squat}, ensure_ascii=False, indent=2))
        return 0
    print("helm projections (%d rows over %s + %s):"
          % (len(rows), home.helm_home(), cache_root()))
    w = max(len(r["name"]) for r in rows)
    for r in rows:
        extra = "" if r["kind"] != "projection" else \
            "  <- %s | rebuild: %s" % (r["source"], r["rebuild"])
        n = len(r["files"])
        print("  %-*s %-10s %4d file%s%s" % (w, r["name"], r["kind"], n,
                                             "s"[:n != 1] or " ", extra))
    n = sum(len(v) for v in squat.values())
    if not n:
        print("  no unclassified squatters")
        return 0
    print("  %d UNCLASSIFIED squatter file%s (no row names them — classify or evict):"
          % (n, "s"[:n != 1]))
    for root_key, root_dir in (("home", home.helm_home()), ("cache", cache_root())):
        for f in squat[root_key][:8]:
            print("    ? " + os.path.join(root_dir, f))
        if len(squat[root_key]) > 8:
            print("    … +%d more under %s" % (len(squat[root_key]) - 8, root_dir))
    return 0
