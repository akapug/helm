# helm command reference

Every dispatcher verb, grouped the way you reach for them. Syntax lines match
the CLI's own `--help` (which is always the last word: `helm <verb> --help`
prints the one-liner; running a multi-action verb bare prints its sub-grammar).
Verbs never take a config file — behavior is shaped by `HELM_*` environment
variables only, documented in [ENVIRONMENT.md](ENVIRONMENT.md).

A convention used throughout: `<sid>` is a session id **prefix** (any
unambiguous head of the full uuid); `--project P` names a project from
`helm projects`; `|`-separated capture arguments are one shell argument each
or one quoted string — helm splits on the pipes.

## map — discovery and the registry

### `helm home`
Print the resolved helm root (`~/.helm`, or `HELM_HOME`).

```console
$ helm home
/home/you/.helm
```

### `helm sync`
Run the auto-map across every harness (Claude Code, Codex, OpenCode), refresh
the registry, scaffold project homes. Additive — never deletes a known
project. This is the first command on any machine.

```console
$ helm sync
helm sync: 14 projects known (2 new, 12 refreshed)
  + myproject
```

### `helm projects [--all]`
The real project list, newest activity first: name, status, last activity,
session count, harnesses, path. `--all` includes shelf repos (on disk, no
observed agent activity) and retired projects.

```console
$ helm projects
myproject  active  today  61  claude+codex  ~/dev/myproject
```

### `helm show <project>`
One project's full JSON record — path, pointers (memory dir, recall scope),
per-harness session counts, lineage edges.

```console
$ helm show myproject
```

### `helm lineage [seed | add <src> <rel> <dst> [note...] | external <name> <path> [note...] | archive-report]`
The project family tree. Bare `helm lineage` renders it. Sub-verbs:

- `seed` — apply the shipped seed edges (idempotent; skips what's missing).
- `add <src> <rel> <dst> [note...]` — author an edge; recognized `<rel>`
  values: `descends-from | forked-from | checkout-of | composes | supersedes |
  launched-as` (free-form is accepted but unrendered relations don't anchor
  the tree).
- `external <name> <path> [note...]` — register a read-only external node
  (prior art you reference but don't own).
- `archive-report` — a ranked, **read-only** "safe to archive and why" report
  over dormant repos. Nothing is ever moved by this verb.

```console
$ helm lineage add myproject descends-from old-prototype "carried the parser"
edge: myproject -descends-from-> old-prototype
```

## knowledge — the typed store and its actuators

### `helm store <verb> [args] [--project P]`
The one typed knowledge store — beliefs, premises, terms, moves, references —
across every root (the adopted live store, the global chain, each project's
chain). The full sub-grammar:

```
helm store list [--type T] [--all]         entries (live; --all incl. retired)
helm store get <id>                        one entry, full record
helm store resolve <text>                  JIT lookup — what fires for this prompt
                                           (or pipe the prompt on stdin)
helm store pinned                          the always-on lane
helm store add <type> <id> | <statement> [| ...]
    prior:     <id> | <statement> [| conf [| keywords [| domain]]]   belief, default 0.6
    premise:   <id> | <statement> [| keywords [| domain]]            certain, conf 1.0
    lexicon:   <term> | <definition> [| kind [| ex1 || ex2]]
    heuristic: <id> | <move> [| trigger-csv [| domain]]
    reference: <id> | <summary> [| url [| keywords [| domain]]]
    flags: [--source S] [--rationale <text...>]    rationale seeds evidence_log
helm store evidence <ts> <id> <delta> <reason...>  move a belief (logged + clamped)
helm store supersede <ts> <old-id> <new-id> [reason]  TOMBSTONE old (file kept)
helm store retire <ts> <id> [why...]               retire (file kept as the record)
helm store counts                                  per-root type inventory
```

`<ts>` is a UTC timestamp (`2026-07-18T09:00:00Z` — `date -u +%Y-%m-%dT%H:%M:%SZ`).
Evidence clamps beliefs to [0.05, 0.99] — a belief never auto-reaches 1.0;
that rail is what makes premises human-only. A certain prior contradicted by
agent evidence keeps its confidence: the contradiction is logged and the drift
report surfaces it.

```console
$ helm store add prior prefer-small-prs | "small reviewable PRs land faster" | 0.7 | pr,review
$ helm store evidence 2026-07-18T09:00:00Z prefer-small-prs +0.1 "three clean merges this week"
```

### `helm inject [--project P] [--json]`
The active-fire surface: prompt text on stdin, injection-worthy context on
stdout — the pinned lane (budget-capped), just-in-time store matches, live
reflex steers. Empty on no match; salience is the scarce resource. This is the
one call a harness hook makes per turn — wiring per harness is in
[HOOKS.md](HOOKS.md).

```console
$ echo "how should we drain the memory backlog?" | helm inject --project myproject
TERM drain: routing raw memory intake to typed homes ...
```

### `helm drain [--apply] [--sweep-dups] [--limit N]`
Classify raw memory intake and route entries to their typed homes, archiving
sources with a reference back. **Dry-run by default** — nothing moves without
`--apply`, and the rollback net is verified non-empty first. Conflicts are
never auto-resolved.

```console
$ helm drain
helm drain plan (12 raw entries): ...
helm drain: DRY-RUN (nothing moved). Re-run with --apply.
```

### `helm drift [--project P] [--peek]`
Surface belief drift — contradictions, tier-crossings, decays — and only
that. Steady beliefs print one summary line. `--peek` reads without updating
the snapshot.

```console
$ helm drift
helm drift: no drift (48 priors steady).
```

### `helm reflex [list [--all] | add <id> | <steer> [--signal S] [--pattern RE] [--marker PATH] [--project P] | retire <id>]`
The (signal → steer) entries a harness delivers every matching turn. Signals:
`prompt` (regex on the turn text — the default; the pattern defaults to the
id as a word), `every-turn`, `marker-file --marker <path>` (fires while the
file exists). Retire is a one-line status flip; the file stays.

```console
$ helm reflex add checkpoint-green | "checkpoint the green slice before refactoring" --signal prompt --pattern "refactor"
```

### `helm whoami [note <text...> [--topic T] [--supersedes <note-name>]]`
The warmth leg: what your agents know about you — profile plus dated,
superseding notes. Bare `helm whoami` prints it; `note` grows it.

```console
$ helm whoami note "prefer terse commit messages" --topic style
```

### `helm interview [--questions] [--redo]`
The five-minute interview that fills the profile. Interactive on a tty;
`--questions` prints the sheet for offline answering; `--redo` revisits a
completed interview. Never nags — done latches it off.

### `helm premise <id> | <statement> [| keywords [| domain]] [--project P] [--no-attest]`
Capture a confidence-1.0 truth: written to the typed store (same shape as
`helm store add premise`) **and** attested — a signed digest committed to the
verifiable ledger when the substrate is up, queued for retry when it isn't.
`--no-attest` stores only. `helm premise --retry-queue` replays the queue.
The full substrate story is [ATTESTATION.md](ATTESTATION.md); helm works
fully without it.

```console
$ helm premise naming-extremes | "metaphors live at the extremes only"
helm premise: LIVE 'naming-extremes' [certain 1.00] - metaphors live at the extremes only
```

### `helm premise-check <id> [--project P]`
Re-verify an attested premise: recompute the digest from the stored statement,
compare to the attested payload, and quote the finality tier the ledger node
proves. Exit 0 on a digest match.

```console
$ helm premise-check naming-extremes
  digest: MATCH prem:b2b:9f2c...
  finality tier: attested-after-next-height (consensus_final at attested_height 43)
```

## sessions — every harness, one catalog

### `helm sessions [<project>] [--limit N] [--all] | helm sessions resume <sid>`
Every local session across every harness, newest first, grouped by real
project (decoded from inside transcripts, never from directory slugs).
`resume <sid>` prints the exact resume command — the harness's own CLI, no
wrapper. `--all` includes synthetic (pruned-copy) sessions.

```console
$ helm sessions myproject
$ helm sessions resume 3f2a
cd ~/dev/myproject && claude --resume 3f2a9c81-...
```

### `helm search <text> [--scope P] [--refs]`
Content search inside transcripts (full-text via the recall index when
present, scoped grep otherwise), work-vs-synthetic classified. `--refs`
includes reference-only hits.

```console
$ helm search "atomic write" --scope myproject
```

### `helm transcript <sid> [--find T] [--limit N]`
A readable, role-tagged window of one transcript, newest-last; `--find`
centers the window on the last match of a term.

### `helm rehome <sid> <new-cwd> | helm rehome <sid> --reset`
Re-home a session's working directory — metadata, never identity. For claude
sessions the file is symlinked into the new cwd's project-slug dir so
`claude --resume` resolves there. `--reset` undoes the override.

### `helm prune <sid> [--preset lean|window20k] [--dry]`
The resume studio: derive a new, smaller, still-resumable **copy** of a
session. The original is never mutated. `--dry` previews the savings.

### `helm capsule <sid>`
The session's git era: find the commit its repo was on at last activity and
print the exact worktree + resume commands to stand that era up. Prints
commands, mutates nothing.

### `helm cmd <sid> [--account A] [--model M]`
The account-aware pasteable resume command: the right harness CLI, the right
credential home, one paste.

```console
$ helm cmd 3f2a --account you@example.com
cd ~/dev/myproject && CLAUDE_CONFIG_DIR=~/.claude-homes/you-example-com claude --resume 3f2a9c81-...
```

## accounts — credentials and quota

### `helm creds`
The live account scorecard: every account across providers, with headroom,
credential state, reset window, and the use-it-or-lose-it weekly verdict.
Degrades to a one-line notice on machines with no quota provider — sessions
and resume still work.

### `helm swap <home-or-account>`
A seat ran dry mid-work: find its live sessions, pick the healthiest other
account of the same provider, and print the exact resume-under-that-account
block per session. helm prints; the human runs — a swap is a seat decision,
never an automatic mutation.

```console
$ helm swap you@example.com
```

### `helm homes [prepare <claude|codex> <email> | verify [<name>] | archive <name> | restore <name> | migrate <name> | archives] [--provider claude|codex]`
Credential-home lifecycle. Bare `helm homes` lists every home with identity,
liveness, and duplicate flags. helm prepares directories and verifies
structure; **you run every login** — helm never touches an auth flow.
Archive/restore are directory moves (archive-not-delete); live-agent homes
are refused.

```console
$ helm homes prepare claude you@example.com
helm homes: prepared claude home you-example-com
  login (YOU run this): CLAUDE_CONFIG_DIR=~/.claude-homes/you-example-com claude login
```

### `helm keepalive [--home NAME|PATH] [--early HOURS]`
Roll idle claude homes' OAuth tokens forward before their refresh chains rot —
the **one** credential-writing verb in helm, with every safety rule inherited:
rotation persists atomically, one live refresher machine-wide, owner-only file
modes from creation, refresh only when due (default `--early 24`). Codex homes
are read-only by design and surfaced as stale-risk instead. Every action is
logged to `~/.cache/helm/keepalive-log.jsonl`; token values appear nowhere.

## configs — every config, every home

### `helm configs [list | show <path> | cascade <cwd> [--harness claude|codex] [--home DIR] | edit <path> | backups | restore <backup-path>]`
The config model over every claude/codex home and project tree — MCPs, hooks,
skills, rules, memory files, settings. `list` groups every discovered config
file by scope; `show` prints one recognized file; `cascade` resolves what a
seat launched at `<cwd>` actually loads. `edit` (new content on stdin) is the
safety-first writer: backup → validate → atomic rename, recognized files under
allowlisted roots only — a bad write must never brick an agent's launch.
`restore` puts a backup back (validated, and re-backed-up first).

```console
$ helm configs cascade ~/dev/myproject --harness claude
$ helm configs edit ~/.claude/settings.json < settings.json
```

### `helm skills [dupes]`
Census of every skill across every home, read-only. `dupes` flags multi-homed
names (identical-everywhere = safe to consolidate vs diverged), same-content-
different-name copies, and hygiene issues (e.g. a skill dir with no SKILL.md).

## substrate — the attested-truth leg

### `helm cell <join|accept|send|recv|heartbeat|roster|status>`
The agent-to-agent substrate, helm-named: a thin wrapper over the ledger-node
binary with `HELM_*` env mapped in (see [ENVIRONMENT.md](ENVIRONMENT.md)).
`status` reports node liveness, chain head, identity profile, and roster;
the other verbs pass through to the binary. A missing binary is a graceful
"substrate unavailable" line, never a traceback — and nothing else in helm
depends on it. See [ATTESTATION.md](ATTESTATION.md).

```console
$ helm cell status
helm cell: node LIVE at http://127.0.0.1:8899 — chain head 43 ...
```

## ops — health, evolution, the browser

### `helm doctor`
Read-only health report over the whole estate: home layout, registry,
adoption, the adopted store, know-your-user, the recall index, env overrides
in effect. Exit 1 only on FAILs.

### `helm evolve`
One observe/propose cycle over the stores: what to drain, which beliefs need
evidence, what's missing. **Proposes, never mutates** — the anti-rulesurf
gate is constitutional (see [EVOLUTION.md](EVOLUTION.md)).

### `helm web [--port N] [--open]`
The same truth, warm, in a browser: four views (knowledge home, quota,
sessions, configs) served self-contained on `127.0.0.1:7433`. `--open`
launches your browser. Full surface, API table, and a systemd unit:
[WEB.md](WEB.md).

---

**A note on `helm seat`:** the multimodel-seat module (giving a non-Claude
model family the full claude-code harness through a local wire proxy) ships in
the codebase (`helm/seat.py`, with its `seat add|up|down|launch|smoke|list|
status|doctor` sub-grammar and tests) but is not yet wired into the
dispatcher — running `helm seat` today reports an unknown verb. It lands as a
top-level verb once its live proving rounds finish.
