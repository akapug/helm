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
    rc = guard_tail("helm projects", args, flags=("--all",),
                    usage="projects [--all]")
    if rc is not None:
        return rc
    reg = registry.load()
    # THE REGISTRY KEY IS THE AUTHORITATIVE NAME. This read used a bare
    # p["name"] among a row of .get()s, so one row missing that field turned
    # the whole listing into a KeyError traceback — every other project on the
    # machine unreadable because of one. A row is FILED UNDER its name, so
    # recovering it from the key is not a guess, it is the more authoritative
    # source; the redundant copy inside the value is the weaker one.
    projects, malformed = [], 0
    for key, p in (reg.get("projects") or {}).items():
        if not isinstance(p, dict):
            malformed += 1          # counted and reported, never silently dropped
            continue
        projects.append(p if p.get("name") else dict(p, name=key))
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
    rc = guard_tail("helm show", args[1:], usage="show <project>")
    if rc is not None:
        return rc
    import json
    p = registry.get(args[0])
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
    "store": _lazy("store", "cmd_store"),
    "capabilities": _lazy("capability", "cmd_capabilities"),
    "index": _lazy("store", "cmd_index"),
    "inject": _lazy("inject", "cmd_inject"),
    "drain": _lazy("drain", "cmd_drain"),
    "promote": _lazy("drain", "cmd_promote"),
    "sweep": _lazy("sweep", "cmd_sweep"),
    "drift": _lazy("drift", "cmd_drift"),
    "reflex": _lazy("reflex", "cmd_reflex"),
    "record": _lazy("record", "cmd_record"),
    "todos": _lazy("todos", "cmd_todos"),
    "lineage": _lazy("lineage", "cmd_lineage"),
    "whoami": _lazy("whoami", "cmd_whoami"),
    "interview": _lazy("whoami", "cmd_interview"),
    "doctor": _lazy("doctor", "cmd_doctor"),
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
    "task": _lazy("tasks", "cmd_task"),
    "dispatch": _lazy("dispatches", "cmd_dispatch"),
    "gate": _lazy("gate", "cmd_gate"),
    "landgate": _lazy("landgate", "cmd_landgate"),
    "lr": _lazy("landreq", "cmd_lr"),
    "coach": _lazy("coach", "cmd_coach"),
    "premise-check": _lazy("premise", "cmd_premise_check"),
    "seat": _lazy("seat", "cmd_seat"),
    "router": _lazy("modelrouter", "cmd_router"),
    "codex": _lazy("codexhomes", "cmd_codex"),
    "work": _lazy("work", "cmd_work"),
    "search": _lazy("transcripts", "cmd_search"),
    "transcript": _lazy("transcripts", "cmd_transcript"),
    "rehome": _lazy("transcripts", "cmd_rehome"),
    "prune": _lazy("transcripts", "cmd_prune"),
    "keepalive": _lazy("keepalive", "cmd_keepalive"),
    "creds": _lazy("creds", "cmd_creds"),
    "cred": _lazy("cred", "cmd_cred"),
    "swap": _lazy("creds", "cmd_swap"),
    "attribute": _lazy("attribute", "cmd_attribute"),
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
    "clarity": _lazy("clarity", "cmd_clarity"),
    "pi": _lazy("pi", "cmd_pi"),
    "eval": _lazy("evalpin", "cmd_eval"),
    "proxywatch": _lazy("proxywatch", "cmd_proxywatch"),
    "proxy-fork-watch": _lazy("proxy_fork_watch", "cmd_proxy_fork_watch"),
    "env": _lazy("envtidy", "cmd_env"),
    "mcp": _lazy("envtidy", "cmd_mcp"),
    "worktree": _lazy("envtidy", "cmd_worktree"),
    "tidy": _lazy("envtidy", "cmd_tidy"),
    "rearm": _lazy("rearm", "cmd_rearm"),
    "beacons": _lazy("beacons", "cmd_beacons"),
    "ready": _lazy("ready", "cmd_ready"),
}

# Verbs whose handlers read NO arguments at all: nothing below main() will
# ever look at the tail, so the ROOT guards it — `helm sync --bogus --help`
# must refuse (exit 2) BEFORE the (possibly mutating) leaf runs, not run
# sync while --help pretends the flag existed. The sweep test DERIVES this
# set from the source (AST: the handler never loads its args param) and
# fails when a new no-arg leaf is born outside it, so the class stays closed.
NOARG_VERBS = ("home", "sync", "doctor", "human")

_VERB_HELP = {
    "brief": "brief [--hours N] [--json] — the operator's morning brief: sessions, knowledge delta (incl. pinned-starvation tail), seats, owner gates (read-only, never probes)",
    "projections": "projections [--json] — the projection registry: every derived store, its class, source + rebuild (laws 2+3's read surface; doctor enforces)",
    "store": "store list|get|resolve|pinned|counts|add <type> <id> | <stmt> [| ...] (type: prior|premise|lexicon|heuristic|reference)|keywords <id> [--add CSV|--remove CSV]|xrev-clear <id> --by <who>|confirm|reject|evidence <ts> <id> <delta> <why>|supersede <ts> <old> <new>|retire <ts> <id>|demote <id> [--undo]|events [--limit N] — the ONE typed knowledge store: add/resolve is the capture-then-JIT loop, candidate -> xrev-clear -> confirm is the ratify ladder, evidence/supersede/retire/demote are the lifecycle receipts (bare verb prints the full grammar)",
    "capabilities": "capabilities [--public] [--json] — the wired-substrate SELF-INDEX: what helm gives you (verb -> what -> wired-via -> live/absent), grouped core vs powerpack. The SAME index JIT-surfaces one line at the reasoning moment via inject (a2a/converge -> meld+chat-deliver; 'solved this before' -> recall; deep code analysis -> a code-analysis powerpack when wired). --public withholds private-hold powerpacks",
    "index": "index cap [--budget-lines N] [--apply] — MEMORY.md budget actuator: demote link lines whose backing entry stays jit-resolvable (provably lossless); documented Stop-hook line `helm index cap --apply`",
    "inject": "inject [--project P] [--json|--explain|--hook-json|--lane-report|--compare-report] — per-turn context for harness hooks (stdin: prompt or hook JSON); the day's first turn leads with a one-line brief whisper; --compare-report is the local-vs-comparison (Cloudflare agentic-memory) divergence verdict",
    "drain": "drain [--apply] — route raw memory intake to typed homes (dry-run default)",
    "promote": "promote [--since Nd] [--cap N] [--apply] — episodic->durable funnel: recent USER messages -> drain-intake candidates (dry-run default)",
    "sweep": "sweep [--apply] [--project P] — lineage-driven supersession sweep of the adopted store (dry-run default)",
    "drift": "drift — surface belief drift; silent when steady",
    "reflex": "reflex list [--all]|add|retire|smoke — (signal -> steer) entries (--all includes retired); signals prompt|marker-file|every-turn|counter|stalled|thrash|drift|stuck (record.py counters, latch once per episode)",
    "record": "record [--hook-json]|status|install — session-keyed tool-outcome recorder (PostToolUse leg); its counters drive the counter/latch reflexes",
    "todos": "todos [--all] [--json]|promote <id> [--owner S]|demote [--dry-run] [--owner S] — the seat todo mirror: what each agent is working on right now (seat | in-progress | done/total | age). The LIST is a PULL-only surface; the recorder captures TodoWrite/Task* into per-session state and pushes at most one rate-capped room line per meaningful transition (never a mention, never a DM). `promote` files ONE personal todo into the SHARED fleet ledger and stamps the row id back onto it, so an open loop stops living only in one session's scratch; `demote` is the CLEANUP leg — it removes personal rows whose ledger twin is closed or is now owned by another seat, and --dry-run shows what it would remove without removing it. BOTH ARE MANUAL VERBS: nothing calls them within a turn, so the bridge moves an item only when someone types it",
    "lineage": "lineage [seed|add|external|archive-report] — the project family tree",
    "whoami": "whoami [note ...] — the operator profile + dated notes",
    "interview": "interview — the five-minute know-your-user interview",
    "doctor": "doctor — health check, read-only",
    "watchdog": "watchdog [--json] [--quiet] — scan proxy seat error logs for the context-window WEDGE signature and raise a loud a2a alert ONCE per wedge (re-fires only if the count climbs; a recovered seat re-arms), so a human can /clear + relaunch. Run it periodically (Monitor/cron) as the live backstop; --quiet detects without posting, --json is the machine form",
    "gc": "gc [--dry | --apply] [--install-timer] — declared retention budgets over the derived exhaust (dry-run default; authored content never touched). --install-timer wires the HOURLY cadence: a retention policy nothing schedules is a policy that does not exist (measured 11,192 items over budget on a gc that had never run)",
    "scratch": "scratch [small|big|durable [--name N]] | gc [--dry | --apply] | status [--json] — the MOUNT plane: the substrate picks where scratch goes so no agent has to guess (small -> the fast ambient tmp; big clone/fork trees -> an UNCAPPED RAM mount while it has headroom, else disk; durable -> disk, never volatile tmpfs), and `scratch gc` reaps dead-session scratch (liveness before age, pressure-escalating TTL, bounded, dry-run default; the automatic pass rides the Stop hook, HELM_SCRATCH_GC=0 disables). `status` measures every mount helm writes on BOTH axes — bytes AND INODES, because a tmpfs `nr_inodes=` cap is invisible to df -h and is what turned a 36%-used /tmp into `No space left on device`",
    "skills": "skills [dupes|sync [--apply]] — census (read-only) + sync: one canonical skills source symlinked into EVERY claude-code config dir (credhomes + seats; dry-run default)",
    "evolve": "evolve — one observe/propose cycle (proposes, never mutates)",
    "mentor": "mentor observe <project> [--since 7d]|teach <project> \"<id> | <steer>\" [--attest]|review <project>|log — the inception actuator: critique brief, taught project reflexes with provenance, before/after review (teach is the one write; the rest read-only)",
    "fleet": "fleet [--json] — composition truth: every live claude process -> seat/sid/daemon/stamps/home, all live-probed; ANSWER FLEET QUESTIONS BY RUNNING THIS, never from memory",
    "sessions": "sessions [<project>] — every local claude + codex session (the two transcript formats the catalog indexes; opencode/pi sessions reach `helm projects` but produce no rows here); resume in one paste",
    "storage-matrix": "storage-matrix [--measure] [--json] — the owner-facing cross-box storage matrix: explicit bounded measurement writes one durable snapshot; bare reads never probe",
    "session": "session ls|doctor|doctor-panes|checkpoint|port|rescue|resume|experts|ask — the session substrate: inventory, persistence-health, checkpoint, port, compose (wraps cv; single-open law + print-don't-launch)",
    "homes": "homes [prepare|verify|archive|restore|migrate|archives] — credential-home lifecycle (migrate renames a name-lies home to the canonical name for the identity it actually HOLDS, alias symlink left at the old path; refuses live sessions, never touches credentials)",
    "configs": "configs [list|show <path>|cascade <cwd> [--harness claude|codex|pi]|edit <path>|backups|restore <backup>] — every config across every home: list/show/cascade read (cascade = what a seat at <cwd> loads, per harness); edit takes new content on stdin through backup -> validate -> atomic write; backups lists the snapshots that makes, restore returns one",
    "hooks": "hooks [install [--dry]|status|sync [--apply]] — self-wire the per-turn inject hook into every claude home; sync reconciles every home to the NAMED canonical hook set (dry-run default)",
    "cell": "cell join|accept|send|recv|heartbeat|roster|status — the a2a substrate, helm-named (status = node liveness; the rest ride an optional HELM_CELL_BIN transport and degrade honestly without it)",
    "chat": "chat post|read [--since N|--follow]|rooms|react <n> <emoji>|verify|log-flush|restore-journal [--apply]|dm <seat>|reply <id|n>|catchup|ack <id>|pending|node up|down|status|meld|council|standup|verdict|reveal|council-status|council-abort|join|deliver|wait|seats|status [<line>|--clear]|seat gc [--apply]|claim|release|claims [--room R] — the human-included groupchat (RAM room + signed dregg transport; web panel = the owner's surface) + the delivery lane (tool-boundary nudge, roster presence, advisory session-bound claims). The rooms are tmpfs and DIE WITH A REBOOT: restore-journal is the ONLY rebuild path (log-flush's inverse — dry-run default, idempotent, delivery cursors protected so old mentions never re-deliver as new), and verify recomputes every signed row's digest (rc 1 on mismatch). verdict|reveal|council-status|council-abort = the embargoed council vote: seal one member's judgment, lift only at quorum, tally without naming signers, abort permanently — an aborted council never reveals",
    "multiplayer": "multiplayer set|status|publish|read|presence|peers|leave [--backend V] — metaharness-agnostic local multiplayer: blind opaque-update relay + decoupled TTL presence (set/status drive the built-in LWW demo board; --backend on any verb selects the adapter pair, default local/HELM_MULTIPLAYER_BACKEND)",
    "launch": "launch [--seat S] [--home H] [--room R] [--no-install] [--] [claude args…] — the metaharness seam: wire hooks, seat the roster, exec claude under a stable addressable name",
    "human": "human (or helm --human) — the operator's curses TUI: chat room + status strip, posts as you",
    "premise": "premise <id> | <statement> — capture a certain truth, attested on the ledger; --supersede <old-id> <new-id> | <statement> evolves the chain (one signed linking turn)",
    "task": "task add <title...> --owner SEAT [--note N] [--ref R]... [--id NNN] [--owner-asked]|list [--all] [--owner S] [--json]|show <id>|resolve <token>|claim <id> [--owner SEAT]|update <id> [--title T] [--note N] [--owner S] [--status S] [--origin owner|agent] [--ref R]...|close <id> <reason...>|comment <id> <text...> — the FLEET TASK LEDGER: shared work items with real ids every seat can resolve, at ~/.helm/_global/tasks.jsonl. Ids KEEP THE NUMBER THEY WERE BORN WITH (task/263), so the week of chat rows citing '#263' still resolves — `resolve` takes any spelling (263, #263, task/263) and answers UNPARSEABLE separately from ABSENT, because a typo and a number nobody filed want opposite answers. A live task must name an OWNER SEAT: an unowned row is a list nobody is accountable to. CLOSED rows include TOMBSTONES for items retired before the ledger existed — 55% of the fleet's citation load points at those, and a tombstone is what makes a week-old citation resolve to a sentence instead of nothing. Deliberately carries NO board projection and NO owner-gate queue: 120 engineering tasks filed where 3 owner rulings live is the exact burial ownerasks.py:687-691 already measured once, at 19 rows",
    "asks": "asks add <text> --needs \"<what only the owner can supply>\"|done <id> <evidence>|report <id> <chat-post-id>|list [--open] [--json] — the durable OWNER-ASK ledger (agents self-add); 'done' stays OPEN until 'report' names the chat post that told the OWNER (owner-surface-is-the-bar); unreported asks ride the stop-whisper's top rung",
    "decide": "decide file <title> [--asker SEAT] [--ref R]... (card body on stdin: context paragraph, then `* label :: consequence` option lines, `*!` = recommended; none = approve/reject)|list [--open] [--json]|show <id> [--json]|verdict <id> <choice> [--comment T]|deliver <id>|comment <id> <text>|board-sync — the durable OWNER DECISION queue (the ask ledger generalized to rulings-among-options): any seat files a card, ONE compact line lands in its room AND the card's HEADLINE is pushed to the OWNER'S PHONE (a room is read by seats, so without the push a card waits on him noticing; the OPTIONS travel in the push and it names no console, because helm web binds 127.0.0.1 which on a phone is the phone; reach is recorded, never assumed — an opt-out or a failed push leaves the card marked NOT PUSHED and the next card's push carries it), the owner answers on the WEB queue at his leisure, and the verdict DMs the FILING SEAT (beacon-woken — never lost to an unwatched pane); 'decided' stays open debt until 'delivered' (the done-vs-reported split), and delivered binds to the DURABLE ledger — a retry re-verifies the tmpfs DM lane and re-sends from the ledger on loss (at-least-once, stated); the board's owner_gated_queue rows it stamps src=ledger are auto-derived from open cards + unreported asks, hand-written rows passing through untouched",
    "dispatch": "dispatch send <recipient> <lane> <message...|body on stdin> --ref TIP --kind build|review --new-work|--supersedes ID [--key K] [--force]|add <recipient> <lane> --ref TIP --kind build|review --new-work|--supersedes ID [--force]|verdict <id-or-unique-prefix> <full-reviewed-tip> --approve|--fix|--supersede|--concur --measured|--inferred|--unverified <evidence>|cancel <id-or-unique-prefix> <reason...>|mark-delivered <id-or-unique-prefix> <delivery-ref>|rebind <id-or-unique-prefix> --to <seat> [--force] [--reason R] [--repo PATH] [--json]|retip <id-or-unique-prefix> --ref NEW_TIP --reason R [--repo PATH] [--json]|hold <id-or-unique-prefix> <reason...>|release <id-or-unique-prefix>|list [--open|--overdue|--held] [--json]|triage [ID...]|mix [--hours N] [--sender SEAT] [--json] — durable DISPATCH ledger. EXACTLY ONE of --new-work / --supersedes <dispatch-id> is REQUIRED on send and add: the LANE is a free-text label and could never say whether a row continues earlier work, so a renamed continuation was invisible to every same-lane rule and a re-dispatch that named nothing built 124 land loops with no way to close them. --supersedes links to the row this continues (its chain root becomes this row's work identity); --new-work roots a fresh chain. Rows written before the field carry NO chain and stay legacy — never retro-fitted, never guessed. A parent that is cancelled or still open is valid. A second OPEN child naming the same parent REFUSES unless --force explicitly declares a deliberate fork; a same-repository OPEN row reusing a --new-work lane label only warns because lanes are labels, not identity. An id that does not resolve, an ambiguous prefix, an unreadable ledger, and a parent with a corrupt chain all REFUSE the write, because unknown work identity must never quietly mean new work. --kind is REQUIRED on send and add: it records whether the dispatch asks the recipient to BUILD or to REVIEW, and `mix` reads it to answer how fleet capacity is allocated. Omitting it was allowed once and made the capacity alarm vacuous — the inverted night it exists to catch reproduced simply by not typing the flag. Rows written before the field carry UNKNOWN and are reported in their own bucket, never assumed. --measured|--inferred|--unverified is REQUIRED on verdict and records HOW THE REVIEWER KNOWS (owner ask, task/338: 'always making legible how much doubt should go into a conversation'): measured = a tool ran and produced the finding, inferred = reasoned from code or output read, unverified = not checked. A MARKER rather than a percentage, because a number invites a precision nobody can audit while these three are checkable against what the reviewer actually did. It was canon at certainty 1.00 for a day and reached 2.34% adoption with ZERO mechanism — required at the CLI for new writes, permissive in mark_verdict so replay is untouched; the 221 rows written before the field read UNMARKED, never 'unverified'. OMIT <message...> to pipe the body on STDIN or use a QUOTED-delimiter heredoc (`<<'EOF'`) — the literal route, since argv bodies and UNQUOTED heredocs both substitute backticks and $() before helm sees them. A dispatch persists before delivery; one operation sends AT MOST ONCE (a retry never re-DMs — confirm at the recipient); ambiguous delivery stays open NEEDS CONFIRMATION; a matching exact-tip verdict closes it only with an explicit polarity; APPROVE additionally requires a verified `gate:<token>`, while FIX/SUPERSEDE remain valid ungated because they authorize no land. CONCUR is the fourth polarity and it authorizes NOTHING: it records that a reader endorses an artifact, binds ungated like FIX, and is absent from every close door and from SPIRAL_TERMINAL_POLARITIES by construction. It exists because the only ungated verdicts were the DISAPPROVING ones, so on a row whose artifact is not a landable tree agreement had to mint a receipt while objection bound free. `cancel` honestly abandons a stranded one with a reason. `rebind` moves one OPEN row to a new recipient in a single operation — cancel-as-REBOUND plus a superseding re-add preserving lane/ref/kind/note/deadline — and is EVIDENCE-GATED: either proxywatch-measured starvation/hang or autocompact-proven fresh context exhaustion suffices; an unreadable proxywatch does not suppress the independent context arm, but no evidence across both arms refuses. --force overrides with a mandatory recorded reason, and the DM body does NOT travel (only its hash is stored), so re-brief the new recipient. `retip` is rebind's mirror for when the BASE moved rather than the reviewer: it re-points one OPEN row at a NEW TIP in place — same row, same recipient, same chain, one strict audit event — refusing on any non-open row because a verdict BINDS its tip. `hold` acknowledges a row while gating it on a named external dependency; `release` returns the HELD row to OPEN; `list --held` shows that bucket. `triage [ID...]` re-measures each open row's claims against the tree checked out RIGHT NOW (file:line by content, counts against the live ledger, cited shas by ancestry AND patch identity), printing the verdict beside the id so a seat picking up work reads the decay before the prose. A proof-gated LR discharge may later annotate a contrary FIX/SUPERSEDE verdict without rewriting it, and accepts only a later land-authorizing approval (tier + gate included). Historical ref-less rows read NEEDS REDISPATCH (redispatch with --ref; no bind/ack verbs exist — retargeting is `rebind`, above). Unavailable storage means obligations UNKNOWN",

    "landgate": "landgate --lane L --tip SHA --tree SHA --gate ID [--base REF] [--repo P] — READ-ONLY: may THIS seat land THIS lane right now? Reports the five clauses of the self-land predicate (landlock held; a gate bound to the POST-REBASE tree it is actually landing, not the reviewed tip; an approving cross-family verdict at the reviewed tip; a changed-file set disjoint from the other approved-unlanded lanes; freeze admits) one line each, and names the clause it refuses on. It NEVER lands — the actuator is a separate verb with a separate bar, so asking is always safe. UNKNOWN never qualifies: a land is irreversible on a shared trunk, so what cannot be proven is refused",
    "gate": "gate run [--label T] [--timeout S] [--repo P] [--json] [-- <argv>]|show <id>|list [--limit N]|import <artifact.jsonl> [--repo PATH] [--id RECEIPT-ID] — MINT a suite result. The only way a gate claim enters helm: helm RUNS the suite (as a child of ITSELF, so the recorded interpreter cannot differ from the running one), reads the runner's own summary, and writes a receipt binding interpreter + HEAD + tree + dirty-flag + counts. `run` prints the one evidence line you paste into `dispatch verdict`, whose `gate:<id>` token that verb then RESOLVES — exact-tip receipts bind unchanged; a later whole-suite head also binds when the standing dispatch repository proves it contains the reviewed tip and the receipt strictly postdates the dispatch. Divergent/reverse/UNKNOWN ancestry, dirty trees, and non-OK status REFUSE. Evidence with no token remains valid and UNVERIFIED for FIX/SUPERSEDE, but a receipt-capable APPROVE is refused before append because it could never authorize landing. Gate under the other interpreter by invoking helm under it (`python -m helm gate run` vs `python3 -m helm gate run`) — on THIS box `python` is GraalPy and `python3` is CPython and they disagree about whether the suite passes. A `--` command records interpreter UNKNOWN and can never bind a verdict, because helm did not choose it. Every receipt also names the HOST and the ROOM it ran in, both on the evidence line: a suite can be honest about the code and wrong about the box (a fab-minted red cost a reviewer a turn on 14 failures that only exist on the other machine), and a green from the shared checkout proves nothing about your lane. The cross-box cure is `import`: a receipt minted on the box that CAN honestly run the suite travels as an artifact.jsonl and enters the binding ledger ONLY through it — content id recomputed and matched, cited head+tree required to resolve HERE, appended VERBATIM and idempotently behind a durable provenance event — so the hand-append that once smuggled seven genuine-but-unprovable remote rows in is the wrong tool forever. A receipt that cannot name its host REFUSES to bind.",
    "lr": "lr list [--all] [--json]|show <id> [--json]|stalls [--json]|foldcheck <tip> [--gate gate:TOKEN] [--repo PATH] [--remote R] [--branch B] [--no-fetch]|legacy-completion-hints [--json]|land <id> [--json]|compose <id> [<id>...] [--trunk REF] [--repo PATH] [--dry-run] [--json]|close <id> --reason landed|superseded|withdrawn|out-of-scope|stranded|subsumed|delivered-report|discharged|resolved [--evidence LINE] [--artifact-ref REF] [--report-ref CHAT_REF] [--tip FULL_SHA] [--repo PATH] [--trunk REF] [--live | --needs-restart WHAT] [--dry-run] [--json]|annotate-delivered-report <id> --artifact-ref REF --report-ref CHAT_REF --evidence LINE [--json]|discharge <id> <full-superseding-tip> <evidence...> [--json]|withdraw <id> <evidence...> [--json]|abandon <id> --reason TEXT [--repo PATH] [--json]|close-landed <id> --trunk REF [--repo PATH] [--json]|refs [--repo PATH] [--json]|migrate --commit-map PATH [--repo PATH] [--apply] [--json] — LAND REQUEST view over dispatch + live Git patch identity. `close` is THE terminal verb: landed proves reviewed work on one pinned trunk and also closes an OPEN BUILD parent through an exact descendant authorized APPROVE review (the parent tip remains a base, never reviewed proof); superseded runs the full chain-walked discharge ladder for any verdict polarity; withdrawn proves absence and stays falsifiable; out-of-scope cancels a moot OPEN row through the cancel boundary after a liveness block; stranded terminates only a provably destroyed substrate; subsumed closes APPROVE or FIX review debt through a later linked same-repo cross-family APPROVE while the original is absent, with FIX requiring an explicit findings-answered-on-trunk statement; delivered-report closes only an OPEN BUILD with a compact typed artifact ref, full 12-character lowercase-hex Helm chat row id, and concise evidence, claiming no verdict or Git landing; discharged closes only an OPEN never-verdicted row whose work is on trunk under a successor's landed, gate-verified APPROVE — the authority is the discharging row's, re-derived from the ledger at write AND replay, never taken from the event; resolved retires a FIX/SUPERSEDE-verdicted row whose OWN reviewed tip reached trunk by ancestry, on one later same-repo cross-family confirmation verdict (approve or supersede, never fix) whose evidence opens `Resolution verified on trunk:` — the overridden reviewer is recorded on the close event; reference syntax is validated, not existence. landed also REQUIRES the LIVE step — trunk and the running fleet are different facts, so `--live` declares CLI-class (a fresh helm process off main, live AT LAND) and `--needs-restart WHAT` declares process-class and names what still holds pre-land code until it re-arms (`helm rearm` measures that); helm never guesses the class and a row closed process-class prints as an OPEN LOOP. `legacy-completion-hints` is a read-only audit over cancelled explicit BUILD rows whose cancel_reason contains the historical standalone word `delivered`; every result remains CANCELLED, classification UNVERIFIED, delivered-report eligibility UNKNOWN, and may be a negative or code delivered under a successor. `annotate-delivered-report` is the separate explicit append-only correction for a historical cancelled BUILD; it preserves the cancel event/reason and never infers authority from prose. A confirmation rooted at the exact original also links a pre-chain legacy row; SUPERSEDE remains outside SUBSUMPTION. Historical proof-v1 closes replay unchanged. discharge/withdraw/close-landed are deprecated aliases of close for one release. abandon stays its own verb (not an alias): it writes off reviewed work only when the row-owned Git repository explicitly reports its commit missing, recording land state UNKNOWN. `lr land` stays the separate at-integration receipt verb and is NOT deprecated. Land receipts stay diagnostic only and fail open. `compose` stands N READY lanes on one measured tip in a detached <repo>-wt/compose room — per-member patch-id carry re-measured across the cherry-pick, refusals name the member (conflict/drift/already-landed/partial), one gate on the composed tip is the only per-tree evidence, prefix re-compose localizes a red batch. `refs` audits every commit id recorded across the land-request + dispatch ledgers against the live repo — a recorded proof that cannot resolve can never be re-verified — and after a history rewrite `migrate --commit-map` (git filter-repo's own map file) translates those ids in place, dry-run by default. `foldcheck` is the five fold checks as ONE refusing rung over a tip — it exits 1 on REFUSE *or* UNKNOWN, because not-measured is not consent and a caller gating on rc needs the same stop for \"no\" and for \"I could not tell\"; the fifth rung (did it reach ORIGIN) is the one hand-folding skips when tired, which is how two lands were announced that origin did not have",

    "coach": "coach <lesson...> [--apply] [--as L] [--id ID] [--project P] [--supersede OLD] [--json] — the capture front door with the 4-step GATE (reframe->place->search-first->simplify); propose-only unless --apply (low-confidence -> drain intake, lossless)",
    "premise-check": "premise-check <id> [--chain] — verify digest + quote the finality tier; --chain walks the supersession chain (attested biography)",
    "seat": "seat add|up|down|launch [--multi]|spawn|where|resume|rebind <seat>|--all [--apply] [--install-timer]|smoke [--multi]|autocompact|silent-drop|idle-dispatch|panes|composers|adopt|doctor|resume-turn|list|status — multimodel seats (codex family via local proxy); spawn <seat> = harness-agnostic SELF-ONBOARDING spawn (reaps a stale same-name seat; orca/herdr pane + onboarding injection, or detached HEADLESS with the onboarding as boot first-prompt when no metaharness; --print dry-runs the exact calls); where <seat> resolves the spawn register (harness/handle/pid/worktree/room/liveness); launch/smoke --multi = mixed-model fleet (no subagent pin, per-agent frontmatter routes, conductor-log-verified fan-out); composers = the READ-ONLY fleet probe for HELD-BUT-UNSENT composer text — a seat whose next instruction was typed and never submitted reads IDLE-AND-HEALTHY to seat_liveness, proxywatch and the beacons alike, so this is the only instrument that sees it; it answers held/clear/CANNOT-TELL per pane and never folds the third into the second (12 of 26 live panes answer `terminal read` with an empty tail while their seats work fine), and it never sends a keystroke, because held text is as often a human's unfinished draft as a stranded directive; resume <seat> relaunches the pane via the detected metaharness (orca/herdr), freshest launch.sh + claude --resume/--continue; autocompact = proxy-seat context watchdog (inject /compact at ~90% before the 100% hang); rebind = THE REBOOT VERB: pane handles die with the machine while seats are durable, so post-reboot `where` reads a live fleet GONE — rebind proves each registered seat's live pane (session -> pid -> /proc) and re-stamps the register (dry-run default; --install-timer wires the cadence); silent-drop = the empty-completion loud-fail rung (a proxy turn ending end_turn with NO text/tool_use/thinking yet output_tokens > 0 generated an answer the pane never saw — read-only, one loud a2a alert naming seat + lost tokens); idle-dispatch = the stranded-obligation rung (open dispatch rows crossed against recipient presence: quiet + NO live claim = STRANDED, re-check or reassign; quiet + HOLDING a claim = busy or WEDGED, rescue and NEVER reassign; unreadable claims = UNKNOWN, never 'no claim')",
    "mcpd": "mcpd serve [--port N] — the stateless MCP endpoint (spec rev 2026-07-28, stdlib-only): one localhost POST route serving server/discover | tools/list | tools/call over the SAME verb layer the CLI fronts. Owner-ruled HYBRID (the 2026-08-05 decision cards): the CLI stays the universal floor and the diff oracle (test_mcpd pins the two front-ends equal on one input), beacon/wake stays Monitor+CLI (an MCP notification informs a client process, never wakes an idle model), and write verbs land in slice 1 behind per-seat bearer auth with server-side signing behind a flippable seam. Slice 0 ships read-only store_resolve; tool lists are deterministic and carry ttlMs/cacheScope for client prompt caches",
    "router": "router up|run|down|status|line|probes — the transparent multi-model router: claude-* forwarded VERBATIM to api.anthropic.com (the client's own OAuth, never an API key), non-claude models to their seat's CLIProxyAPI; line prints the claude-parent mixed-fleet launch line, probes mints the per-model example agents",
    "codex": "codex [list]|pool <name>|unpool <name>|pooled|capacity|sync-orca [--watch]|launch [-i N|--instance N] [--force] — codexhome roster (ultra/team) + proxy cred pooling: translate ~/.codex-homes/<name>/auth.json into the seat proxy's hot-reloaded auth-dir (0600), so the :8317 pool falls through usage caps; launch = cred-% gate (rollout rate_limits) ahead of the seat mint; sync-orca = one-way adapter pooling the codexhome matching orca's selected codex account (accounts.list over the daemon socket; no/ambiguous match refuses printing both rosters; --watch re-pools on selection change)",
    "work": "work claim <lane> [--lease ID] [--ttl N]|release [<lane>] --lease ID [--park]|release <lane> --stale|peek <committish> [--json]|peek --drop <path-or-committish>|gc [--apply]|list|stash [list|apply|pop|drop|show <message-substring>]|install-guard [--apply] — worktree lifecycle on the claims lane: private room <repo>-wt/<lane> per lane (lease = room key, git worktree lock = do-not-disturb), peek mints an unlocked disposable READ-ONLY detached worktree at exactly one commit under <repo>-wt/peeks/<sha12> (the reviewer door — no lease, no branch, and the ref guard admits it structurally with NO env override; --drop retires it, occupied/pane-bound/dirty refuse), gc rescue-commits dirty lease-less rooms to their branch (never discards), install-guard dry-runs/installs composed deterministic shared-checkout guards incl. the pre-commit never-track staged-set scan",
    "search": "search <text> [--scope P] [--refs] — content search inside transcripts",
    "transcript": "transcript <sid> [--find T] [--limit N] — windowed role-tagged transcript read",
    "rehome": "rehome <sid> <new-cwd>|--reset — re-home a session (claude slug symlink)",
    "prune": "prune <sid> [--preset lean|window20k] [--dry] — resume-optimized copy, original untouched",
    "keepalive": "keepalive [--home H] [--early N] [--apply] — dry-run by default; roll idle claude homes' tokens after a stable pre-image (codex read-only)",
    "creds": "creds [crosscheck [--json]] — live account scorecard (headroom/state/reset), all providers; crosscheck sums local session JSONL into the same 5h/7d windows and cross-checks the header truth (drift = health signal)",
    "cred": "cred [list|ls|backup [--all] [--apply]|switch-guard|guard [--install] [--apply]|heal [--apply]] — the SAFE /login: content identity plus transactional 0600 snapshots/restore; every mutation is dry-run without --apply and live-session uncertainty refuses (ls/guard = the short aliases the code accepts)",
    "swap": "swap <home|email> — seat ran dry: print resume-under-healthier-account blocks",
    "attribute": "attribute [--by project|model|cred] [--since Nd|Nh] [--limit N] [--project P] [--json] — token-effort rollup over the session catalog (output + cache-creation, never raw input; UNATTRIBUTED always visible)",
    "who": "who [--json] — pid->cred attribution table: every live claude/codex process, its cred home, account, cwd, and session (shared sessions flagged)",
    "capsule": "capsule <sid> — the session's git era: worktree + resume commands",
    "ship": "ship [--apply] [--remote URL] | ship pull | ship hosts — the authored chain over git (dry-run default; pull merges + re-derives)",
    "corpus": "corpus backup [--dry] [--dest DIR] | status — training-corpus transcript backup: copy-only incremental archive of every CLAUDE + CODEX + /tmp-estate session/subagent/workflow transcript (opencode and pi transcripts are NOT collected; default ~/corpus-archive; HELM_CORPUS_DEST)",
    "handoff": "handoff check [--hook-json]|write|recover <sid> — the compaction-continuity contract (PreCompact/SessionEnd nag; typed journal handoff; cv pre-compaction recovery)", "now": "now capture [--hook-json]|show — automatic session-continuity snapshot (_global/now.md, 40 lines newest-first, 48h freshness gate; SessionStart context)",
    "cmd": "cmd <sid> [--account A] [--model M] — account-aware pasteable resume command",
    "web": "web [--port N] — the same, warm, in a browser",
    "board": "board show|landed <name> <sha> <note>|set <key> <value...> [--new]|note <key> <text...> — the integration board (the owner console's lane truth) through its LOCKED, idempotent write path; a hand-rolled json.dump has already lost one update and can tear the file. set updates an EXISTING top-level scalar (--new declares the reader was coordinated with); note prepends one line to an EXISTING list-of-strings log",
    "note": "note set <key> <headline...> [--detail <body...>] [--goto <ledger|chat|roster|board|url>]|list [--json]|retire <key>|restore <key>|rm <key> — FLEET NOTES (retire stops a note competing for attention but KEEPS it, restore undoes that; rm DELETES — retire is the undoable one): one owner-facing headline, collapsed detail, and an explicit place to act. One note per key, durable and last-writer-wins; headlines over 90 characters warn, while 64 keys and 2000 total characters are hard caps. Board landings derive the current landed headline automatically. HELM_FLEET_NOTES overrides the path",
    "wiring": "wiring [--verbose] [--json] [--gate] — built/reachable/actuated/exercised/verified: modules no entry point can reach plus declared actions with NO installed schedule or hook. --gate remains the narrower this-tree-added reachability rung used by Stop",
    "punt": "punt [--text T | --transcript P] [--json] — the DRESSED declination detector: a first-person declined action plus an excuse from a named punt class (owner-presence, assumed-disruption, unproven-as-excuse, size-or-cost, someone-elses-lane) in ONE sentence. The Stop hook refuses an idle stop on a hit when the owner ask ledger has nothing open — decline loudly (helm asks add) or do the work",
    "clarity": "clarity check [<file>|-] [--strict|--owner] [--project P] [--no-store] [--json] | rules [--json] | skill [--project P] — the ASD-STE100-descended clarity die over coordination text: ONE rule table (helm/clarity/rules.py) read by both consumers (the write-time skill and the deterministic linter, exit 1 on violation). Eleven deterministic rules: four STE-derived, three from the writing skill, two owner-mode adapter rules from the helmese register (gloss a register symbol once per owner-bound artifact, then use it bare; open on the outcome, not the mechanism), and two that are helm's own: domain-term drift against the store's curated lexicon, and MEASURED/TRACED/INFERRED provenance on load-bearing claims. one-instruction is APPROXIMATE and reports advisory only; hedge-term is the measured-weakest rule and its findings carry [weak]. --owner is for text the OWNER reads (the four owner-bound verbs run it as an advisory automatically; silence with HELM_CLARITY_ADVISE_OFF). No POS tagger exists here (stdlib-only), so the STE dictionary/noun-cluster/verb-form rules and gloss-once on the ASCII operators are honestly absent, not faked",
    "pi": "pi extension [--seat S] [--apply] | run [SEAT] [--model helm-SEAT/MODEL] [PI_ARGS...] | launch [--model M] [--session ID] [--print] | resume --session ID|--continue | status — the pi harness seam: GENERATE a provider for one seat's CLIProxyAPI; run pins that provider and execs with the key only in child env; launch/resume print key-free commands; status reports pi binary + sessions. Zero TypeScript in the repo; the key is never a literal",
    "eval": "eval arms|register|seed|run — the cc-codex vs pi-codex eval's guard rails AND its pilot-shaped runner. `arms` asks whether the two arms are COMPARABLE (same endpoint, same model); without that pin a harness comparison measures the PROVIDER and the numbers still look like an answer. `register` writes the §C.5 decision rule BEFORE any run, stamped with the arms fingerprint so a later run against different arms cannot inherit it. `seed` premise-checks a task atom against current code — a refuted premise is recorded stale-and-skipped with the refuting file:line, never seeded. `run` drives the seat's OWN launch.sh (-p) under a per-run hook-stripped config copy in a per-run dir, refusing any elapsed<=0 row — the 2026-07-29 pilot's five defects, each owned in helm/evalrun.py",
    "proxywatch": "proxywatch [--post] [--json] [--force] [--install-timer]|vendor-reset set <family> <ISO-8601|epoch-ms>|vendor-reset show|vendor-reset clear <family> — fifteen-minute composite over every minted seat and represented family: local CONFIG/DROPS/HANGS/PROBE/LOG plus authenticated UPSTREAM canaries (each request capped at eight tokens; dark primaries confirmed then corroborated by healthy siblings, whose completed failures may also confirm once after 3s; client timeouts never retried), followed by the existing fused TURN verdict. Stable family since values and dark/recovered edges join the change-latched chat report. A dark family also drives delivery's PAUSED-CRED-WALL latch: addressed rows survive cursor/rotation/Stop paths, UNKNOWN holds the known wall, a last-good snapshot survives observer-file damage, verified per-session runtime family drives custom labels and canaries, and measured HEALTHY resumes existing waiters automatically. --force never invents an edge",
    "proxy-fork-watch": "proxy-fork-watch [--json] [--post] [--force] [--install-timer] — run the durable CLIProxyAPI fork's upstream checker; read-only by default, one #helm line on first run or semantic change, daily user timer available",
    "env": "env census [--json] — READ-ONLY estate picture: every claude config dir's hooks + MCPs, the variance vs canonical, and the orphan-worktree snapshot (replaces poking the configs UI by hand)",
    "mcp": "mcp sync [--apply] — reconcile canonical MCP servers into every home (dry-run default; additive + fail-closed; backup-first, superset-refusal)",
    "worktree": "worktree gc [--apply] — prune orphan worktree-*/lane/* branches + landed worktrees (dry-run default; rescue-dirty-first, locked/occupied-immune, unmerged-blocked; composes `helm work gc` for lane rooms)",
    "tidy": "tidy [--apply] [--repo PATH] — the umbrella: census + hooks sync + mcp sync + worktree gc, all dry-run; one consolidated report (--apply runs them all backup-first; --repo points the census + worktree-gc legs at another repo root)",
    "beacons": "beacons [--seat S] [--json] [--post] [--install-timer] — the inbox-beacon registry census. A beacon is a seat's ONLY wake path, so the alarm is TWO-DIRECTIONAL: a DEAF SEAT (no live beacon — nothing can reach it) and a GHOST WAITER (a beacon whose session is dead — it eats the seat's rows into a pipe nobody reads AND makes a dark seat read as covered). Liveness is a LIVE SESSION behind the shape, not an argv match. The bare read signals nothing, because a superseded beacon is stopped by its own seat's next re-arm. --post (the helm-beacons.timer entry) writes each verdict onto its roster row — the attendance register: state/since/seen/covered — and delivers reachability edges on BOTH channels change-latched per channel — #helm, and the OWNER'S PHONE (HELM_NTFY_TOPIC, the shared notify.owner_push), because when the fleet is unreachable every reader of the fleet room is one of the unreachable seats; exit 0 clean, 1 faults FOUND, 2 the watchdog itself failed. --install-timer wires the cadence",
    "rearm": "rearm [--apply] [--json] — land-to-live: report (dry-run default) which long-lived processes still hold pre-HEAD code (helm chat wait waiters, the web unit, advisory proxies/daemons); --apply announces (ambient), SIGTERMs ONLY the stale waiters so each owner re-arms on new code at its own turn boundary (the OWNED beacon-cycle), and restarts a stale web unit — proxies/daemons/seats never signaled",
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
    (console-design caught the cost; the design intent had been zero git calls in
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
    """Is `root` specifically a helm checkout?

    It must contain either a `helm/` package directory (with __init__.py) or a
    `bin/helm` entry script. Standing in an arbitrary non-helm git repository
    (e.g., an adopter's own project) is NOT standing in a helm checkout, so
    running a global `helm` binary from there is normal usage and must NOT
    fire a tree mismatch warning."""
    if not root:
        return False
    if os.path.isfile(os.path.join(root, "helm", "__init__.py")):
        return True
    if os.path.isfile(os.path.join(root, "bin", "helm")):
        return True
    return False


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
    returns before touching git at all.
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


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if os.environ.get("HELM_NO_TREE_WARNING") != "1":
        _warn = which_helm_warning()
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
    return fn(rest)


if __name__ == "__main__":
    sys.exit(main())
