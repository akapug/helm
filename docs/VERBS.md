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

### `helm projections [--json]`
The projection registry — constitution laws 2+3 as an executable manifest.
Every on-disk store helm reads or writes (both roots: the helm home and
`~/.cache/helm`), classified `authored | projection | events | state |
backup`, with each **projection** naming its authoritative source and its
rebuild command — a projection that cannot name its rebuild cannot be safely
wiped or gitignored. Any file no row names is an unclassified **squatter**.
Read surface only; `helm doctor` enforces (undeclared rebuild/source = FAIL,
orphaned projection = FAIL, declared staleness = WARN, squatters = WARN).

```console
$ helm projections
helm projections (27 rows over /home/you/.helm + /home/you/.cache/helm):
  registry          projection    1 file   <- harness session stores + disk repo scan | rebuild: helm sync
  catalog-cache     projection    1 file   <- local claude/codex transcripts (cv ls, or the scanner) | rebuild: helm sessions
  cwd-overrides     authored      1 file
  ...
  no unclassified squatters
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
helm store list [--type T] [--all] [--candidates]
                                           entries (live; --all incl. retired;
                                           --candidates = inferred captures only)
helm store get <id>                        one entry, full record
helm store resolve <text>                  JIT lookup — what fires for this prompt
                                           (or pipe the prompt on stdin)
helm store confirm <id> [--edit <def...>]  promote a candidate -> live (lexicon v1)
helm store pinned [--stats]                the always-on lane (--stats: budget
                                           walk + ledger made-it/starved counts)
helm store add <type> <id> | <statement> [| ...]
    prior:     <id> | <statement> [| conf [| keywords [| domain]]]   belief, default 0.6
    premise:   <id> | <statement> [| keywords [| domain]]            certain, conf 1.0
    lexicon:   <term> | <definition> [| kind [| ex1 || ex2]]
    heuristic: <id> | <move> [| trigger-csv [| domain]]
    reference: <id> | <summary> [| url [| keywords [| domain]]]
    flags: [--source S] [--rationale <text...>] [--candidate]
           --candidate (lexicon v1): SAFE inferred capture — writes a non-live
           candidate EXCLUDED from inject/resolve until `confirm`ed
helm store evidence <ts> <id> <delta> <reason...>  move a belief (logged + clamped)
helm store supersede <ts> <old-id> <new-id> [reason]  TOMBSTONE old (file kept)
helm store retire <ts> <id> [why...]               retire (file kept as the record)
helm store demote <id> [--undo] <reason...>        flip always->jit with a receipt
                                                   (--undo = provenanced restore)
helm store events [--limit N]                      the mutation-receipt trail
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

**Candidate tier (safe inferred capture).** An agent-inferred entry lands as
`status:candidate` (v1: lexicon; `add lexicon ... --candidate`, `source:
inferred`). A candidate is a *non-live* status, so the resolver's live-filter
already EXCLUDES it from resolve / pinned / inject — the hard law: nothing
inferred is ever silently authoritative. `list --candidates` surfaces them (and
coach's dup-search reads `store.candidates()`); `helm store confirm <id>
[--edit <def...>]` promotes candidate → live (`source:explicit`) with an events
receipt. Decay is operator-visible, never a silent job: `helm drain
--expire-candidates [--days N] [--apply]` archives-then-prunes unconfirmed
candidates older than N days (14 default; a no-timestamp candidate never
expires; dry-run default; net + receipt).

**Adopted project roots.** With `--project P`, the store also reads P's OWN
claude memory dir(s) as `adopted-project` roots (canonical cwd + observed
worktrees, collapsed to one project, registry-resolved + mtime-cached). Shadow
order: `project > adopted-project > helm-global > adopted` — so `--project P`
fires P's own priors, and the authored `~/.helm/P` layer still overrides raw
adopted content.

Every mutating verb leaves a **receipt**: one `{v, ts, actor, verb, target,
summary}` line appended to `_global/.state/events.jsonl` through one pk-level
chokepoint (`pk.event`) — `add`/`evidence`/`supersede`/`retire`/`demote`, and
drain `--apply`/`--rekey`. Receipts, never truth: the files stay source and
nothing may read the journal over them (a hand-edit is legal; it just has no
receipt). Fire-ledger laws apply — O(1) append, 5MB one-generation rotation
(`.1`), fail-open (journal trouble never fails the write it describes). The
actor is `$HELM_ACTOR`, else `$CLAUDE_SESSION_ID` (the hook session), else
`cli`. `helm store events [--limit N]` renders the recent trail. The `.state/`
home is deliberate: the journal is host-local telemetry and **never ships**
(the authored/derived split) — a rotating trail is not a durable record;
durable provenance rides in the artifacts themselves (evidence logs, attest
keys, tombstones).

The pinned lane injects under a byte budget, so membership is not delivery.
The budget walk is deterministic and **earned**: confidence desc, then recency
(mixed ISO/epoch timestamps normalized; a blank timestamp ranks last), then id
— the old alphabetical walk silently starved 8 of 11 live always-entries.
`helm store pinned --stats` renders the walk exactly as inject would (who fits
under the budget now) plus each entry's made-the-budget count over the
fire-ledger window, and names every starved entry. `helm store demote <id>
<reason...>` flips always→jit with an evidence receipt carrying the exact
prior state (`was: {load_class, pin}`) — never silent, never a delete;
`--undo` is the one provenanced flip back. An explicit `pin: false` beats the
founding-pin tuple, which is what makes a demote stick.

```console
$ helm store add prior "prefer-small-prs | small reviewable PRs land faster | 0.7 | pr,review"
$ helm store evidence 2026-07-18T09:00:00Z prefer-small-prs +0.1 "three clean merges this week"
```

### `helm inject [--project P] [--json] [--explain] [--hook-json] [--compare-report]`
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

On the **first turn of a calendar day** (session-bearing, non-empty prompt), the
output leads with a one-line **brief digest** under a distinct `BRIEF:` prefix —
sessions since you left, the knowledge delta, the count of owner gates — so the
day's first agent turn surfaces "since you left" and relays it warmly in its own
voice. It is latched once per day in `_global/.state/greeted.json`: the hot-path
check is one small-JSON day-compare (O(1)), so only that first turn pays a brief
compose — every later turn is silent. Seats are omitted (standing state, not
news); the line points at `helm brief` for the full detail on demand. Day
rollover re-greets. Fully fail-open: brief unavailable → no whisper, never a
blocked hook; the latch is stamped on the first attempt, so a bad brief costs
the day's greeting, not a per-turn read. `--explain` renders it read-only.

**Pluggable comparison resolver** (the [ARCHITECTURE.md](ARCHITECTURE.md)
Pluggability seam): the local keyword JIT resolver is the AUTHORITY; a
registered COMPARISON backend runs in parallel and is logged/compared, never
trusted. It is **OFF by default** — no `HELM_CF_ENDPOINT` ⇒ zero cost (one env
read). Set the endpoint (the owner one-step; see [ENVIRONMENT.md](ENVIRONMENT.md))
and each turn appends one divergence row (local-only vs compare-only vs agreed,
ids never prompt text) to `_global/.state/compare-ledger.jsonl`.
`helm inject --compare-report` renders the accumulated verdict — or `comparison
backend off (set HELM_CF_ENDPOINT)` when unconfigured — so the owner judges a
real backend (Cloudflare agentic-memory) on evidence, not a vibe. Fail-open (a
comparison error never touches the local lane or blocks the turn) and the local
output is byte-identical whether the comparison is on or off. The connector is
a thin stdlib-`urllib` stub: helm never invents credentials and never calls a
real endpoint in tests.

```console
$ echo "how should we drain the memory backlog?" | helm inject --project myproject
TERM drain: routing raw memory intake to typed homes ...

$ helm inject --compare-report
comparison backend off (set HELM_CF_ENDPOINT). No comparison backend is configured ...
```

### `helm drain [--apply] [--sweep-dups] [--limit N] [--project P] | --rekey [--apply] | --expire-candidates [--days N] [--apply]`
Classify raw memory intake and route entries to their typed homes, archiving
sources with a reference back. **Dry-run by default** — nothing moves without
`--apply`, and the rollback net is verified non-empty first. Conflicts are
never auto-resolved. Drained feedback becomes a prior with keywords derived
from the full statement's distinctive words (≥ 5 chars, non-generic,
frequency-then-length ranked, cap 8).

`--project P` drains P's OWN claude memory dir (the adopted per-project pile)
with the identical classify/apply gauntlet — including the upgrade op below. A
registry **alias map** (built-in `buildr → buildr-private-beta`, `mc →
mission-control`, plus any authored per-project `aliases`) routes the otherwise
unroutable project entries; a short alias (`mc`) matches by filename only (a
2-char word wallpapers a description scan).

**The upgrade op (drain v2).** Typed-PREFIX files that never carried typed
fields — `prem-`/`prior-`/`lex-` bulk that falls back to episodic (visible,
never injected) — are upgraded in place to real typed entries: `lex-` →
lexicon; `prem-`/`prior-` → a prior at **0.9 jit, NOT 1.0** (a `prem-` prefix
on bulk memory is a naming accident, not an attestation — the premise tier
needs the ledger path). id from the filename, statement from the description,
keywords via the same derivation, body verbatim. Upgrade fires only when **≥ 2
specific keywords** survive; terser files stay episodic and are reported. All
the normal machinery applies (dry-run, verified net, receipt, MEMORY.md
re-pointing); `--sweep-dups` still carries the twin sweeps in the same pass.

`--expire-candidates [--days N]` prunes unconfirmed store candidates older than
N days (14 default) — the operator-visible candidate decay leg (archive-first
net + receipt; a no-timestamp candidate never expires).

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

### `helm promote [--since Nd] [--cap N] [--apply]`
The episodic → durable funnel: the mouth that moves knowledge from the "did"
leg (thousands of transcripts) into "believes/means". Scans recent transcript
jsonl for durable-knowledge markers (`from now on`, `remember this`, `always`,
`never`, `the rule is`, `going forward`, `make it a rule`) in **REAL
USER-typed text only** — tool_result blocks and assistant turns never count,
and a length guard drops long task prompts. Each hit becomes a drain-intake
CANDIDATE file (`feedback-promoted-*`, `type:feedback`) carrying
`{origin_session, origin_line, proposed_type, capture_confidence}` — **never
directly into the typed store**; the existing drain `classify → dry-run →
apply` gauntlet gates it (routes feedback → prior). Precision is load-bearing:
USER-role only + length guard + per-run cap + dedupe (slug + token-overlap vs
existing priors and already-written intake) + an incremental `(mtime,size)`
scan cache. **Dry-run by default**; `--apply` writes the intake candidates,
then `helm drain` reviews and `helm drain --apply` lands them.

```console
$ helm promote --since 3d
helm promote: scanned 278 files (0 cached-skip), 34 promotion candidates found
  + codex-final-blackboard-delivery [prior c=0.50] (b8477e64 L34576): Codex delivery rule: always end ...
helm promote: DRY-RUN (no intake files written). Re-run with --apply, then `helm drain` to route them.
```

### `helm index cap [--budget-lines N] [--apply]`
The MEMORY.md budget actuator (memGC's missing half). MEMORY.md is injected
natively at every SessionStart and post-compact and nothing evicts it, so it
regrows past budget. `index cap` DEMOTES (unlinks) index link lines whose
backing file is a typed jit-resolvable entry — the safety rail: the content
stays reachable through inject, so unlinking is **provably lossless** — ranked
oldest `last_updated` first, until under the line budget (60 default). It
**never** demotes an always-class line (pinned) or an untyped/dormant line
(unreachable via inject — run `helm drain` v2 first so more lines gain a typed
backing; sequencing is the point). Demoted lines archive to a drain-style net +
receipt; **dry-run by default**; on `--apply` MEMORY.md is re-read fresh and
atomically rewritten so a concurrent native append survives. The self-firing
half is one documented **Stop-hook line** — `helm index cap --apply` — so the
write path structurally cannot exceed budget.

```console
$ helm index cap
helm index cap: MEMORY.md 65 lines / 19402B (budget 60 lines, 5 over); 36 demotable (typed jit-resolvable — lossless)
  - - [payload frugality](prior-payload-frugality.md) — ...  [prior-payload-frugality.md stays live via inject]
helm index cap: DRY-RUN (MEMORY.md untouched). Re-run with --apply.
```

### `helm sweep [--apply] [--project P]`
A lineage-driven supersession sweep of the adopted store. The store feeds
inject, and a corpus whose majority carries superseded premises fires stale
knowledge into live turns — so sweep walks the family tree's succession edges
(`supersedes`, `descends-from`, `forked-from`; a `checkout-of` worktree
duplicate is not a succession and is excluded) and proposes superseding the
ancestor-era entries the store still holds by their successor-era twins (the
sesh-era premise superseded by its helm successor).

**Never on weak signal.** A proposal needs BOTH a directed lineage edge AND a
match between an ancestor-era entry and a successor-era one: *same-slug* (equal
base after stripping the era token — `sesh-rollover` / `helm-rollover` →
`rollover`) or *high-overlap* (keyword/statement Jaccard ≥ 0.6). Term-mention
alone never qualifies — an entry that merely *mentions* a dead harness while
stating a live lesson is not superseded, which is why the dry-run quotes both
statements as evidence and emits a per-entry command to veto.

**Dry-run by default** — nothing is superseded without `--apply`. Each line
prints the signal, the old → new pair, the edge that justifies it, the quoted
before/after evidence, and the exact `helm store supersede` command. `--apply`
runs each through `store.mark_superseded` (the public API): old is tombstoned
`delete_eligible` with a backpointer, the **file stays** (never deleted,
re-promotable), and a receipt lands on the events journal. Fail-open: a missing
registry or unreadable store yields a clean line, never a crash.

```console
$ helm sweep
helm sweep — 1 lineage supersession proposed across 1 edge (PROPOSE-ONLY; nothing superseded):
  [same-slug] sesh-rollover-policy -> helm-rollover-policy   (helm supersedes sesh)
      old: "on rollover, re-home the sesh session under the freshest account"
      new: "on rollover, helm re-homes the session under the freshest account"
      ->  helm store supersede <ts> sesh-rollover-policy helm-rollover-policy lineage: helm supersedes sesh + same base slug 'rollover-policy'
helm sweep: DRY-RUN (nothing superseded). Re-run with --apply to tombstone via store.supersede (files KEPT — re-promotable).
```

### `helm drift [--project P] [--peek]`
Surface belief drift — contradictions, tier-crossings, decays, and evolutions
— and only that. A superseded premise reports `EVOLVED`, exactly once:
*attested chain* when its native supersession link verifies (offline; the
biography is one `helm premise-check --chain` away), *unbacked* when the
supersession is store-only. Steady beliefs print one summary line. `--peek`
reads without updating the snapshot.

```console
$ helm drift
helm drift: no drift (48 priors steady).
```

### `helm reflex [list [--all] | add <id> | <steer> [--signal S] [--pattern RE] [--marker PATH] [--counter NAME --threshold N [--latch] [--escalate N] [--window S]] [--project P] | retire <id> | smoke [--session S] [--project P]]`
The (signal → steer) entries a harness delivers every matching turn. Signals:
`prompt` (regex on the turn text — the default; the pattern defaults to the
id as a word), `every-turn`, `marker-file --marker <path>` (fires while the
file exists). Retire is a one-line status flip; the file stays.

**Counter/latch signals (v2)** turn `helm record`'s per-session counters into
dynamic reflexes — every threshold and steer is an editable store entry, not
hardcoded Python. The named signals map a counter to a field-tested default:
`stalled` (passive-streak ≥ 6), `thrash` (loop-streak ≥ 3), `drift`
(dirty-streak ≥ 8), `stuck` (stuck-streak ≥ 3, escalating); generic `counter`
reads any counter via `--counter NAME --threshold N`. `--latch` fires ONCE per
episode (the latch persists in the session's `reflex-state/<sid>/latch.json`
and clears on the inverse event — a forward op, a commit, a recovery);
`--escalate N` re-fires as the streak worsens. An optional `--pattern` gates on
the **prompt text only** (fixed-text law), `--window S` on the counter's
freshness. Fail-open: no threaded `session_id` (or missing counters) → every
counter signal is silently absent (v1 degrade), never a crash. Specificity law:
a counter reflex with no counter or threshold ≤ 0 is skipped, never wallpaper.
A fresh session re-arms every latch — correct, and by design.

`smoke` is the LIVE read-only check: the real `helm record` counters, which
counter reflexes fire against them, and each one's idle/armed/FIRE state —
mutating no latch. The seeded pack ships `stalled-driver`, `loop-thrash`,
`uncommitted-drift`, and `stuck-commonsense`, all latched, editable like any
reflex.

```console
$ helm reflex add "checkpoint-green | checkpoint the green slice before refactoring" --pattern "refactor"
$ helm reflex add "spin-guard | stop retrying; read the error" --signal stuck --threshold 3 --latch
$ helm reflex smoke
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

### `helm mentor [observe <project> [--since 7d] | teach <project> "<id> | <steer>" [--signal S] [--pattern RE] [--marker PATH] [--teacher NAME] [--attest] | review <project> | log [--project P]]`
The inception actuator — observe / teach / review over a junior, where the
junior is a **project/seat** (v1 same-home): a taught reflex in
`<project>/reflexes/` reaches every future session of every harness through
the inject hooks already installed, so delivery is free and teaching is the
only new verb.

`observe` is the ranked critique brief, **read-only**: the window's sessions
(the catalog lens, all harnesses) joined with the recorder's per-session
counters (stuck / loop-thrash / passive / uncommitted streaks), their
transcript tails scanned for **lexicon-seeded bug-class terms** — literal
needles only, seeded exclusively from lexicon entries of kind `bug-class`
(`helm store add lexicon "<term> | <definition> | bug-class"`), never open
inference — plus what the estate already observes (drift findings read
snapshot-free, evolve's fire-ledger and reflex observers). Deterministic
signals supply the eyes and hands; the senior **model** does the judging:
every miss-pattern mints a paste-ready `teach` command, nothing is ever
auto-taught.

`teach` (the incept) is the **one writing subverb**: one reflex file into the
target project's `reflexes/`, written by the reflex byte-shape owner and then
annotated in place with `teacher`/`target`/`stated_ts` provenance, plus one
events-journal receipt. A same-id file in the project is a hard refuse
(supersede-not-duplicate), an unknown project is refused (a taught reflex
must have a reachable junior). `--attest` records the incept into the **native
attestation chain**: one record carrying `ment:b2b:<blake2b-256>` over the
canonical incept text `<target>/<id> | <steer> | teacher: <name>` — teaching
provenance provable, `premise-check`-style, offline. The native record always
lands; only an OPTIONAL dregg anchor is best-effort. `helm mentor teach
<project> <id> --attest` (no steer) backfills the record later if the primitive
was interrupted.

`review` re-runs observe scoped per taught reflex: fires since its
`stated_ts` off the inject fire-ledger (**did it even fire?**) and the
id-term's transcript recurrence before → after (session-granular mentions,
the teaching session included). The ledger logs fires, not heeds; mentions
are not outcomes — the report says so. `log` is who taught what, when,
fired-since, retired/attested marked; hand-authored reflexes stay invisible
(no teacher, not taught).

**Writer gate (loud in `--help`):** teach writes ONE file in THIS helm home —
`<project>/reflexes/reflex-<id>.md` — and nothing else, ever; observe /
review / log write nothing (no drift snapshot consumed, no registry sync, no
ledger row). v1 is same-home; cross-operator teach is out of scope until
packs exist. Fail-open throughout: a dead catalog, torn counters, or an
absent ledger degrades to an empty section, never a crash.

```console
$ helm mentor observe myproject
helm mentor observe myproject — last 7d: 5 sessions scanned:
  MISS-PATTERNS — bug-class terms in the window's transcripts:
    identified-fix-filed-not-fixed       2 sessions, 3 mentions
      teach: helm mentor teach myproject "identified-fix-filed-not-fixed | <steer>" --pattern "identified\-fix\-filed\-not\-fixed"
$ helm mentor teach myproject "identified-fix-filed-not-fixed | an identified fix lands in-pass, never a TODO" --attest
helm mentor: TAUGHT 'identified-fix-filed-not-fixed' -> myproject (prompt) by david
$ helm mentor review myproject
  identified-fix-filed-not-fixed  taught 2026-07-19T09:00:00Z by david [attested]
    fired since taught: 3 turns (inject ledger)
```

### `helm whoami [note <text...> [--topic T] [--supersedes <note-name>]]`
The know-your-user leg: what your agents know about you — profile plus dated,
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
`helm store add premise`) **and** attested into the **native, offline hash
chain** — the primary, tamper-evident proof, which always lands with no binary
and no node. If a dregg node is reachable, the record hash is also posted as an
OPTIONAL external anchor (fail-open; honestly labelled a node commitment, never
a user-cell signature); when no node answers the anchor is queued.
`--no-attest` stores only. `helm premise --retry-queue` re-attempts pending
anchors. The full story is [ATTESTATION.md](ATTESTATION.md).

```console
$ helm premise "naming-extremes | metaphors live at the extremes only"
helm premise: LIVE 'naming-extremes' [certain 1.00] - metaphors live at the extremes only
```

### `helm premise --supersede <old-id> <new-id> | <statement> [| keywords [| domain]]`
Evolve the chain instead of editing in place: captures the NEW premise,
tombstones the old one through the store's own lifecycle (`replaced_by` /
`supersedes`, file kept), and appends ONE native `op: supersede` record whose
`supersedes_record` links to the old premise's record hash (mirrored on the new
entry as `attest_supersedes_record`). All three legs land offline. Re-stating a
LIVE attested premise with a different statement is refused toward this verb —
an in-place edit would orphan the attestation.

```console
$ helm premise --supersede naming-extremes "naming-poles | metaphors live at the poles"
helm premise: LIVE 'naming-poles' [certain 1.00] - metaphors live at the poles
  supersedes 'naming-extremes' — tombstoned (delete_eligible, file kept)
  attested (native): record 4d8a4c1306028e80 at chain_index 7 — recorded by 'david'
  chain: -> prior record 29114915ece8b4c2 (attest_supersedes_record)
```

### `helm premise-check <id> [--chain] [--project P]`
Re-verify an attested premise, honestly tiered: recompute the digest from the
stored statement (payload binding), verify the **native chain** record
recomputes and links to its predecessor (the primary proof), and — if an
external anchor was taken — report whether a reachable dregg node still shows it
(CONFIRMED / unverified / none). Exit 0 when the digest matches AND the native
record verifies; the anchor never fails the check. `--chain` walks the whole
supersession chain through any link — every digest, every record, every hop
linkage re-verified — and prints the attested biography ("held X until T, then
Y").

```console
$ helm premise-check naming-poles
  digest: MATCH prem:b2b:9f2c...
  native chain: VERIFIED — record 4d8a4c130602 at index 7 links to 29114915ece8…
  external anchor: none (native-only)
$ helm premise-check --chain naming-poles
  1. naming-extremes [delete_eligible] - metaphors live at the extremes only
  2. naming-poles [live] - metaphors live at the poles
       native chain VERIFIED — record 4d8a4c130602 at index 7 links to 29114915ece8…
       link 1->2 ATTESTED — native record linkage verified (29114915ece8 -> 4d8a4c130602)
  biography:
    held 'metaphors live at the extremes only' until 2026-07-19T..., then 'metaphors live at the poles' — LIVE now
```

### `helm coach <lesson...> [--apply] [--as L] [--id ID] [--project P] [--supersede OLD] [--json]`
The capture front door — the chat-path partner to `helm premise`. The owner (or
an agent) says a lesson in plain words and coach PLACES it right instead of
letting the store accrete duplicates. It is `/premise` for the whole store:
speak, and coach routes to the correct layer. The intake discipline that was
trapped in a claude-only command file, now a CLI every harness shares.

A mechanical **4-step GATE** (deterministic shape rules, no model call — the
routing is identical on every host and family):

1. **reframe** — echo the lesson's intent back.
2. **place** — shape rules pick the layer (same spirit as `drain.classify`):
   `X = Y` / `define X` → **lexicon**; a certain standing truth (always/never/
   must, no hedge) → **premise**; a hedged claim (probably/usually/might) →
   **prior** with confidence scaled to the hedge; a fires-unbidden `when X, do
   Y` → **reflex** (a concrete marker path picks a marker-file signal, else a
   prompt regex over the trigger's distinctive words); a procedure/how-to/
   numbered steps → a **skill/hook pointer** (not the typed store).
3. **no-cruft** — `resolve` + fuzzy token search across the store **including
   retired/tombstoned** entries; near matches ranked, each live near-dup
   carrying a ready-to-run `helm store supersede` command. Upgrade-in-place
   beats a new near-duplicate.
4. **simplify** — flag what the new entry could retire.

Propose-only by default: it prints the routing, the related entries, and the
**exact one-paste landing verb**. `--apply` lands it through that layer's own
verb (`helm premise` / `helm store add` / `helm reflex add`), so every store
guard runs — coach composes resolve + add, it never re-implements them. A
low-confidence route (no clear shape) is never guessed at on `--apply`: it
drops into the drain intake dir as a `feedback-*.md` entry, lossless, for `helm
drain` to route later. `--as` forces the layer, `--id` the id, `--supersede
OLD` lands-then-tombstones in one step (the upgrade path), `--json` emits the
plan. The lesson may also pipe on stdin (the voice/chat path). Fail-open
throughout; framing is neutral proof-engineering.

```console
$ helm coach "always scrub internal planning docs before any push"
helm coach: reframed -> "always scrub internal planning docs before any push"
  place:    premise   [id: scrub-planning-before-push]  - certain standing truth (signal 'always', no hedge)
  no-cruft: 1 related entry (resolve + fuzzy, incl. retired/tombstoned; 1 near-dup):
      - push-scrub-first [prior 0.80] 62% overlap
        upgrade in place:  helm store supersede 2026-07-19T.. push-scrub-first scrub-planning-before-push <reason>
  land it:  helm premise "scrub-planning-before-push | always scrub internal planning docs before any push"
  (propose-only - re-run with --apply to land, or paste the verb above)

$ helm coach --apply "the prefilter might be faster than the regex scan"
helm store: LIVE 'prefilter-faster-regex-scan' [prior 0.50] - the prefilter might be faster than the regex scan
coached -> prior (prefilter-faster-regex-scan) | new
```

A thin `~/.claude/commands/coach.md` wrapper (one line: `helm coach "$ARGUMENTS"`)
gives claude a `/coach` slash-verb; codex/opencode/bare terminal use the CLI
directly — same front door, no per-harness lane.

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

## corpus — the training-data archive

### `helm corpus backup [--dry] [--dest DIR] | helm corpus status`
The transcripts-are-training-corpus premise made executable: **copy** every
local session, subagent, and workflow transcript into a dated archive —
`~/corpus-archive/<date>/` by default, `HELM_CORPUS_DEST` (or `--dest`)
points it at a bigger local mount. Sources covered: `~/.claude/projects`
(top-level sessions **plus** the `<sid>/subagents/**` agent and workflow
transcripts the session catalog doesn't list), every `~/.claude-homes/*/projects`
(inode-deduped — symlinked homes cost nothing), `~/.codex/sessions` +
`~/.codex-homes` rollouts, and transcript-shaped subtrees under `/tmp/claude-*`
(the reboot-ephemeral ones). Extra roots ride the catalog's own
`HELM_CLAUDE_ROOTS`/`HELM_CODEX_ROOTS`.

Copy-only by law — no code path moves, rewrites, or deletes a source, and the
archive is append-only too (a grown transcript re-copies into today's dir;
older snapshots stay). Incremental like the catalog cache: a manifest under
`<dest>/.helm-corpus/` skips unchanged files by (size, mtime_ns), so re-runs
cost stats, not bytes; mtime-only churn is hash-detected and never duplicates
a body. Fail-open per file (errors report, land nothing in the manifest, retry
next run); fail-safe on space (the plan must fit the dest filesystem plus
margin or the run aborts before the first byte). `--dry` reports the full plan
and copies nothing.

```console
$ helm corpus backup --dry
helm corpus backup — 6031 transcripts on the estate (dry-run — reporting only) -> /home/owner/corpus-archive
  claude   5173 transcripts  43.7GB — 5173 new/changed (43.7GB)
  codex     857 transcripts  4.4GB — 857 new/changed (4.4GB)
  tmp         1 transcripts  13.7KB — 1 new/changed (13.7KB)
helm corpus: 6031 would be copied (48.1GB) into /home/owner/corpus-archive/2026-07-20 — nothing copied (--dry)
$ helm corpus status
helm corpus status — dest /home/owner/corpus-archive
  ...
  archived: 6031 of 6031 (100.0%) · pending 0 (0B) · 12 retired sources kept in archive
  last run: 2026-07-20T09:00:04Z — copied 41, refreshed 2, 0 errors
```

`helm corpus status` is the coverage view: per-source counts, archived vs
pending, sources retired upstream but kept in the archive, and the last run's
receipt. A daily `systemd --user` timer ships in `scripts/` — enable it with
one step (nothing is enabled by installing helm):

```console
$ cp scripts/helm-corpus.{service,timer} ~/.config/systemd/user/ \
    && systemctl --user daemon-reload && systemctl --user enable --now helm-corpus.timer
```

## accounts — credentials and quota

### `helm creds`
The live account scorecard: every account across providers, with headroom,
credential state, reset window, and the use-it-or-lose-it weekly verdict.
Degrades to a one-line notice on machines with no quota provider — sessions
and resume still work.

### `helm creds crosscheck [--json]`
The local-session-scan SECOND SOURCE: sums per-record work
(`output_tokens + cache_creation_input_tokens` — never raw cumulative input)
from the local claude session JSONL into the same 5h / 7d / 7d-Fable windows
the header probe reports, then cross-checks the two. Drift is a **health
signal, reported never fatal**: header-active/scan-empty means the usage
happened elsewhere (another machine, an incomplete store);
scan-active/header-idle means a rolled window or accounting lag. Accounts
group by the canonical `projects` store behind their homes — the shared store
(every helm claude home symlinks onto `~/.claude/projects`) yields ONE
commingled row that never masquerades as a per-account cross-check; only a
truly isolated store cross-checks its one account. The header stays the
decision-point read; the scan is observational.

```console
$ helm creds crosscheck
helm creds crosscheck — local-session scan vs header truth (drift = a health signal, reported never fatal)
  ~/.claude/projects  (SHARED by 4: you@example.com, ...)
    scan:   5h 1.2M | 7d 45M | fable-7d 12M tokens over 1834 records (newest 2026-07-20T17:05Z)
    commingled — 4 accounts share this store; not a per-account cross-check
```

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
node whose data-dir lives on tmpfs, so a chat turn never lands on a
disk-persisted chain — every post also rides a **signed self-write turn** on
the poster's cell there: the turn payload carries the message digest
(`chat:b2b:<blake2b-256>`), the RAM room carries the text (thin claim, fat
corroboration — the same pattern as premise attestation, see
[ATTESTATION.md](ATTESTATION.md)). Signed rows render clean (the web panel
shows a subtle ✓ tick, chain index on hover); node down → the v1 path
automatically, tagged `[unsigned]` — the message never dies, the signature is
what degrades. Agents sign as `HELM_CELL_PROFILE` (else `helm-agent`); the
owner's web posts sign server-side as `david`. `helm chat node up` provisions
the room node (`helm-chat-node.service`, `dregg-cave-node` on
`/dev/shm/helm-chat-node`, port 8898, faucet ON — the node auto-funds joining
cells and helm tops up before each turn: **chat turns never die on
computrons**). The transport is node-agnostic (`HELM_CHAT_NODE_URL`; empty
disables) — the node migration (`scripts/node-migration.sh`) just
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

### `helm gc [--dry | --apply]`
The retention policy plane over the derived exhaust — the ancestor hit 32MB
of guard-ledger before learning this, so helm declares budgets on day one.
Every derived stream (ledgers, rotated generations, per-session state,
caches, config backups) carries one row in `gc.POLICIES`: a class, ONE budget
axis (size / age-days / count), and an action. **Dry-run is the default** —
report what WOULD be reaped, reap nothing; `--apply` enforces. Laws:

- **Authored content is out of reach by class.** Only class `exhaust` is ever
  reaped. `source` (attest-queue — pending attestations), `archive` (drain
  archives, skills-trash — the only copy of authored bytes), and `state`
  (drift baselines) get loud report-only rows whatever their budget says;
  the queue row points at `helm premise --retry-queue`, the archives at the
  owner. The typed store itself never appears in the table at all.
- **Reaping is rotate or prune, never a partial rewrite.** Over-size jsonl
  ledgers rotate by ONE atomic rename to a dated sibling (the rename is the
  write-archive-first step; the writer recreates the live file on its next
  append). Stale derived files/dirs (inject-seen past its own `SEEN_TTL`,
  dead reflex-state session dirs, aged `.1` generations, store caches,
  config backups) are pruned. Self-rotating ledgers (inject-ledger, events)
  budget at their module cap + slack, so gc firing means the module's own
  rotation broke; `keepalive-log`, `mints`, and `native-usage-history` have
  no rotation of their own — gc is their only cap.
- **Fail-open, per stream.** One unreadable stream reports an error row and
  the sweep continues; rc 0 always — gc is a janitor, not a gate. Age reads
  the basename stamp before mtime (backup/trash moves preserve the origin's
  mtime — the wrong axis), and a session dir stays fresh while any file in
  it moves. An `--apply` that reaped writes one `gc` receipt on the events
  journal.

`helm projections` is the read surface for what these stores ARE; gc is the
budget surface for how long their exhaust may live.

```console
$ helm gc
helm gc — declared retention over the derived exhaust (dry-run; `helm gc --apply` enforces)
  OVER  inject-seen  exhaust  age>7d      3 stale of 11 (12.4KB)  would prune 3 items
  in budget: inject-ledger, events, ... (14/15 streams)
helm gc: 3 items would be reaped; 0 report-only over budget — nothing touched
```

### `helm web [--port N] [--open]`
The same truth, warm, in a browser: five views (knowledge home, quota,
sessions, configs, chat) served self-contained on `127.0.0.1:7433`. `--open`
launches your browser. Full surface, API table, and a systemd unit:
[WEB.md](WEB.md).

## continuity — the authored chain over git

### `helm ship [--apply] [--remote URL] [--message M] | ship pull | ship hosts`

Cross-machine continuity on the overlay-not-store constitution: the ONLY
thing worth syncing is the **AUTHORED chain** — premises/heuristics/lexicon/
prd/journal/evals/archive, know-your-user, reflexes, priors/references, and
`registry-authored.json` (lineage edges + notes: unrebuildable). Everything
**DERIVED** re-derives per host via `helm sync` and never ships:
`registry.json` at any depth (the `_global` projection and every per-project
mirror), `.state/`, caches, `seats/` (host-local seat runtime — carries
provider auth). Adopted-by-symlink homes (mission-control) are host-local:
the symlink is ignored; that chain ships from its own repo. RAM-canon (chat
rooms, tmpfs node state) never touches this disk — always out of scope.

Bare `helm ship` is a **dry-run** (the drain law): it reports what would
ship, names the derived exclusions, and runs the secret scan — touching
nothing. `--apply` git-inits `~/.helm` (branch `main`), refreshes the managed
`.gitignore` block, writes this host's observation block, commits, and pushes
when an origin is set; **no remote = commit-only, said explicitly**. The
remote must be **private** — journal/premise content is operator-internal.

**Nothing-secret-staged law:** before any commit exists, every staged byte
is scanned for token/key patterns (anthropic/api keys, github tokens/PATs,
AWS key ids, slack tokens, private-key blocks, JWTs, bearer headers). A hit
refuses loudly, names the file and pattern, and unstages everything.

`helm ship pull` fetches + merges the chain, then regenerates the local
projections (`helm sync`). Merges are **append-only by construction**:
entries are per-file, and two hosts revising the same knowledge meet through
the store's supersede-chains (tombstone + replacement file), never three-way
markdown conflicts. pull resolves no content — git merges files, the store's
status filter picks live over tombstone. A rejected push says exactly what
to do: `helm ship pull`, then re-ship.

`helm ship hosts` reads the per-host observation blocks
(`_global/hosts/<host>.json` — one file per host, so they never conflict):
who observed what, when — live-on-pug, dormant-on-droopy.

```console
$ helm ship
helm ship --dry (nothing touched):
  home:   /home/owner/.helm
  git:    not initialized (would `git init -b main`)
  remote: none configured — would COMMIT ONLY (add --remote <private-url>)
  would ship: 82 authored files, 0.9MB
  derived (never ships): .state/ x1 adopted-symlink x1 registry.json x22 seats/ x1
  adopted symlink homes (host-local): mission-control
  secret scan: clean (9 patterns over 82 files)
  apply with: helm ship --apply [--remote <private-url>]
```

Deferred to v2 (by the card, deliberately): `helm manifest` — digested
store-state roots per host, `--attest` self-written receipts, and pull's
CONTRADICTED-ACROSS-HOSTS drift entry when the same id diverges with no
supersede link.

### `helm handoff [check [--hook-json] | write [--project P] | recover <sid>]`

Compaction continuity, leg 1 — the AUTHORED handoff contract. `write` reads
DONE/REMAINING/NEXT prose on stdin and lands it as a typed journal entry on
the project shelf (`~/.helm/<project>/journal/<date>-handoff-<sid8>.md`);
the frontmatter carries the one-line DONE/REMAINING/NEXT summaries plus the
session id, so the entry is greppable, cv-searchable, and ships with the
authored chain. A same-day same-session re-write lands on the same path —
the newest handoff wins. `check` asks one question: does a handoff artifact
for THIS session exist (a journal entry carrying the session id, or a repo
`HANDOFF_NEXT_SESSION.md`) newer than session start? With `--hook-json` on
PreCompact (and SessionEnd, for sessions that die uncompacted) it first
takes the automatic now-snapshot (leg 2, below — two legs, one trigger),
then NAGS in a few lines when the contract is unmet: a nag, never a capture
— the agent authors audited prose; helm never invents a summary. Hook mode
is fail-open total: rc 0 always, silent when satisfied, a garbled payload
nags nobody, and capture can never block the compaction. `recover <sid>`
re-reads the span a compaction discarded by wrapping the ONE recall index
(`cv show <sid> --pre-compaction`, architecture law 4 — helm records how to
query it, never a second index); a missing `cv` prints that exact command.

### `helm now [capture [--hook-json] [--session S] | show]`

Leg 2 — the AUTOMATIC safety net (the write side of the compaction-
continuity reflex; the ancestor's now.md was advisory-without-actuator and
sat at 0 bytes). `capture` snapshots the session id + registry project +
git branch/status/changed paths + this session's recorded edits and reflex
counters (record.py's session state — session-keyed, never pane) into
`_global/now.md`: newest-first, 40-line cap, fail-open total. `show` prints
the snapshot ONLY while under 48h fresh — a stale now.md actively misleads,
so staleness is silence (the gate is load-bearing, not polish) — shaped for
SessionStart additionalContext; the legacy `~/.remember/now.md` is honored
as a read fallback until retired. now.md is DERIVED telemetry: rebuildable,
never ships, no mutation receipt.

Hook recipes (the HOOKS.md fail-open law — `timeout` + `|| true`, absolute
helm path):

```json
"PreCompact":   [{"hooks": [{"type": "command",
  "command": "timeout 10 <helm>/bin/helm handoff check --hook-json || true"}]}],
"SessionEnd":   [{"hooks": [{"type": "command",
  "command": "timeout 10 <helm>/bin/helm handoff check --hook-json || true"}]}],
"SessionStart": [{"hooks": [{"type": "command",
  "command": "timeout 10 <helm>/bin/helm now show || true"}]}]
```

---

**A note on `helm seat`:** the multimodel-seat verb (giving a non-Claude
model family the full claude-code harness through a local wire proxy —
`seat add|up|down|launch|smoke|list|status|doctor`) is wired into the
dispatcher; see [MULTIMODEL_SEATS_ADDENDUM.md](MULTIMODEL_SEATS_ADDENDUM.md)
while its live proving rounds finish.
