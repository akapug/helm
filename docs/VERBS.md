# helm command reference

Every dispatcher verb, grouped the way you reach for them. Syntax lines match
the CLI's own `--help` (which is always the last word: `helm <verb> --help`
prints the one-liner; running a multi-action verb bare prints its sub-grammar).
Verbs never take a config file — behavior is shaped by `HELM_*` environment
variables only, documented in [ENVIRONMENT.md](ENVIRONMENT.md).

Conventions used throughout: `<sid>` is a session id **prefix** (any
unambiguous head of the full uuid); `--project P` names a project from
`helm projects`. Capture verbs take `|`-separated fields — **quote the whole
capture as one argument** (`helm premise "id | statement"`): helm joins its
args and splits on the pipes itself, and an unquoted `|` is a shell pipe.

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

`add` enforces **supersede-not-duplicate**: a same-id entry that is already
LIVE in scope is a hard refuse — never a silent overwrite — and the refusal
prints the exact `evidence`/`supersede` commands to run instead. A *different*
id whose statement is near-identical (token-set overlap ≥ 0.8) warns loudly,
names the other id and the supersede command, and proceeds — similarity alone
never blocks. Lexicon is exempt: redefining a term is its update lane.

`resolve` ranks JIT hits DF-weighted: each matched probe scores 1/df (df = how
many entries carry that keyword), summed and confidence-weighted — one rare
keyword outranks a pile of shared ones. Ties break most-recently-updated,
never alphabetical. `helm inject --explain` shows the per-probe contributions.

```console
$ helm store add prior "prefer-small-prs | small reviewable PRs land faster | 0.7 | pr,review"
$ helm store evidence 2026-07-18T09:00:00Z prefer-small-prs +0.1 "three clean merges this week"
```

### `helm inject [--project P] [--json] [--explain] [--hook-json]`
The active-fire surface: prompt text on stdin, injection-worthy context on
stdout — the pinned lane (budget-capped), just-in-time store matches, live
reflex steers. Empty on no match; salience is the scarce resource. This is the
one call a harness hook makes per turn — `helm hooks install` wires it;
per-harness recipes are in [HOOKS.md](HOOKS.md). `--hook-json` reads the
harness hook's full JSON on stdin instead (`prompt`/`cwd`/`session_id`),
derives `--project` from the cwd (longest registry-path prefix, global-only
fallback) and stamps the session onto the fire-ledger row; malformed JSON
injects nothing, rc 0 (fail-open). `--explain` shows what would fire and why
(plus the derived `[scope: …]`), writing no ledger row and mutating no state.

With a session, the habituation guard extends to the JIT lane: a fired entry
**cools down** for 15 turns of that session (state per session under
`_global/.state/inject-seen/`, stale files self-pruning) unless its score
jumps to ~2x its score at last fire — pinned and reflex lanes are exempt,
suppression happens pre-cap so freed slots reach the next candidates, and
suppressed ids ride the ledger row (`suppressed`). `--explain` renders a
cooled hit as `- id (cooldown, fired Nt ago)`. Plain stdin (no session) never
cools. inject is also the **coinage 3-strikes** recorder: quoted 1-3 word
phrases and hyphenated neologisms (code identifiers and file paths are
structurally excluded) are counted per distinct turn in
`_global/.state/coinages.json`; a term hitting 3 turns while missing the
lexicon gets ONE reflex-lane nudge — offer to define it via `helm coach` if
the owner is present, write a lexicon candidate if away — then latches
forever: one line per term, ever. Both are fully fail-open: any state trouble
means no cooldown and no nudge, never a blocked turn.

```console
$ echo "how should we drain the memory backlog?" | helm inject --project myproject
TERM drain: routing raw memory intake to typed homes ...
```

### `helm drain [--apply] [--sweep-dups] [--limit N] | --rekey [--apply]`
Classify raw memory intake and route entries to their typed homes, archiving
sources with a reference back. **Dry-run by default** — nothing moves without
`--apply`, and the rollback net is verified non-empty first. Conflicts are
never auto-resolved. Drained feedback becomes a prior with keywords derived
from the full statement's distinctive words (≥ 5 chars, non-generic,
frequency-then-length ranked, cap 8).

`--rekey` is the one-time migration for the already-drained cohort: every
store prior whose evidence log says "drained from feedback memory" gets its
keywords recomputed with that derivation, edited **in place** (atomic; every
untouched byte preserved) plus a `rekeyed` evidence receipt — which is also
the idempotence marker, so re-runs skip. Dry-run by default, counts reported.

```console
$ helm drain
helm drain plan (12 raw entries): ...
helm drain: DRY-RUN (nothing moved). Re-run with --apply.
$ helm drain --rekey
helm drain --rekey: 413 drained priors — 413 to rekey, 0 already rekeyed
...
helm drain --rekey: DRY-RUN (nothing written). Re-run with --apply.
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
$ helm reflex add "checkpoint-green | checkpoint the green slice before refactoring" --pattern "refactor"
```

### `helm record [--hook-json] | status [--session S] | install [--dry] [--home NAME]`
The session-keyed tool-outcome recorder — the sensing half of the behavior
leg. A `PostToolUse` hook pipes each tool event's full JSON in (claude first;
codex when its hook surface lands); helm keeps tiny per-session counters under
`_global/.state/reflex-state/<session_id>/` that dynamic reflexes, the
stuck-hook, mentor observe triggers, and evolve's behavior observers read:
`counters.json` (passive-streak; dirty-streak with cached last-dirty — `git
status` probed only on dirtying tools, in the tool's own workdir; a
conservative stuck-signal gated to action tools — reads carry error text as
*data*; a loop-thrash hash chain over recent commands) plus the two
verify-grounding artifacts: `command-log.jsonl` (test-runner invocations with
their **real** exit codes — token + digest, never the raw command line) and
`edit-targets.log` (basenames actually edited). Keyed by the payload's
`session_id`, never a pane — sessions are helm's key. A pure-Python argv verb
(no shell string ever interpolates tool content — the ancestor's
apostrophe-breaks-`python -c` silent-noop class is structurally dead).
Fail-open and silent by law: any payload, any trouble — record nothing, rc 0,
never block a turn; the passive hot path is one JSON read + one atomic write,
no subprocess. `install` merges the `PostToolUse` → `helm record --hook-json`
hook into every claude home on the same rails as `helm hooks install`
(merge-preserving, idempotent, backup → validate → atomic write, fail-open
`timeout` + `|| true`); `status` is the read-only wiring + per-session counter
table; doctor mirrors it (`record coverage: N of M claude homes`, state
freshness).

```console
$ helm record install
helm record: command: timeout 10 /path/to/helm/bin/helm record --hook-json || true
  you-example-com    add    backup: none — new file
helm record: 2 of 2 claude homes covered
$ helm record status
  wiring: 2 of 2 claude homes (PostToolUse)
  session        age   passive dirty stuck loop  cmds edits
  a1b2c3d4       3m    4       0     0     1     2    5
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
$ helm premise "naming-extremes | metaphors live at the extremes only"
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

### `helm hooks [install [--harness claude|codex] [--home NAME] [--dry] | status]`
The self-closing installer for the per-turn inject wiring. `install` merges
the `UserPromptSubmit` → `helm inject --hook-json` hook into every claude
home's `settings.json` (the default `~/.claude` included) — MERGE-preserving,
idempotent (an up-to-date entry reports `ok`), on the configs safety rails
(backup → validate → atomic write, backup restored on any failure). The
generated command is fail-open by construction (`timeout` + `|| true` — a
broken helm never blocks a turn). `--dry` prints the would-be diff per home;
`--home NAME` narrows to one. `status` is the read-only per-home coverage
table (hook present / helm resolvable / fail-open intact), mirrored by
doctor's `inject coverage: N of M claude homes` line. Codex is reported as
recipe-pending until [HOOKS.md](HOOKS.md) carries a mechanical shape.

```console
$ helm hooks install
helm hooks: command: timeout 10 /path/to/helm/bin/helm inject --hook-json || true
  you-example-com    add    backup: none — new file
helm hooks: 2 of 2 claude homes covered
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

## chat — the human-included groupchat

### `helm chat [post <text...> | read [--since N] [--follow] | rooms | react <n> <emoji> | log-flush | node up|down|status] [--room R]`
One shared conversation log + notify + read/write loop, **owner in the room**.
Rooms live in RAM (`/dev/shm/helm-chat/<room>.jsonl`, dir 0700, default room
`main`; `HELM_CHAT_DIR` overrides) — ephemeral presence-chat, not the durable
record: past ~2 MB the oldest half rotates out, so `/premise` anything that
must outlive the room. `post` writes as `$HELM_CHAT_NAME` (else the best local
identity guess: session, then user). `read` prints the room (`--since N` skips
the first N messages); `read --follow` polls and prints new lines until Ctrl-C.

**The signed transport (v2).** When the chat **room node** answers — a dregg
cave whose data-dir lives on tmpfs, so a chat turn never lands on a
disk-persisted chain — every post also rides a **signed self-write turn** on
the poster's cell there: the turn payload carries the message digest
(`chat:b2b:<blake2b-256>`), the RAM room carries the text (thin claim, fat
corroboration — the same pattern as premise attestation, see
[ATTESTATION.md](ATTESTATION.md)). Signed rows render clean (the web panel
shows a subtle ✓ tick, chain index on hover); node down → the v1 path
automatically, tagged `[unsigned]` — the message never dies, the signature is
what degrades. Agents sign as `HELM_CELL_PROFILE` (else `meld-agent`); the
owner's web posts sign server-side as `david`. `helm chat node up` provisions
the room node (`helm-chat-cave.service`, `dregg-cave-node` on
`/dev/shm/helm-chat-cave`, port 8898, faucet ON — the node auto-funds joining
cells and helm tops up before each turn: **chat turns never die on
computrons**). The transport is node-agnostic (`HELM_CHAT_NODE_URL`; empty
disables) — the ONE-CAVE unification (`scripts/cave-unification.sh`) just
repoints it.

**The log-after leg.** RAM stays pure in the hot path (premise
`a2a-ram-only-disk-log-after`): the ONLY disk writer is `helm chat
log-flush`, an out-of-band append of delivered history to
`~/.helm/helm/journal/chat-<date>.log` — idempotent (per-room high-water
mark; a rotation gap is logged loudly), run by the operator, a cron, or
`helm --human` at exit. `HELM_CHAT_LOG=0` disables it.

**Emojis + reactions.** `:fire:` → 🔥 at post time on every surface (CLI,
web input, TUI input — ~140 shortcodes, `helm/emoji.py`). `helm chat react
<n> :tada:` attaches a reaction to message *n* (1-based; `-1` = latest),
rendered inline (`🎉×2`) in the web panel and TUI; the web panel gets
click-to-react on hover. Reactions ride the same transport as posts (signed
turns on v2). Nothing gates emojis to humans — **agents are encouraged to
emoji and react like anyone else in the room**.

The notify loop: when the owner posts (web panel or `helm --human`), helm
drops a `<room>.owner-unread` marker and the shipped `owner-chat-unread`
reflex steers every local agent's **next turn** to read and reply — any
`helm chat read` that consumes past the owner's post clears it. Agents never
poll; the already-installed inject hooks deliver the nudge. Identical in
both transports.

```console
$ helm chat post "seat B: web slice landed :rocket:"
$ helm chat react -1 :tada:
$ helm chat read --since 40
$ helm chat read --follow        # the owner's orca pane sidecar, exactly this
$ helm chat node status          # room node + chain head + cell balances
$ helm chat log-flush            # the durable record, out-of-band
```

**Chat path (the AX↔UX law — the owner never types the CLI).** Say it and
your agent runs it:

- "**tell the fleet:** …" / "**post in helm chat:** …" → `helm chat post "…"`
- "**any chat for me?**" / "**read the room**" → `helm chat read`
- "**watch the chat**" (in an orca pane) → `helm chat read --follow`
- "**that chat point about X — keep it**" → `/premise` (chat is ephemeral;
  the store is the record)

The owner's own surfaces are the web panel (**chat** tab in `helm web`) and
`helm --human` in a terminal: type there and every local agent sees it next
turn — see [WEB.md](WEB.md).

### `helm --human` (alias `helm human`)
The operator's TUI — stdlib curses, runs beautifully as an orca pane. v1
panes: the chat room (scrollback with reactions inline + live follow + an
input line posting **as you**, dropping the owner-unread marker) and a
one-line status strip: transport `SIGNED #<chain-head>` / `UNSIGNED`, and the
quota headline from **cached** creds observations only (never a probe — no
cache says `run helm creds`). Input: Enter posts, `:shortcodes:` expand,
`/react <n> <emoji>` reacts, `q` on an empty line or Ctrl-C exits clean; exit
runs the log-after flush unless `HELM_CHAT_LOG=0`. Built as panes so
brief/sessions views bolt on later.

## ops — health, evolution, the browser

### `helm brief [--hours N] [--json]`

The operator's morning brief, composed from what the estate already knows —
read-only everywhere, never probes the network. Four sections, each omitted
when empty: **SINCE YOU LEFT** (session activity in the window, bucketed by
project; `--hours`, default 12), **KNOWLEDGE DELTA** (store entries
added/updated/retired + drain receipts + inject-ledger turn stats),
**SEATS** (the freshest cached quota observation per account — no cache says
`quota: run helm creds`), **WAITING ON YOU** (owner-gated items the estate
already records: the interview, queued attestations, stale project pointers,
prem/prior duplicates). `--json` prints the raw dict.

```console
$ helm brief
helm brief — 2026-07-19T07:27:15Z (last 12h)

SINCE YOU LEFT — 7 sessions, 4 projects
  helm                   3  Review Helm 0.1-alpha release verification
```

### `helm doctor`
Read-only health report over the whole estate: home layout, registry,
adoption, the adopted store, know-your-user, the recall index, env overrides
in effect. Exit 1 only on FAILs.

### `helm evolve`
One observe/propose cycle over the stores: what to drain, which beliefs need
evidence, what's missing. **Proposes, never mutates** — the anti-rulesurf
gate is constitutional (see [EVOLUTION.md](EVOLUTION.md)).

### `helm web [--port N] [--open]`
The same truth, warm, in a browser: five views (knowledge home, quota,
sessions, configs, chat) served self-contained on `127.0.0.1:7433`. `--open`
launches your browser. Full surface, API table, and a systemd unit:
[WEB.md](WEB.md).

---

**A note on `helm seat`:** the multimodel-seat verb (giving a non-Claude
model family the full claude-code harness through a local wire proxy —
`seat add|up|down|launch|smoke|list|status|doctor`) is wired into the
dispatcher; see [MULTIMODEL_SEATS_ADDENDUM.md](MULTIMODEL_SEATS_ADDENDUM.md)
while its live proving rounds finish.
