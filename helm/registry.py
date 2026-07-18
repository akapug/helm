#!/usr/bin/env python3
"""The helm registry — the master project list + per-project overlay pointers.

The registry is a PROJECTION of observed reality (harness stores + disk) plus
two authored layers that survive re-sync: lineage EDGES and manual annotations.
Sync law: additive and idempotent — a re-sync refreshes observations, never
deletes a known project, never touches authored fields.

Adoption law: where a project already has a knowledge home (mission-control's
~/.mc/mission-control), ~/.helm/<name> becomes a SYMLINK to it — one chain,
never a second copy.
"""
import os
import time

from . import automap, home, pk

# Authored fields on a project record that a re-sync must never clobber.
AUTHORED_FIELDS = ("edges", "notes", "aliases", "external", "retired")

# Existing knowledge homes helm adopts by symlink instead of scaffolding.
ADOPTED_HOMES = {
    "mission-control": os.path.join(os.path.expanduser("~"), ".mc", "mission-control"),
}


def load():
    return pk.read_json(home.registry_path(), {"version": 1, "projects": {}})


def save(reg):
    reg["generated_ts"] = pk.now_ts()
    pk.write_json(home.registry_path(), reg)


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
    rec["cv_scope"] = {"cwd_prefix": rec["path"]}
    rec["home"] = home.project_dir(rec["name"])
    return rec


def sync(observations=None):
    """Run the auto-map, merge into the persistent registry, scaffold homes.
    Returns (registry, report) where report = {"new": [...], "updated": [...]}."""
    reg = load()
    known = reg.setdefault("projects", {})
    mapped = automap.build_map(observations=observations)
    report = {"new": [], "updated": []}

    # match on path (a rename of the display name must not duplicate a project)
    by_path = {p["path"]: n for n, p in known.items()}
    for name, rec in sorted(mapped.items()):
        cur_name = by_path.get(rec["path"], name)
        cur = known.get(cur_name)
        _overlay_pointers(rec)
        if cur is None:
            rec["first_seen"] = rec["last_seen"] or time.time()
            known[cur_name] = rec
            report["new"].append(cur_name)
        else:
            preserved = {k: cur[k] for k in AUTHORED_FIELDS if k in cur}
            preserved["first_seen"] = cur.get("first_seen", rec["last_seen"])
            rec["name"] = cur_name
            cur.update(rec)
            cur.update(preserved)
            report["updated"].append(cur_name)

    home.scaffold_global()
    for name, rec in known.items():
        if rec.get("external") or rec.get("retired"):
            continue
        _adopt_or_scaffold(name)
    save(reg)
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
