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
    shelf = sum(1 for p in projects if p.get("status") == "shelf")
    for p in projects:
        if (p.get("retired") or p.get("status") == "shelf") and not show_all:
            continue
        sess = p.get("sessions") or {}
        stotal = sum(sess.values())
        hlist = "+".join(sorted(sess)) if sess else "-"
        rows.append((p["name"], p.get("status", "?"), _age(p.get("last_seen")),
                     str(stotal), hlist, p.get("path", "")))
    if not rows:
        print("helm: %d shelf repos only — `helm projects --all`" % shelf)
        return 0
    w = [max(len(r[i]) for r in rows) for i in range(5)]
    for r in rows:
        print("  ".join(r[i].ljust(w[i]) for i in range(5)) + "  " + r[5])
    if shelf and not show_all:
        print("  (+%d shelf repos on disk with no agent activity — `helm projects --all`)" % shelf)
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
    "projections": _lazy("registry", "cmd_projections"),
    "show": cmd_show,
    "brief": _lazy("brief", "cmd_brief"),
    "store": _lazy("store", "cmd_store"),
    "index": _lazy("store", "cmd_index"),
    "inject": _lazy("inject", "cmd_inject"),
    "drain": _lazy("drain", "cmd_drain"),
    "promote": _lazy("drain", "cmd_promote"),
    "sweep": _lazy("sweep", "cmd_sweep"),
    "drift": _lazy("drift", "cmd_drift"),
    "reflex": _lazy("reflex", "cmd_reflex"),
    "record": _lazy("record", "cmd_record"),
    "lineage": _lazy("lineage", "cmd_lineage"),
    "whoami": _lazy("whoami", "cmd_whoami"),
    "interview": _lazy("whoami", "cmd_interview"),
    "doctor": _lazy("doctor", "cmd_doctor"),
    "gc": _lazy("gc", "cmd_gc"),
    "skills": _lazy("skills", "cmd_skills"),
    "evolve": _lazy("evolve", "cmd_evolve"),
    "mentor": _lazy("mentor", "cmd_mentor"),
    "sessions": _lazy("sessions", "cmd_sessions"),
    "homes": _lazy("homes", "cmd_homes"),
    "configs": _lazy("configs", "cmd_configs"),
    "hooks": _lazy("hooks", "cmd_hooks"),
    "cell": _lazy("cell", "cmd_cell"),
    "chat": _lazy("chat", "cmd_chat"),
    "human": _lazy("human", "cmd_human"),
    "premise": _lazy("premise", "cmd_premise"),
    "coach": _lazy("coach", "cmd_coach"),
    "premise-check": _lazy("premise", "cmd_premise_check"),
    "seat": _lazy("seat", "cmd_seat"),
    "search": _lazy("transcripts", "cmd_search"),
    "transcript": _lazy("transcripts", "cmd_transcript"),
    "rehome": _lazy("transcripts", "cmd_rehome"),
    "prune": _lazy("transcripts", "cmd_prune"),
    "keepalive": _lazy("keepalive", "cmd_keepalive"),
    "creds": _lazy("creds", "cmd_creds"),
    "swap": _lazy("creds", "cmd_swap"),
    "capsule": _lazy("capsule", "cmd_capsule"),
    "ship": _lazy("ship", "cmd_ship"),
    "corpus": _lazy("corpus", "cmd_corpus"),
    "handoff": _lazy("handoff", "cmd_handoff"), "now": _lazy("handoff", "cmd_now"),
    "cmd": _lazy("transcripts", "cmd_cmd"),
    "web": _lazy("web", "cmd_web"),
}

_VERB_HELP = {
    "brief": "brief [--hours N] [--json] — the operator's morning brief: sessions, knowledge delta, seats, owner gates (read-only, never probes)",
    "projections": "projections [--json] — the projection registry: every derived store, its class, source + rebuild (laws 2+3's read surface; doctor enforces)",
    "store": "store list|get|add|resolve|confirm|demote|events|... — the one typed knowledge store",
    "index": "index cap [--budget-lines N] [--apply] — MEMORY.md budget actuator: demote link lines whose backing entry stays jit-resolvable (provably lossless); documented Stop-hook line `helm index cap --apply`",
    "inject": "inject [--project P] [--json|--explain|--hook-json|--lane-report|--compare-report] — per-turn context for harness hooks (stdin: prompt or hook JSON); the day's first turn leads with a one-line brief whisper; --compare-report is the local-vs-comparison (Cloudflare agentic-memory) divergence verdict",
    "drain": "drain [--apply] — route raw memory intake to typed homes (dry-run default)",
    "promote": "promote [--since Nd] [--cap N] [--apply] — episodic->durable funnel: recent USER messages -> drain-intake candidates (dry-run default)",
    "sweep": "sweep [--apply] [--project P] — lineage-driven supersession sweep of the adopted store (dry-run default)",
    "drift": "drift — surface belief drift; silent when steady",
    "reflex": "reflex list|add|retire|smoke — (signal -> steer) entries; signals prompt|marker-file|every-turn|counter|stalled|thrash|drift|stuck (record.py counters, latch once per episode)",
    "record": "record [--hook-json]|status|install — session-keyed tool-outcome recorder (PostToolUse leg); its counters drive the counter/latch reflexes",
    "lineage": "lineage [seed|add|external|archive-report] — the project family tree",
    "whoami": "whoami [note ...] — the operator profile + dated notes",
    "interview": "interview — the five-minute know-your-user interview",
    "doctor": "doctor — health check, read-only",
    "gc": "gc [--dry | --apply] — declared retention budgets over the derived exhaust (dry-run default; authored content never touched)",
    "skills": "skills [dupes] — skills census across every home, read-only",
    "evolve": "evolve — one observe/propose cycle (proposes, never mutates)",
    "mentor": "mentor observe <project> [--since 7d]|teach <project> \"<id> | <steer>\" [--attest]|review <project>|log — the inception actuator: critique brief, taught project reflexes with provenance, before/after review (teach is the one write; the rest read-only)",
    "sessions": "sessions [<project>] — every local session, all harnesses; resume in one paste",
    "homes": "homes [prepare|verify|archive|restore|archives] — credential-home lifecycle",
    "configs": "configs [list|show|cascade <cwd>] — every config across every home, read-only",
    "hooks": "hooks [install [--dry]|status] — self-wire the per-turn inject hook into every claude home",
    "cell": "cell join|send|recv|heartbeat|roster|status — the a2a substrate, helm-named",
    "chat": "chat post|read [--since N|--follow]|rooms|react <n> <emoji>|log-flush|node up|down|status [--room R] — the human-included groupchat (RAM room + signed dregg transport; web panel = the owner's surface)",
    "human": "human (or helm --human) — the operator's curses TUI: chat room + status strip, posts as you",
    "premise": "premise <id> | <statement> — capture a certain truth, attested on the ledger; --supersede <old-id> <new-id> | <statement> evolves the chain (one signed linking turn)",
    "coach": "coach <lesson...> [--apply] [--as L] [--id ID] [--project P] [--supersede OLD] [--json] — the capture front door with the 4-step GATE (reframe->place->search-first->simplify); propose-only unless --apply (low-confidence -> drain intake, lossless)",
    "premise-check": "premise-check <id> [--chain] — verify digest + quote the finality tier; --chain walks the supersession chain (attested biography)",
    "seat": "seat add|up|down|launch|smoke|list|status — multimodel seats (codex family via local proxy)",
    "search": "search <text> [--scope P] [--refs] — content search inside transcripts",
    "transcript": "transcript <sid> [--find T] [--limit N] — windowed role-tagged transcript read",
    "rehome": "rehome <sid> <new-cwd>|--reset — re-home a session (claude slug symlink)",
    "prune": "prune <sid> [--preset lean|window20k] [--dry] — resume-optimized copy, original untouched",
    "keepalive": "keepalive [--home H] [--early N] — roll idle claude homes' tokens (codex read-only)",
    "creds": "creds [crosscheck [--json]] — live account scorecard (headroom/state/reset), all providers; crosscheck sums local session JSONL into the same 5h/7d windows and cross-checks the header truth (drift = health signal)",
    "swap": "swap <home|email> — seat ran dry: print resume-under-healthier-account blocks",
    "capsule": "capsule <sid> — the session's git era: worktree + resume commands",
    "ship": "ship [--apply] [--remote URL] | ship pull | ship hosts — the authored chain over git (dry-run default; pull merges + re-derives)",
    "corpus": "corpus backup [--dry] [--dest DIR] | status — training-corpus transcript backup: copy-only incremental archive of every session/subagent/workflow transcript (default ~/corpus-archive; HELM_CORPUS_DEST)",
    "handoff": "handoff check [--hook-json]|write|recover <sid> — the compaction-continuity contract (PreCompact/SessionEnd nag; typed journal handoff; cv pre-compaction recovery)", "now": "now capture [--hook-json]|show — automatic session-continuity snapshot (_global/now.md, 40 lines newest-first, 48h freshness gate; SessionStart context)",
    "cmd": "cmd <sid> [--account A] [--model M] — account-aware pasteable resume command",
    "web": "web [--port N] — the same, warm, in a browser",
}


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "--human":   # the operator flag IS the verb
        argv[0] = "human"
    if argv and argv[0] in ("--version", "-V", "version"):
        from . import __version__
        print("helm " + __version__)
        return 0
    if not argv or argv[0] in ("-h", "--help", "help"):
        print("helm — the steering station for you and your agent fleet\n")
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
    rest = argv[1:]
    if rest and rest[0] in ("-h", "--help"):
        print("helm " + (_VERB_HELP.get(verb) or (fn.__doc__ or verb).strip().split("\n")[0]))
        return 0
    return fn(rest)


if __name__ == "__main__":
    sys.exit(main())
