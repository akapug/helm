#!/usr/bin/env python3
"""helm sessions — every local session, all harnesses, keyed to your projects.

The catalog (absorbed from sesh) is the row source; the registry is the lens:
sessions group under the helm-known project whose tree their cwd lives in, so
"what was I doing on meld?" is one verb. Resume stays one copy-paste away —
the exact command, not a wrapper (it's just the harness's own CLI).
"""
import os
import time

from . import catalog, registry


def _project_lens():
    """[(name, prefix)] longest-prefix-first, so nested repos match before
    parents. Uses the registry's full cv_scope prefix set — sibling-dir
    worktree cwds don't share the canonical root's prefix."""
    reg = registry.load()
    pairs = []
    for p in reg["projects"].values():
        if not p.get("path") or p.get("external"):
            continue
        prefixes = (p.get("cv_scope") or {}).get("cwd_prefixes") or [p["path"]]
        pairs += [(p["name"], pre) for pre in prefixes]
    return sorted(pairs, key=lambda t: -len(t[1]))


def _project_for(cwd, lens):
    cwd = os.path.expanduser(cwd or "")
    for name, path in lens:
        if cwd == path or cwd.startswith(path + "/"):
            return name
    return None


def rows_for(project=None, include_synthetic=False, limit=None):
    rows, _ = catalog.build()
    lens = _project_lens()
    out = []
    for r in rows:
        r["project"] = _project_for(r.get("cwd") or r.get("c"), lens)
        if project and r["project"] != project:
            continue
        if not include_synthetic and r.get("syn"):
            continue
        out.append(r)
        if limit and len(out) >= limit:
            break
    return out


def resume_command(row):
    """The harness's own resume invocation. claude resume is cwd-scoped, so the
    command carries the cd; codex resume is global-by-UUID (the cd is comfort)."""
    cwd = os.path.expanduser(row.get("cwd") or "") or "."
    if row["h"] == "claude":
        return "cd %r && claude --resume %s" % (cwd, row["i"])
    return "cd %r && codex resume %s" % (cwd, row["i"])


def _age(mt):
    if not mt:
        return "?"
    d = (time.time() - mt) / 86400.0
    if d < 1:
        return "today"
    return "%dd" % int(d)


def cmd_sessions(args):
    """sessions [<project>] [--limit N] [--all] | sessions resume <id-prefix>"""
    if args and args[0] == "resume":
        if len(args) < 2:
            print("usage: helm sessions resume <session-id-prefix>")
            return 2
        pref = args[1]
        hits = [r for r in rows_for(include_synthetic=True)
                if r["i"].startswith(pref)]
        if not hits:
            print("helm sessions: no session id starts with '%s'" % pref)
            return 1
        if len(hits) > 1:
            print("helm sessions: %d sessions match '%s' — disambiguate:" % (len(hits), pref))
            for r in hits[:8]:
                print("  %s  (%s, %s)" % (r["i"], r["h"], r.get("u", "?")))
            return 1
        print(resume_command(hits[0]))
        return 0

    project = None
    limit = 25
    if "--limit" in args:
        limit = int(args[args.index("--limit") + 1])
    for a in args:
        if not a.startswith("--") and (not args.index(a) or args[args.index(a) - 1] != "--limit"):
            project = a
    rows = rows_for(project=project, include_synthetic="--all" in args, limit=limit)
    if not rows:
        print("helm sessions: none%s." % (" for project '%s'" % project if project else ""))
        return 0
    scope = " — " + project if project else ""
    print("helm sessions (%d newest%s):" % (len(rows), scope))
    for r in rows:
        proj = r.get("project") or "-"
        print("  %-6s %-7s %-20s %-9s %s" % (
            _age(r.get("mt")), r["h"], proj[:20], r["i"][:8],
            (r.get("t") or "(untitled)")[:70]))
    print("resume any: helm sessions resume <id-prefix>")
    return 0
