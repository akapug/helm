#!/usr/bin/env python3
"""The project-lineage map — the registry's EDGE layer, thin-first and READ-ONLY.

Edges live on each project record (registry.add_edge): forked-from | composes |
supersedes | launched-as | checkout-of (+ free-form). External read-only nodes
(vendored/reference/upstream clones, registry.add_external) anchor lineage and
are NEVER cleanup candidates.

This slice derives and renders; it moves nothing. archive_report() is a ranked
"safe to archive and why" REPORT — the executable archive-move is a later slice
behind a dry-run+confirm gate (lineage-map-decisions Q3).

Seed law: lineage_seed.json carries an illustrative founding ancestry (the
shipped default names no real estate — operators seed their own) and applies
idempotently — externals only where the path exists and the name is not already
a registry project, edges only where both endpoints are known (missing
endpoints skip quietly + report).
"""
import os
import subprocess
import sys
import time

from . import pk, registry

SEED_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lineage_seed.json")

# Rels that place a node UNDER its parent in the tree (edge src = child,
# edge dst = parent). checkout-of is ancestry for rendering — a checkout hangs
# under the project it duplicates.
ANCESTRY_RELS = ("descends-from", "forked-from", "checkout-of")

# Rels rendered as components under the SOURCE node (mc composes dregg -> dregg
# indents under mc).
COMPOSE_RELS = ("composes",)

# Rels that never make a dormant node a lineage ANCHOR of an active project:
# a checkout duplicate and a superseded brand are the archive candidates,
# not the anchors.
NON_ANCHOR_RELS = ("checkout-of", "supersedes")


def apply_seed(seed=None):
    """Apply the packaged (or given) seed idempotently. Externals first (path
    exists AND name not already a registry project), then edges where BOTH
    endpoints exist. Returns {"externals", "externals_skipped", "edges",
    "edges_skipped"} — skipped edges carry their missing endpoints."""
    if seed is None:
        seed = pk.read_json(SEED_PATH, {"edges": [], "external": []})
    report = {"externals": [], "externals_skipped": [], "edges": [], "edges_skipped": []}

    known = registry.load().get("projects", {})
    for x in seed.get("external", []):
        name = x["name"]
        path = os.path.expanduser(x.get("path", ""))
        if name in known:
            report["externals_skipped"].append({"name": name, "reason": "already in registry"})
            continue
        if not os.path.exists(path):
            report["externals_skipped"].append({"name": name, "reason": "path missing: " + path})
            continue
        registry.add_external(name, path, x.get("note", ""))
        report["externals"].append(name)

    known = registry.load().get("projects", {})
    for e in seed.get("edges", []):
        src, rel, dst = e["src"], e["rel"], e["dst"]
        missing = [n for n in (src, dst) if n not in known]
        if missing:
            report["edges_skipped"].append({"src": src, "rel": rel, "dst": dst, "missing": missing})
            continue
        registry.add_edge(src, rel, dst, e.get("note", ""), e.get("confirmed", True))
        report["edges"].append("%s -%s-> %s" % (src, rel, dst))
    return report


def graph():
    """The full node+edge view: {name: {"edges": [...], "rec": record}}."""
    reg = registry.load()
    return {n: {"edges": r.get("edges", []), "rec": r}
            for n, r in reg.get("projects", {}).items()}


def _tags(rec):
    if rec is None:
        return " [unknown]"
    t = ""
    if rec.get("external"):
        t += " [external]"
    if rec.get("retired"):
        t += " [retired]"
    if rec.get("status") == "dormant":
        t += " [dormant]"
    return t


def render(fmt="tree"):
    """Readable ASCII lineage, grouped by family. Roots = nodes with no outgoing
    ancestry edge; children indent under parents with the rel on the connector;
    non-ancestry edges (supersedes/launched-as/free-form) render as `·` marks
    under their source. Edge-less nodes list under "unmapped". A multi-parent
    node expands once and is `(see above)` after."""
    if fmt != "tree":
        raise ValueError("unsupported lineage render format: %r" % (fmt,))
    g = graph()
    if not g:
        return "(no projects — run `helm sync` first)"

    children, annots, has_parent = {}, {}, set()
    for name, node in g.items():
        for e in node["edges"]:
            rel, dst, note = e.get("rel", ""), e.get("to", ""), e.get("note", "")
            if rel in ANCESTRY_RELS:
                children.setdefault(dst, []).append((name, rel, note))
                has_parent.add(name)
            elif rel in COMPOSE_RELS:
                children.setdefault(name, []).append((dst, rel, note))
                has_parent.add(dst)
            else:
                annots.setdefault(name, []).append((rel, dst, note))

    expanded = set()
    out = []

    def walk(name, rel, note, prefix, branch):
        rec = g[name]["rec"] if name in g else None
        line = prefix + branch + name
        if rel:
            line += " (%s)" % rel
        line += _tags(rec)
        if note:
            line += " — " + note
        kids = sorted(children.get(name, []))
        marks = sorted(annots.get(name, []))
        if name in expanded and (kids or marks):
            out.append(line + "  (see above)")
            return
        out.append(line)
        expanded.add(name)
        ext = prefix + ("│   " if branch == "├─ " else "    " if branch == "└─ " else "")
        total = len(kids) + len(marks)
        for i, (child, crel, cnote) in enumerate(kids):
            walk(child, crel, cnote, ext, "└─ " if i == total - 1 else "├─ ")
        for j, (arel, adst, anote) in enumerate(marks):
            mline = ext + ("└· " if len(kids) + j == total - 1 else "├· ")
            mline += "%s %s" % (arel, adst)
            if anote:
                mline += " — " + anote
            out.append(mline)

    roots = sorted((set(g) | set(children)) - has_parent)
    families = [r for r in roots if children.get(r) or annots.get(r)]
    unmapped = [r for r in roots if not (children.get(r) or annots.get(r))]
    for i, r in enumerate(families):
        if i:
            out.append("")
        walk(r, None, None, "", "")
    if unmapped:
        if out:
            out.append("")
        # shelf nodes (on disk, no observed activity) compress to one line —
        # 100+ of them would bury the observed unmapped set that matters.
        shelf = [r for r in unmapped
                 if r in g and (g[r]["rec"] or {}).get("status") == "shelf"]
        rest = [r for r in unmapped if r not in shelf]
        if rest:
            out.append("unmapped (no lineage edges):")
            for r in rest:
                out.append("  " + r + _tags(g[r]["rec"] if r in g else None))
        if shelf:
            out.append("(+%d shelf repos unmapped — `helm projects --all`)" % len(shelf))
    return "\n".join(out)


def _du_mb(path):
    try:
        out = subprocess.run(["du", "-sm", path], capture_output=True, text=True, timeout=60)
    except Exception:
        return None
    if out.returncode != 0:
        return None
    try:
        return int(out.stdout.split()[0])
    except (ValueError, IndexError):
        return None


def _git_dirty(path):
    """None = not a git repo; -1 = status failed (cannot verify); else the count
    of uncommitted paths in `git status --porcelain`. A linked worktree's .git
    is a FILE — exists, not isdir, or a dirty checkout dup would slip through."""
    if not os.path.exists(os.path.join(path, ".git")):
        return None
    try:
        out = subprocess.run(["git", "-C", path, "status", "--porcelain"],
                             capture_output=True, text=True, timeout=60)
    except Exception:
        return -1
    if out.returncode != 0:
        return -1
    return len([l for l in out.stdout.splitlines() if l.strip()])


def archive_report():
    """Ranked "safe to archive and why" — READ-ONLY, moves nothing. Candidates =
    dormant, non-external registry projects. HOLD when the node lineage-anchors
    an active project (reachable via non-checkout/supersedes edges) or its git
    tree is dirty/unverifiable. Checkout duplicates of an active project rank
    safest. Rows: {name, path, why_safe, why_not, disk_mb, verdict}."""
    projects = registry.load().get("projects", {})
    active = {n for n, r in projects.items() if r.get("status") == "active"}

    adj = {}
    for n, r in projects.items():
        for e in r.get("edges", []):
            if e.get("rel") in NON_ANCHOR_RELS:
                continue
            d = e.get("to")
            adj.setdefault(n, set()).add(d)
            adj.setdefault(d, set()).add(n)

    def anchored_to(name):
        seen, queue = {name}, [name]
        while queue:
            for nxt in sorted(adj.get(queue.pop(), ())):
                if nxt in seen:
                    continue
                if nxt in active:
                    return nxt
                seen.add(nxt)
                queue.append(nxt)
        return None

    rows = []
    for name, rec in sorted(projects.items()):
        if rec.get("status") != "dormant" or rec.get("external"):
            continue
        path = rec.get("path", "")
        why_safe, why_not = [], []
        dup_of = None
        for e in rec.get("edges", []):
            if e.get("rel") == "checkout-of" and e.get("to") in active:
                dup_of = e["to"]
        if dup_of:
            why_safe.append("worktree/checkout duplicate of active '%s'" % dup_of)
        for a in sorted(active):
            for e in projects[a].get("edges", []):
                if e.get("rel") == "supersedes" and e.get("to") == name:
                    why_safe.append("superseded by active '%s'" % a)
        anchor = anchored_to(name)
        if anchor:
            why_not.append("lineage anchor — linked to active '%s' via edges" % anchor)
        else:
            why_safe.append("no lineage link to any active project")
        last = rec.get("last_seen")
        if last:
            why_safe.append("dormant %dd" % int((time.time() - last) / 86400))
        else:
            why_safe.append("dormant (no recorded activity)")
        dirty = _git_dirty(path) if path else None
        if dirty is not None and dirty < 0:
            why_not.append("git status failed — cannot verify a clean tree")
        elif dirty:
            why_not.append("uncommitted changes (%d paths in git status --porcelain)" % dirty)
        elif dirty == 0:
            why_safe.append("clean git tree")
        rows.append({
            "name": name, "path": path, "why_safe": why_safe, "why_not": why_not,
            "disk_mb": _du_mb(path) if path and os.path.isdir(path) else None,
            "verdict": "hold" if why_not else "safe",
            "checkout_dup": bool(dup_of),
        })
    rows.sort(key=lambda r: (r["verdict"] != "safe", not r["checkout_dup"],
                             -(r["disk_mb"] or 0), r["name"]))
    return rows


def cmd_lineage(args):
    """lineage [seed | add <src> <rel> <dst> [note] | external <name> <path> [note] | archive-report] — the project-lineage map (no sub-verb renders the tree)."""
    if not args:
        print(render())
        return 0
    verb, rest = args[0], args[1:]
    if verb == "seed":
        # seed MUTATES the registry; trailing junk refuses before it applies.
        from .cli import guard_tail
        rc = guard_tail("helm lineage seed", rest, usage="lineage seed")
        if rc is not None:
            return rc
        rep = apply_seed()
        ne, nx = len(rep["edges"]), len(rep["externals"])
        print("helm lineage seed: %d external%s added, %d edge%s applied" % (
            nx, "s"[:nx != 1], ne, "s"[:ne != 1]))
        for n in rep["externals"]:
            print("  + [external] " + n)
        for e in rep["edges"]:
            print("  + " + e)
        for s in rep["externals_skipped"]:
            print("  ~ external '%s' skipped: %s" % (s["name"], s["reason"]))
        for s in rep["edges_skipped"]:
            print("  ! edge %s -%s-> %s skipped: missing %s (helm sync, or `helm lineage external`)"
                  % (s["src"], s["rel"], s["dst"], ", ".join(s["missing"])))
        return 0
    if verb == "add":
        if len(rest) < 3:
            print("usage: helm lineage add <src> <rel> <dst> [note...]", file=sys.stderr)
            return 2
        _, err = registry.add_edge(rest[0], rest[1], rest[2], " ".join(rest[3:]))
        if err:
            print("helm: " + err, file=sys.stderr)
            return 1
        print("edge: %s -%s-> %s" % (rest[0], rest[1], rest[2]))
        return 0
    if verb == "external":
        if len(rest) < 2:
            print("usage: helm lineage external <name> <path> [note...]", file=sys.stderr)
            return 2
        path = os.path.abspath(os.path.expanduser(rest[1]))
        registry.add_external(rest[0], path, " ".join(rest[2:]))
        print("external: %s -> %s" % (rest[0], path))
        return 0
    if verb == "archive-report":
        from .cli import guard_tail
        rc = guard_tail("helm lineage archive-report", rest,
                        usage="lineage archive-report")
        if rc is not None:
            return rc
        rows = archive_report()
        print("helm lineage archive-report — READ-ONLY (ranked; nothing is moved by this slice)")
        if not rows:
            print("  no dormant archive candidates")
            return 0
        for r in rows:
            size = "%d MB" % r["disk_mb"] if r["disk_mb"] is not None else "? MB"
            print("  %-4s %-24s %9s  %s" % (r["verdict"].upper(), r["name"], size, r["path"]))
            for w in r["why_safe"]:
                print("         + " + w)
            for w in r["why_not"]:
                print("         - " + w)
        return 0
    print("helm: unknown lineage sub-verb '%s'" % verb, file=sys.stderr)
    return 2
