#!/usr/bin/env python3
"""helm sessions — every local session, all harnesses, keyed to your projects.

The catalog is the row source; the registry is the lens:
sessions group under the helm-known project whose tree their cwd lives in, so
"what was I doing on that project?" is one verb. Resume stays one copy-paste away —
the exact command, not a wrapper (it's just the harness's own CLI).
"""
import os
import sys
import time

from . import registry


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
    """Catalog rows via transcripts.get_catalog() — the single-flight cached
    path every other consumer (/api/catalog, /api/burn, the CLI) shares, so
    /api/sessions never re-spawns a full catalog build per GET. Same rows,
    plus cwd overrides applied. Rows are COPIED before the project annotation —
    the cache's row objects are shared and must never be mutated here."""
    from . import transcripts
    rows = transcripts.get_catalog()["rows"]
    lens = _project_lens()
    out = []
    for r in rows:
        proj = _project_for(r.get("cwd") or r.get("c"), lens)
        if project and proj != project:
            continue
        if not include_synthetic and r.get("syn"):
            continue
        out.append(dict(r, project=proj))
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


def resume_warnings(row):
    """Why THIS resume might not do what you expect — the catalog-level signals
    (make_cmd's provider-coupled preflight is the richer surface; this is the
    same truth the one-paste `sessions resume` path can carry for free). Order:
    a session that cannot resume at all first, then cwd caveats."""
    warn = []
    if row.get("xl"):
        mb = (row.get("z") or 0) / 1e6
        warn.append("OVERSIZED (~%.0fMB in ~%d lines): a single/few-message "
                    "session whose content likely exceeds the 200k window and "
                    "cannot compact — plain resume will fail (common for "
                    "daily-memory/summarizer sessions)." % (mb, row.get("m") or 0))
    elif row.get("syn"):
        warn.append("REFERENCE session (daily-memory summarizer) — a "
                    "read/training artifact, not a resumable work session.")
    if row["h"] == "claude":
        cwd = os.path.expanduser(row.get("cwd") or "")
        if not cwd:
            warn.append("no recorded cwd; claude resume is cwd-scoped — the "
                        "command may not resolve.")
        elif not os.path.isdir(cwd):
            warn.append("recorded cwd no longer exists: %s (resume from another "
                        "dir may fork a fresh session)." % cwd)
    return warn


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
        # warnings ride stderr so `$(helm sessions resume <id>)` stays the pure
        # command, while an interactive caller still sees why it might not resume
        for w in resume_warnings(hits[0]):
            print("  # " + w, file=sys.stderr)
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
