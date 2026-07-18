#!/usr/bin/env python3
"""helm CLI — CLI-first for advanced users; every curation verb here has (or
grows) a web equivalent. Verbs are a flat dispatch table so new legs bolt on
without touching the core."""
import os
import sys
import time

from . import home, registry


def _age(epoch):
    if not epoch:
        return "never"
    d = (time.time() - epoch) / 86400.0
    if d < 1:
        return "today"
    if d < 2:
        return "1d"
    return "%dd" % int(d)


def cmd_home(args):
    """home — print the resolved ~/.helm root."""
    print(home.helm_home())
    return 0


def cmd_sync(args):
    """sync — run the auto-map across every harness, refresh the registry,
    scaffold project homes. Additive: never deletes a known project."""
    reg, report = registry.sync()
    n = len(reg["projects"])
    print("helm sync: %d project%s known (%d new, %d refreshed)" % (
        n, "s"[:n != 1], len(report["new"]), len(report["updated"])))
    for name in report["new"]:
        print("  + " + name)
    return 0


def cmd_projects(args):
    """projects [--all] — the real project list, newest activity first."""
    reg = registry.load()
    projects = list(reg["projects"].values())
    if not projects:
        print("helm: no projects yet — run `helm sync`")
        return 0
    show_all = "--all" in args
    projects.sort(key=lambda p: -(p.get("last_seen") or 0))
    rows = []
    for p in projects:
        if p.get("retired") and not show_all:
            continue
        sess = p.get("sessions") or {}
        stotal = sum(sess.values())
        hlist = "+".join(sorted(sess)) if sess else "-"
        rows.append((p["name"], p.get("status", "?"), _age(p.get("last_seen")),
                     str(stotal), hlist, p.get("path", "")))
    w = [max(len(r[i]) for r in rows) for i in range(5)]
    for r in rows:
        print("  ".join(r[i].ljust(w[i]) for i in range(5)) + "  " + r[5])
    return 0


def cmd_show(args):
    """show <project> — one project's full record (pointers, sessions, edges)."""
    if not args:
        print("usage: helm show <project>", file=sys.stderr)
        return 2
    import json
    p = registry.get(args[0])
    if p is None:
        print("helm: unknown project '%s' (try `helm projects`)" % args[0], file=sys.stderr)
        return 1
    print(json.dumps(p, indent=2, ensure_ascii=False))
    return 0


def _lazy(module, fn):
    """Import a leg only when its verb runs — `helm projects` never pays for
    the web server's imports, and one broken leg never takes the CLI down."""
    def run(args):
        import importlib
        return getattr(importlib.import_module("helm." + module), fn)(args)
    return run


VERBS = {
    "home": cmd_home,
    "sync": cmd_sync,
    "projects": cmd_projects,
    "show": cmd_show,
    "store": _lazy("store", "cmd_store"),
    "inject": _lazy("inject", "cmd_inject"),
    "drain": _lazy("drain", "cmd_drain"),
    "drift": _lazy("drift", "cmd_drift"),
    "reflex": _lazy("reflex", "cmd_reflex"),
    "lineage": _lazy("lineage", "cmd_lineage"),
    "whoami": _lazy("whoami", "cmd_whoami"),
    "interview": _lazy("whoami", "cmd_interview"),
    "doctor": _lazy("doctor", "cmd_doctor"),
    "skills": _lazy("skills", "cmd_skills"),
    "evolve": _lazy("evolve", "cmd_evolve"),
    "sessions": _lazy("sessions", "cmd_sessions"),
    "web": _lazy("web", "cmd_web"),
}

_VERB_HELP = {
    "store": "store list|get|add|resolve|... — the one typed knowledge store",
    "inject": "inject [--project P] — per-turn context for harness hooks (stdin: prompt)",
    "drain": "drain [--apply] — route raw memory intake to typed homes (dry-run default)",
    "drift": "drift — surface belief drift; silent when steady",
    "reflex": "reflex list|add|retire — (signal -> steer) entries",
    "lineage": "lineage [seed|add|external|archive-report] — the project family tree",
    "whoami": "whoami [note ...] — the operator profile + dated notes",
    "interview": "interview — the five-minute warmth-leg interview",
    "doctor": "doctor — health check, read-only",
    "skills": "skills [dupes] — skills census across every home, read-only",
    "evolve": "evolve — one observe/propose cycle (proposes, never mutates)",
    "sessions": "sessions [<project>] — every local session, all harnesses; resume in one paste",
    "web": "web [--port N] — the same, warm, in a browser",
}


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help", "help"):
        print("helm — the personal knowledge home for people who build with agents\n")
        print("usage: helm <verb> [args]\n")
        for name in VERBS:
            doc = _VERB_HELP.get(name) or (VERBS[name].__doc__ or "").strip().split("\n")[0]
            print("  " + doc)
        return 0
    verb = argv[0]
    fn = VERBS.get(verb)
    if fn is None:
        print("helm: unknown verb '%s' (helm --help)" % verb, file=sys.stderr)
        return 2
    return fn(argv[1:])


if __name__ == "__main__":
    sys.exit(main())
