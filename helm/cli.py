#!/usr/bin/env python3
"""helm CLI — CLI-first for advanced users; every curation verb here has (or
grows) a web equivalent. Verbs are a flat dispatch table so new legs bolt on
without touching the core."""
import os
import sys
import time

# NOTHING HEAVY AT MODULE SCOPE. Every helm process imports this module, and
# the per-tool-call hooks import it several times per turn on every seat --
# `registry` alone measured 45 ms of CPU (automap, shutil, pathlib, json) for
# an argv-guard that reads no registry on any call its string gates do not
# select. `home`, `registry` and `selfrepo`
# are read by a handful of verbs and by one advisory warning, so each is
# imported inside the function that reads it rather than here.
# `wiring.graph` reads function-level imports, so the module graph and the
# reachability law see exactly the same edges they saw before.


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
    from . import home
    print(home.helm_home())
    return 0


def cmd_sync(args):
    """sync — run the auto-map across every harness, refresh the registry,
    scaffold project homes. Additive: never deletes a known project."""
    from . import registry
    try:
        reg, report = registry.sync()
    except (OSError, ValueError) as exc:
        print("helm sync: registry UNKNOWN: %s" % exc, file=sys.stderr)
        return 1
    n = len(reg["projects"])
    print("helm sync: %d project%s known (%d new, %d refreshed)" % (
        n, "s"[:n != 1], len(report["new"]), len(report["updated"])))
    for name in report["new"]:
        print("  + " + name)
    # THE STORE HALF OF A SHIPPED ACT RUNG (task/2980): keyword cells whose
    # moment an argv-guard rung now owns leave the entry once, here, beside
    # the reflex re-keys registry.sync applies. Here and not in
    # registry.sync, which tests call against the real adopted memory dir;
    # this verb is the operator's.
    from . import actsteer
    for eid, state in actsteer.retire_moved():
        if state in ("applied", "held"):
            print("  %s act-owned keywords: %s" % (state, eid))
    return 0


def cmd_projects(args):
    """projects [--all]|forget|restore|forgotten|repoint — registry membership and location."""
    if args and args[0] == "state":
        return _project_state(args[1:])
    if args and args[0] == "residency":
        return _project_residency(args[1:])
    if args and args[0] in ("forget", "restore", "forgotten", "repoint"):
        return _project_membership(args)
    rc = guard_tail("helm projects", args, flags=("--all",),
                    usage="projects [--all]")
    if rc is not None:
        return rc
    from . import registry
    try:
        reg = registry.load()
    except (OSError, ValueError) as exc:
        print("helm projects: registry UNKNOWN: %s" % exc, file=sys.stderr)
        return 1
    # THE REGISTRY KEY IS THE AUTHORITATIVE NAME. This read used a bare
    # p["name"] among a row of .get()s, so one row missing that field turned
    # the whole listing into a KeyError traceback — every other project on the
    # machine unreadable because of one. A row is FILED UNDER its name, so
    # recovering it from the key is not a guess, it is the more authoritative
    # source; the redundant copy inside the value is the weaker one.
    projects, malformed = [], 0
    # ONE AUTHORED READ FOR THE WHOLE LISTING, keyed like the registry is: the
    # light's authorship is resolved by (key, path), not read off the row.
    lit = registry.lights(reg)
    for key, p in (reg.get("projects") or {}).items():
        if not isinstance(p, dict):
            malformed += 1          # counted and reported, never silently dropped
            continue
        projects.append(dict(p, name=p.get("name") or key, _light=lit[key]))
    if not projects:
        if malformed:
            print("helm: %d unreadable registry row%s and no usable project — "
                  "`helm sync` rebuilds" % (malformed, "s"[:malformed != 1]),
                  file=sys.stderr)
            return 1
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
        # THE LIGHT, NOT THE SCAN'S HALF OF IT. `status` is only what the scan
        # saw; an authored colour outranks it and is the answer to "may I work
        # here", so the column carries the resolved light and marks which half
        # it came from — a reader who cannot tell them apart reads the owner's
        # silence as the owner's permission.
        rows.append((p["name"], p["_light"]["colour"] + "*" * p["_light"]["authored"],
                     _age(p.get("last_seen")),
                     str(stotal), hlist, p.get("path", "")))
    if not rows:
        print("helm: %d shelf repos only — `helm projects --all`" % shelf)
        return 0
    w = [max(len(r[i]) for r in rows) for i in range(5)]
    for r in rows:
        print("  ".join(r[i].ljust(w[i]) for i in range(5)) + "  " + r[5])
    if any(r[1].endswith("*") for r in rows):
        print("  (* an AUTHORED light — somebody decided it; the rest is what "
              "the scan saw. A project's light outranks any credential flag: "
              "a green cred is capacity, never permission.)")
    if shelf and not show_all:
        print("  (+%d shelf repos on disk with no agent activity — `helm projects --all`)" % shelf)
    return 0


def _project_state(args):
    """projects state [<name> <colour> [--reason R] [--apply]] — the light."""
    from . import home, registry
    usage = ("projects state [<name> (%s|clear) [--reason TEXT] [--apply]]"
             % "|".join(registry.STATE_COLOURS))
    if not args:                                  # the read: who has decided what
        try:
            reg = registry.load()
        except (OSError, ValueError) as exc:
            print("helm projects state: registry UNKNOWN: %s" % exc, file=sys.stderr)
            return 1
        rows = [(key, lit["colour"], lit["by"] or "-", _age(lit["ts"]), lit["reason"])
                for key, lit in sorted(registry.lights(reg).items()) if lit["authored"]]
        if not rows:
            print("helm projects state: no project light is authored — every "
                  "one is whatever the scan last saw")
            return 0
        w = [max(len(r[i]) for r in rows) for i in range(4)]
        for r in rows:
            print("  ".join(r[i].ljust(w[i]) for i in range(4)) +
                  ("  " + r[4] if r[4] else ""))
        return 0
    if len(args) < 2 or args[1].startswith("-"):
        print("usage: " + usage, file=sys.stderr)
        return 2
    rc = guard_tail("helm projects state", args[2:], flags=("--apply",),
                    valued=("--reason",), usage=usage)
    if rc is not None:
        return rc
    apply = "--apply" in args[2:]
    reason = args[args.index("--reason") + 1] if "--reason" in args else None
    if args[1] != "clear" and not reason:
        # A LIGHT WITHOUT A REASON IS UNREADABLE BY THE NEXT PERSON, and the
        # next person is usually the owner a week later asking why a project he
        # meant to ship is amber. The colour is the instruction; the reason is
        # the only part that says when it stops applying.
        print("helm projects state: a colour needs --reason (what decided it, "
              "and what would change it back)", file=sys.stderr)
        return 2
    try:
        by = home.chat_name() or ""
    except ValueError as exc:       # a seat name this helm will not record
        print("helm projects state: %s" % exc, file=sys.stderr)
        return 1
    try:
        row, err = registry.state(args[0], args[1], reason=reason, by=by,
                                  apply=apply)
    except (OSError, ValueError) as exc:
        row, err = None, "registry UNKNOWN: %s" % exc
    if err:
        print("helm projects state: " + err, file=sys.stderr)
        return 1
    was = (row["was"] or {}).get("colour") if isinstance(row["was"], dict) else None
    now = (row["state"] or {}).get("colour") if row["state"] else "(the scan's)"
    print("helm projects state: %s %s %s -> %s" % (
        "applied" if apply else "dry-run", row["name"], was or "(the scan's)", now))
    if not apply:
        print("  repeat with --apply to author it")
    return 0


def _project_residency(args):
    """projects residency [<name> <value> --reason R [--apply]] — whether a
    project's turn text may leave the LAN for an outside scorer."""
    from . import home, registry
    usage = ("projects residency [<name> (%s|clear) [--reason TEXT] [--apply]]"
             % "|".join(registry.RESIDENCY_VALUES))
    if not args:                        # the read: every project, fail closed
        try:
            reg = registry.load(strict=True)
            auth = registry._authored_load(strict=True)
        except (OSError, ValueError) as exc:
            print("helm projects residency: registry UNKNOWN: %s — every "
                  "project reads lan-only" % exc, file=sys.stderr)
            return 1
        for key, rec in sorted((reg.get("projects") or {}).items()):
            if not isinstance(rec, dict):
                continue
            r = registry.residency(key, rec.get("path"), auth)
            print("%s  %s%s%s" % (key, r["value"], "*" * r["authored"],
                                  ("  " + r["reason"]) if r["reason"] else ""))
        print("  (* authored. Anything unmarked is lan-only because nobody "
              "wrote the field; only an authored may-leave-lan lets turn text "
              "leave the LAN.)")
        return 0
    if len(args) < 2 or args[1].startswith("-"):
        print("usage: " + usage, file=sys.stderr)
        return 2
    rc = guard_tail("helm projects residency", args[2:], flags=("--apply",),
                    valued=("--reason",), usage=usage)
    if rc is not None:
        return rc
    apply = "--apply" in args[2:]
    reason = args[args.index("--reason") + 1] if "--reason" in args else None
    try:
        by = home.chat_name() or ""
    except ValueError as exc:
        print("helm projects residency: %s" % exc, file=sys.stderr)
        return 1
    try:
        row, err = registry.set_residency(args[0], args[1], reason=reason,
                                          by=by, apply=apply)
    except (OSError, ValueError) as exc:
        row, err = None, "registry UNKNOWN: %s" % exc
    if err:
        print("helm projects residency: " + err, file=sys.stderr)
        return 1
    was = (row["was"] or {}).get("value") if isinstance(row["was"], dict) else None
    now = (row["residency"] or {}).get("value") if row["residency"] else None
    print("helm projects residency: %s %s %s -> %s" % (
        "applied" if apply else "dry-run", row["name"],
        was or "lan-only (no field)", now or "lan-only (no field)"))
    if not apply:
        print("  repeat with --apply to author it")
    return 0


def _project_membership(args):
    from . import registry
    verb = args[0]
    usage = ("projects forget <name> [--apply] | restore <name> [--apply] | forgotten | "
             "repoint <name> --from PATH (--to PATH | --undo) [--apply]")
    if verb == "forgotten":
        rc = guard_tail("helm projects forgotten", args[1:], usage=usage)
        if rc is not None:
            return rc
        try:
            entries = registry._forgotten(registry._authored_load(strict=True))
        except (OSError, ValueError) as exc:
            print("helm projects: registry UNKNOWN: %s" % exc, file=sys.stderr)
            return 1
        for name, row in sorted(entries.items()):
            print("%s  forgotten  %s" % (name, row["record"]["path"]))
        if not entries:
            print("helm projects: no forgotten registrations")
        return 0
    if len(args) < 2 or args[1].startswith("-"):
        print("usage: " + usage, file=sys.stderr)
        return 2
    rc = guard_tail("helm projects " + verb, args[2:],
                    flags=("--apply", "--undo") if verb == "repoint" else ("--apply",),
                    valued=("--from", "--to") if verb == "repoint" else (), usage=usage)
    if rc is not None:
        return rc
    apply = "--apply" in args[2:]
    kwargs = {"apply": apply}
    if verb == "repoint":
        if "--from" not in args or ("--to" in args) == ("--undo" in args):
            print("usage: " + usage, file=sys.stderr)
            return 2
        kwargs.update(source=args[args.index("--from") + 1], undo="--undo" in args,
                      target=args[args.index("--to") + 1] if "--to" in args else None)
    try:
        row, err = getattr(registry, verb)(args[1], **kwargs)
    except (OSError, ValueError) as exc:
        row, err = None, "registry UNKNOWN: %s" % exc
    if err:
        print("helm projects: " + err, file=sys.stderr)
        return 1
    print("helm projects: %s %s %s (%s) — project files are untouched" % (
        "applied" if apply else "dry-run", verb, args[1], row["record"]["path"]))
    if not apply:
        print("  repeat with --apply to change registry membership")
    return 0


def cmd_show(args):
    """show <project> — one project's full record (pointers, sessions, edges)."""
    if not args:
        print("usage: helm show <project>", file=sys.stderr)
        return 2
    rc = guard_tail("helm show", args[1:], usage="show <project>")
    if rc is not None:
        return rc
    import json
    from . import registry
    try:
        p = registry.get(args[0])
    except (OSError, ValueError) as exc:
        print("helm show: registry UNKNOWN: %s" % exc, file=sys.stderr)
        return 1
    if p is None:
        print("helm: unknown project '%s' (try `helm projects`)" % args[0], file=sys.stderr)
        return 1
    print(json.dumps(p, indent=2, ensure_ascii=False))
    return 0


def suggest(word, candidates, n=1):
    """The nearest-match hint — pure, shared by the root's unknown-verb
    refusal, guard_tail's unknown-arg refusal, and every subdispatcher's
    unknown-subverb refusal, so a typo anywhere in the tree says what its
    author probably meant instead of only the generic usage line.

    n DEFAULTS TO 1 so those three refusals are unchanged: for a mistyped verb
    there is one right answer and a list of guesses is noise. Recipient
    resolution passes n>1 because its miss has a DIFFERENT SHAPE — `claude` is
    not a typo of any seat, it is the FAMILY name of several, and answering
    with only `helm-claude` hides that helm-claude-2 was the equally likely
    intent. One hint per candidate answer, not one hint per site."""
    import difflib
    near = difflib.get_close_matches(word, list(candidates), n=max(1, n))
    if not near:
        return ""
    return " — did you mean %s?" % ", ".join("'%s'" % s for s in near)


def guard_tail(prog, args, flags=(), valued=(), usage=None):
    """The nested-dispatcher honesty contract, companion to main()'s
    unknown-verb refusal: once a subverb is matched, every REMAINING token
    must be a known flag. Trailing junk refuses with exit 2 BEFORE any work
    runs (`seat down codex --bogus` used to stop the seat and exit 0), and
    `--help` after junk still refuses — the existence probe stays honest.
    A clean tail carrying -h/--help prints `usage` and returns 0. `valued`
    flags must carry a non-flag value exactly once. Returns None to proceed,
    else the exit code for the caller to return."""
    args = list(args or [])
    junk, want_help, seen = [], False, set()
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("-h", "--help"):
            want_help = True
        elif a in valued:
            if a in seen:
                print("%s: duplicate %s" % (prog, a), file=sys.stderr)
                return 2
            seen.add(a)
            if i + 1 >= len(args) or args[i + 1].startswith("-"):
                print("%s: %s wants a value" % (prog, a), file=sys.stderr)
                return 2
            i += 1
        elif a not in flags:
            junk.append(a)
        i += 1
    if junk:
        print("%s: unknown arg '%s'%s%s" % (
            prog, junk[0], suggest(junk[0], tuple(flags) + tuple(valued)),
            (" (%s)" % usage) if usage else ""), file=sys.stderr)
        return 2
    if want_help:
        print(usage or prog)
        return 0
    return None


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
    # THE OWNER'S POSTURE, two verbs because the door is a phone. `back` is a
    # verb rather than a flag on purpose: it is four fewer keystrokes than
    # `away off` at the moment he least wants to type.
    "away": _lazy("away", "cmd_away"),
    "back": _lazy("away", "cmd_back"),
    "store": _lazy("store", "cmd_store"),
    "capabilities": _lazy("capability", "cmd_capabilities"),
    "index": _lazy("store", "cmd_index"),
    "inject": _lazy("inject", "cmd_inject"),
    "saguide": _lazy("saguide", "cmd_saguide"),
    "drain": _lazy("drain", "cmd_drain"),
    "promote": _lazy("drain", "cmd_promote"),
    "sweep": _lazy("sweep", "cmd_sweep"),
    "drift": _lazy("drift", "cmd_drift"),
    "reflex": _lazy("reflex", "cmd_reflex"),
    "friction": _lazy("friction", "cmd"),
    "record": _lazy("record", "cmd_record"),
    "todos": _lazy("todos", "cmd_todos"),
    "lineage": _lazy("lineage", "cmd_lineage"),
    "whoami": _lazy("whoami", "cmd_whoami"),
    "interview": _lazy("whoami", "cmd_interview"),
    "doctor": _lazy("doctor", "cmd_doctor"),
    "injectbudget": _lazy("injectbudget", "cmd_injectbudget"),
    "fixedtext": _lazy("fixedtext", "cmd"),
    "mcpd": _lazy("mcpd", "cmd_mcpd"),
    "watchdog": _lazy("watchdog", "cmd_watchdog"),
    "gc": _lazy("gc", "cmd_gc"),
    "scratch": _lazy("scratch", "cmd_scratch"),
    "storage-matrix": _lazy("storage_matrix", "cmd_storage_matrix"),
    "skills": _lazy("skills", "cmd_skills"),
    "evolve": _lazy("evolve", "cmd_evolve"),
    "mentor": _lazy("mentor", "cmd_mentor"),
    "sessions": _lazy("sessions", "cmd_sessions"),
    "fleet": _lazy("fleet", "cmd_fleet"),
    "session": _lazy("session", "cmd_session"),
    "homes": _lazy("homes", "cmd_homes"),
    "configs": _lazy("configs", "cmd_configs"),
    "hooks": _lazy("hooks", "cmd_hooks"),
    "cell": _lazy("cell", "cmd_cell"),
    "chat": _lazy("chat", "cmd_chat"),
    "multiplayer": _lazy("multiplayer", "cmd_multiplayer"),
    "launch": _lazy("launch", "cmd_launch"),
    "human": _lazy("human", "cmd_human"),
    "premise": _lazy("premise", "cmd_premise"),
    "asks": _lazy("ownerasks", "cmd_asks"),
    "decide": _lazy("ownerasks", "cmd_decide"),
    "telegram": _lazy("telegram", "cmd_telegram"),
    "task": _lazy("tasks", "cmd_task"),
    "dispatch": _lazy("dispatches", "cmd_dispatch"),
    "owed": _lazy("obligation", "cmd_owed"),
    "owed-push": _lazy("owedpush", "cmd_owed_push"),
    "gate": _lazy("gate", "cmd_gate"),
    "landgate": _lazy("landgate", "cmd_landgate"),
    "compose": _lazy("foldcompose", "cmd_compose"),
    "lr": _lazy("landreq", "cmd_lr"),
    "stale": _lazy("stalebot", "cmd_stale"),
    "derive": _lazy("rowworld", "cmd_derive"),
    "coach": _lazy("coach", "cmd_coach"),
    "premise-check": _lazy("premise", "cmd_premise_check"),
    "seat": _lazy("seat", "cmd_seat"),
    "router": _lazy("modelrouter", "cmd_router"),
    "codex": _lazy("codexhomes", "cmd_codex"),
    "work": _lazy("work", "cmd_work"),
    "ownership": _lazy("ownership_census", "cmd_ownership"),
    "search": _lazy("transcripts", "cmd_search"),
    "transcript": _lazy("transcripts", "cmd_transcript"),
    "rehome": _lazy("transcripts", "cmd_rehome"),
    "prune": _lazy("transcripts", "cmd_prune"),
    "keepalive": _lazy("keepalive", "cmd_keepalive"),
    "creds": _lazy("creds", "cmd_creds"),
    "burn": _lazy("burnflags", "cmd_burn"),
    "route": _lazy("route", "cmd_route"),
    "accounts": _lazy("accounts", "cmd_accounts"),
    "cred": _lazy("cred", "cmd_cred"),
    "swap": _lazy("creds", "cmd_swap"),
    "attribute": _lazy("attribute", "cmd_attribute"),
    "proxy-usage": _lazy("proxy_usage", "cmd_proxy_usage"),
    "who": _lazy("who", "cmd_who"),
    "capsule": _lazy("capsule", "cmd_capsule"),
    "ship": _lazy("ship", "cmd_ship"),
    "corpus": _lazy("corpus", "cmd_corpus"),
    "handoff": _lazy("handoff", "cmd_handoff"), "now": _lazy("handoff", "cmd_now"),
    "cmd": _lazy("transcripts", "cmd_cmd"),
    "web": _lazy("web", "cmd_web"),
    "board": _lazy("board", "cmd_board"),
    "note": _lazy("fleetnotes", "cmd_note"),
    "wiring": _lazy("wiring", "cmd_wiring"),
    "punt": _lazy("punt", "cmd_punt"),
    "relevance": _lazy("relevance_cli", "cmd_relevance"),
    "nouncensus": _lazy("nouncensus", "cmd_nouncensus"),
    "clarity": _lazy("clarity", "cmd_clarity"),
    "pi": _lazy("pi", "cmd_pi"),
    "eval": _lazy("evalpin", "cmd_eval"),
    "proxywatch": _lazy("proxywatch", "cmd_proxywatch"),
    "rogue": _lazy("roguescan", "cmd_rogue"),
    "proxy-fork-watch": _lazy("proxy_fork_watch", "cmd_proxy_fork_watch"),
    "upstream-watch": _lazy("upstream_watch", "cmd_upstream_watch"),
    "env": _lazy("envtidy", "cmd_env"),
    "mcp": _lazy("envtidy", "cmd_mcp"),
    "worktree": _lazy("envtidy", "cmd_worktree"),
    "tidy": _lazy("envtidy", "cmd_tidy"),
    "rearm": _lazy("rearm", "cmd_rearm"),
    "beacons": _lazy("beacons", "cmd_beacons"),
    "ready": _lazy("ready", "cmd_ready"),
    "reviewers": _lazy("reviewer_eligibility", "cmd_reviewers"),
    "preread": _lazy("preread", "cmd_preread"),
}

# Verbs whose handlers read NO arguments at all: nothing below main() will
# ever look at the tail, so the ROOT guards it — `helm sync --bogus --help`
# must refuse (exit 2) BEFORE the (possibly mutating) leaf runs, not run
# sync while --help pretends the flag existed. The sweep test DERIVES this
# set from the source (AST: the handler never loads its args param) and
# fails when a new no-arg leaf is born outside it, so the class stays closed.
NOARG_VERBS = ("home", "sync", "human")

_VERB_HELP = {
    "preread": "preread <dispatch-id> | --ref <sha> --repo <path> "
               "[--no-dm] [--json] — A COUNCIL OF CHEAP READERS PRE-READS A "
               "REVIEW ROW. Each file of the row's diff goes to every "
               "configured reader endpoint under several seeds with the "
               "defect-class checklist; a DIFFERENT model judges each file's "
               "drafts and writes down what it dropped; a citation check "
               "discards any finding whose quoted line is not in the diff. "
               "The survivors are written under the helm home and the row's "
               "recipient is DM'd the path. THIS MINTS NO VERDICT and never "
               "touches the dispatch ledger — weak models read, they do not "
               "approve, and what comes out is material a strong reviewer or "
               "the integrator checks line by line. Endpoints, models and the "
               "judge's key live in the config under the helm home; an absent "
               "config refuses and names the path.",
    "reviewers": "reviewers <row-id> [--all-seats] [--json] — WHO CAN REVIEW "
                 "THIS ROW RIGHT NOW, and for every seat that cannot, THE ONE "
                 "CONJUNCT THAT EXCLUDES IT. Eligibility is six conjuncts "
                 "living on six surfaces that nothing joined: the approval "
                 "TIER in the typed policy store (a seat outside it is a valid "
                 "reviewer whose findings are INPUT — its APPROVE cannot close "
                 "the row); MINT, the immutable verdict-time runtime-family "
                 "evidence without which no authorizing verdict can be written "
                 "in that seat's name, and which had no CLI at all; the CHAIN "
                 "contributor index, which until now surfaced only in a "
                 "refusal AFTER a review had already been burned; AWAKE, the "
                 "same `can_take_work` the dispatch write door refuses on, "
                 "labelled by shape so DEAF (no live beacon — helm cannot wake "
                 "it, and it looks healthy on every other axis) is told apart "
                 "from a vendor WALL and from a gone pane; the pooled family "
                 "BUDGET proxywatch persists; and IDLE, the pane tail the "
                 "owner reads by looking. The seats that CAN review print "
                 "first, most idle first; every other candidate prints with "
                 "the ONE conjunct that stops it, because a list of who can is "
                 "half an answer — the other half is whether to WAIT, REPAIR "
                 "or ROUTE ELSEWHERE. The ladder runs from the refusals "
                 "nothing local can change to the one that clears itself, so a "
                 "seat is named under the furthest-out reason it cannot "
                 "review, and the one expensive read (a pane tail per seat) is "
                 "paid only for seats nothing cheaper excluded. NOTHING HERE "
                 "IS MEASURED TWICE: every rung calls the authority that owns "
                 "it and prints that authority's own sentence verbatim. AN "
                 "UNREADABLE INPUT IS NEVER A REFUSAL — a conjunct that cannot "
                 "be measured renders UNKNOWN, names the input it could not "
                 "read, and leaves the seat a candidate; a shared input that "
                 "would not read makes the whole answer PARTIAL and says so, "
                 "because 'no eligible reviewer' and 'I could not read the "
                 "roster' are different worlds with different repairs. Exit 0 "
                 "is at least one eligible seat with every shared input read; "
                 "exit 1 covers BOTH the measured empty set and the unread "
                 "one, since a caller gating on the code needs the same stop "
                 "for 'no' and for 'I cannot tell' — the TEXT is what tells "
                 "them apart, and it always does. Candidates are the seats "
                 "whose register HOME ROOM serves this row's project, never a "
                 "family's numbered seats as a pool; --all-seats widens and "
                 "the count of seats homed elsewhere always prints",
    "projects": "projects [--all]|state [<name> (green|yellow|orange|red|clear) --reason TEXT [--apply]]|residency [<name> (lan-only|may-leave-lan|clear) --reason TEXT [--apply]]|forget <name> [--apply]|restore <name> [--apply]|forgotten|repoint <name> --from PATH (--to PATH|--undo) [--apply] — the project LIGHT (an authored colour outranks the scan's, and outranks any credential flag), the data RESIDENCY (only an authored may-leave-lan lets turn text leave the LAN; no field is lan-only), plus reversible registry membership/location; dry-run by default, project files untouched",
    "away": "away [off|status] — DECLARE yourself away, so surfaces that wait "
            "on your attention say so instead of assuming it; `status` reads "
            "the flag and your fleet notice. The web console's away mode card "
            "writes the same flag. The state IS one flag "
            "file and nothing infers it: no clock, no activity heuristic, no "
            "second sentinel — twenty minutes of quiet is a man thinking, and "
            "a dashboard that fires then interrupts exactly the person it is "
            "for. The flag is the ONLY record: nothing else is written on a "
            "transition, because a second representation of one boolean is how "
            "two surfaces come to disagree about where a person is. Its "
            "lifetime is the chat store's and this verb makes no durability "
            "promise either way. `helm back` lifts it",
    "back": "back — lift your away posture (same as `away off`). A separate "
            "verb rather than a flag because the door is orca mobile on a "
            "phone, and this is four fewer keystrokes at the moment you least "
            "want to type",
    "brief": "brief [--hours N] [--json] — the operator's morning brief: sessions, knowledge delta (incl. pinned-starvation tail), seats, owner gates (read-only, never probes)",
    "projections": "projections [--json] — the projection registry: every derived store, its class, source + rebuild (laws 2+3's read surface; doctor enforces)",
    "store": "store list|get|resolve|pinned|counts|add <type> <id> | <stmt> [| ...] (type: prior|premise|lexicon|heuristic|reference)|keywords <id> [--add CSV|--remove CSV|--set CSV]|gates <id> [--add CSV|--remove CSV|--set CSV] (the rule's PRECONDITIONS ride in the same whisper and share its vocabulary)|gloss <id> [--set TEXT...|--clear] (the short line that FIRES; refused with the overage past LINE_CAP)|rescope <id> <project|fleet|-> (RECORD THE PROJECT AN ENTRY IS ABOUT: a registry project name, `fleet` for owner policy that applies everywhere, or `-` to clear back to the root's default; the ONLY door that makes an adopted-root entry project-scoped, and a seat is injected fleet entries plus its own project's)|xrev-clear <id> --by <who>|revise <id> <corrected stmt...>|confirm|reject|evidence <ts> <id> <delta> <why>|supersede <ts> <old> <new>|retire <ts> <id>|demote <id> [--undo]|events [--limit N]|doctor [--fix] — the ONE typed knowledge store: add/resolve is the capture-then-JIT loop, candidate -> xrev-clear -> confirm is the ratify ladder, evidence/revise/supersede/retire/demote are the lifecycle receipts (bare verb prints the full grammar). `revise` CORRECTS A LIVE ENTRY IN PLACE: it stages the corrected statement alongside the one still being served, and `confirm` swaps it in under the SAME id — the verb for when the claim is right and only a clause is wrong. Neither neighbour could do it: `evidence` moves CONFIDENCE, a lie about a claim that holds, and `supersede` TOMBSTONES the id, splitting retrieval weight across two ids for a statement that is mostly correct. It keeps SERVING while staged on purpose — demoting an entry to stage its correction would take canon DARK exactly while it is being corrected, which is worst for the safety entries most worth correcting",
    "capabilities": "capabilities [--public] [--json] — the wired-substrate SELF-INDEX: what helm gives you (verb -> what -> wired-via -> live/absent), grouped core vs powerpack. The SAME index JIT-surfaces one line at the reasoning moment via inject (a2a/converge -> meld+chat-deliver; 'solved this before' -> recall; deep code analysis -> a code-analysis powerpack when wired). --public withholds private-hold powerpacks",
    "index": "index cap [--budget-lines N] [--apply] — MEMORY.md budget actuator: demote link lines whose backing entry stays jit-resolvable (provably lossless); documented Stop-hook line `helm index cap --apply`",
    "inject": "inject [--project P] [--json|--explain|--hook-json|--lane-report|--compare-report] — per-turn context for harness hooks (stdin: prompt or hook JSON); the day's first turn leads with a one-line brief whisper; --compare-report is the local-vs-comparison (Cloudflare agentic-memory) divergence verdict",
    "saguide": "saguide [--hook-json|--show] — the SubagentStart initial-physics seam: the ONE canonical block CONFIGURED for a fresh subagent (arrival unobserved) (hookEventName minted as SubagentStart, never inherited); --show prints the long form that block points at, for the guide/help",
    "drain": "drain [--apply] — route raw memory intake to typed homes (dry-run default)",
    "promote": "promote [--since Nd] [--cap N] [--apply] — episodic->durable funnel: recent USER messages -> drain-intake candidates (dry-run default)",
    "sweep": "sweep [--apply] [--project P] — lineage-driven supersession sweep of the adopted store (dry-run default)",
    "drift": "drift — surface belief drift; silent when steady",
    "reflex": "reflex list [--all]|add|retire|rescope <id> <project|fleet|->|smoke — (signal -> steer) entries (--all includes retired); signals prompt|marker-file|every-turn|counter|stalled|thrash|drift|stuck (record.py counters, latch once per episode). A reflex records the PROJECT IT IS ABOUT and unrecorded reaches every project; `rescope` records one without moving the file, and `list` shows each row's scope",
    "friction": "friction [--days N] [--seat] [--json] | record <guard> [--reason TOKEN] [--session ID] | dial [N] [--json] — refusals per guard over a window (default 7 days), counted from the friction ledger every guard refusal appends one line to; --seat adds the per-seat split; an unreadable ledger reads UNREADABLE, never zero. `record` is the door a shell hook counts through: silent, and it never fails its caller. `dial` prints how many refusals by one guard in one day a seat meets before it is told (the owner's number, with who set it and when); with N, a whole number from 2 to 50, it sets it",
    "record": "record [--hook-json]|status|install|swallows — session-keyed tool-outcome recorder (PostToolUse leg); its counters drive the counter/latch reflexes, and `swallows` lists the exceptions broad handlers ate",
    "todos": "todos [--all] [--json]|promote <id> [--owner S]|demote [--dry-run] [--owner S] — the seat todo mirror: what each agent is working on right now (seat | in-progress | done/total | age). The LIST is a PULL-only surface; the recorder captures TodoWrite/Task* into per-session state and pushes at most one rate-capped room line per meaningful transition (never a mention, never a DM). `promote` files ONE personal todo into the SHARED fleet ledger and stamps the row id back onto it, so an open loop stops living only in one session's scratch; `demote` is the CLEANUP leg — it removes personal rows whose ledger twin is closed or is now owned by another seat, and --dry-run shows what it would remove without removing it. BOTH ARE MANUAL VERBS: nothing calls them within a turn, so the bridge moves an item only when someone types it",
    "lineage": "lineage [seed|add|external|archive-report] — the project family tree",
    "whoami": "whoami [note ...] — the operator profile + dated notes",
    "interview": "interview — the five-minute know-your-user interview",
    "doctor": "doctor [--ensure] [--probe-memory] [--quiet] — health report; --ensure backs up named/default and minted-seat credentials, then heals named-home drift with unattended safety guards; --probe-memory spends one haiku call proving Claude Code still honours the auto-memory base variable in a scratch tree, and records the result per Claude Code version (run it after every Claude Code upgrade); --quiet reports failures only",
    "injectbudget": "injectbudget [--whole] [--verbose] [--json] [--session ID] [--path P] — what SHARE of a seat's context its own hooks injected, bucketed by hook kind, plus the within-compaction-window repeat rate for store entries. Reads the CURRENT context window by default (single-digit MB of a transcript that reaches hundreds); --whole reads the session and segments it at every boundary. Never wired to the per-turn loop: an auditor that becomes the cost it audits has refuted itself",
    "fixedtext": "fixedtext [--json] — the instruction files and project memory index each CHECKOUT loads before a seat's first turn, heaviest first, with every seat that shares it. Read-only: it caps and prunes nothing, because shared text is the business of every seat that loads it",
    "watchdog": "watchdog [--json] [--quiet] — scan proxy seat error logs for the context-window WEDGE signature and raise a loud a2a alert ONCE per wedge (re-fires only if the count climbs; a recovered seat re-arms), so a human can /clear + relaunch. Run it periodically (Monitor/cron) as the live backstop; --quiet detects without posting, --json is the machine form",
    "gc": "gc [--dry | --apply] [--install-timer] — declared retention budgets over the derived exhaust (dry-run default; authored content never touched). --install-timer wires the HOURLY cadence: a retention policy nothing schedules is a policy that does not exist (measured 11,192 items over budget on a gc that had never run)",
    "scratch": "scratch [small|big|durable [--name N]] | gc [--dry | --apply] | unattributable [--json] | status [--json] — the MOUNT plane: the substrate picks where scratch goes so no agent has to guess (small -> the fast ambient tmp; big clone/fork trees -> an UNCAPPED RAM mount while it has headroom, else disk; durable -> disk, never volatile tmpfs), and `scratch gc` reaps dead-session scratch (liveness before age, pressure-escalating TTL, bounded, dry-run default; the automatic pass rides the Stop hook, HELM_SCRATCH_GC=0 disables). PRESSURE HAS TWO PLANES: a tmpfs percentage cannot see that the BOX is out of memory, so on a volatile mount the TTL also escalates with /proc/meminfo (MemAvailable, swap) and PSI — a mount at 44% while the host is 23 GB into swap is a mount under pressure. And liveness bounds a TREE, not its contents, so a SECOND TIER reaps an aged direct child of a LIVE session when nothing holds it open, no live cwd is inside it and it is not a harness-owned name (HELM_SCRATCH_GC_TIER2=0 disables that leg alone). `unattributable` lists what helm can NEVER reap — biggest first, with size and age — because the one thing worse than refusing to delete a stranger's tree is refusing silently. `status` measures every mount helm writes on BOTH axes — bytes AND INODES, because a tmpfs `nr_inodes=` cap is invisible to df -h and is what turned a 36%-used /tmp into `No space left on device`",
    "skills": "skills [dupes|sync [--apply]] — census (read-only) + sync: one canonical skills source symlinked into EVERY claude-code config dir (credhomes + seats; dry-run default)",
    "evolve": "evolve — one observe/propose cycle (proposes, never mutates)",
    "mentor": "mentor observe <project> [--since 7d]|teach <project> \"<id> | <steer>\" [--attest]|review <project>|log — the inception actuator: critique brief, taught project reflexes with provenance, before/after review (teach is the one write; the rest read-only)",
    "fleet": "fleet [--json] — composition truth: every live claude process -> seat/sid/daemon/stamps/home, all live-probed; ANSWER FLEET QUESTIONS BY RUNNING THIS, never from memory",
    "sessions": "sessions [<project>] — every local claude + codex session (the two transcript formats the catalog indexes; opencode/pi sessions reach `helm projects` but produce no rows here); resume in one paste",
    "storage-matrix": "storage-matrix [--measure] [--json] — the owner-facing cross-box storage matrix: explicit bounded measurement writes one durable snapshot; bare reads never probe",
    "session": "session ls|doctor|doctor-panes|checkpoint|port|rescue|resume|experts|ask — the session substrate: inventory, persistence-health, checkpoint, port, compose (wraps cv; single-open law + print-don't-launch)",
    "accounts": "accounts [--json]|show <id> [--json]|set <id> --vendor V --plan P --count N --good-for TEXT --not-for TEXT --reach R [--price P] [--measured-as NAME] [--renews-on YYYY-MM-DD] [--notes TEXT] [--confirm]|rm <id>|seed [--apply]|line|teach — the OWNER-DECLARED account inventory: what we pay for, how many of each, what each one is FOR and what it must NOT be used for, plus how an agent reaches it. AUTHORITATIVE ABOUT INTENT ONLY: never active/healthy/headroom, which are the quota provider\u2019s measured rows \u2014 declared rows JOIN them through --measured-as and decorate, never overwrite. NO SECRETS: the schema has no field for a key, token, password, cookie or recovery code, and a value shaped like one is refused with a plain sentence that names the masked token and never echoes it; the one field that may hold an address is --measured-as (the provider's own account name, which is what the join needs), masked in every human rendering and whole only in --json. A save MERGES: a flag you do not pass keeps what is on disk. The owner edits this on the cockpit\u2019s quota tab (headline per account, detail on click, add/edit/remove buttons, a revision so two tabs cannot clobber each other); this verb is for agents, `line` is the one-line pointer and `teach` prints the store commands that put it on the JIT lane. A row helm cannot parse is reported and skipped, never dropped on the next save. HELM_ACCOUNTS overrides the path",
    "homes": "homes [prepare|provision|verify|archive|restore|migrate|archives] — credential-home lifecycle (provision runs the one benefit pass prepare runs over an existing claude home, additively; migrate renames a name-lies home to the canonical name for the identity it actually HOLDS, alias symlink left at the old path; refuses live sessions, never touches credentials)",
    "configs": "configs [list|show <path>|cascade <cwd> [--harness claude|codex|pi]|injection [--seat SEAT] [--session SID]|edit <path>|backups|restore <backup>] — every config across every home: list/show/cascade read; injection is the opt-in requested-vs-verified identity + versioned weight observation; edit takes new content on stdin through backup -> validate -> atomic write",
    "hooks": "hooks [install [--dry]|status|latency [--json] [--since T] [--until T]|sync [--apply]|run <EVENT> [--tool NAME]|preflight --config-dir DIR] — self-wire the per-turn inject hook into every claude home; sync reconciles every home to the NAMED canonical hook set (dry-run default)",
    "cell": "cell join|send|status — the a2a substrate, helm-named (status = node liveness; join and send ride an optional HELM_CELL_BIN transport and degrade honestly without it)",
    "chat": "chat post|read [--since N|--follow]|rooms|react <n> <emoji>|verify|log-flush|restore-journal [--apply]|retire-rooms [--apply] [--idle-days N]|dm <seat>|reply <id|n>|catchup|ack <id>|pending|receipts <broadcast-id>[@occurrence]|node up|down|status|prepare|refuel|meld|council|standup|verdict|reveal|council-status|council-abort|join|deliver|wait|seats|status [<line>|--clear]|seat gc [--apply]|claim|release|claims [--room R] — the human-included groupchat (RAM room + signed dregg transport; web panel = the owner's surface) + the delivery lane (tool-boundary nudge, roster presence, advisory session-bound claims). The rooms are tmpfs and DIE WITH A REBOOT: restore-journal is the ONLY rebuild path (log-flush's inverse — dry-run default, idempotent, delivery cursors protected so old mentions never re-deliver as new), and verify recomputes every signed row's digest (rc 1 on mismatch). retire-rooms archives meld rooms idle past the bound (default 7 days) to the journal, with every cursor they carried; the restore never resurrects a retired room (dry-run default). verdict|reveal|council-status|council-abort = the embargoed council vote: seal one member's judgment, lift only at quorum, tally without naming signers, abort permanently — an aborted council never reveals",
    "multiplayer": "multiplayer set|status|publish|read|presence|peers|leave [--backend V] — metaharness-agnostic local multiplayer: blind opaque-update relay + decoupled TTL presence (set/status drive the built-in LWW demo board; --backend on any verb selects the adapter pair, default local/HELM_MULTIPLAYER_BACKEND)",
    "launch": "launch [--seat S] [--home H] [--room R] [--model M] [--no-install] [--] [claude args…] — the metaharness seam: wire hooks, sync a named credhome from Orca's live copy of its account when that copy is fresher (a disagreeing identity execs nothing), seat the roster, exec claude under a stable addressable name carrying --model",
    "human": "human (or helm --human) — the operator's curses TUI: chat room + status strip, posts as you",
    "premise": "premise <id> | <statement> — capture a certain truth, attested on the ledger; --supersede <old-id> <new-id> | <statement> evolves the chain (one signed linking turn)",
    "task": "task add <title...> --owner SEAT [--note N] [--ref R]... [--id NNN] [--owner-asked] [--project NAME]|mirror [--apply] [--all-statuses] [--json]|list [--all] [--all-projects] [--project NAME] [--owner S] [--json]|triage [--apply] [--limit N] [--legacy] [--project NAME]|show <id>|resolve <token>|claim <id> [--owner SEAT]|takeover <id> --from-lane L --transfer-id ID [--superseding]|update <id> [--title T] [--note N] [--owner S] [--status S] [--origin owner|agent] [--ref R]...|close <id> <reason...>|comment <id> <text...>|standdown <id> <reason...> [--lift TEXT] [--until DURATION-OR-DATE] [--clear] — the FLEET TASK LEDGER: shared work items with real ids every seat can resolve. `mirror` is the harness-task loop: it carries every agent's PERSONAL harness list (the free scratchpad at <claude-home>/tasks/<session>/) into this ledger one-way, so work parked in a scratchpad stops being invisible and no agent has to notice. It DEFAULTS TO SHOWING — the pass that writes is `--apply` — because the report is the thing you reach for first and it must cost nothing; that default is what caught a 628-row sweep priced as 29 before a row was written. Live rows file OPEN and UNOWNED with their true harness status in refs (the ledger REFUSES an unowned in_progress row, and is right to), finished scratchpad items are COUNTED not imported so tombstones never bury the live board (`--all-statuses` carries them), and re-running is idempotent because the dedup key rides in each row's refs, at ~/.helm/_global/tasks.jsonl. Ids KEEP THE NUMBER THEY WERE BORN WITH (task/263), so the week of chat rows citing '#263' still resolves — `resolve` takes any spelling (263, #263, task/263) and answers UNPARSEABLE separately from ABSENT, because a typo and a number nobody filed want opposite answers. A live task must name an OWNER SEAT: an unowned row is a list nobody is accountable to. CLOSED rows include TOMBSTONES for items retired before the ledger existed — 55% of the fleet's citation load points at those, and a tombstone is what makes a week-old citation resolve to a sentence instead of nothing. `triage` is the RANK SWEEP: the priority field has existed since the free-text census and nothing ever swept it, so the board could not answer what is most important. It is DRY by default, counts the whole debt rather than the slice `--limit` writes, and `--apply` ranks through `update()` one row at a time so each rank is its own audited event. It applies only the clauses a stamped field can decide — owner-asked P1, the rest P2 — and NEVER writes a P0, because a fleet blocker is a judgement about what a row BLOCKS and no field records it. UNSCOPED legacy rows are opt-in behind `--legacy` and counted either way. `add`, `list` and `triage` take `--project NAME` and THE FLAG WINS OVER cwd, so a task ABOUT HELM files to helm from any checkout instead of landing in whatever project the filer happened to stand in (task/2446, measured from another project's checkout); each says which scope it used and how it decided, and an unregistered name is refused naming the registry. `update`/`close`/`show` take a FLEET-WIDE id and read no scope, so they refuse the flag rather than no-op it. An UNRANKED row is not a P3 and nothing defaults it. Deliberately carries NO board projection and NO owner-gate queue: 120 engineering tasks filed where 3 owner rulings live is the exact burial ownerasks.py:687-691 already measured once, at 19 rows",
    "asks": "asks add <text> --needs \"<what only the owner can supply>\" [--kind owner-input|usage-reset]|done <id> <evidence>|report <id> <chat-post-id>|list [--open] [--json] — the durable OWNER-ASK ledger (agents self-add); kind is typed at write time and never inferred from prose; 'done' stays OPEN until 'report' names the chat post that told the OWNER (owner-surface-is-the-bar); unreported asks ride the stop-whisper's top rung",
    "decide": "decide file <title> [--asker SEAT] [--ref R]... (card body on stdin: context paragraph, then `* label :: consequence` option lines, `*!` = recommended; none = approve/reject)|list [--open] [--json]|show <id> [--json]|verdict <id> <choice> [--comment T] (always refused: the CLI is never the owner's door)|deliver <id>|comment <id> <text> (recorded and relayed as the acting seat)|board-sync — the durable OWNER DECISION queue (the ask ledger generalized to rulings-among-options): any seat files a card, ONE compact line lands in its room AND the card's HEADLINE is pushed to the OWNER'S PHONE (a room is read by seats, so without the push a card waits on him noticing; the OPTIONS travel in the push and it names no console, because helm web binds 127.0.0.1 which on a phone is the phone; reach is recorded, never assumed — an opt-out or a failed push leaves the card marked NOT PUSHED and the next card's push carries it), the owner answers on the WEB queue at his leisure, and the verdict DMs the FILING SEAT (beacon-woken — never lost to an unwatched pane); 'decided' stays open debt until 'delivered' (the done-vs-reported split), and delivered binds to the DURABLE ledger — a retry re-verifies the tmpfs DM lane and re-sends from the ledger on loss (at-least-once, stated); the board's owner_gated_queue rows it stamps src=ledger are auto-derived from open cards + unreported asks, hand-written rows passing through untouched",
    "telegram": "telegram status|send <text> [--reply-key SEAT]|poll [--apply] [--room R] — the owner's phone as a TWO-WAY surface, and the INBOUND half of notify.py's founding rule. That rule (an alarm about the fleet being unreachable must not depend on a fleet member being reachable) fixes OUTBOUND only: the owner can be told, and still needs a way to reach IN without chat, beacons, panes or a web console bound to 127.0.0.1 — which on a phone IS the phone. Orca panes may span daemon generations, so the shared boundary is fleet-local reachability, not one daemon common to every pane. Telegram holds the queue, so his reply survives the fleet being gone; `decide` cards have always pushed their OPTIONS to his phone while the ANSWER required the web queue he cannot reach from it. NOT A SECOND PUSH PATH: callers keep using notify.owner_push, which fans out to both transports behind one door, and a test pins the importer set to notify.py alone. `status` reports which HALVES are bound and never a value — the token can speak AS the fleet to him and READ every message the bot sees, so `configured()` requiring BOTH halves is the whole public surface and UNSET is a deliberate opt-out that makes no network call. `poll` is PULL, never a webhook (this box has no public endpoint and should not grow one) and is TWO-PHASE: it reads without acknowledging, routes, then acks only what it actually routed, so a crash mid-loop re-delivers rather than swallowing — at-least-once, because a duplicated reply is visible and annoying while a lost one is invisible to the one person who cannot see it fail. Without --apply nothing is posted AND nothing is acknowledged, so a dry run stays repeatable. A reply carrying the footer key DMs the seat that asked; free text with no key posts as the owner, which is the common case because he mostly just types. Every inbound row is refused unless its chat id is HIS — that check is what authenticates the origin=telegram owner rail, which is otherwise advisory exactly as chat.post says",
    "dispatch": "dispatch send <recipient> <lane> <message...|body on stdin> --ref TIP --kind build|review --new-work|--supersedes ID [--key K] [--force] [--read-only-because REASON]|add <recipient> <lane> --ref TIP --kind build|review --new-work|--supersedes ID [--force] [--read-only-because REASON]|verdict <id-or-unique-prefix> <full-reviewed-tip> --approve|--fix|--supersede|--concur --measured|--inferred|--unverified [--worse-than-main PATH ...|--imperfect] [--patch-tip FULL_SHA|--no-patch-because REASON] [--reviewer-model M --reviewer-run RUN [--author-model M]] <evidence>|cancel <id-or-unique-prefix> <reason...>|mark-delivered <id-or-unique-prefix> <delivery-ref>|rebind <id-or-unique-prefix> --to <seat> [--force] [--reason R] [--repo PATH] [--json]|retip <id-or-unique-prefix> --ref NEW_TIP --reason R [--repo PATH] [--json]|hold <id-or-unique-prefix> <reason...>|release <id-or-unique-prefix>|list [--open|--overdue|--held] [--mine] [--issued] [--to SEAT] [--all-projects] [--json]|triage [ID...] [--all-projects]|mix [--hours N] [--sender SEAT] [--json]|briefs [--cut]|collisions [--json] — durable DISPATCH ledger. EXACTLY ONE of --new-work / --supersedes <dispatch-id> is REQUIRED on send and add: the LANE is a free-text label and could never say whether a row continues earlier work, so a renamed continuation was invisible to every same-lane rule and a re-dispatch that named nothing built 124 land loops with no way to close them. --supersedes links to the row this continues (its chain root becomes this row's work identity); --new-work roots a fresh chain. Rows written before the field carry NO chain and stay legacy — never retro-fitted, never guessed. A parent that is cancelled or still open is valid. A second OPEN child naming the same parent REFUSES unless --force explicitly declares a deliberate fork; a same-repository OPEN row reusing a --new-work lane label only warns because lanes are labels, not identity. An id that does not resolve, an ambiguous prefix, an unreadable ledger, and a parent with a corrupt chain all REFUSE the write, because unknown work identity must never quietly mean new work. --kind is REQUIRED on send and add: it records whether the dispatch asks the recipient to BUILD or to REVIEW, and `mix` reads it to answer how fleet capacity is allocated. Omitting it was allowed once and made the capacity alarm vacuous — the inverted night it exists to catch reproduced simply by not typing the flag. Rows written before the field carry UNKNOWN and are reported in their own bucket, never assumed. --measured|--inferred|--unverified is REQUIRED on verdict and records HOW THE REVIEWER KNOWS (owner ask, task/338: 'always making legible how much doubt should go into a conversation'): measured = a tool ran and produced the finding, inferred = reasoned from code or output read, unverified = not checked. A MARKER rather than a percentage, because a number invites a precision nobody can audit while these three are checkable against what the reviewer actually did. It was canon at certainty 1.00 for a day and reached 2.34% adoption with ZERO mechanism — required at the CLI for new writes, permissive in mark_verdict so replay is untouched; the 221 rows written before the field read UNMARKED, never 'unverified'. OMIT <message...> to pipe the body on STDIN or use a QUOTED-delimiter heredoc (`<<'EOF'`) — the literal route, since argv bodies and UNQUOTED heredocs both substitute backticks and $() before helm sees them. A dispatch persists before delivery; one operation sends AT MOST ONCE (a retry never re-DMs — confirm at the recipient); ambiguous delivery stays open NEEDS CONFIRMATION; a matching exact-tip verdict closes it only with an explicit polarity; APPROVE additionally requires a verified `gate:<token>`, while FIX/SUPERSEDE remain valid ungated because they authorize no land. CONCUR is the fourth polarity and BY ITSELF authorizes NOTHING: it records that a reader endorses an artifact, binds ungated like FIX, and is absent from every global close allowlist and from SPIRAL_TERMINAL_POLARITIES. Only the separately opted-in prospective bounded compose/landed-proof-v3 contract can close a qualifying row; see lr help. It exists because the only ungated verdicts were the DISAPPROVING ones, so on a row whose artifact is not a landable tree agreement had to mint a receipt while objection bound free. `cancel` honestly abandons a stranded one with a reason, and ALSO closes a reviewed row whose verdict declared NO polarity: that verdict authorized nothing and demanded nothing, so it is advisory by construction and the close is recorded as an ADVISORY CLOSE whose reason names it (a verdict WITH a polarity is still refused, and a row the lr ladder already retired is still refused). `rebind` moves one OPEN row to a new recipient in a single operation — cancel-as-REBOUND plus a superseding re-add preserving lane/ref/kind/note/deadline — and is EVIDENCE-GATED: either proxywatch-measured starvation/hang or autocompact-proven fresh context exhaustion suffices; an unreadable proxywatch does not suppress the independent context arm, but no evidence across both arms refuses. --force overrides with a mandatory recorded reason, and the DM body does NOT travel (only its hash is stored), so re-brief the new recipient. `retip` is rebind's mirror for when the BASE moved rather than the reviewer: it re-points one OPEN row at a NEW TIP in place — same row, same recipient, same chain, one strict audit event — refusing on any non-open row because a verdict BINDS its tip. `hold` acknowledges a row while gating it on a named external dependency; `release` returns the HELD row to OPEN; `list --held` shows that bucket. `list --mine` keeps ONLY the rows naming THIS seat, `--issued` ONLY the rows it SENT, and `--to SEAT` asks the recipient question about a named one — the filter the resume-turn hook has always instructed compacted seats to apply and the verb could not perform: measured from a seat with zero obligations, `list --open` returned two rows naming two OTHER seats and that seat adopted one (task/1007). `--mine` derives identity through the SAME door that stamps a dispatch author (declared name and roster binding must agree; a DERIVED family floor — no declared name AND no roster binding — is refused, while a name a process explicitly declares is taken at its word), and when identity cannot be resolved it REFUSES and prints no rows at all, because a listing the reader believes is 'mine' while it names every seat is the failure the flag exists to close. `--mine` alone was still FALSE for an AUTHOR (measured on one seat, 2026-08-12): it is RECIPIENT-scoped, so it printed 'no matching rows' to a seat holding an OPEN row it had SENT, two hours past deadline, that only it was positioned to chase — what a sender owes is the DELIVERY LEG, so a row sent but never delivered, or delivered to a seat that went quiet, is the sender's to chase. `--issued` is that half, resolved through the same identity door and refusing the same way; `--mine --issued` is the UNION ON THE DIRECTION AXIS, which the resume hook names as ONE command because two commands rebuild the incident at execution time (run the first, read a reassuring nothing, stop). It is SILENT ON THE STATE AXIS: `--open` selects status `open`, so a HELD row is in neither half whichever way it points, and an empty union means 'nothing OPEN owed' — ask `--held` for the rest. Both halves bind to the SAME identity, so the union widens to no other seat's rows, and rows in it addressed to someone else are marked YOURS TO CHASE — a row you sent, read as a row you must DO, is duplicate work. A row whose sender names nobody matches NO seat under `--issued`. `triage [ID...]` re-measures each open row's claims against the tree checked out RIGHT NOW (file:line by content, counts against the live ledger, cited shas by ancestry AND patch identity), printing the verdict beside the id so a seat picking up work reads the decay before the prose; EVERY NAMED ID GETS AN ANSWER — a token resolving to NO row refuses on stderr with rc 2, and a named row triage deliberately skips (verdicted/cancelled/held/carried by a live successor) prints one line naming its state, because a silent skip made a typo'd id and finished work byte-identical and silence is the one failure mode nobody re-checks. A proof-gated LR discharge may later annotate a contrary FIX/SUPERSEDE verdict without rewriting it, and accepts only a later land-authorizing approval (tier + gate included). Historical ref-less rows read NEEDS REDISPATCH (redispatch with --ref; no bind/ack verbs exist — retargeting is `rebind`, above). Unavailable storage means obligations UNKNOWN. A ROW IS KEYED BY THE REPOSITORY ITS REF LIVES IN (task/2437): this ledger holds rows for ANY repository a registered helm project claims — send/add admit a ref in the project the cwd or --repo resolves to and refuse an UNREGISTERED repository by name, naming `helm sync` on that checkout as the repair, so a team using helm never needs a helm package inside their own tree; `list` and `triage` scope to the project of the directory you run them in and take --all-projects, while --mine/--issued are never narrowed by project because a row that names you is yours wherever its code lives. A ROW THIS REGISTRY CANNOT PLACE IS NOT COUNTED IN THE READER'S PROJECT (task/2468): a row naming no repository at all, or one whose project lookup came back empty — nobody registered that repository, its path would not resolve, or the registry could not be read, which are ONE answer here — goes to its own UNKNOWN PROVENANCE bucket. What that bucket reports is the FAILED LOOKUP and never proof that the row is in no project, because the ordinary resolver fails open to the same empty answer. It is printed with a count, below the project's own rows, and never inside whichever project the reader happens to be standing in, which is how another project's checkout came to report 10 rows of which 9 were helm's. `list --json` carries the bucket as a `scope_class` field on every row (local / origin_unknown / project_unresolved) rather than dropping those rows, because the machine surface is the one with no headings to read. NO REVIEWER IS NEVER A BLOCKER (task/2948): every UNUSABLE refusal ends on the fallback ladder with a command per rung — any other-family seat (the openrouter seat is always one), then a Fable one-agent Workflow, then on a Fable limit Fable through another credential or seat; Sonnet and Haiku never review — and a Workflow run, which is not a seat, is recorded on the row by its sender or recipient with `verdict ... --reviewer-model M --reviewer-run RUN [--author-model M]` as an ADVISORY read (an `advisory-read` event): the reviewing model must be ANOTHER FAMILY than the author's (the sender's runtime record, or the recorder's declared model), or Fable for a Claude author; a model helm does not recognise, a Sonnet or Haiku model and the author's own model are refused, and so is APPROVE (a model run CONCURs or FIXes). It discharges NOTHING: the row stays owed until helm can verify the run on disk (task/2966). EVERY REVIEW ROW GETS A qwen27 FINDINGS NOTE (task/2960): send and add of a review row, and retip, start one detached, queued local read (`python3 -m helm.findingspass <id>` re-runs one by hand; HELM_QWEN27_FINDINGS=0 switches it off), and its answer lands on the row as a findings-note that triage and both verdict paths print. It is NEVER a review, an approval, a gate or the different-model read (--reviewer-model qwen27 is refused), and an empty, partial or failed read says it is not clean. A `send` to a CODEX recipient also consults the POOLED CODEX BUDGET (the snapshot `helm proxywatch` writes; the send never probes): it REFUSES with the UNUSABLE shape — `--force` files it anyway — only when every pooled account was READ and every one is at or past HELM_CODEX_WEEKLY_CEILING_PCT (default 90) on its LONGEST window, WARNS naming the headroom left when some are past and some are not or any is unreadable, and admits silently with no snapshot, a stale one, or a non-codex recipient (task/2480: all five pooled accounts hit their WEEKLY caps while the 5h window each seat was paced on still read healthy). A BRIEF IS STORED WHOLE, BY REFERENCE, and the row keeps a bounded copy. The ledger row is parsed by every list read, so its body stays under MESSAGE_BODY_CAP; the brief itself is written to one content-addressed file under the helm home BEFORE the row that names it, so a row can never point at a file that does not exist and a kill in between leaves an orphan file rather than a dangling reference. The row carries the file's byte length and its blake2b-128 digest, and every reader that renders a brief — triage, the rebind note, the compose contract — recomputes that digest before showing the text, falling back to the row's bounded copy with a LOUD line when the file is missing or is not the brief the row was sent with. `send` REFUSES a brief over 32768 UTF-8 bytes rather than cutting it: measured briefs run 5-11 KB and a cut brief reads as a whole one to its recipient, which is a review-quality defect at the door. A REVIEW brief that tells its reader not to edit (read-only, do not edit, do not commit, no edits, report only) is REFUSED at send and add unless --read-only-because REASON records why, and the row keeps that reason: a reader that may not edit cannot commit the cure for what it finds, which is the move the whole review procedure is built on. Every review send also prints the procedure in one line. Symmetrically, a FIX verdict with no --patch-tip REFUSES unless --no-patch-because REASON (one quoted argv token) says which of the two reasons applied -- a DESIGN finding bound for a meld is a valid one and is recorded like any other. --imperfect, which alone is still refused as not-a-block, is ADMITTED together with --patch-tip: that is the reader who found the tip no worse than main on any touched path and committed a real improvement anyway, and the row records polarity fix, exit answer IMPERFECT and the patch. It asks the lane's author to AGREE TO THE PATCH and blocks nothing about the original tip. `briefs [--cut]` is the read-only census of OPEN rows written before this store whose brief survives only as a cut copy — their tails went out in the original DM and are not recoverable here, so re-send them. `collisions [--json]` is the read-only list of every event the fold DROPPED because it reused a seq an applied event on its row already held: a LIVE line is on a row still open or held that no successor carries, and `helm doctor` WARNs on each; a HISTORY line is on a row that has ended (closed, retired or superseded), and the doctor only counts those. --json is the whole list, each entry carrying its row's ended word.",

    "compose": "compose --room LANE --base REF --car ROWID:LANE [--car ...] [--apply] [--repo P] — the fold that produces its own evidence. READ-ONLY without --apply: prints the cars it WOULD pick and touches nothing. With --apply (gated by HELM_WORK_INTEGRATOR=1, the same door `helm work` uses) it resets the room to the base, cherry-picks every plus commit of every car, runs the controls, and records per car the source ROW, source COMMIT, source PARENT TREE, result COMMIT and result TREE under the project home keyed by the composed tip. Replay and trees, never patch identity: git patch-id hashes CONTEXT, so a car picked underneath other cars that touched its files re-keys and a MISS proves only that no commit carries that EXACT diff. A conflicted or hand-edited car records exact-review-required rather than a weaker automatic answer",
    "landgate": "landgate --lane L --tip SHA --tree SHA --gate ID [--base REF] [--repo P] — READ-ONLY: may THIS seat land THIS lane right now? Reports the five clauses of the self-land predicate (landlock held; a gate bound to the POST-REBASE tree it is actually landing, not the reviewed tip; an approving cross-family verdict at the reviewed tip; a changed-file set disjoint from the other approved-unlanded lanes; freeze admits) one line each, and names the clause it refuses on. It NEVER lands — the actuator is a separate verb with a separate bar, so asking is always safe. UNKNOWN never qualifies: a land is irreversible on a shared trunk, so what cannot be proven is refused",
    "gate": "gate run [--focus [--plan]] [--label T] [--timeout S] [--repo P] [--json] [-- <argv>] (exit 0 bound green, 1 red or refused, 2 usage, 3 NOT RUN: this node refused it on capacity — whole-suite cap, memory-stall floor or pressured tmp; no receipt, retry later or on another node)|equiv [--repo P] [--repeats N] [--no-timing]|show <id>|list [--limit N]|fab contract|submit|observe|wait|fetch-receipt (the detach-safe Fab job door: durable handle keyed by repo+tree+scope+interpreter, submit-or-join, disposable followers)|window launch [--repo P] [--label T] [--trunk REF] [--supersede]|show (ONE whole-suite gate per project LANDING WINDOW, enforced by a refusing door instead of by a seat remembering the rule: a window is one trunk head, a green receipt on a stacked train's top car lands every car beneath it, and twelve runs were launched for four trains before this door existed. `launch` records the room, the trunk head and the project BEFORE it dispatches, then REFUSES a second whole suite on the same window — or on a room a running run already CONTAINS — naming the running id, host, label, elapsed time and room, and printing the only two doors that are open: WAIT, or --supersede, which kills the running run and gates this room in its place and is allowed only when this room contains the killed run's head, so no car it carried stops being gated. Liveness is never remembered: every read asks the NODE whether the id is still in flight and retires a record it no longer reports, while an unreachable node is UNKNOWN and keeps the record rather than opening a window a live run still holds. `show` prints the in-flight window per project.)|import <artifact.jsonl> [--repo PATH] [--id RECEIPT-ID] — MINT a suite result. `equiv` is DIAGNOSTIC ONLY: it compares literal serial authority with fresh-process sharded observations, and a match never qualifies or authorizes sharded evidence. `--focus` MEASURES its own scope (changed files vs merge-base -> import-graph consumers), runs exactly those test modules OUTSIDE the whole-suite FIFO, and mints a v6 receipt whose scope is bound into its content id. The scope has TWO halves and neither is the caller's: the SELECTION is what the repository says had to run, and the RAN set is what the child's own verbose protocol reported it actually ran — a selected module that produces no test is visible on `gate show` as NEVER RAN and REFUSES at bind, because a receipt may only claim the coverage a runner produced, never the coverage an argv asked for; it binds a CURE-ROUND verdict (FIX/SUPERSEDE/CONCUR) at the exact tip after bind re-derives the diff and proves the recorded scope covers it, and is REFUSED by APPROVE, the land door and the fold rung — the whole suite still runs once, at the land gate, on the composed tip. `--focus --plan` prints the selection without running: the reviewer's set-challenge surface; `--plan` ALONE prints the resolved whole-suite command and its source, which is the seam anything outside helm asks before spending a box. Binding RE-DERIVES every field of the recorded scope from the standing repository — base (merge-base, refused when ambiguous), changed set both directions, consumer selection and universe recomputed at the tip's committed tree — so an assembled focus block refuses on the first fact the repository disagrees with; and a plain focused artifact NEVER imports, because every generic artifact check is a computation its submitter could run; cross-box focus uses `gate run --focus --box HOST`, whose challenge-framed live session independently binds the shipped head/tree, remote node, framed receipt object and fetched artifact before the local ledger admits v6. The scope planner FAILS CLOSED: non-module changes, module-shaped symlinks, and multi-merge-base histories refuse toward the whole suite, while a dynamic import or opaque python child nothing can bound makes its module a consumer of everything rather than silently under-selecting. WHICH COMMAND IS THE PROJECT'S OWN, not helm's: a registered project declares it once as the authored `gate` field in registry-authored.json (`{\"command\": [\"pnpm\", \"-r\", \"test\"], \"protocol\": \"exit\"}` — an argv LIST, never a shell string, spawned from the repo root), and the receipt carries that command in a `suite_command` block bound into its content id, so foldcheck, the land gate and the verdict binding accept it unchanged. `protocol: exit` means the command's own exit status IS the verdict and the receipt records NO count — a runner that prints `Ran 9 tests ... OK` must not be able to claim a count helm never counted — and the command's whole stdout+stderr is kept: in `gate-command-output.txt` beside the receipt ledger, on helm's own stderr, and as a bounded tail in the receipt's `stderr_tail` (meta `source: declared-command-output`) at every status, with `detail` naming the exit code and the sidecar path; `protocol: unittest` reads python unittest's summary off stderr as always. helm's OWN source tree keeps its own suite with no registry edit and its receipts do not move, and a repo that declares nothing and ships no helm is REFUSED naming the field rather than gated with a command it never asked for. The only way a gate claim enters helm: helm RUNS the suite (as a child of ITSELF, so the recorded interpreter cannot differ from the running one), reads the runner's own summary, and writes a receipt binding interpreter + HEAD + tree + dirty-flag + counts. Receipt v7 is permanently WITHDRAWN; a whole-suite run whose failure list exceeds the display cap mints the current v8 BOUNDED FAILURE RECORD instead of silently dropping the excess identities: the receipt keeps the capped diagnostics plus the exact total, and every full identity travels as content-addressed sibling chunk events the receipt id binds. A focused (v6) run keeps the legacy capped-with-a-marker list. `run` prints the one evidence line you paste into `dispatch verdict`, whose `gate:<id>` token that verb then RESOLVES — exact-tip receipts bind unchanged; a later whole-suite head also binds when the standing dispatch repository proves it contains the reviewed tip and the receipt strictly postdates the dispatch. Divergent/reverse/UNKNOWN ancestry, dirty trees, and non-OK status REFUSE. Evidence with no token remains valid and UNVERIFIED for FIX/SUPERSEDE, but a receipt-capable APPROVE is refused before append because it could never authorize landing. Gate under the other interpreter by invoking helm under it (`python -m helm gate run` vs `python3 -m helm gate run`) — on THIS box `python` is GraalPy and `python3` is CPython and they disagree about whether the suite passes. A `--` command records interpreter UNKNOWN and can never bind a verdict, because helm did not choose it. Every receipt also names the HOST and the ROOM it ran in, both on the evidence line: a suite can be honest about the code and wrong about the box (a fab-minted red cost a reviewer a turn on 14 failures that only exist on the other machine), and a green from the shared checkout proves nothing about your lane. The cross-box cure is `import`: a receipt minted on the box that CAN honestly run the suite travels as an artifact.jsonl and enters the binding ledger ONLY through it — referenced chunks validated as the complete failure record, every event kept within the ledger byte limit, content ids recomputed and matched, cited head+tree required to resolve HERE, then canonical content-equivalent chunks and the gate row made durable before the canonical binding for THIS importing repository is appended as authority; the later audit pointer is warning-only — so the hand-append that once smuggled seven genuine-but-unprovable remote rows in is the wrong tool forever. A receipt that cannot name its host REFUSES to bind.|audits [--repo P] [--json] [-- <test module>...] (prints ONE pasteable `fab test` command naming every TREE-WIDE AUDIT, the test modules that enumerate the package and judge every module they find, which no consumer sweep over a change ever selects; name the lane's own modules after `--`; paste it on the COMPOSED tree before a whole-suite gate; it starts nothing itself)",
    "stale": "stale sweep [--dry-run] [--quiet] [--json] [--ensure-timer]|redispatch <dispatch-id> --reviewer <current-seat> [--repo <checkout>] — the standing HOUSEKEEPING loop over aged open work (task/445): walks lr loops past their per-stage STALLED threshold, open dispatch rows past their deadline with no visible progress, task-ledger rows untouched past 3d, and rows whose author CURED and never re-dispatched (bare triage's own cured-unwitnessed classifier, called never re-derived — cured rows are CLOSED, so every open-frontier instrument is blind to them by construction and the pool grew 8→11 in one night with zero drainage, task/983), re-measures each with the triage machinery (clearspan claims; landreq's ancestry→patch-id landing predicate; the lr projection's own base_behind), and POSTS ONE DIGEST PER ROW-OWNER proposing a disposition per row — supersede-candidate naming the carrier sha, cancel-with-reason (dispatch claims rotted AND content proven absent), retip-candidate (base past the measured bar), reanchor-needed when a task's citations rotted but its work truth is unknown, redispatch-candidate addressed to a cured row's AUTHOR (the sender — the one seat that can re-dispatch the review; when that author's family is provider-walled per proxywatch's CANONICAL family identity — verified runtime metadata first, name-parse only as fallback, so a seat whose display name differs from its credential family is not read as wall-free — or the wall state is unreadable, the row rides the integrator digest as redispatch-by-proxy instead, because a proposal addressed to a walled seat is parked with the only reader who cannot act), or still-live-keep with the evidence line. EVERY PRINTED DOOR IS RUNNABLE ON ITS OWN ROW: a cured row is CLOSED by construction, and rebind/retip accept only OPEN rows, so it is offered the superseding review dispatch instead — an actuator named in a digest is a promise the reader will try. Cured rows are measured per REPOSITORY, each against its own git index (the ledger is global, a git index is not; one shared index reports every foreign-repo cure as nonexistent), and a row with no repo_id is UNKNOWN and reported rather than resolved against whatever tree the sweep stands in. SWEEP PROPOSES AND NEVER EXECUTES; `stale redispatch` is the explicit mutating actuator that records and delivers one exact cured successor after repository, reviewer, wall, and self-delivery checks. The sweep reply form is CONCUR/OVERRULE per line (task/431's async convergence), and every terminal or citation-edit verb stays the owner's. Attention budget: one post per owner per sweep, a row re-proposes only after 3d or when its proposal CHANGES — keyed on the disposition AND the row's content identity (the cure tip / reviewed tip), so a second cure commit under an unchanged disposition is still news; unowned rows ride one integrator digest; an undelivered digest never latches its rows; a digest capped at 12 lines latches ONLY the lines it rendered and declares the remainder, which rides the next sweep rather than being recorded as asked; and a sweep whose sources were not all readable SUSPENDS re-arm, because absence from an incomplete enumeration is not departure from the population. --ensure-timer installs the daily systemd user timer (identity HELM_CHAT_NAME=stale-bot with session vars unset — repo-watch's measured env hygiene); the doctor line reports rows swept / proposals filed / oldest unproposed age plus the loop's own last-run age, so a dead timer is visible",
    "lr": "lr list [--all] [--all-projects] [--cold] [--json]|show <id> [--json]|stalls [--json]|foldcheck <tip> [--gate gate:TOKEN] [--repo PATH] [--remote R] [--branch B] [--no-fetch]|legacy-completion-hints [--json]|land <id> [--json]|compose <id> [<id>...] [--trunk REF] [--repo PATH] [--bounded-concur] [--dry-run] [--json]|close <id> --reason landed|superseded|withdrawn|out-of-scope|stranded|subsumed|delivered-report|discharged|resolved|carried|chain-proof|expired|endorsement-moot [--evidence LINE] [--artifact-ref REF] [--report-ref CHAT_REF] [--tip FULL_SHA] [--repo PATH] [--trunk REF] [--live | --needs-restart WHAT] [--compose-manifest PATH --compose-gate gate:ID] [--dry-run] [--json]|retire <id> --reason TEXT [--note N] [--seat S] [--dry-run] [--json]|retire --sweep --older-than 14d [--all-projects] [--dry-run] [--json]|retire --off-frontier [--apply] [--all-projects] [--json]|annotate-delivered-report <id> --artifact-ref REF --report-ref CHAT_REF --evidence LINE [--json]|discharge <id> <full-superseding-tip> <evidence...> [--json]|withdraw <id> <evidence...> [--json]|abandon <id> --reason TEXT [--repo PATH] [--json]|close-landed <id> --trunk REF [--repo PATH] [--json]|refs [--repo PATH] [--json]|migrate --commit-map PATH [--repo PATH] [--apply] [--json] — LAND REQUEST view over dispatch + live Git patch identity. `close` is THE terminal verb: landed proves reviewed work on one pinned trunk and also closes an OPEN BUILD parent through an exact descendant authorized APPROVE review (the parent tip remains a base, never reviewed proof); ONE LAND CLOSES EVERY ROW BOUND TO THAT COMMIT — both landed doors then sweep every other live row in the same repository whose bound tip is byte-identical (a second cross-family review leg, a re-ask, a duplicate mint), each re-proving itself through its own landed ladder or, if it never got a verdict, through discharged's TIP tier; the same sweep takes every never-verdicted chain PREDECESSOR whose bound tip is an ancestor of the landed tip (the rung a re-tip leaves behind) through discharged's CHAIN tier; a lane NAME never groups anything and a FIX/SUPERSEDE peer on that tip stays OPEN because findings are debt, not duplication; --dry-run names both lists and the sweep is fail-open, so refusals ride back as data and a retry finishes a partial fan-out; superseded runs the full chain-walked discharge ladder for any verdict polarity; withdrawn proves absence and stays falsifiable, and is ALSO the terminal for a FIX answered by WITHDRAWAL — the author accepts the finding, the artifact should not exist, and there is no successor tip to supersede; the closing seat is recorded and `lr show` names it; out-of-scope cancels a moot OPEN row through the cancel boundary after a liveness block; stranded terminates only a provably destroyed substrate; subsumed closes APPROVE or FIX review debt through a later linked same-repo cross-family APPROVE while the original is absent, with FIX requiring an explicit findings-answered-on-trunk statement; delivered-report closes only an OPEN BUILD with a compact typed artifact ref, full 12-character lowercase-hex Helm chat row id, and concise evidence, claiming no verdict or Git landing; discharged closes only a never-verdicted row whose work is on trunk under a successor's landed, gate-verified APPROVE, and the row is OPEN or HELD under a SOURCE-CLEAN hold (zero findings: an ordinary or owner-gated hold, or an advisory read naming a finding, keeps the row, because a verdict is where findings live and a close reason is not a verdict) — the authority is the discharging row's, re-derived from the ledger at write AND replay, never taken from the event; resolved retires a FIX/SUPERSEDE-verdicted row whose OWN reviewed tip reached trunk by ancestry, on one later same-repo cross-family confirmation verdict (approve or supersede, never fix) whose evidence opens `Resolution verified on trunk:` — the overridden reviewer is recorded on the close event; reference syntax is validated, not existence; carried closes a row whose work is PROVEN CARRIED at trunk HEAD right now but whose discharge was never chain-linked — the gate is a replay of the row's chain-bound work onto HEAD asking whether the result IS HEAD, strictly stronger than landed's ancestry-or-patch-equivalence, re-derived at ladder, write AND replay, with the evidence line saying only WHY no discharge was recorded; endorsement-moot retires a CONCUR whose own reviewed tip is an ANCESTOR of trunk — the fourth cell of the table landed/resolved/discharged fill for the other three verdict shapes, and the one that had no door. A concur authorizes nothing, so if the work is on trunk this row did not put it there and the close records where the authority was NOT; it grants none, renders SUPERSEDED rather than LANDED, refuses any polarity but concur (an approve over landed work is landed's row), refuses patch-identity-only carriage, and refuses an ABSENT tip by name because that is withdrawn's question or expired's. landed also REQUIRES the LIVE step — trunk and the running fleet are different facts, so `--live` declares CLI-class (a fresh helm process off main, live AT LAND) and `--needs-restart WHAT` declares process-class and names what still holds pre-land code until it re-arms (`helm rearm` measures that); helm never guesses the class and a row closed process-class prints as an OPEN LOOP. `legacy-completion-hints` is a read-only audit over cancelled explicit BUILD rows whose cancel_reason contains the historical standalone word `delivered`; every result remains CANCELLED, classification UNVERIFIED, delivered-report eligibility UNKNOWN, and may be a negative or code delivered under a successor. `annotate-delivered-report` is the separate explicit append-only correction for a historical cancelled BUILD; it preserves the cancel event/reason and never infers authority from prose. A confirmation rooted at the exact original also links a pre-chain legacy row; SUPERSEDE remains outside SUBSUMPTION. Historical proof-v1 closes replay unchanged. discharge/withdraw/close-landed are deprecated aliases of close for one release. abandon stays its own verb (not an alias): it writes off reviewed work only when the row-owned Git repository explicitly reports its commit missing, recording land state UNKNOWN. `lr land` stays the separate at-integration receipt verb and is NOT deprecated. Land receipts stay diagnostic only and fail open. `compose` normally stands N READY lanes on one measured tip in a detached <repo>-wt/compose room; --bounded-concur admits only prospectively opted-in, complete, zero-findings CONCUR/MEASURED review ranges whose declared effects and independent protected-owner diff permit reversibility. Bounded compose requires a registered project with active composition proof and records all source/result cars in the canonical project-home compose-manifests artifact, never a room sidecar; final fold authority stays pending the whole-tree suite gate. The paired --compose-manifest/--compose-gate landed-close path resolves that canonical artifact and revalidates each row. Both writers are disabled unless HELM_COMPOSE_LAND_V1=1; deploy compatible proof-v3 and canonical fold readers first, and confirm compatibility externally before enabling. This is not global CONCUR authority, a retrospective REVIEWED upgrade, or an integrator-name privilege — per-member patch-id carry re-measured across the cherry-pick, refusals name the member (conflict/drift/already-landed/partial), one gate on the composed tip is the only per-tree evidence, prefix re-compose localizes a red batch. `retire --off-frontier` is the census over rows that are not on the LIVE FRONTIER at all — a non-terminal row with no lane-family ref, no lane-family room, no live lease and no live branch or room holding its tip off trunk, so the work either reached trunk under a later tip (landed-by-ancestry / landed-by-patch-id) or was abandoned and the lane reaped (abandoned-unreachable). The census is the default and it is dry; --apply routes each row on TWO facts — the git measurement and the verdict's chain polarity — into the EXISTING close reason registry (landed for a non-contrary landing, carried for a CONTRARY whose work reached trunk, withdrawn for an unreachable tip) whose ladder re-proves the claim under the lock, so a refusal rides back with the ladder's own words and nothing is forced. UNCLASSIFIED is its own counted bucket and --apply never touches it: an unreadable repository, ref table, worktree registry or claims ledger, a tip git gc pruned, a spent derive budget, an underivable chain polarity, or a tip some ref OUTSIDE the lane family still reaches. The `helm lr list` header and the web board's filed strip stop printing a bare `open` count and render this split from the SAME classification the verb runs — in-flight on the live frontier first (unclassified rows disclosed inside it as work owed), the off-frontier residue named beside it as the count this verb will act on. `refs` audits every commit id recorded across the land-request + dispatch ledgers against the live repo — a recorded proof that cannot resolve can never be re-verified — and after a history rewrite `migrate --commit-map` (git filter-repo's own map file) translates those ids in place, dry-run by default. `foldcheck` is the five fold checks as ONE refusing rung over a tip — it exits 1 on REFUSE *or* UNKNOWN, because not-measured is not consent and a caller gating on rc needs the same stop for \"no\" and for \"I could not tell\"; the fifth rung (did it reach ORIGIN) is the one hand-folding skips when tired, which is how two lands were announced that origin did not have",
    "coach": "coach <lesson...> [--apply] [--as L] [--id ID] [--project P] [--supersede OLD] [--json] — the capture front door with the 4-step GATE (reframe->place->search-first->simplify); propose-only unless --apply (low-confidence -> drain intake, lossless)",
    "premise-check": "premise-check <id> [--chain] — verify digest + quote the finality tier; --chain walks the supersession chain (attested biography)",
    "seat": "seat add|up|down|launch [--model M] [--multi]|spawn|where|rehome <seat> --home H [--model M] [--apply]|resume <seat>|--all [--apply]|rebind <seat>|--all [--apply] [--install-timer]|reassign <seat-or-session> --to <seat> [--reason R] [--force] [--apply]|smoke [--multi]|autocompact|unblock|silent-drop|idle-dispatch|panes|composers [--json] [--submit HANDLE]|retire-deny <tool> [--seat S|--all] [--apply]|retitle [--apply] [--json]|adopt|cred-follow [--apply] [--json]|doctor [--ensure] [--json] [--quiet]|boot-brief|resume-turn|lifecycle [show|record]|list|status — multimodel seats (codex family via local proxy); spawn <seat> = harness-agnostic SELF-ONBOARDING spawn, where <seat> is <family>, <family>-N, or the PROJECT-CANONICAL <project>-<family> (acme-codex, acme-claude — EVERY FORM LISTED IS ONE SOME DOOR ADMITS: the project form takes claude or codex only, because those are the families whose spawn gate admits a non-family instance; <family>-N takes the OAuth-pool (mode=proxy) families only, since a numbered seat IS a per-instance proxy and the gate refuses a numbered proxy-key family; native claude has no bare or numbered form at all; the family is the last segment, the project must be registered, and the WORKSPACE must be that project's too — its own checkout, a registered linked worktree of it, or anything under its recorded cv scope — so a seat can never cut a home worktree and a branch in a repository its name does not claim. native claude rides `helm launch --seat` because it has no proxy and no port; a project-codex seat gets its OWN allocated proxy endpoint, distinct from the family base and from every numbered and project sibling. Both legs share one lifecycle: the per-seat lock, the stale reap, the --replace refusal, the surface-ownership proof, the home provision, the onboarding submit and the role posture. The project is recorded on the seat and round-trips into where and the reboot sweep, and into resume/up/down as an ANSWER rather than an act: a native seat has no launch.sh to replay and no proxy to start or stop, so each of those three says exactly that and names `seat spawn <seat> --replace`, instead of routing into a proxy leg that calls the seat's own registered family unknown; a second same-family seat for one project prints a notice naming the existing one, and an unregistered project, unknown family or wrong-project workspace is refused naming the registry and the admitted forms) (reaps a stale same-name seat; orca/herdr pane + onboarding injection, or detached HEADLESS with the onboarding as boot first-prompt when no metaharness; --print dry-runs the exact calls); where <seat> resolves the spawn register (harness/handle/pid/worktree/room/liveness); rehome <seat> --home H = MOVE A LIVE SEAT ONTO A CREDHOME IN ONE VERB, the hand procedure the integrator ran twice in a week made policy: it proves the home's token is live or syncable from Orca's live copy BEFORE touching the pane — a home whose refresh chain has EXPIRED, and that Orca therefore will not sync, is REFUSED naming the login remedy, because `helm launch` on such a home prints one line and starts the session on the stale token — then resolves the seat's CURRENT roster session (never a history sid) and its pane bound to a stamped live process, types /exit as ONE pane-input wake (never a signal: a killed claude loses the transcript flush the relaunch resumes), waits for that process to be PROVEN gone, types `helm launch --seat S --home H -- --model M --resume SID` into the same pane, and verifies the pane, the roster register and the inbox beacon as three separate answers before recording one ledger row. Dry-run without --apply and the dry run IS the plan, printing the exact launch line; launch/smoke --multi =mixed-model fleet (no subagent pin, per-agent frontmatter routes, conductor-log-verified fan-out); composers = the HELD-BUT-UNSENT composer oracle: its default scan is keystroke-free and answers held/helm-pending/helm-stranded/clear/cannot-tell without folding unknown into clear; explicit `--submit HANDLE` is a guarded actuator that sends only bare Enter for an exact recorded Helm injection proven persistent across independent observations, while unmatched text remains a possible human draft and is never submitted; retitle = THE TAB-TITLE half of the reboot repair, on demand: every pane helm can NAME is asserted to THE SEAT NAME exactly as the fleet board shows it and nothing else — the tab strip is meant to be matched against the board by eye, and anything appended breaks that match. Orca reverts these to an auto-generated summary on every reboot, and a seat RENAME leaves a title that is still a perfect seat-name checksum naming the wrong seat, so the re-stamp also rides `seat rebind` (through the same proof) and every rename. Dry-run by default and the dry run IS the table (current title beside the desired one); idempotent, so a pane already reading correctly is never re-written and the surface does not become noise; never fatal, so a rename orca refuses degrades to a row. A pane whose seat identity the join REFUSED, and any pane no roster seat claims, keeps its orca title UNTOUCHED — a wrong helm-written title is worse than an honest host-written one. The same re-stamp rides `resume --all` for free, because that sweep runs after the boot that clobbers the titles and already holds both a proof and the pane inventory. helm only ever WRITES a title: A TITLE IS AN OUTPUT, NEVER AN AUTHORITY, and no identity decision anywhere reads one; resume <seat> relaunches the pane via the detected metaharness (orca/herdr), freshest launch.sh + claude --resume/--continue; resume --all [--apply] = THE POST-REBOOT SWEEP and the timer payload: one row per registered seat classified through rebind's own proof — LIVE (re-stamped, never relaunched), DEAD-PANE (rebind refused with its two zero-count sentences, the pane KEY still resolves to a live bare shell: TERMINAL_DISARM then the launch line are typed INTO that pane, layout kept), PANE-GONE (orca answers terminal_not_found: a fresh pane), UNKNOWN (any other refusal, an unreadable register, a config dir with no transcript, a pane that is not writable — rendered with its reason and never folded into skip or dead; rc 1 under --apply). Only a register that PREDATES the current boot is acted on, so the every-interval timer never relaunches a seat that died mid-day; a <HELM_HOME>/_global/.state/fleet-hold marker renders every dead row HELD and acts on nothing; dry-run default; no new session is ever minted; autocompact = proxy-seat context watchdog (inject /compact at ~90% before the 100% hang); rebind = THE REBOOT VERB: pane handles die with the machine while seats are durable, so post-reboot `where` reads a live fleet GONE — rebind proves each registered seat's live pane (session -> pid -> /proc) and re-stamps the register (dry-run default; --install-timer wires the cadence); silent-drop = the empty-completion loud-fail rung (a proxy turn ending end_turn with NO text/tool_use/thinking yet output_tokens > 0 generated an answer the pane never saw — read-only, one loud a2a alert naming seat + lost tokens); reassign = THE ONE DOOR for a dead or renamed seat's holdings (the dead-seat holdings mandate): dispatch rows both directions, task rows and worktree leases move in one verb and one ledger event, source keyed by SESSION so a rename cannot orphan the move itself, target resolved through a single function so a ROLE can plug in later without touching four ledgers; dry-run default, refuses a measurably LIVE source without --force --reason and never refuses on absence; cred-follow = THE POOL FOLLOWS ORCA: the codex proxy pool carries whichever account orca has ACTIVE, read daemon-free from orca's provenance file (`<userData>/codex-runtime-home/shared-runtime-auth-provenance.json`) and its per-account auth.json, so an account the owner switches in orca reaches the proxy within one proxywatch pass or one `seat doctor --ensure` instead of by hand (the owner asked why the switch was manual, 2026-09-14). Dry-run by default and the dry run IS the table (PRESENT/IMPORTED/MISSING/DISABLED/COLLISION/INCOMPLETE/CHANGED/MISSING-AUTH/UNREADABLE/SKIPPED/UNKNOWN). It imports an account ONCE and never over one already pooled BY ACCOUNT ID, whatever its email or file name: measured 2026-09-14 03:47Z, forty seconds after an import the pool copy's refresh AND access tokens had both rotated away from orca's copy, so a re-import would replace a live credential with a stale one. A disabled member is reported, never flipped, and a pooled record already carrying disabled=true keeps it across every refresh (nothing in helm re-enables a parked cred); the destination is resolved BY ACCOUNT ID and a pool file name another account already holds is a loud COLLISION refusal naming both accounts, never an overwrite, while a credential naming no email or no plan is an INCOMPLETE refusal rather than a file called codex-unknown-unknown.json; no other pool member is read for deletion; orca's own files are never written; no token material reaches any surface; an absent or unparseable provenance file, an absent auth file, and an owner other than `managed` are each a ROW, never a refusal; idle-dispatch = the stranded-obligation rung (open dispatch rows crossed against recipient presence: quiet + NO live claim = STRANDED, re-check or reassign; quiet + HOLDING a claim = busy or WEDGED, rescue and NEVER reassign; unreadable claims = UNKNOWN, never 'no claim')",
    "mcpd": "mcpd serve [--port N]|token — the stateless MCP endpoint (spec rev 2026-07-28, stdlib-only): one localhost POST route serving server/discover | tools/list | tools/call over the SAME verb layer the CLI fronts. `token` mints a bearer for THE CALLING SEAT ONLY — there is no --seat flag, because a flag naming another seat would let any caller forge a peer and make MCP strictly WEAKER than the CLI it mirrors; the seat comes from the same env identity `helm chat post` already trusts, so this is exactly as strong and no stronger. Owner-ruled HYBRID (the 2026-08-05 decision cards): the CLI stays the universal floor and the diff oracle (test_mcpd pins the two front-ends equal on one input), beacon/wake stays Monitor+CLI (an MCP notification informs a client process, never wakes an idle model), and write verbs land in slice 1 behind per-seat bearer auth with server-side signing behind a flippable seam. Slice 0 ships read-only store_resolve; tool lists are deterministic and carry ttlMs/cacheScope for client prompt caches",
    "router": "router up|run|down|status|line|probes — the transparent multi-model router: claude-* forwarded VERBATIM to api.anthropic.com (the client's own OAuth, never an API key), non-claude models to their seat's CLIProxyAPI; line prints the claude-parent mixed-fleet launch line, probes mints the per-model example agents",
    "codex": "codex [list]|pool <name>|unpool <name>|pooled|capacity|resets [--dry-run] [--consume <account>] [--json]|sync-orca [--watch]|launch [-i N|--instance N] [--force] — codexhome roster (ultra/team) + proxy cred pooling: translate ~/.codex-homes/<name>/auth.json into the seat proxy's hot-reloaded auth-dir (0600), so the :8317 pool falls through usage caps; launch = cred-% gate (rollout rate_limits) ahead of the seat mint; resets = the earned rate-limit RESET CREDITS: bare lists what each pooled account holds beside its weekly percent and its natural reset, --dry-run prints what the automatic policy would do this instant and why (per account, spending nothing), --consume <account> is the explicit manual door and never picks an account for you. The same policy runs unattended in the proxywatch pass: a credit is spent only when a FRESH reading puts the account-wide WEEKLY window at zero remaining — 100% used, or at the wall's edge with a 429 helm itself already recorded for that credential naming that window, because the usage percentage lags the refusals (a 5h wall alone never qualifies) — AND the vendor calls that wall a RATE LIMIT rather than a depleted workspace credit balance, which a rate-limit reset does not lift, AND the reading binds to EXACTLY ONE pooled credential by the member identity (account id and chatgpt user id: a Team account id is the WORKSPACE, shared by its members, so matching on it alone redeems with a sibling's bearer), AND a credit is spendable on that same resolved credential, AND the natural reset is more than an hour away, AND no attempt for that credential sits inside the cool-down; every attempt is journaled under a digest of the member identity with its idempotency key, under a lock spanning re-read/cool-down/write-ahead so two overlapping passes cannot both spend for one wall, and an outcome-UNKNOWN call is re-driven under the SAME key rather than spending a second credit; sync-orca = one-way adapter pooling the codexhome matching orca's selected codex account (accounts.list over the daemon socket; no/ambiguous match refuses printing both rosters; --watch re-pools on selection change); every pool write admits identity through one rule — a pooled file holding a KNOWN account is refreshed by that account only, never replaced by a different one or by a credential naming NO account id (unknown-identity-cannot-replace-known)",
    "work": "work claim <lane> [--lease ID] [--ttl N[s|m|h|d]]|release [<lane>] --lease ID [--park]|release <lane> --stale|peek <committish> [--json]|peek --drop <path-or-committish>|gc [--apply]|list|stash [list|apply|pop|drop|show <message-substring>]|install-guard [--apply] — worktree lifecycle on the claims lane: private room <repo>-wt/<lane> per lane (lease = room key, git worktree lock = do-not-disturb), peek mints an unlocked disposable READ-ONLY detached worktree at exactly one commit under <repo>-wt/peeks/<sha12> (the reviewer door — no lease, no branch, and the ref guard admits it structurally with NO env override; --drop retires it, occupied/pane-bound/dirty refuse), gc rescue-commits dirty lease-less rooms to their branch (never discards), install-guard dry-runs/installs composed deterministic shared-checkout guards incl. the never-track staged-set scan at pre-commit and at pre-merge-commit (a merge commit never reaches pre-commit)",
    "ownership": "ownership census — re-derive, from BOTH ledgers at the moment of the call, who still holds open rows and whether that seat is working. A row is held by LIVENESS, not by a name: DARK means no TERMINAL RESPONSE inside the threshold AND no live descendant work, and both halves are load-bearing because a seat waiting 34 minutes on a remote gate has emitted no terminal response and is emphatically working. NO SOURCE CLAIMS THE HARNESS ACCEPTED A STOP, because none can: a plugin Stop hook outside helm's dispatcher vetoes after every helm handler allows, so the strongest honest evidence is the seat's last terminal response, read from its own session transcript and bound by session id. A record too young for the longest configured Stop-hook timeout is PENDING and reads UNKNOWN, because a veto may still be in flight. A BEACON IS NEVER SUFFICIENT ON ITS OWN and is reported as evidence only — one seat read COVERED while 11.2h silent and held 23 rows. Every read that cannot answer (unreadable roster, unreadable claims, a transcript scan too short to cover the threshold, a descendant probe that raises or answers None) returns UNKNOWN, which never reverts, because judging a live seat dead gives one piece of work two owners. Excluded categories print their own size: HELD dispatch rows are parked on a named dependency and are set aside, never reverted. THE THRESHOLD IS AN UNCALIBRATED PLACEHOLDER: the bimodal distribution it was first derived from measured beacon polling rather than seat activity and is WITHDRAWN, so the tripwire reports which clock it is actually reading and refuses to certify the line from a corroboration-only source. READ-ONLY: this verb reverts nothing.",
    "search": "search <text> [--scope P] [--refs] — content search inside transcripts",
    "transcript": "transcript <sid> [--find T] [--limit N] — windowed role-tagged transcript read",
    "rehome": "rehome <sid> <new-cwd>|--reset — re-home a session (claude slug symlink)",
    "prune": "prune <sid> [--preset lean|window20k] [--dry] — resume-optimized copy, original untouched",
    "keepalive": "keepalive [--home H] [--early N] [--apply] [--ensure-timer] — dry-run by default; roll idle claude homes' tokens after a stable pre-image (codex read-only). --ensure-timer installs/verifies the HOURLY systemd user timer (identity HELM_CHAT_NAME=keepalive-cron, session vars unset) and reports a hand-installed crontab line as superseded without touching it; a verb that only runs when somebody types it is how an expired helm copy of a healthy account reads reauth-needed, and `helm doctor` now reports the timer's absence and its last recorded grant",
    "burn": "burn [--json] | burn why <family> [--json] | burn burst [--json] | burn runway [--json] [--window <hours>] | burn declare <family> <colour> --until <iso> [reason...] — THE FIRE-DANGER READING per model family, folded once by the watchdog pass and read by everybody else. GREEN open more lanes for work that is already built up; never speculative. YELLOW normal work, no extra lanes. ORANGE critical path only, one delegate at a time, prefer another family. RED start nothing new on this family; finish or park what is running. GREY is NOT MEASURED, behaves as yellow and never renders green. Every flag carries WHEN IT CHANGES and why, from measured reset instants rather than a guess, and names which of the four axes set it — money (wait for a reset), reach (bounce a proxy), policy (change the model mix) or declared (what the owner said). An unread account NEVER worsens a colour: it caps it, makes GREEN unreachable and names the repair. `declare` records an owner colour that may only WORSEN a measured reading, expires at the instant he states, and renders as DECLARED wherever it is shown. Exit 3 when the snapshot is absent or stale — the reader never probes, so a stale file yields nothing rather than an old colour. `burn burst` answers the OTHER question one colour was being asked: may THIS seat exceed the one-delegate rule on the credential its own home holds, measured from that credential's own live row rather than from the pool — granted only when the tightest account-wide window will expire headroom UNSPENT at its own average pace, named to the credential and expiring at the sooner of that window's reset and the reading going stale, lifting the COUNT and never the reach rule or the project's light (exit 0 granted, 1 no exemption — a refusal is an answer). `burn runway` reads the RATE a level cannot see: percent per hour per codex ACCOUNT on its longest window (UNKNOWN, never 0, under 3 readings or 1h), and the fleet's runway — supply in tokens over the ledger's codex tokens per hour — against the HORIZON, the next Pro reset; a short runway steps the codex money axis UP one colour and a long one with an account that will strand 20% of its week steps it DOWN, through the fold's own step table and never to RED; the bare `helm burn` carries it as one line",
    "relevance": "relevance [status|report [--hours N] [--json]|show --session S|warm [--project P]|remeasure ...|serve ...|score-turn] — the long-tail RE-RANK: one probability per (turn, candidate store line). The hook starts ONE detached worker per turn; an outside evaluator scores only turns whose project's AUTHORED residency is may-leave-lan (no field = lan-only), the local head behind `helm relevance serve` scores the rest and is the evaluator's fallback. The hook waits at most 1 s (live mode) and the score still lands in the per-turn cache that a later read takes without calling a model (`helm relevance show` today); a fallen-back live turn carries one word and every fallback is counted per tier. Mode off|shadow|live in <helm home>/_global/relevance.json (absent = off); the scorer URL is the `relevance` key of endpoints.json; local-only BY CHOICE waits for a passing `remeasure` receipt",
    "route": "route <kind> [--from F] [--project P] [--row R] [--explain] [--json] (kind: review|build|verify|delegate|research|council) — WHO SHOULD TAKE THIS WORK RIGHT NOW, answered from the burn flags and the owner's own stored rulings instead of a guess or a measurement the asking agent pays for. Six nodes: the kind chooses candidate families, the project's own bench narrows them, the flag drops a RED family and rations an ORANGE one while a GREY one is admitted AND SAYS SO, the usability join (or `helm reviewers` itself, with --row) answers who is live, the cap says how many delegates may start, and the rank puts judgment-bound work up the SMARTS axis and volume-bound work up the SPEED axis. Every line carries the edge, the store id and the owner's sentence resolved from the store at render time — no routing sentence is written in Python, so editing the store changes the answer; an id that does not resolve prints UNRESOLVED and makes the reply PARTIAL rather than silently dropping a rule. NEVER spawns, probes or blocks. Exit 0 an answer, 1 measured-nobody, 3 PARTIAL, 2 usage — and `helm route up|run|down|status|line|probes` refuses at exit 2 naming `helm router`, the HTTP relay",
    "creds": "creds [crosscheck [--json]] — live account scorecard (headroom/state/reset), all providers; codex rows read the SEAT PROXY POOL (its auth-dir holds the live cred; a codex CLI home is refreshed only while codex runs in it, and all seven on this host were 39-77 days stale), print EVERY window on a continuation line (5h and 7d), and bind headroom/reset to the FULLEST account-wide window — pacing on the 5h one read 48% headroom while every pooled account was weekly-capped (task/2480, closes task/2283 for codex); a non-pooled account falls back to its home and says so; crosscheck sums local session JSONL into the same 5h/7d windows and cross-checks the header truth (drift = health signal)",
    "cred": "cred [list|ls|backup [--all] [--apply]|switch-guard|guard [--install] [--apply]|heal [--apply]|sync-orca --home H [--apply] [--replace-own-chain]] — the SAFE /login: content identity plus transactional 0600 snapshots/restore; list shows each credhome's token FRESHNESS against Orca's copy of the same account (FRESH/STALE-vs-ORCA/OWN-CHAIN/CHAIN-UNPROVEN/NO-ORCA-COPY/UNKNOWN/DISAGREE) and sync-orca copies Orca -> home only when Orca is strictly fresher, the home's own chain is spent (or --replace-own-chain says it is Orca's), and Orca's family is live in no other home; a CHAIN-UNPROVEN home refuses launch; switch-guard --install retires old per-turn credential hooks (no scheduler installed); every mutation is dry-run without --apply and live-session uncertainty refuses (ls/guard = the short aliases the code accepts)",
    "swap": "swap <home|email> — seat ran dry: print resume-under-healthier-account blocks",
    "attribute": "attribute [--by project|model|cred|seat] [--since Nd|Nh] [--limit N] [--project P] [--json] — token-effort rollup over the session catalog (output + cache-creation, never raw input) joined with the sidecar meter (per proxy-family seat, model and pooled account: output AND input, because a retained reader re-sends its context every call); UNATTRIBUTED always visible, an unread sidecar is a row that says why, and a seat whose last pass lost records is PARTIAL with the rollup marked incomplete",
    "proxy-usage": "proxy-usage [--json] — pop every proxy-family sidecar's usage queue (the fork's management route, loopback, the seat's own management secret) into the proxy-usage ledger and print one status line per seat: READ with the record count, UNREADABLE with the reason (no secret minted, port closed, routes not enabled, key refused), or FAILED-PERSIST with how many popped records the ledger refused (the pop is destructive, so a refused append is its own state and a non-zero exit); a record's source is written as the meter's contract says (docs/VERBS.md, helm proxy-usage: the pooled account email under a per-record join, otherwise a cred:<sha256 8> label, with one stated read-time bound), and a pool fault or an unjoined record prints a pool: trailer under the seat line and rides the read event; proxywatch --post runs the same pop every fifteen minutes",
    "who": "who [--json] — pid->cred attribution table: every live claude/codex process, its cred home, account, cwd, and session (shared sessions flagged)",
    "capsule": "capsule <sid> — the session's git era: worktree + resume commands",
    "ship": "ship [--apply] [--remote URL] | ship pull | ship hosts — the authored chain over git (dry-run default; pull merges + re-derives)",
    "corpus": "corpus backup [--dry] [--dest DIR] | status — training-corpus transcript backup: copy-only incremental archive of every CLAUDE + CODEX + /tmp-estate session/subagent/workflow transcript (opencode and pi transcripts are NOT collected; default ~/corpus-archive; HELM_CORPUS_DEST)",
    "owed": "owed [--seat S] [--rows] [--json] — the lanes sitting CURED-BUT-UNREVIEWED, oldest first. A FIX verdict tells an author to cure; NOTHING tells them that curing creates a NEW obligation (a review dispatch on the cured tip), and no surface showed a lane in that state — so authors cured, re-gated because re-gating is the action the tooling makes obvious, and the lanes sat. Derived from the dispatch ledger alone: a FIX verdict that no LIVE row supersedes, where live means a live row anywhere BELOW it, not merely a live direct child — a cancelled child is what `rebind` leaves behind and its chain continues one level down, so asking about direct children alone bills 112 rows of work somebody already did. Counted in LANES rather than rows, because a lane whose chain was orphaned appears once per orphaned root and a burn-down that triple-counts its worst-maintained chains is not believed twice; --rows shows every row. Also reports FORKED chains — one row with two live successors, where one branch can never be discharged by anyone — and reports them even when nothing is owed, because a fork is invisible until somebody asks why a lane never closes. An unreadable ledger EXITS 1 and says UNKNOWN: an empty burn-down and an unreadable one look identical on a screen, and this screen exists to be believed",
    "owed-push": "owed-push [--dry-run] [--quiet] [--json] [--ensure-timer] — DELIVER what `owed` computes, as one DM per owing seat. `owed` is PULL: a verb somebody must type, beside a web burn-down somebody must open, so the debt it names reaches nobody who is not already looking — measured on the live estate, 130 debts at a median age of 12.7 DAYS, and one cure committed 15 minutes after its FIX verdict whose row then sat 22 hours. This is the push, and ONLY the push: every item, addressee and word of remedy text comes from `obligation`, and the DM carries that module's own sentence naming --supersedes (the flag whose absence built 124 unclosable land loops). Deduped by CHAIN ROOT, never the lane label, so one badly-maintained chain bills its author once instead of seven times; capped per digest with the remainder COUNTED; rows nobody owns ride one integrator digest. Latched per row on (kind, reviewed tip, owed-since) so a re-tip or a newly-declared polarity is NEWS while the same debt is not re-raised for three days, and re-armed when a row leaves the owed set. An UNDELIVERED digest never latches — its debts re-deliver next sweep. It NEVER BLOCKS: the whole surface is a chat post, because a false notification costs one line while a false block costs a seat its turn. An unreadable ledger EXITS 1 and delivers nothing rather than publishing a clean burn-down from the one moment it cannot see",
    "handoff": "handoff check [--hook-json]|write|recover <sid> — the compaction-continuity contract (PreCompact/SessionEnd nag; typed journal handoff; cv pre-compaction recovery)", "now": "now capture [--hook-json]|show — automatic session-continuity snapshot (_global/now.md, 40 lines newest-first, 48h freshness gate; SessionStart context)",
    "cmd": "cmd <sid> [--account A] [--model M] — account-aware pasteable resume command",
    "web": "web [--port N] — the same, warm, in a browser",
    "board": "board show|landed <name> <sha> <note>|set <key> <value...> [--new]|note <key> <text...> — the integration board (the owner console's lane truth) through its LOCKED, idempotent write path; a hand-rolled json.dump has already lost one update and can tear the file. set updates an EXISTING top-level scalar (--new declares the reader was coordinated with); note prepends one line to an EXISTING list-of-strings log",
    "note": "note set <key> <headline...> [--detail <body...>] [--goto <ledger|chat|roster|board|url>]|list [--json]|retire <key>|restore <key>|rm <key> — FLEET NOTES (retire stops a note competing for attention but KEEPS it, restore undoes that; rm DELETES — retire is the undoable one): one owner-facing headline, collapsed detail, and an explicit place to act. One note per key, durable and last-writer-wins; headlines over 90 characters warn, while 64 keys and 2000 total characters are hard caps. Board landings derive the current landed headline automatically. HELM_FLEET_NOTES overrides the path",
    "wiring": "wiring [--verbose] [--json] [--gate] — built/reachable/actuated/exercised/verified: modules no entry point can reach plus declared actions with NO installed schedule or hook. --gate remains the narrower this-tree-added reachability rung used by Stop",
    "punt": "punt [--text T | --transcript P] [--json] — the DRESSED declination detector: a first-person declined action plus an excuse from a named punt class (owner-presence, assumed-disruption, unproven-as-excuse, size-or-cost, someone-elses-lane) in ONE sentence. The Stop hook refuses an idle stop on a hit when the owner ask ledger has nothing open — decline loudly (helm asks add) or do the work",
    "nouncensus": "nouncensus [--noun N] [--ambiguous] [--tier kernel|candidate] [--json] — the per-SITE uses-closure census for the seat-state nouns (actor/seat/session/pane/lane/lease/routing/obligation/verb), derived from call sites and never hand-counted: who READS and who WRITES each noun, which sites span more than one (the 'assigned exactly once' violations), and which attributions rest on evidence the subject could have forged (argv/environ) versus kernel proof (/proc/<pid>/exe)",
    "clarity": "clarity check [<file>|-] [--strict|--owner] [--project P] [--no-store] [--json] | rules [--json] | skill [--project P] — the ASD-STE100-descended clarity die over coordination text: ONE rule table (helm/clarity/rules.py) read by both consumers (the write-time skill and the deterministic linter, exit 1 on violation). Eleven deterministic rules: four STE-derived, three from the writing skill, two owner-mode adapter rules from the helmese register (gloss a register symbol once per owner-bound artifact, then use it bare; open on the outcome, not the mechanism), and two that are helm's own: domain-term drift against the store's curated lexicon, and MEASURED/TRACED/INFERRED provenance on load-bearing claims. one-instruction is APPROXIMATE and reports advisory only; hedge-term is the measured-weakest rule and its findings carry [weak]. --owner is for text the OWNER reads (the four owner-bound verbs run it as an advisory automatically; silence with HELM_CLARITY_ADVISE_OFF). No POS tagger exists here (stdlib-only), so the STE dictionary/noun-cluster/verb-form rules and gloss-once on the ASCII operators are honestly absent, not faked",
    "pi": "pi extension [--seat S] [--apply] | run [SEAT] [--model helm-SEAT/MODEL] [PI_ARGS...] | launch [--model M] [--session ID] [--print] | resume --session ID|--continue | status — the pi harness seam: GENERATE a provider for one seat's CLIProxyAPI; run pins that provider and execs with the key only in child env; launch/resume print key-free commands; status reports pi binary + sessions. Zero TypeScript in the repo; the key is never a literal",
    "eval": "eval arms|register|seed|run — the cc-codex vs pi-codex eval's guard rails AND its pilot-shaped runner. `arms` asks whether the two arms are COMPARABLE (same endpoint, same model); without that pin a harness comparison measures the PROVIDER and the numbers still look like an answer. `register` writes the §C.5 decision rule BEFORE any run, stamped with the arms fingerprint so a later run against different arms cannot inherit it. `seed` premise-checks a task atom against current code — a refuted premise is recorded stale-and-skipped with the refuting file:line, never seeded. `run` drives the seat's OWN launch.sh (-p) under a per-run hook-stripped config copy in a per-run dir, refusing any elapsed<=0 row — the 2026-07-29 pilot's five defects, each owned in helm/evalrun.py",
    "rogue": "rogue [--dry-run] [--json] [--kill|--no-kill] [--quiet] [--min-age S] [--min-cpu PCT] [--grace S] — the LOCAL rogue-compute watchdog (task/1039): flag heavy-compute processes (python unittest/pytest, node vitest/jest/webpack/next/open-next/opennext build, pnpm/npm/npx install+run, cargo build/test, go test/build) running on THIS box outside the fab, attribute each to a seat by walking HELM_CHAT_NAME up /proc parent chains (UNKNOWN when absent, never guessed), capture evidence, alert #helm loudly, and KILL after a grace window under the owner's standing kill authority (default on: he wants this stopped, not reported; HELM_ROGUE_KILL=0 for alert-only). NETWORK-BOUND: `wrangler` and `opennextjs-cloudflare`, direct or via npx or pnpm/npm exec/dlx, are alerted once and NEVER signalled. IDENTITY-FIRST: the exe decides what a process is, so a local fab CLIENT whose argv contains a suite spelling verbatim is structurally unflaggable, and anything under a fab wrapper or the human escape env (FAB_ALLOW_LOCAL_SUITE/BUILD=1) is exempt. Rides the silent-drop 90s cadence — no new daemon",
    "proxywatch": "proxywatch [--post] [--json] [--force] [--install-timer]|vendor-reset set <family> <ISO-8601|epoch-ms>|vendor-reset show|vendor-reset clear <family> — fifteen-minute composite over every minted seat and represented family: local CONFIG/DROPS/HANGS/PROBE/LOG plus authenticated UPSTREAM canaries (each request capped at eight tokens; dark primaries confirmed then corroborated by healthy siblings, whose completed failures may also confirm once after 3s; client timeouts never retried), followed by the existing fused TURN verdict. Each pass also runs the cred-follow rung (`helm seat cred-follow`) so the codex pool follows whichever account orca has active — the pass's one write, into helm's own pool only, and it never moves the exit code — and the sidecar-meter pop (`helm proxy-usage`), which keeps the proxy-usage ledger continuous; a FAILED-PERSIST row (popped records the ledger refused) posts through the chat outbox and exits 1, UNREADABLE rows never move the exit code. Stable family since values and dark/recovered edges join the change-latched chat report. A dark family also drives delivery's PAUSED-CRED-WALL latch: addressed rows survive cursor/rotation/Stop paths, UNKNOWN holds the known wall, a last-good snapshot survives observer-file damage, verified per-session runtime family drives custom labels and canaries, and measured HEALTHY resumes existing waiters automatically. --force never invents an edge",
    "proxy-fork-watch": "proxy-fork-watch [--json] [--post] [--force] [--install-timer] — run the durable CLIProxyAPI fork's upstream checker; read-only by default, one #helm line on first run or semantic change, daily user timer available",
    "upstream-watch": "upstream-watch [--dry-run] [--bundle] [--json] [--install-timer] — the daily upstream-change watcher: find a Claude Code release newer than the last one read, diff the two programs schema-aware (settings keys and describes, CLI flags, env names, hook events, SDK protocol, model catalog, bundled skills) plus the CHANGELOG entries and any changed tier-2 vendor page, have one headless `claude -p` run (Max OAuth, never an API key; Read/Grep/Glob only) propose helm tweaks citing verbatim evidence and one more run per proposal try to refute it, then file each survivor as a task owned by the integrator and post one #helm digest, silent when nothing survived. The state advances only after a pass succeeds. --dry-run runs every stage and files, posts and writes nothing; --bundle prints the evidence only; --install-timer installs the daily systemd user timer. HELM_UPSTREAM_WATCH=0 is the off switch",
    "env": "env census [--json] — READ-ONLY estate picture: every claude config dir's hooks + MCPs, the variance vs canonical, and the orphan-worktree snapshot (replaces poking the configs UI by hand)",
    "mcp": "mcp sync [--apply] — reconcile canonical MCP servers into every home (dry-run default; additive + fail-closed; backup-first, superset-refusal)",
    "worktree": "worktree gc [--apply] — prune orphan worktree-*/lane/* branches + landed worktrees (dry-run default; rescue-dirty-first, locked/occupied-immune, unmerged-blocked; composes `helm work gc` for lane rooms)",
    "tidy": "tidy [--apply] [--repo PATH] — the umbrella: census + hooks sync + mcp sync + worktree gc, all dry-run; one consolidated report (--apply runs them all backup-first; --repo points the census + worktree-gc legs at another repo root)",
    "beacons": "beacons [--seat S] [--json] [--post] [--install-timer] — the inbox-beacon registry census. A beacon is HELM'S ONLY wake path to a seat, so the alarm is TWO-DIRECTIONAL: a DEAF SEAT (no live beacon — helm cannot reach it; a wake leg outside helm is not something this census can see) and a GHOST WAITER (a beacon whose session is dead — it eats the seat's rows into a pipe nobody reads AND makes a dark seat read as covered). Liveness is a LIVE SESSION behind the shape, not an argv match. The bare read signals nothing, because a superseded beacon is stopped by its own seat's next re-arm. --post (the helm-beacons.timer entry) writes each verdict onto its roster row — the attendance register: state/since/seen/covered — and delivers reachability edges on BOTH channels change-latched per channel — #helm, and the OWNER'S PHONE (HELM_NTFY_TOPIC, the shared notify.owner_push), because when the fleet is unreachable every reader of the fleet room is one of the unreachable seats. --post ALSO ACTUATES: a seat whose wake path is LIVE and whose turns are not consuming its rows is DEAF-IN-EFFECT, and re-arming the beacon fixes nothing there, so the repair TYPES INTO THAT SEAT'S PANE (bounded per spell and per hour, refused while delivery to the seat is paused). WITHOUT --post nothing is written and nothing is typed; --json is a RENDERING choice and not a dry run, so `--json --post` attends, escalates and can type exactly as the text form does. Exit 0 clean, 1 faults FOUND, 2 the watchdog itself failed. --install-timer wires the cadence",
    "rearm": "rearm [--apply] [--json] — land-to-live: report (dry-run default) which long-lived processes still hold pre-HEAD code (helm chat wait waiters, the web unit, advisory proxies/daemons); --apply announces (ambient), SIGTERMs ONLY the stale waiters so each owner re-arms on new code at its own turn boundary (the OWNED beacon-cycle), and restarts a stale web unit — proxies/daemons/seats never signaled",
    "derive": "derive [--repo PATH] [--trunk REF] [--families] [--json] — the state the ARTIFACTS support, beside the one the ledger remembers. A land-request row reads AWAITING_BUILD because somebody ran `dispatch mark-delivered` and nobody ran a closing verb since; git is consulted only for rows a verb ALREADY closed, so an open row is structurally blind to its own work reaching trunk. This verb asks the artifacts instead — lane branches, commits, trees, patch-ids, gate receipts and verdict polarity — and prints every row whose derived state DISAGREES with the ledger, because agreement is not news. States: UNSTARTED / BUILDING / BUILT / GATED / APPROVED / REVIEWING / REVIEWED / LANDED / SUPERSEDED / CANCELLED / UNKNOWN, each with the evidence that produced it. The ledger's own recorded terminals outrank inferred progress: a CANCELLED row is nobody's obligation whatever its branch did, a fix/supersede/concur/undeclared verdict ends its row at REVIEWED (it authorized no landing), an open review is REVIEWING rather than BUILDING, and a structured close carries its own proof (landed -> LANDED, delivered-report -> BUILT, discharged -> REVIEWED). Landing is proved by ancestry, by patch-id content identity (a rebase lands the work under a different sha, and ancestry truthfully answers no), or by a trunk commit naming the ROW id — never by a lane NAME, which is a substring that matches every sibling round of a chain. --families pays for cross-family approval evidence (seconds per proxied seat, resolved from the model and never the seat label) and is off by default, so without it an approvable row honestly reads GATED. An absent artifact yields UNKNOWN and never a positive claim: a reaped lane branch and one that never existed are the same absence",
    "ready": "ready [--json] — the five-signal fleet-readiness gauge: may forward work resume after a crash/reboot? Reads five existing authorities (metaharness daemon; registered seats' panes; inbox beacons; proxywatch's LATEST recorded family sweep, never a fresh canary; shared checkout clean at origin/main) and renders red/green per signal plus the composed verdict. ADVISORY: it never gates a dispatch — the instrument for 0.3's reboot-and-resume gate. A signal whose instrument cannot answer reads UNKNOWN, never green; a family dark with a named cause is WALLED and stays ready-with-note. Exit 0 READY, 1 NOT READY, 2 cannot prove",
}


def _tree_of(path):
    """The git root containing `path`, or None. Walks up looking for `.git`
    (a DIR in a clone, a FILE in a worktree — both count). No subprocess: this
    sits in front of every invocation, so it must cost a few stats."""
    try:
        cur = os.path.realpath(path)
    except OSError:
        return None
    while True:
        if os.path.exists(os.path.join(cur, ".git")):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            return None
        cur = parent


def _tree_state(root):
    """(short_sha, helm_pkg_is_dirty) for `root` in ONE subprocess, or ('?', True)
    when we could not tell.

    `status --porcelain=v2 --branch -- helm` answers BOTH questions at once: it
    prints `# branch.oid <sha>` in the header and any changed helm/ entry in the
    body. Asking git one question instead of two is not micro-optimisation here —
    the honest first version cost FOUR subprocesses on the same-commit path (two
    rev-parse, two status), MEASURED at 15.1ms against 0.03ms for the in-tree
    path. That path is the FRESHLY-REBASED SEAT, i.e. arguably the commonest
    cross-tree case of all, paying 15ms on every helm command to be told nothing
    (a live run caught the cost; the design intent had been zero git calls in
    the common case). Two calls, one per tree, is the floor: each tree's state has
    to be read from that tree.

    The pathspec scoping is git's, not ours — a dirty file outside helm/ never
    appears in the body, verified rather than assumed.

    And the dirtiness half is load-bearing, not thoroughness: matching HEADs do
    NOT prove matching code, because a worktree at the same commit can carry
    UNCOMMITTED edits — the single likeliest shape of the bug this whole surface
    exists to catch. Scoped to `helm/` because that is what an invocation
    actually imports; a docs or tests edit cannot change which behaviour ran."""
    import subprocess
    try:
        out = subprocess.run(
            ["git", "-C", root, "status", "--porcelain=v2", "--branch",
             "--", "helm"],
            capture_output=True, text=True, timeout=5)
        if out.returncode != 0:
            return "?", True
    except Exception:
        return "?", True
    sha, dirty = "?", False
    for line in (out.stdout or "").splitlines():
        if line.startswith("# branch.oid "):
            oid = line.split(" ", 2)[2].strip()
            sha = oid[:7] if oid and oid != "(initial)" else "?"
        elif line and not line.startswith("# "):
            dirty = True          # any non-header record is a helm/ change
    return sha, dirty


def _is_helm_checkout(root):
    """Is `root` specifically a helm SOURCE checkout?

    `selfrepo.is_helm_source_tree` is the predicate — the package a helm
    invocation imports, `helm/__init__.py`, at the tree's root — and it is
    shared with the guard profile default so the two surfaces cannot drift into
    different answers about the same repo. It is the package ROOT alone:
    requiring a second module beside it silenced this warning in a helm tree
    whose module was mid-move, the tree whose own import would fail.

    THE `bin/helm` DISJUNCT WAS THE BUG (task/2442). An entry script LAUNCHES
    helm; it is not helm's source. A project repo that ships a one-line
    `bin/helm` wrapper — the natural thing for an adopter to do — therefore read
    as a helm checkout, and every command run from it printed a maker-only
    warning about which helm tree got exercised. A team USING helm must never
    need to know how helm is made. Standing in an adopter's own repository is
    normal usage and must stay silent.

    A PACKAGE ROOT MISSING FROM A TREE THAT STILL CARRIES THE PACKAGE STILL
    SPEAKS, which is why this asks `is_helm_tree_a_maker_edits` and not the
    narrow predicate. Deleting or moving `helm/__init__.py` in a real helm
    worktree — the modules under it still there — made the narrow predicate
    answer False and this warning went SILENT in the one tree whose own
    `import helm` cannot succeed, so the invocation that did work necessarily
    ran another tree's code and nothing said so. The guard's profile default
    keeps the narrow reading, because its worse failure is the opposite one:
    arming helm's shared-checkout rail in an adopter's repo.

    AN UNANSWERABLE PREDICATE SPEAKS. When the filesystem can neither confirm
    nor rule out the package root, this surface is the caller that fails toward
    the warning: it is advisory, it changes no exit code, and the failure it
    exists to prevent — a dogfood that PASSES against another tree's code — is
    worse than one noisy line. The guard profile default, sharing the same
    module, fails the other way for its own reason."""
    from . import selfrepo
    try:
        return selfrepo.is_helm_tree_a_maker_edits(root)
    except OSError:
        return True


def which_helm_warning(cwd=None, package_dir=None):
    """The one-line honesty surface, or None when there is nothing to say.

    Seat homes (slice 0) isolate each agent's TREE, but `helm` on PATH still
    resolves to whichever checkout installed it — so an agent can dogfood its
    own change and actually exercise a DIFFERENT tree. Two silent failure
    directions: the change looks broken when it is fine, or — the dangerous
    one — the dogfood PASSES because the other tree already has an equivalent
    fix, and a broken version ships behind a green demo. That is a check
    passing because its input was not what you thought, the fourth instance of
    that class in one day.

    DETECT AND REPORT, never refuse: stderr only, no exit-code change, and it
    must fire on a PASSING command too, because passing-for-the-wrong-reason
    is precisely the case it exists for.

    Cost: a few stats. If the cwd is inside the binary's own tree — main, or a
    seat correctly using ./bin/helm, the overwhelmingly common case — this
    returns before touching git at all. That case has one question left, and
    its sibling asks it: whether the tree itself is behind trunk
    (`stale_tree_warning`, printed by the same door).
    """
    package_dir = package_dir or os.path.dirname(os.path.abspath(__file__))
    try:
        cwd = os.path.realpath(cwd or os.getcwd())
    except OSError:
        return None                      # a deleted cwd is not our problem here
    bin_tree = _tree_of(package_dir)
    if not bin_tree:
        return None                      # installed outside a checkout: nothing to compare
    if cwd == bin_tree or cwd.startswith(bin_tree + os.sep):
        return None                      # the common case, zero git calls
    cwd_tree = _tree_of(cwd)
    if not cwd_tree or cwd_tree == bin_tree:
        return None                      # not in a checkout, or the same one
    if not _is_helm_checkout(cwd_tree):
        return None                      # standing in a non-helm git repository (e.g. an adopter repo)
    # DIFFERENT PATHS ARE NOT DIFFERENT CODE. The first version compared only
    # the tree paths and fetched the HEADs for the message AFTER it had already
    # decided to speak, so it fired from every worktree on every invocation —
    # including a worktree sitting at the SAME COMMIT as the binary, where there
    # is nothing whatsoever to warn about. That is the noise case this was
    # explicitly designed against: a warning on every command is a warning
    # agents learn to skip, and then it is on screen during the one invocation
    # that mattered and nobody reads it. Three of us missed it (author, reviewer
    # and me landing it) because we all exercised the DIFFER case, where it
    # correctly fires, and none of us exercised the SAME case.
    bin_head, bin_dirty = _tree_state(bin_tree)
    cwd_head, cwd_dirty = _tree_state(cwd_tree)
    if bin_head == cwd_head and "?" not in (bin_head, cwd_head) \
            and not cwd_dirty and not bin_dirty:
        # Same commit AND both helm/ packages match their commit -> the code that
        # ran really is the code you are standing in. Silent.
        #
        # The uncommitted check is NOT belt-and-braces, it closes a false NEGATIVE
        # my first fix introduced: matching HEADs do not prove matching code, and
        # "edited helm/ in my lane, ran plain helm" is the MOST likely shape of the
        # very bug this surface exists to catch. Silencing that would have made the
        # guard worst-of-both — noisy where it did not matter, quiet where it did.
        # (helm's own law: when a guard false-alarms, bind the TRUE provenance;
        # never just relax the predicate until it stops complaining.)
        return None
    if "?" in (bin_head, cwd_head):
        # We could not read a HEAD, so we do NOT know that they differ, and must
        # not assert it. Still speak — this surface is advisory and missing a real
        # mismatch is the worse failure — but say what was actually established.
        return ("[helm] you ran %s from %s and could not compare their commits "
                "— this command exercised the FIRST tree, not the one you are "
                "standing in. Use ./bin/helm to exercise this tree."
                % (bin_tree, cwd_tree))
    return ("[helm] you ran %s@%s from %s@%s — this command exercised the "
            "FIRST tree, not the one you are standing in. A dogfood here "
            "tests that binary's code, including when it PASSES. Use "
            "./bin/helm to exercise this tree."
            % (bin_tree, bin_head, cwd_tree, cwd_head))


#: The local ref a tree is measured against. LOCAL, never fetched: the line
#: below is advisory, and a network call in front of every command is not.
TRUNK_REF = "refs/remotes/origin/main"

#: Trees this process has already told about. At most one line per process.
_STALE_TREE_SAID = []


def _trunk_distance(root):
    """(ahead, behind) of `root`'s HEAD against `TRUNK_REF`, or None when
    either cannot be read (no such ref, not a repository, git missing). ONE
    git call, `rev-list --left-right --count`, which answers both halves.

    THROUGH THE VCS SEAM, with the repository-selecting variables scrubbed
    the way `selfrepo` scrubs them: a helm run from inside a git hook
    inherits GIT_DIR, and this question is about the binary's own tree, not
    whichever repository the ambient environment names."""
    from . import selfrepo, vcs
    try:
        rc, out, _err = vcs.backend(root).text(
            root, "rev-list", "--left-right", "--count",
            "HEAD..." + TRUNK_REF, timeout=5, env=selfrepo._git_env())
    except Exception:
        return None
    parts = out.split()
    if rc != 0 or len(parts) != 2 or not all(p.isdigit() for p in parts):
        return None
    return int(parts[0]), int(parts[1])


def stale_tree_warning(cwd=None, package_dir=None):
    """The one-line stale-tree surface, or None. The sibling of
    `which_helm_warning`, for the case that function is silent on: the cwd is
    INSIDE the binary's own tree, and that tree is behind origin/main.

    WHY A TREE BEHIND TRUNK IS WORTH A LINE. The dispatch ledger is shared by
    every seat, so a helm older than the ledger reads events it has no arm
    for. Its fold does not advance a row's seq past them, so every seq it
    WRITES on such a row reuses the unseen event's, and every current reader
    drops the write — while this binary's own check, folding with the same
    old vocabulary, reports success. `dispatches.KNOWN_EVENT_KINDS` makes a
    binary built from here on refuse such a row; a binary older than that has
    no such check to run, so a tree left behind is reached only by this line.

    A SEPARATE FUNCTION, NOT A NEW RETURN OF `which_helm_warning`. The gate
    reads that function's truthiness as "this helm came from another tree"
    and refuses to mint a receipt on it (`gate._cross_tree_refusal`); a lane
    behind trunk is ordinary and must still gate its own tip.

    CHEAP AND QUIET BY CONSTRUCTION: no git call unless the cwd is inside the
    binary's tree; then ONE `rev-list` against the local `TRUNK_REF`, never a
    fetch; silent at or ahead of it, silent when the ref cannot be read, and
    at most once per process. Hooks set HELM_NO_TREE_WARNING, which `_main`
    honours for this line exactly as for its sibling, so no hook pays it."""
    if _STALE_TREE_SAID:
        return None
    package_dir = package_dir or os.path.dirname(os.path.abspath(__file__))
    try:
        cwd = os.path.realpath(cwd or os.getcwd())
    except OSError:
        return None
    bin_tree = _tree_of(package_dir)
    if not bin_tree or not (cwd == bin_tree
                            or cwd.startswith(bin_tree + os.sep)):
        return None                      # not standing in this helm's tree
    distance = _trunk_distance(bin_tree)
    if not distance or not distance[1]:
        return None                      # unreadable, or at/ahead of trunk
    ahead, behind = distance
    _STALE_TREE_SAID.append(bin_tree)
    cure = ("fast-forward it: git -C %s merge --ff-only origin/main" % bin_tree
            if not ahead else
            "it carries %d commit%s of its own, so rebase it onto origin/main"
            % (ahead, "" if ahead == 1 else "s"))
    return ("[helm] this helm tree %s is %d commit%s behind origin/main — an "
            "older helm misreads ledger events it does not know, and a seq it "
            "writes on such a row is dropped by every current reader; %s"
            % (bin_tree, behind, "" if behind == 1 else "s", cure))


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    # Only the installed hook selectors. This marker is AFTER Python/package
    # startup; it cannot measure interpreter startup or census failed launches.
    selector = None
    if argv == ["record", "--hook-json"]:
        selector = ("standalone", "PostToolUse-or-Failure", "record")
    elif argv[:2] == ["chat", "deliver"] and "--hook-json" in argv[2:]:
        selector = ("standalone", "PostToolUse", "delivery")
    elif argv == ["hooks", "run", "PostToolUse", "--installed", "--hook-json"]:
        selector = ("composite", "PostToolUse", None)
    if selector:
        from . import hooklatency
        if hooklatency.entry_allowed():
            mode, event, stage = selector
            with hooklatency.event_scope(mode, event), hooklatency.stage(stage):
                rc = _main(argv)
                if rc:
                    hooklatency.mark("nonzero")
                return rc
    return _main(argv)


def _main(argv):
    if os.environ.get("HELM_NO_TREE_WARNING") != "1":
        _warn = which_helm_warning() or stale_tree_warning()
        if _warn:
            print(_warn, file=sys.stderr)
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
        # Honest even under --help: an unknown verb NEVER falls through to the
        # global usage with exit 0 — that false positive taught the fleet to
        # distrust `helm <verb> --help` as an existence probe.
        print("helm: unknown verb '%s'%s (helm --help)" % (verb, suggest(verb, VERBS)),
              file=sys.stderr)
        return 2
    rest = argv[1:]
    if rest and rest[0] in ("-h", "--help"):
        # help-FIRST short-circuits with the tail unread — deliberately: the
        # root does not know a verb's flag surface, so refusing `--help
        # --json` here would lie about real flags. No work ever runs on this
        # path; junk-beats-help binds where a handler parses its own tail.
        print("helm " + (_VERB_HELP.get(verb) or (fn.__doc__ or verb).strip().split("\n")[0]))
        return 0
    if verb in NOARG_VERBS:
        rc = guard_tail("helm " + verb, rest, usage=_VERB_HELP.get(verb)
                        or (fn.__doc__ or verb).strip().split("\n")[0])
        if rc is not None:
            return rc
    # THE ONE DOOR. A fleet-scoped hook outside helm's project exits
    # silently here, where the verb is known — never via a shell prefix
    # on the generated command (that suppressed the fail-open alarms and
    # broke envtidy's identification of helm's own hooks).
    try:
        from .hooks import hook_skips_here
        if hook_skips_here(verb, rest):
            from . import hooklatency
            hooklatency.mark("skipped")
            # SKIPPED, NOT ANSWERED. This handler never ran, and its 0 is
            # indistinguishable from a guard that ran and passed. A caller
            # aggregating "did every handler answer" has to be told.
            try:
                from . import hookoutcome
                hookoutcome.declare(hookoutcome.SKIPPED,
                                    "hook is scoped outside this project")
            except Exception:     # noqa: BLE001 — never break the skip
                pass
            return 0
    except Exception:
        pass                      # cannot tell -> run the verb
    return fn(rest)


if __name__ == "__main__":
    sys.exit(main())
