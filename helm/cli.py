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
    scaffold project homes. Additive: never deletes a known project.

    NO ARGUMENTS AND NO DRY RUN (NOARG_VERBS refuses `helm sync --apply`
    with exit 2). A plain `helm sync` WRITES three things: the registry
    (registry.sync), the act-owned keyword cells it retires from their store
    entries (actsteer.retire_moved), and the doors' route cells
    (doors.apply_routes, a store.retag of each entry in doors.ROUTED)."""
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
    # THE DOORS' STORE HALF (task/1135 E): route cells onto the entries a
    # helm verb's door now says, and the common cells those routes replace
    # off them, in the same pass (doors.ROUTED).
    from . import doors
    for eid, state in doors.apply_routes():
        if state in ("applied", "held", "partial"):
            print("  %s door routes: %s" % (state, eid))
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
    "classify": _lazy("classify", "cmd"),
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
    "goal": _lazy("goals", "cmd_goal"),
    "dispatch": _lazy("dispatches", "cmd_dispatch"),
    "review": _lazy("review_done", "cmd_review"),
    "delegate": _lazy("delegate_grant", "cmd_delegate"),
    "owed": _lazy("obligation", "cmd_owed"),
    "owed-push": _lazy("owedpush", "cmd_owed_push"),
    "gate": _lazy("gate", "cmd_gate"),
    "landgate": _lazy("landgate", "cmd_landgate"),
    "compose": _lazy("foldcompose", "cmd_compose"),
    "train": _lazy("landwindow", "cmd_train"),
    "lr": _lazy("landreq", "cmd_lr"),
    "stale": _lazy("stalebot", "cmd_stale"),
    "pile": _lazy("pile", "cmd_pile"),
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
    "team": _lazy("teams_cli", "cmd_team"),
    "accounts": _lazy("accounts", "cmd_accounts"),
    "cred": _lazy("cred", "cmd_cred"),
    "swap": _lazy("creds", "cmd_swap"),
    "attribute": _lazy("attribute", "cmd_attribute"),
    "proxy-usage": _lazy("proxy_usage", "cmd_proxy_usage"),
    "who": _lazy("who", "cmd_who"),
    "capsule": _lazy("capsule", "cmd_capsule"),
    "ship": _lazy("ship", "cmd_ship"),
    "release": _lazy("releasenightly", "cmd_release"),
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
    "offpeak": _lazy("offpeak", "cmd_offpeak"),
    "upstream-watch": _lazy("upstream_watch", "cmd_upstream_watch"),
    "pressure-watch": _lazy("pressurewatch", "cmd_pressure_watch"),
    "slice-limits": _lazy("slicelimits", "cmd_slice_limits"),
    "env": _lazy("envtidy", "cmd_env"),
    "mcp": _lazy("envtidy", "cmd_mcp"),
    "worktree": _lazy("envtidy", "cmd_worktree"),
    "tidy": _lazy("envtidy", "cmd_tidy"),
    "rearm": _lazy("rearm", "cmd_rearm"),
    "beacons": _lazy("beacons", "cmd_beacons"),
    "ready": _lazy("ready", "cmd_ready"),
    "office": _lazy("officeweather", "cmd_office"),
    "weather": _lazy("officeweather", "cmd_office"),
    "reviewers": _lazy("reviewer_eligibility", "cmd_reviewers"),
    "preread": _lazy("preread", "cmd_preread"),
    "remote": _lazy("remote_relay", "cmd_remote"),
}

# Verbs whose handlers read NO arguments at all: nothing below main() will
# ever look at the tail, so the ROOT guards it — `helm sync --bogus --help`
# must refuse (exit 2) BEFORE the (possibly mutating) leaf runs, not run
# sync while --help pretends the flag existed. The sweep test DERIVES this
# set from the source (AST: the handler never loads its args param) and
# fails when a new no-arg leaf is born outside it, so the class stays closed.
NOARG_VERBS = ("home", "sync", "human")


# NAMES cli PUBLISHES THAT ANOTHER MODULE DEFINES, as (module, (names...)),
# the form `web_compat` uses. ONE LITERAL, AND IT DRIVES THE BINDING:
# `__getattr__` resolves each name from its module on first read, and
# `__dir__` lists it, so the declaration and the runtime binding cannot drift.
# It is also what the retired-name rung reads: `cli._VERB_HELP` is a declared
# move to `cli_help`, not a retired name. The help table is most of the CLI's
# source bytes and an ordinary verb never reads it, so nothing here imports it
# at module load.
_OWNER_NAMES = (("cli_help", ("_VERB_HELP",)),)


def __getattr__(name):
    """A name `_OWNER_NAMES` hands to another module, imported on first read:
    `cli._VERB_HELP` and `from helm.cli import _VERB_HELP` are
    `cli_help._VERB_HELP`."""
    for module, names in _OWNER_NAMES:
        if name in names:
            import importlib
            return getattr(importlib.import_module("." + module, __package__),
                           name)
    raise AttributeError("module %r has no attribute %r" % (__name__, name))


def __dir__():
    """The module's own names plus every name `_OWNER_NAMES` publishes, so a
    reader that finds the help table by walking `dir(cli)` still finds it."""
    return sorted(set(globals()).union(*(names for _m, names in _OWNER_NAMES)))


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

    NOT FOR A COORDINATION VERB (task/3382). A ledger write must come from
    trunk, so there "use ./bin/helm" is the wrong advice: it sent seats to
    lane code whose writes current readers drop. `trunkroute` runs those
    verbs on trunk from any tree and tells `_main` not to print this line.

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


#: EVERY INSTALLED HOOK ENTRY IS A MEASURED EVENT, not only PostToolUse's
#: (task/3040). The latency ledger could say how long a delivery took and
#: nothing about the Stop, PreToolUse and SessionStart hooks the owner was
#: waiting on. Keyed by the entry's exact argv; the value is (event, stage).
#: `handoff check --hook-json` rides PreCompact AND SessionEnd with one argv,
#: so its event is the provisional pair, the way `record` is filed under
#: `PostToolUse-or-Failure`. tests/test_hooklatency.py holds this table to
#: `hooks.SPECS` in both directions, so a new spec cannot ship unmeasured.
_HOOK_SPANS = {
    ("inject", "--hook-json"): ("UserPromptSubmit", "inject"),
    ("chat", "delegation-stop", "--hook-json"): ("SubagentStop", "delegation-stop"),
    ("saguide", "--hook-json"): ("SubagentStart", "saguide"),
    ("chat", "join", "--hook-json"): ("SessionStart", "join"),
    ("seat", "resume-turn", "--hook-json"): ("SessionStart", "resume-turn"),
    ("now", "show", "--hook-json"): ("SessionStart", "working-set"),
    ("chat", "stop-guard", "--hook-json"): ("Stop", "stop-guard"),
    ("chat", "argv-guard", "--hook-json"): ("PreToolUse", "argv-guard"),
    ("handoff", "check", "--hook-json"): ("PreCompact-or-SessionEnd", "handoff"),
}


def _hook_selector(argv):
    """(mode, event, stage) for an installed hook entry, else None."""
    if argv == ["record", "--hook-json"]:
        return ("standalone", "PostToolUse-or-Failure", "record")
    if argv[:2] == ["chat", "deliver"] and "--hook-json" in argv[2:]:
        return ("standalone", "PostToolUse", "delivery")
    if argv == ["hooks", "run", "PostToolUse", "--installed", "--hook-json"]:
        return ("composite", "PostToolUse", None)
    span = _HOOK_SPANS.get(tuple(argv))
    return ("standalone",) + span if span else None


#: What `bin/helm-hook` hands a hook child it started through the shebang
#: because it had no interpreter recorded for that PATH python3 yet.
HOOK_INTERP_RECORD = "HELM_HOOK_INTERP_RECORD"
HOOK_INTERP_KEY = "HELM_HOOK_INTERP_KEY"
#: Distinct PATH python3 entries the record keeps; the newest wins a slot.
HOOK_INTERP_KEEP = 8


def _record_hook_interpreter(environ=None):
    """Write `<PATH python3> TAB <sys.executable>` where bin/helm-hook asked.

    THE OTHER HALF OF THE WRAPPER'S INTERPRETER FLOOR. The wrapper can find
    the python3 that `#!/usr/bin/env python3` would start, but not where that
    file leads: on an agents box it is a shim that picks another interpreter.
    This process IS where it led, so `sys.executable` is the answer, and the
    wrapper execs it with -S from the next hook on. Asked only on a miss, so a
    warm hook pays two dict pops here and nothing else.

    BOTH VARIABLES ARE REMOVED FROM THIS PROCESS, so no child it spawns can
    record for a launch it was not. NEVER RAISES and writes only what the
    wrapper's reader accepts: absolute paths without a tab or a line break, an
    executable interpreter, a process that did run the site stage (one that
    did not was not started through the shebang, so it answers nothing about
    it). The file is replaced whole, other keys kept, so a reader sees the old
    record or the new one and never half of either."""
    environ = os.environ if environ is None else environ
    dest = environ.pop(HOOK_INTERP_RECORD, None)
    key = environ.pop(HOOK_INTERP_KEY, None)
    exe = sys.executable
    if not dest or not key or not exe or sys.flags.no_site:
        return False
    # THE RECORD FILE MUST BE ABSOLUTE TOO. The wrapper spells it from
    # `$HELM_HOME` unexpanded, where `home.helm_home()` would expanduser: a
    # `HELM_HOME=~/.helm` from a settings env block arrives here as a path
    # relative to the hook's cwd, and writing it would plant a literal `~/`
    # tree inside whatever project the hook ran in. Refused, the wrapper
    # never finds a record there and every hook takes the old path.
    if not (os.path.isabs(key) and os.path.isabs(exe) and os.path.isabs(dest)) \
            or any(c in s for s in (key, exe) for c in "\t\r\n"):
        return False
    tmp = None
    try:
        if not os.access(exe, os.X_OK):
            return False
        try:
            with open(dest, encoding="utf-8") as fh:
                lines = fh.read(1 << 16).splitlines()
        except OSError:
            lines = []
        entry = "%s\t%s" % (key, exe)
        keep = [ln for ln in lines
                if ln.count("\t") == 1 and ln.split("\t", 1)[0] != key]
        keep = keep[-(HOOK_INTERP_KEEP - 1):] + [entry]
        if keep == lines:
            return True
        import tempfile
        where = os.path.dirname(dest) or "."
        os.makedirs(where, mode=0o700, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=where, prefix=".hook-interp-")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write("".join(ln + "\n" for ln in keep))
        os.replace(tmp, dest)
        tmp = None
        return True
    except Exception:                    # noqa: BLE001 — a hook never dies here
        return False
    finally:
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def main(argv=None):
    # NO argv IS THE PROCESS ENTRY (`bin/helm`, `python3 -m helm`), and only
    # the process entry may route a coordination verb to trunk helm, because
    # routing replaces the process. An in-process caller passes argv.
    entry = argv is None
    argv = list(sys.argv[1:] if argv is None else argv)
    _record_hook_interpreter()
    # Only the installed hook selectors. This marker is AFTER Python/package
    # startup; it cannot measure interpreter startup or census failed launches.
    selector = _hook_selector(argv)
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
    banner = True
    if entry:
        # A coordination verb runs TRUNK helm from any tree (task/3382):
        # this returns only when the verb runs here, or when it is refused.
        from . import trunkroute
        rc, banner = trunkroute.enter(argv)
        if rc is not None:
            return rc
    return _main(argv, banner=banner)


def _main(argv, banner=True):
    """`banner` False: this process is trunk helm running a coordination
    verb, or a tree that opted in and already said so, so the maker's
    which-tree line has nothing true to say (`trunkroute`)."""
    if os.environ.get("HELM_NO_TREE_WARNING") != "1":
        _warn = (which_helm_warning() if banner else None) \
            or stale_tree_warning()
        if _warn:
            print(_warn, file=sys.stderr)
    if argv and argv[0] == "--human":   # the operator flag IS the verb
        argv[0] = "human"
    if argv and argv[0] in ("--version", "-V", "version"):
        from . import __version__
        print("helm " + __version__)
        return 0
    if argv[:1] == ["help"] and argv[1:] and not argv[1].startswith("-"):
        # `helm help <verb>` IS `helm <verb> --help`: the verb's entry whole,
        # and an unknown verb's refusal (bare `helm help` is the listing). A
        # FLAG after `help` names no verb: `helm help --json` is `helm --help
        # --json`, the listing, and never "unknown verb '--json'".
        argv = [argv[1], "--help"] + argv[2:]
    if not argv or argv[0] in ("-h", "--help", "help"):
        # ONE LINE PER VERB. The full entries run to about 120 KB, and this is
        # the first command any model runs; `helm <verb> --help` prints one.
        from . import cli_help
        print(cli_help.root_help(VERBS))
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
        from . import cli_help
        print("helm " + cli_help.entry(verb, fn))
        return 0
    # An empty tail passes the guard without reading `usage`, so only a tail
    # that will be refused or answered with help imports the help table.
    if verb in NOARG_VERBS and rest:
        from . import cli_help
        rc = guard_tail("helm " + verb, rest, usage=cli_help.entry(verb, fn))
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
    # A TIMER'S PASS IS A TICK LEG (task/4189): a timer entry that keeps
    # failing alarms once (`tickalarm.TIMER_ENTRIES`); any other verb runs
    # as it always did.
    from . import tickalarm
    leg = tickalarm.timer_leg(verb, rest)
    try:
        if leg is None:
            return fn(rest)
        return tickalarm.watch(leg[0], lambda: fn(rest), failed=leg[1])
    except OSError as exc:
        # A DISPATCH-LEDGER WRITER PAST ITS LOCK DEADLINE (task/3562) raises
        # `LedgerLockDeadline` out of `_ledger_write`, whichever verb reached
        # it, so this one door is where every writer's caller hears it: one
        # refusal line naming the lock and its knob, never a traceback. Only
        # a process that imported the ledger can have raised it.
        ledger = sys.modules.get("helm.dispatches")
        if ledger is None or not isinstance(exc, ledger.LedgerLockDeadline):
            raise
        print("helm: %s refused: %s" % (verb, exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
