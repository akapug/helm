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
a source-free projection containing data = FAIL, declared staleness = WARN,
squatters = WARN). A producer may declare its exact empty JSON **genesis** in
the manifest; while no seat is registered, only that source-free shape is
healthy. Once a seat exists, missing sources fail again even if the projection
is still empty. Malformed, extended, or nonempty shapes always fail closed.

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
helm store xrev-clear <id> --by <who> [--type T] [--force-new]
                                           candidate -> PROVISIONAL: a cross-family
                                           /x review cleared it (the reviewer
                                           attests; the verb never runs the review).
                                           Provisional FIRES with a [provisional]
                                           tag, awaiting owner ratify
helm store confirm <id> [--type T] [--edit <stmt...>] [--force-new]
                                           owner ratify -> live (candidate OR
                                           provisional)
helm store reject <id> [--type T] [why...] reject a candidate/provisional —
                                           retired in place (file kept as record)
                                           (--type on any: disambiguate a slug
                                           shared across reviewable types)
helm store pinned [--stats]                the always-on lane (--stats: budget
                                           walk + ledger made-it/starved counts)
helm store add <type> <id> | <statement> [| ...]
    prior:     <id> | <statement> [| conf [| keywords [| domain]]]   belief, default 0.6
    premise:   <id> | <statement> [| keywords [| domain]]            certain, conf 1.0
    lexicon:   <term> | <definition> [| kind [| keywords [| domain]]]
    heuristic: <id> | <move> [| trigger-csv [| domain]]
    reference: <id> | <summary> [| url [| keywords [| domain]]]
    flags: [--source S] [--rationale <text...>] [--candidate] [--force-new]
           --candidate (prior|lexicon|heuristic|reference): SAFE inferred
           capture — writes a non-live candidate EXCLUDED from inject/resolve
           until `confirm`ed (premise refused: certainty is human-only)

    keywords are LINTED at add: empty, lone-word, and comma-less >=4-word
    salads REFUSE (symptom phrases of 1–3 words are what resolve matches);
    >=3-word phrases get 1–2-word stems auto-added; keywords that already
    resolve to a live sibling refuse toward `helm store keywords <id> --add`
    (--force-new or HELM_STORE_FORCE_NEW=1 overrides, recorded).

    gloss:  the SHORT line that FIRES, standing in for a long statement.
            An entry keeps its full statement on disk as the durable record;
            the gloss is what reaches a seat every turn, inside the pinned
            budget. Write one when a rule is worth keeping in full but too
            long to fire — keep the canon, shorten the delivery.
            TYPES: prior/premise, heuristic, reference, lexicon (all four).
            LIMIT: the RENDERED line (tag + id + gloss) must fit LINE_CAP,
            400 bytes today. An oversized gloss is REFUSED at write with the
            exact overage, never silently cut — a gloss the injector truncates
            recreates the severed-sentence failure it exists to prevent.
            DERIVED, so it is DROPPED whenever the statement's meaning
            changes and no replacement is supplied: `confirm --edit` and any
            re-mint over a retired id both scrub it. A gloss kept across an
            edit would fire a line the entry no longer says, which is worse
            than a truncation — a severed sentence is visibly incomplete, a
            stale gloss is confidently wrong.
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
never blocks. Lexicon is exempt: redefining a term is its update lane — except
a `--candidate` add over a LIVE term, which is refused (writing
`status:candidate` in place would DE-canonize the confirmed definition;
capture may coin, never demote — redefine live or pick a distinct id).

`resolve` ranks JIT hits DF-weighted: each matched probe scores 1/df (df = how
many entries carry that keyword), summed and confidence-weighted — one rare
keyword outranks a pile of shared ones. Ties break most-recently-updated,
never alphabetical. `helm inject --explain` shows the per-probe contributions.

**Candidate tier (safe inferred capture — capture everything, canonize
nothing automatically).** An agent-inferred entry lands as `status:candidate`
(`add <type> ... --candidate`, `source:inferred` — every capturable type:
prior, lexicon, heuristic, reference; **premise is refused**: an inference may
not claim certainty even in escrow, the 1.0 rail stays human-only — capture it
as a prior belief instead). A candidate is a *non-live* status, so the
resolver's live-filter already EXCLUDES it from resolve / pinned / inject —
the hard law: nothing inferred is ever silently authoritative. `list
--candidates` surfaces them (and coach's dup-search reads
`store.candidates()`).

**Provisional tier (xrev-cleared → provisionally live).** Owner canon: a lot of
the technical auto-learns will be Greek to the owner, so they may go
*provisionally live* — but ONLY after a cross-family `/x` review clears them
(xrev is the gate, not the owner). `helm store xrev-clear <id> --by <reviewer>`
graduates candidate → `status:provisional`, recording the who/when in
`xrev_by`/`xrev_ts` (all types) + the prior's own evidence_log + the events
journal. Because candidate → provisional is the first injectable activation, it
runs the same keyword/findability mint guard as `store add`; `--force-new`
overrides that refusal and records the same duplicate-override receipt. The
reviewer **attests** a cross-family review happened — the verb never runs the
review itself. A provisional entry FIRES through resolve / inject like live (it
is usable knowledge) but renders with a visible `[provisional]`
tag everywhere (CLI `list`, `resolve`, the inject line, the web panel) so an
agent can weight it as not-yet-owner-ratified. An un-cleared candidate still
fires NOTHING.

Exits (all receipted, none silent): `helm store confirm <id> [--edit <stmt...>]
[--force-new]` is the owner ratify — it works on BOTH a candidate and a
provisional, promoting → live (`source:explicit`; a prior keeps its captured
confidence — confirming ratifies the capture, never inflates the belief — and
carries the who/when receipt in its own evidence_log; the xrev provenance
survives). Candidate → live is a first activation and runs the mint guard;
`--force-new` is its recorded override. Provisional → live already fires and is
ratification only, so it is not re-linted. `helm store reject <id> [why...]`
retires the wrong inference (candidate OR provisional) IN
PLACE (file kept as the record, never deleted). `xrev-clear`/`confirm`/`reject`
resolve against the reviewable set (candidate + provisional) first, and a slug
shared across types is refused without `--type T` (never act on the wrong
entry). The **owner's review surface is the web UI**: near the configs view, a
*store review* panel lists candidate + provisional entries with Approve
(confirm), Reject (reason box), and the per-row xrev-clear display — the owner
actions route through the SAME store functions as the CLI (one writer path).
Decay is operator-visible, never a silent job: `helm drain --expire-candidates
[--days N] [--apply]` archives-then-prunes unconfirmed **candidates** of every
type older than N days (14 default; a no-timestamp candidate never expires;
a provisional is never age-expired — xrev cleared it; dry-run default; net +
receipt).

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
(plus the derived `[scope: …]`), writing no ledger row and mutating no state;
every line carries its discovery attribution — `← adopted | helm-global |
adopted-project | project` (reflexes: `← helm-global | project`) — so a fired
line traces to its file's home at a glance.

With a session, the habituation guard extends to the JIT lane: a fired entry
**stays sent** for the life of that session (state per session under
`_global/.state/inject-seen/`, stale files self-pruning) unless its score
jumps to ~2x its score at last fire. There is no turn window — a seat forgets
at **compaction**, not on a timer, and the SessionStart legs clear this state
at exactly that boundary, so everything re-fires to a seat that actually lost
it. (A 15-turn window used to re-send every entry about every 16th turn
forever: 94.2% of measured JIT re-deliveries were that window expiring rather
than a real score escape.) Pinned and reflex lanes are exempt,
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

### `helm drain [--apply] [--sweep-dups] [--force-new] [--limit N] [--project P] | --rekey [--apply] | --expire-candidates [--days N] [--apply]`
Classify raw memory intake and route entries to their typed homes, archiving
sources with a reference back. **Dry-run by default** — nothing moves without
`--apply`, and the rollback net is verified non-empty first. Conflicts are
never auto-resolved. Drained feedback becomes a prior with keywords derived
from the full statement's distinctive words (≥ 5 chars, non-generic,
frequency-then-length ranked, cap 8).

Retype and upgrade are semantic mints, so the dry-run first passes them through
the same keyword/findability and duplicate guard as `store add`. One existing
store snapshot plus each earlier accepted batch entry catches both pre-existing
and same-batch collisions; a refusal leaves its source byte-identical in intake
while independent accepted actions may still apply. Guard-expanded keywords
(or a heuristic trigger) are the values serialized into the destination.
`--force-new` deliberately overrides only duplicate refusal, and its
`store.dup_override` receipt is recorded after that action's physical write
succeeds—not during preflight. Route-only moves and adopted external files are
not retroactively treated as Helm-authored semantic mints.

`--project P` drains P's OWN claude memory dir (the adopted per-project pile)
with the identical classify/apply gauntlet — including the upgrade op below. A
registry **alias map** (config-driven built-ins from `HELM_PROJECT_ALIASES`,
empty by default, plus any authored per-project `aliases`) routes the otherwise
unroutable project entries; matching is longest-handle-first and malformed
config fails loudly — the full syntax/precedence contract lives in
[ENVIRONMENT.md](ENVIRONMENT.md); a short 2-char alias matches by filename only
(a 2-char word wallpapers a description scan).

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
helm drain: plan (12 raw entries): ...
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
half is the **Stop hook**: `helm chat stop-guard` runs `index cap --apply`
in-process as its silent mechanical leg (best-effort, never blocks, never
prints; `HELM_STOP_GUARD_INDEX=0` disables) — so the write path structurally
cannot exceed budget on any home `helm hooks install` covers.

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
meld-era premise superseded by its helm successor).

**Never on weak signal.** A proposal needs BOTH a directed lineage edge AND a
match between an ancestor-era entry and a successor-era one: *same-slug* (equal
base after stripping the era token — `meld-rollover` / `helm-rollover` →
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
  [same-slug] meld-rollover-policy -> helm-rollover-policy   (helm supersedes meld)
      old: "on rollover, re-home the session under the freshest account"
      new: "on rollover, helm re-homes the session under the freshest account"
      ->  helm store supersede <ts> meld-rollover-policy helm-rollover-policy lineage: helm supersedes meld + same base slug 'rollover-policy'
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
`edit-targets.log` (basenames actually edited) and `todos.json` (the seat
todo mirror — see `helm todos`). Keyed by the payload's
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

### `helm todos [--all] [--json]`
The seat todo mirror — **what each agent in the fleet is working on right
now**, readable without asking it. The recorder's todo leg captures the
seat's CURRENT list off `TodoWrite` *and* the `Task*` family (`TaskCreate` /
`TaskUpdate` / the legacy `TaskUpdateTODO` — the tools the live fleet
actually emits; create's id lives only in the tool_result string, so it is
parsed from there) into `_global/.state/reflex-state/<session_id>/todos.json`,
beside `counters.json`. Bounded (64 items, 120 chars each), atomic, no
subprocess, and walled off behind its own `try` — a broken mirror can never
cost the counters, command-log or edit-targets that back the stop-whisper.

The bridge is **digest + pull, never a firehose** (decision-spirit #23, the
attention budget). Todo state churns on nearly every turn, so:

* **Pull is the surface.** `helm todos` is this seat's list; `helm todos
  --all` is the fleet table (seat · in-progress · done/total · age, with
  never-mirrored seats collapsed into one footer line and unclaimed sessions
  cut to the 25 freshest — gc keeps `reflex-state` for 30 days, and a wall of
  dead sessions is the same attention tax); `--json` for tooling, and it is
  the only surface that carries every item (the table and `/api/todos` read
  the digest alone, so the polled endpoint stays kilobytes on a big estate).
  The same state rides `helm chat seats` and the roster row, so the delivery
  roster now answers *who is working on what*, not just who is here. Reading
  costs the reader nothing until they ask.
* **Push is a rate-capped exception.** One room line lands only on a
  transition a teammate would act on — a task **finished**, or an idle seat
  picking up a **new** in-progress task — collapsed to the latest state, at
  most once per 5 minutes per seat, and never at all when nothing materially
  changed (a re-word, a reorder, a new *pending* item is the seat's own
  bookkeeping). `HELM_TODO_POST=0` switches it off entirely.
* **Never an @mention, never a DM — and never a wake.** Every `@` is
  stripped from the posted text, so a todo that merely contains "@someone"
  cannot become a mention by accident. That alone is not enough: a seat's
  **home room** delivers every plain row to every seat homed there, so on a
  `helm launch --room team-x` fleet the line would wake the whole team. The
  row therefore rides `ambient` — a row class `seats.deliverable()` drops
  before every wake rule, so it renders on every read surface (the room,
  `helm chat read`, the web feed) and wakes nobody. `ambient` is for machine
  status a teammate *pulls*; a mention or a DM never carries it.

The owner's parity surface is the web tab's **fleet todos** panel (and
`GET /api/todos`) — who is working on what, without a CLI.

```console
$ helm todos --all
  seat              in-progress                                 done    age
  project-claude  —                                            2/2      5s
  helm-claude       wire helm todos + roster + web panel         2/4      5s
  example-codex     Wire the new endpoint into the router        0/2      5s
  (1 seat with no mirrored todos — they fill on the next TodoWrite/Task* call)
```

### `helm mentor [observe <project> [--since 7d] | teach <project> "<id> | <steer>" [--signal S] [--pattern RE] [--marker PATH] [--teacher NAME] [--attest] | review <project> | log [--project P]]`
The inception actuator — observe / teach / review over a junior, where the
junior is a **project/seat** (v1 same-home): a taught reflex in
`<project>/reflexes/` reaches every future **claude-harness** session through
the inject hooks already installed, so delivery is free and teaching is the
only new verb. Not every harness — `helm hooks install` writes claude homes
and seat `CLAUDE_CONFIG_DIR`s only, so a pi session receives no inject hook
and therefore no taught reflex.

`observe` is the ranked critique brief, **read-only**: the window's sessions
(the catalog lens: claude + codex) joined with the recorder's per-session
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
helm mentor: TAUGHT 'identified-fix-filed-not-fixed' -> myproject (prompt) by alice
$ helm mentor review myproject
  identified-fix-filed-not-fixed  taught 2026-07-19T09:00:00Z by alice [attested]
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

### `helm premise <id> | <statement> [| keywords [| domain]] [--project P] [--no-attest] [--force-new]`
Capture a confidence-1.0 truth: written to the typed store (same shape as
`helm store add premise`) **and** attested into the **native, offline hash
chain** — the primary, tamper-evident proof, which always lands with no binary
and no node. If a dregg node is reachable, the record hash is also posted as an
OPTIONAL external anchor (fail-open; honestly labelled a node commitment, never
a user-cell signature); when no node answers the anchor is queued.

The typed-store leg pays the shared keyword/findability and duplicate guard
before any store, chain, anchor, annotation, or queue side effect. Omitted
keywords are derived from the statement; supplied keywords retain their authored
shape plus any accepted stem probes. `--force-new` deliberately overrides a
strong duplicate and records that override only after the premise serializer
succeeds. An identical same-id re-capture excludes only its exact current row so
its own document frequency cannot make an idempotent retry fail.

`--no-attest` stores only. `helm premise --retry-queue` re-attempts pending
anchors. Backfill (`--attest-existing` / `--attest-sweep`) annotates already
existing entries and is not a semantic mint, so it does not rerun this guard.
The full story is [ATTESTATION.md](ATTESTATION.md).

```console
$ helm premise "naming-extremes | metaphors live at the extremes only"
helm premise: LIVE 'naming-extremes' [certain 1.00] - metaphors live at the extremes only
```

### `helm premise --supersede <old-id> <new-id> | <statement> [| keywords [| domain]] [--project P] [--force-new]`
Evolve the chain instead of editing in place: guards and captures the NEW
premise, tombstones the old one through the store's own lifecycle (`replaced_by`
/ `supersedes`, file kept), and appends ONE native `op: supersede` record whose
`supersedes_record` links to the old premise's record hash (mirrored on the new
entry as `attest_supersedes_record`). The duplicate corpus excludes exactly the
selected predecessor (and an exact same-id retry destination), so unrelated
siblings still refuse the revision. All three legs land offline. Re-stating a
LIVE attested premise with a different statement is refused toward this verb —
an in-place edit would orphan the attestation.

```console
$ helm premise --supersede naming-extremes "naming-poles | metaphors live at the poles"
helm premise: LIVE 'naming-poles' [certain 1.00] - metaphors live at the poles
  supersedes 'naming-extremes' — tombstoned (delete_eligible, file kept)
  attested (native): record 4d8a4c1306028e80 at chain_index 7 — recorded by 'alice'
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
guard runs — coach composes resolve + add, it never re-implements them. Premise
routes include guardable derived keywords; premise evolution uses native `helm
premise --supersede` so the attested linkage is preserved. Other supported typed
routes use `helm store supersede` only after their landing succeeds. A landing
failure never advances lifecycle state, a supersession failure returns nonzero
instead of printing a false success, and unsupported supersession shapes (such
as lexicon alias-fold or reflex/skill routes) refuse before minting.

A low-confidence route (no clear shape) is never guessed at on `--apply`: it
drops into the drain intake dir as a `feedback-*.md` entry, lossless, for `helm
drain` to route later. `--as` forces the layer, `--id` the id, `--supersede
OLD` selects the truthful layer-specific evolution path, and `--json` emits the
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

### `helm clarity check [<file>|-] [--strict] [--project P] [--no-store] [--json] | rules [--json] | skill [--project P]`
The ASD-STE100-descended clarity die over coordination text (chat posts,
dispatch briefs, verdicts, handoffs — docs inherit the same table for free).
The invariant: clarity is a property you can CHECK, not a property you can
request. ONE rule table (`helm/clarity/rules.py`) feeds both consumers — the
write-time system (`clarity skill`) and the deterministic linter (`clarity
check`, exit 1 on violation) — so they cannot drift.

The STE-derived deterministic rules: sentence length (20 words under
`--strict` for instructions, 25 descriptive), paragraphs capped at 6
sentences, no semicolons, no contractions, one-instruction-per-sentence
(APPROXIMATE — reported as an advisory, never a violation), and hedge or
marketing terms (the measured-weakest rule; findings carry `[weak]`). The two
rules that are helm's own: **lexicon-drift** — the store's curated lexicon
(`~/.helm/_global/lexicon`, plus the project layer via `--project`) is the
controlled vocabulary, and a spacing/fusion variant or known synonym of a
term flags where the one name exists — and **provenance** — a load-bearing
claim (number + unit, ratio, or verification verdict) that carries no
MEASURED / TRACED / INFERRED tier in its paragraph flags.

Honest limits, in the module rather than faked: helm is stdlib-only, so no
POS tagger exists here, and the ASD-STE100 approved-word dictionary,
noun-cluster and verb-form rules are ABSENT, not approximated. A store read
failure reports "the drift rule DID NOT RUN" — an absent lexicon never
renders as a clean check. `--no-store` skips the store deliberately
(hermetic/offline). The suite carries a real historical helm commit message
as a live positive control; when the control stops firing the linter is
presumed broken.

```console
$ echo "The cli proxy is essentially done; 4 of 5 seats resumed." | helm clarity check
<stdin>:1: [hedge-term weak] hedge/marketing term 'essentially': ...
<stdin>:1: [lexicon-drift] 'cli proxy' drifts from lexicon term 'cli-proxy' — one name for one thing: ...
<stdin>:1: [no-semicolon] semicolon — write two sentences: ...
<stdin>:1: [provenance] load-bearing claim carries no provenance tier (MEASURED / TRACED / INFERRED): ...
helm clarity: 4 violations, 0 advisories in 12 words — 33.33 violations per 100 words (lexicon: 16 terms)
```

Out of v1, deliberately: the Stop-hook rung (a die aimed at coordination text
can silence the fleet — it waits until the linter has run against real
traffic), anything needing POS, any model-assisted rule.

## sessions — claude + codex, one catalog

### `helm sessions [<project>] [--limit N] [--all] | helm sessions resume <sid> [--go] [--title T] [--skip-permissions] [--force]`
Every local **claude + codex** session, newest first, grouped by real
project (decoded from inside transcripts, never from directory slugs). Those
two are the catalog's whole scope: it indexes exactly those two transcript
formats (`catalog.py`). opencode and pi sessions are scanned by the auto-map
and so reach `helm projects`, but they produce no rows here. (Not a statement
about what helm can resume — `helm pi resume` exists.)
`resume <sid>` prints the exact resume command — the harness's own CLI, no
wrapper. `--all` includes synthetic (pruned-copy) sessions.

```console
$ helm sessions myproject
$ helm sessions resume 3f2a
cd ~/dev/myproject && claude --resume 3f2a9c81-...
$ helm sessions resume 3f2a --go          # actually open it in a pane
helm sessions: resumed 3f2a1b9c via orca — pane term_aae27b65…
  cred: ~/.claude-homes/other-example-com
  cwd : ~/dev/myproject
```

`resume` prints the exact command; `--go` runs it. The command PINS
`CLAUDE_CONFIG_DIR` to the home that owns the session — without the pin it does
not fail on the wrong account, it silently succeeds on it, because every OAuth
credhome symlinks `projects/` into one shared directory so the transcript
resolves from anywhere. Ownership comes from `<home>/session-env/<sid>`, latched
into `~/.helm/_global/session-creds.tsv` on first lookup because claude prunes
that directory (≈91% of sessions touched in the last 2 days resolve, ≈18% past
30 days — the latch freezes coverage instead of letting it decay).

`--go` REFUSES rather than opening a pane that cannot work:

* the session is already open in another pid (two panes on one sessionId
  interleave their writes and each loses turns) — `--force` overrides
* the home has not accepted the trust dialog for that cwd — the prompt renders
  into an alternate screen buffer the adapter cannot read, so the pane looks
  blank and healthy forever; `--skip-permissions` passes
  `--dangerously-skip-permissions`
* the session is OVERSIZED or a REFERENCE transcript

The default account (`~/.claude`) is deliberately left UNPINNED: it is where
claude stores state when `CLAUDE_CONFIG_DIR` is unset, and pinning it makes
claude look for the onboarding marker in the wrong place and open the first-run
wizard.

## session — the session substrate (wraps cv, owns the policy)

### `helm session ls | doctor-panes`
Every live claude pane with a **persistence column**, classified from transcript
truth: transcript present = persisted; proven SID with no transcript =
**MEMORY-ONLY**; unresolved SID = **UNKNOWN**, never a safety/absence claim. A
child stamp explains why a pane may be memory-only but is not the verdict.
PID-keyed Claude Code records are used only when their canonical UUID and
`procStart` match the same live process. Also flags proven DOUBLE-OPEN SIDs (law
1 violations).

### `helm session doctor <sid> | checkpoint <sid> [--window N] | rescue <pid|sid>`
`doctor` classifies a session (normal / forked / compacted / live /
MEMORY-ONLY / UNKNOWN) + the live-pane state. A proven live memory-only SID is
diagnosed directly even though its missing transcript gives `cv` and the catalog
nothing to open. `checkpoint` mints a NEW resumable id (`cv prune --thinking`,
original untouched) so a maxed/forked session becomes branchable. `rescue` tells
stamped and unstamped transcriptless panes to harvest side-channels and write a
self-recap while still live; it never invents a resume line for a missing
transcript.

### `helm session port --cred <home> <sid> | resume <sid> [--launch]`
Cred-switch resume PREP (`port`: verifies the target home's projects/trust,
prints the `CLAUDE_CONFIG_DIR=… claude --resume` line) and the single-open-
guarded resume. TWO LAWS enforced by every verb: (1) never two live copies of
one session — a live pid holding the sid means close-first, never the
incantation; (2) prepare + print, never launch — only `resume --launch`
spawns, attached to the current terminal under a per-session exclusion lock and
an under-lock law-1 recheck. Every printed line bakes
`CLAUDE_CODE_FORCE_SESSION_PERSISTENCE=1` + unsets the child-stamp trio, so a
paste into a stamped pane can't re-trap.

### `helm session experts [--register <sid> --domain D [--note N]] [--refresh <sid>] | helm session ask <domain> <q…>`
The expertise layer (expert-sessions-beat-fresh-research): a durable O(1)
registry (sid → domain → last-refreshed) and the query ladder — registry hit →
expert-transcript search → `cv pack` context digest → resume-live
(print-don't-launch, with a mandatory RE-GROUND step: the expert re-verifies
its facts against the current substrate
before answering — expertise goes stale like everything else).

```console
$ helm session ls
$ helm session rescue 622078
$ helm session experts --register 96416633 --domain helm-orchestration
$ helm session ask helm-orchestration what is the integration order
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
helm corpus backup — 6031 transcripts on the estate (dry-run — reporting only) -> ~/corpus-archive
  claude   5173 transcripts  43.7GB — 5173 new/changed (43.7GB)
  codex     857 transcripts  4.4GB — 857 new/changed (4.4GB)
  tmp         1 transcripts  13.7KB — 1 new/changed (13.7KB)
helm corpus: 6031 would be copied (48.1GB) into ~/corpus-archive/2026-07-20 — nothing copied (--dry)
$ helm corpus status
helm corpus status — dest ~/corpus-archive
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

### `helm cred [list | backup [--all] [--apply] | switch-guard [--install] [--apply] | heal [--apply]]`
**The safe `/login`.** Hitting a session limit and running `/login` must never
be a scary act. It cannot be redirected — a live session's `CLAUDE_CONFIG_DIR`
is fixed, so the new account lands in the config dir that session is pinned to
while the directory keeps its old NAME. `helm cred` makes that write
**truthful, non-destructive and reversible**.

Identity here always comes from CONTENT: `<dir>/.claude.json`'s `oauthAccount`
block (email / uuid / org — metadata; the tokens live in `.credentials.json`
and are never read aloud), cached by mtime. helm never infers an account from a
directory name, and an unreadable `.claude.json` yields **no account claim at
all** rather than a guess.

| sub-verb | what it does |
| --- | --- |
| `list` (default) | DIR NAME &#124; ACTUAL ACCOUNT &#124; verdict (`AGREE` / `DRIFT` / `UNKNOWN` / `N/A`) &#124; backup depth. Pure read — the owner-visible truth surface. |
| `backup [--all] [--home H] [--apply] [--quiet]` | Plan a snapshot of a home's stable `.credentials.json` bytes + `oauthAccount` block; **dry-run by default**. `--apply` writes it under `~/.cred-backups/<folded-email>/<ts>/` (dirs `0700`, files `0600`, owner-only from creation). Concurrent identity/token changes, symlinks, unreadable files, and failed pre-image capture refuse. Skips an identical newest snapshot; keeps 20 per account. `--quiet` is hook mode. |
| `switch-guard [--home H] [--apply]` | **Run with `--apply` before a `/login`.** Dry-run is the default. Applied mode backs the home's current account up, then prints the login command. `--install --apply` wires `cred backup --apply --quiet` as both `SessionStart` and `Stop` hooks in every claude home, keeping the pre-image within one turn of the rotating live token. |
| `heal [<home>] [--apply]` | Put the correctly-named account back into a DRIFTED home from its newest snapshot. **Dry-run by default.** |

`heal` refuses more than it acts, on purpose:

* **held** — any live process pinned to that config dir (probed through
  `/proc/<pid>/environ`, agent processes named first). A live session is never
  evicted; the probe is re-run immediately before the write, so a holder that
  arrives mid-heal still wins.
* **cannot-probe** — `/proc` is unavailable, an own process cannot be read, or a
  PID changes identity during the bracketed scan. Uncertainty is never treated
  as absence.
* **no-backup** — nothing to restore; the account can only come back through a
  fresh login, and helm says so instead of inventing a restore.
* **ambiguous-backup** — snapshots sharing a folded home name claim multiple or
  missing identities. A lossy filename fold is never enough to select an account.
* **revocation-risk** — the snapshot's refresh token is still LIVE in another
  home. Restoring it would leave byte-copies of one token family in two homes,
  and reuse detection revokes the whole family (see `helm homes verify`). helm
  prints the fresh-login command instead.
* **no-preimage** — the current occupant could not be snapshotted (full or
  blocked disk). Evicting it would delete the only copy of a live credential,
  so heal refuses: **no eviction without a pre-image**, enforced, not merely
  attempted.

A `ready` plan additionally carries `stale_pre_image` when the snapshot's own
access token had already expired — meaning the home almost certainly refreshed
(and ROTATED the refresh token) after the snapshot was taken, so the copy on
disk may already be spent, and a *spent* refresh token is what reuse detection
revokes a family over. That is the temporal twin of `revocation-risk`; it warns
rather than refuses (the snapshot is still the only recovery on disk) and
prints the fresh-login command beside it.

An applied heal snapshots the CURRENT occupant first (the undo is itself
undoable), re-probes holders after capture and again at the restore commit,
restores, then VERIFIES the home now reads as the expected account. The restore
is transactional across `.credentials.json` and `.claude.json`: both are durably
staged at `0600` before either rename, the directory is fsynced, and every
staging/rename/fsync/verification failure restores exact original bytes, modes,
and absence for every file already changed. Snapshot/home/file symlinks and
snapshot identity/digest/length mismatches are refused. A present-but-
unparseable `.claude.json` is never rewritten from
scratch — it holds the home's whole state. Restored credentials can still be stale (refresh
tokens rotate); when claude rejects them the fix is one fresh login, and the
identity is right either way.

**What heal does NOT do:** it never *places* an account into a home that is not
already drifted. After a heal, the account the `/login` brought in lives only as
a pre-image snapshot; giving it a home of its own is a fresh `/login` into that
home (a non-destructive act — nothing is evicted). No credential is lost either
way, but there is no `restore-into` verb yet.

Secrets never surface: credential bytes are copied and compared, never printed,
logged, or placed in an error string. The only derived value ever written is a
12-hex sha256 prefix — a content fingerprint, the same idiom as the
shared-family audit.

`helm doctor` carries two rows from here: a loud `credhome <name> HOLDS
<other-account> (drift)` WARN naming `helm cred heal`, and a `no cred backup
for <account>` WARN — an account with no snapshot cannot be put back after the
next eviction. `helm launch --home H` prints the account that home ACTUALLY
holds before it execs, so asking for a home by name can no longer silently hand
you a different account.

```console
$ helm cred list
helm cred — identity read from CONTENT (.claude.json oauthAccount), never from the dir name:
  DIR NAME                       ACTUAL ACCOUNT                   VERDICT  BACKUPS  NOTE
  cto-example-com                you@example.com                  DRIFT    2        this account's home is you-example-com; alias: cto; live pids 57699
  you-example-com                you@example.com                  AGREE    2
helm cred: 2 homes, 1 drift — `helm cred heal` (dry-run) shows the repair

$ helm cred switch-guard --home you-example-com --apply
helm cred switch-guard: you@example.com is protected (snapshot /home/you/.cred-backups/you-example-com/20260722T032630Z)
  now safe to run:  CLAUDE_CONFIG_DIR=/home/you/.claude-homes/you-example-com claude /login
  after the login:  `helm cred list` shows what this home now holds; `helm cred heal` puts you@example.com back when no session holds it.
```

### `helm swap <home-or-account>`
A seat ran dry mid-work: find its live sessions, pick the healthiest other
account of the same provider, and print the exact resume-under-that-account
block per session. helm prints; the human runs — a swap is a seat decision,
never an automatic mutation.

```console
$ helm swap you@example.com
```

### `helm attribute [--by project|model|cred] [--since Nd|Nh] [--limit N] [--project P] [--json]`
Historical token-effort attribution — the HR-capacity frame: how much effort
(in tokens) has a project / model / cred consumed, so cred homing can follow
need. A BOUNDED on-demand rollup over the session catalog (the bound is
always printed — a scoped result never masquerades as a whole-corpus total).
Effort = output + cache-creation tokens (claude) / output tokens (codex),
**never** raw cumulative input. Cred attribution is a path-boundary match
against `-homes/` dirs only: codex rollouts under `~/.codex-homes/<name>/`
attribute cleanly; claude sessions live in the ONE shared store and stay
**UNATTRIBUTED** rather than being guessed — that bucket is always visible,
so a coverage gap never hides inside an attributed total.

```console
$ helm attribute --by cred --since 7d
helm attribute — effort by cred, 143 sessions (last 7d, limit 200; measure: output + cache-creation tokens, never raw input)
  UNATTRIBUTED             123.4M  118 sessions
  codex:seat@example.com    12.1M  25 sessions
```

### `helm who [--json]`
The pid→cred attribution table: every live claude/codex process with its
cred home (`CLAUDE_CONFIG_DIR`/`CODEX_HOME` from `/proc`; the provider
default only when the environ was READ and the key is genuinely absent — an
unreadable environ is no evidence, so that row stays visible but unattributed,
marked `environ-unreadable`), the account that home maps to, cwd, and the
session it is running. A pid whose stat starttime changes mid-scan (pid reuse)
is discarded. Codex session ids are exact (the rollout file is held open —
the fd names it); claude ids are exact only when the home+cwd project dir
holds a single live candidate, else newest-first candidates are listed.
Subagent / helper processes are marked `child` (rotation targets the
top-level session); two live processes on one session carry a loud `SHARED`
marker — resume once, never twice. Reads `/proc` and transcript filenames
only, never token contents. This is the missing link for a rotation executor: a rebalance names
an account, `who` names the pids on it.

### `helm homes [prepare <claude|codex> <email> | verify [<name>] | archive <name> | restore <name> | migrate <name> | archives] [--provider claude|codex]`
Credential-home lifecycle. Bare `helm homes` lists every home with identity,
liveness, and duplicate flags. helm prepares directories and verifies
structure; **you run every login** — helm never touches an auth flow.
Archive/restore are directory moves (archive-not-delete); live-agent homes
are refused.

`verify` (and the list's hygiene column) includes the **shared-family** audit
— the revocation bomb: byte-identical refresh tokens across homes mean copies
of ONE token family, and the vendor's reuse detection revokes the whole
family at once. Detection compares sha256 content hashes only (10-hex digest
prefix); token bytes are never printed, logged, or persisted. The only fix is
a fresh login per home — one home = one login = one token family. Doctor
mirrors it as the `cred token families` row (byte-copies = FAIL).

```console
$ helm homes prepare claude you@example.com
helm homes: prepared claude home you-example-com
  login (YOU run this): CLAUDE_CONFIG_DIR=~/.claude-homes/you-example-com claude login
```

### `helm codex [list | pool <name> | unpool <name> | pooled]`
The codexhome roster + proxy cred pooling — how helm manages codex accounts
(the `codex-credhome-proxy-pooling` premise, codified). Accounts live as
credhomes under `~/.codex-homes/<name>/` (`HELM_CODEX_HOMES_DIR`-overridable);
codex seats run `claude` against the local CLIProxyAPI, which pools creds
from its auth-dir (`~/.helm/_global/seats/codex/auth/`), **hot-reloads** that
dir, and falls through to a working cred when one is usage-capped.

`list` classifies every home by plan tier — `pro` = **ultra** (handles
multiple concurrent codexes), `team` = one codex each — with symlink aliases
and same-account dirs folded onto one row, plus whether the account is
currently pooled. `pool` translates the home's auth.json (nested `tokens`)
into the proxy's flat record and writes it 0600 as `codex-<name>.json` —
idempotent, and re-pooling IS the cure for the proxy 401-ing on a stale
copy (the codex CLI autorefreshes the home; a past access-token `exp` is a
refresh-first warning, never a death verdict). `unpool` removes the pooled
file (fail-open when absent); `pooled` lists what the proxy can draw on.
Sources stay read-only forever; token material never reaches stdout —
email / account / tier / paths only.

Note: `helm seat add codex` replaces only the pooled file(s) carrying the
SAME account it mints — the other pooled accounts survive a seat re-add
(the usage-cap fall-through stays intact; it prints what it replaced).

`capacity` is the one policy readout: per-pooled-cred `email tier seats` +
the fleet total (what the POOL holds — an unpooled ultra contributes 0),
plus the codex* seats live on the roster.

```console
$ helm codex list
helm codex: 4 codexhomes under ~/.codex-homes (2 pooled -> ~/.helm/_global/seats/codex/auth)
  you-example-com  you@example.com  ultra  1f04aa08…  pooled:codex-you-example-com.json
  other-example-com              other@example.com              team   9c21be77…  -
$ helm codex pool other-example-com
helm codex: pooled other@example.com -> ~/.helm/_global/seats/codex/auth/codex-other-example-com.json (team, account 9c21be77…)
  the proxy hot-reloads its auth-dir — no restart needed
$ helm codex capacity
helm codex: fleet seat capacity 4 (what the POOL holds, ultra=3/cred via HELM_CODEX_ULTRA_SEATS, team=1)
  you@example.com      ultra  3 seats
  other@example.com                  team   1 seat
  live codex seats: codex, codex-2
```

**N codex per credhome (slice 6).** An ultra credhome (plan pro, 20x) drives
N concurrent codex seats against the SAME proxy/pool — zero new proxies,
ports, tokens, or creds (the proxy hot-reloads one auth-dir and falls
through usage caps; requests are stateless). `helm seat launch codex -i N`
prints the pasteable line for seat `codex-N` (`-i 1` = today's exact line):
it swaps the three identity vars (`HELM_CHAT_NAME`/`HELM_CELL_PROFILE`/
`DREGG_PROFILE`) and the config dir (`instances/codex-N/claude`, born-wired)
so N instances share the proxy but never session/config state. Guards warn
on stderr (stdout stays the bare line) when N exceeds pooled capacity and
when the roster already shows a live `codex-N` — warn never refuses. Tier
is the policy key: ultra = `HELM_CODEX_ULTRA_SEATS` (dflt 3), team/unknown =
1. `helm seat status` shows `instances: k live / cap n` on the codex row.
Pool writes are atomic (0600 tmp + rename) so the proxy's hot-reload never
reads a half-written cred.

**`helm codex launch [-i N] [--force]` — the cred-% gate.** `helm seat
launch` warns but never refuses; the headroom discipline lives in this verb
 Before minting, it reads each credhome's OWN rollout logs
(`~/.codex-homes/<name>/sessions/**/rollout-*.jsonl`) for the newest
`rate_limits` event the codex CLI appends per turn — the same feed the usage
MCP forwards — and classifies every authed home: `ok` (freshest event <60m
old and every un-expired window <80% used), `near` (≥80%), `exhausted`
(100% or a `rate_limit_reached_type`), `unknown` (no rollout telemetry or a
stale tail — stale/unread is NOT ok). The launch proceeds only while at
least one POOLED cred reads `ok`; otherwise rc 1 with the concrete fix
(`helm codex pool <best-unpooled>`, preferring an ok ultra) — pooling, not
retrying. `--force` overrides (the gate advises, the operator decides; the
warn is loud). A green gate delegates to the exact `helm seat launch codex`
mint (`-i`/`--room`/`--model` pass through) — one mint path, the gate only
guards entry to it.

```console
$ helm codex launch -i 3
helm codex: launch gate (fresh = rollout tail <60m, near >= 80%)
  ok      owner-example-com  owner@example.com  ultra  pooled  12% used
  near    you-example-com    you@example.com     ultra  pooled  85% used
  unknown other-example-com                other@example.com                team   -       (rollout tail stale)
env -u ANTHROPIC_API_KEY ANTHROPIC_BASE_URL=http://127.0.0.1:8317 … HELM_CHAT_NAME=codex-3 …
```

### `helm keepalive [--home NAME|PATH] [--early HOURS] [--apply]`
Roll idle claude homes' OAuth tokens forward before their refresh chains rot.
**Dry-run by default:** without `--apply` it makes no network call, lock/log
file, backup, or credential write. Applied mode captures a stable pre-image
*before* the rotating grant and refuses the grant if capture fails; then it
persists the rotated pair atomically at `0600`. Codex homes remain read-only and
surface as stale-risk. Applied outcomes are logged to
`~/.cache/helm/keepalive-log.jsonl`; token and exception values appear nowhere.

## configs — every config, every home

### `helm configs [list | show <path> | cascade <cwd> [--harness claude|codex] [--home DIR] | edit <path> | backups | restore <backup-path>]`
The config model over every claude/codex home and project tree — MCPs, hooks,
skills, rules, memory files, settings, owner-authored `commands/*.md`, and
Codex `rules/*.rules`. `list` groups every discovered config file by scope and
deduplicates canonical symlink aliases; `show` prints one recognized regular
file; `cascade` resolves what a seat launched at `<cwd>` actually loads. `edit`
(new content on stdin) is the safety-first writer: validate + exact-byte backup,
same-directory atomic exchange, file/directory fsync, concurrent-revision
checks, and rollback, under allowlisted roots only. Symlinks, directories,
devices, non-UTF-8 and oversized files are refused; existing encoding,
newlines, mode and ownership are preserved. `restore` puts a backup back
(validated, and re-backed-up first).

```console
$ helm configs cascade ~/dev/myproject --harness claude
$ helm configs edit ~/.claude/settings.json < settings.json
```

### `helm hooks [install [--harness claude|codex] [--home NAME] [--dry] | status | sync [--apply]]`
The self-closing installer for the per-turn inject wiring **and the
fleet-delivery lane**. `install` merges the `UserPromptSubmit` → `helm inject
--hook-json` hook plus the delivery lane (`PostToolUse` → `chat deliver`,
`SessionStart` → `chat join`, `Stop` → `chat stop-guard` — the idle gate) into
every claude home's `settings.json` (the default `~/.claude` included) —
MERGE-preserving, idempotent (an up-to-date entry reports `ok`), on the
configs safety rails (backup → validate → atomic write, backup restored on any
failure). The generated command is fail-open by construction (`timeout` +
`|| true` — a broken helm never blocks a turn). `--dry` prints the would-be
diff per home; `--home NAME` narrows to one. The four entries close the loop:
turn start + tool boundary + session start + idle gate.

**The beacon permit rides every install.** The join directive's mandatory
first action — `Monitor(command: "helm chat wait … --follow")` — used to hang
every fresh session on a human permission prompt (no allow rule existed
anywhere). `install` now merges `Bash(helm chat wait:*)` +
`Monitor(helm chat wait:*)` into `permissions.allow` on every home AND seat —
additive, idempotent, existing entries never dropped, same gated write. A
fresh seat/home arms its beacon with NO human. `install` also SURFACES any
RUNNING pane whose named identity has no live roster row (a joined-late idle
pane can never self-heal — relaunch is the only repair).

**Seats are covered too.** A full `install` (no `--home` filter) ALSO wires the
delivery lane (deliver + subagent-end + join + stop-guard, **not** inject) into every
multimodel seat's isolated `CLAUDE_CONFIG_DIR`
(`<helm_home>/_global/seats/<family>/claude`), so a launched codex/kimi/… seat
receives `@<family>` and owner posts under its family name and cannot idle past
NEW ones (the stop-guard blocks once per pending-fingerprint; a re-stop on the
SAME rows passes) — same merge-preserving laws, a seat's own settings (theme, model,
permissions, any foreign hook) untouched. `status` shows a `seats (fleet
delivery)` block and a `seat delivery: N of M seats` line beside the per-home
coverage table (hook present / helm resolvable / fail-open intact, plus the
`deliver`/`join`/`stop` lane columns), mirrored by doctor's `inject coverage: N
of M claude homes` line. Codex-harness NOTIFY hooks are reported as
recipe-pending until [HOOKS.md](HOOKS.md) carries a mechanical shape.

```console
$ helm hooks install
helm hooks: inject (UserPromptSubmit): timeout 10 /path/to/helm/bin/helm inject --hook-json || true
  you-example-com    add    backup: none — new file
helm hooks: seats (fleet delivery — deliver + join):
  codex              add    backup: ...
  kimi               add    backup: ...
helm hooks: 5 of 5 claude homes covered
helm hooks: 2 of 2 seats covered (fleet delivery)
```

### `helm skills [dupes]`
Census of every skill across every home, read-only. `dupes` flags multi-homed
names (identical-everywhere = safe to consolidate vs diverged), same-content-
different-name copies, and hygiene issues (e.g. a skill dir with no SKILL.md).

### `helm skills sync [--apply]`
Universal skill distribution — the deliberate mutation verb the census
anticipated. One canonical skills source (an instance-home hub — a
gitignored symlink farm into the canonical skills repo, never the repo's own tracked dir;
`HELM_SKILLS_CANONICAL` overrides), and every claude-code config dir — each real
credhome under `~/.claude-homes` (alias symlinks fold onto their target), the
default `~/.claude`, every seat and seat-instance `CLAUDE_CONFIG_DIR` — becomes
a whole-dir symlink to it. Two phases, dry-run by default:

1. **merge** — union every stray (an entry a REAL skills dir holds that
   canonical lacks, or that outnews canonical's copy — newest-wins) into
   canonical; displaced canonical entries land in the backup root first.
2. **wire** — repoint each config dir's `skills/`. A real dir is MOVED whole
   into `~/.skills-premerge-backup/<home>/` (the backup IS the original), the
   symlink lands atomically (tmp + rename), and a post-swap superset check
   proves every previously-visible skill name is still visible — or the move
   rolls back. Indirect chains are normalized to direct links.

Idempotent: a wired estate reports zero changes, so re-running after minting a
new credhome (or adding a skill to canonical) is the whole maintenance story.
Seat mint (`helm seat add`) links new seats to the same canonical at birth.
This kills the stranding class where a skill dropped into one home's private
farm stayed invisible everywhere else (observed live 2026-07-21).

## tidy — the estate janitor

`skills sync`'s shape, widened from skills to the whole claude-config estate:
census the picture, reconcile every home's hooks + MCP servers to a NAMED
canonical set, gc the git worktrees. **Dry-run is the DEFAULT everywhere** —
every destructive step needs an explicit `--apply`, and every mutate is
backup-first (`~/.env-premerge-backup/`, the backup IS the pre-image), atomic
(tmp + rename), superset-checked (a foreign hook, an existing MCP server —
never lost), idempotent, and fail-CLOSED (a `settings.json` that will not parse
is reported and SKIPPED, never a partial destructive apply). One discovery
feeds all of it: `skillsync.config_dirs()` — every credhome (alias symlinks
folded), the default `~/.claude`, every seat + seat-instance, smoke dirs
skipped.

### `helm env census [--json]`
READ-ONLY: the whole estate in one screen — every config dir's helm hooks,
which canonical hooks it is MISSING, the stray (non-helm) hooks it carries
(repo-hygiene, herdr, orca — legit on the host home, always PRESERVED), its
effective MCP names, the MCP variance vs the canonical set, and the orphan /
stray / lane worktree snapshot. Replaces poking the configs UI by hand. Probes
nothing, mutates nothing.

```console
$ helm env census
helm env census — 9 config dir(s)
  home                       kind      hooks miss strays
  you-example-com  home      8     0    0
  owner-example-invalid        home      8     0    1
  seat:codex                 seat      3     0    0
...
```

### `helm hooks sync [--apply]`
Reconcile every home's hooks to the NAMED canonical set — the survey's
`proposed_canonical_hooks`, a module constant `CANONICAL_HOOKS` with a clear
docstring and a `HELM_HOOKS_CANONICAL` env override (a JSON file of
`[{event, matcher?, args, timeout?}]`), exactly like `skills sync`'s
`canonical()`. Credhomes + the default get the full 8-hook set (inject, record
×2 events, chat deliver, chat join, stop-guard, handoff ×2 events); seats get
the LEAN delivery subset (deliver + join + stop-guard) derived from the same
constant — a seat never ground-injects, records, or writes handoffs. **Additive
only**: missing canonical hooks are ADDED (reusing the merge-preserving
`hooks._merge_event` primitive), strays PRESERVED; the post-write superset
check refuses any swap that would drop a foreign hook, restoring the backup.
Dry-run prints the per-home diff; idempotent (a wired estate reports zero
changes). Reachable as `helm hooks sync` beside `install`/`status`.

### `helm mcp sync [--apply]`
Same shape for MCP servers. `CANONICAL_MCPS` names the servers every home
should reach (`HELM_MCPS_CANONICAL`, a JSON file `{name: config|null}`,
overrides). Fail-CLOSED by construction: a server is ADDED to a home's
`.claude.json` `mcpServers` only when (a) the name is missing from the home's
EFFECTIVE set (not already provided by a plugin or a raw entry) AND (b) a
concrete config is in hand — otherwise the gap is SURFACED to the owner, never
guessed (blindly minting a raw `team-tools` entry where a plugin already
provides it would create the exact duplicate-shadow the census warns about).
The default canonical carries no configs, so `--apply` is report-only until an
override supplies them. Backup-first, superset-preserving (an existing server
is never dropped), idempotent.

### `helm worktree gc [--apply]`
Prune orphan `worktree-*` and `lane/*` branch stubs + landed estate worktrees.
**COMPOSES** `helm work gc`, which exclusively owns guarded `<repo>-wt/` lanes
and harness-minted `.claude/worktrees/` rooms, then scans only the remaining
unmanaged estate. Missing records follow the same split: lane/harness phantoms
are delegated once; remaining-estate phantoms use the VCS seam's exact,
metadata-only record removal, which never deletes a checkout that revives after
the absence proof. Peek records under `<repo>-wt/peeks/` belong only to `helm work peek
--drop`. The rules the survey pins: **RESCUE-DIRTY-FIRST**, never touch LOCKED or
meaningful OCCUPIED worktrees, and never remove work ahead of the base.

**LANDED IS ASKED BY CONTENT, NOT BY SHA.** A stub retires when its commits are
on the trunk by ancestry **or** by patch identity (`git cherry`, stable across
rebase). That is the difference between a reaper that works and one that does
not: helm lands work REBASED, so the commit on trunk carries a different sha,
ancestry answers "not merged" *truthfully*, and every well-behaved lane
accumulates forever — measured 2026-08-03 at 107 `lane/*` branches against 24
branch-holding worktrees.

**THE TWO PROOFS ARE NOT THE SAME GRADE OF FACT, AND THE CODE PAYS FOR THE
DIFFERENCE.** Ancestry is reachability: after `git branch -d` the tip is still
on the trunk, so nothing can be lost. Patch identity proves only that every
commit's DIFF is upstream — the bytes survive, the commit objects and their
messages do not. So a patch-identity retirement DEMOTES instead of deleting:
the tip is written to `refs/helm-retired/<branch>` **first**, the write is read
back, and the branch is removed only if that succeeded. The objects stay
permanently reachable (immune to `git gc`), the sidebar clears because Orca
renders `refs/heads/*`, `git for-each-ref refs/helm-retired/` is the audit
trail, and restore is one command. A failed or unverifiable preservation KEEPS
the branch.

Every retirement names which proof authorized it. Everything else KEEPS: a
partly-landed stack, an unreadable object, a range past the cap, any commit
`git cherry` cannot speak for (it skips MERGES, so the range is counted
independently and every commit must be accounted for), and any range
containing an EMPTY commit (an empty diff has an empty patch-id, so every
empty commit is "patch-identical" to every other one — that supplies no
evidence at all). Dry-run classifies every row, and apply re-proves landedness
plus lock + occupancy + metaharness panes before removal.

### `helm tidy [--apply]`
The umbrella: census + hooks sync + mcp sync + worktree gc, all in DRY-RUN, one
consolidated owner-facing report — *here is everything that would change*.
`--apply` runs them all backup-first.

## rearm — land-to-live compression

Code lands on main and every fresh `helm` invocation is a process off main, so
a CLI-class land is live AT LAND. But LONG-LIVED processes keep running the code
they loaded at start: an armed `helm chat wait --follow` inbox beacon, the web
service, seat proxies/daemons. Until each re-arms, the fix has not reached them.
The fleet re-arms them ad hoc today, and an UNOWNED mass-SIGTERM of waiters is a
known incident class (three beacons killed in one minute, unexplained to their
owners). `helm rearm` is the CHEAP OWNED pass that closes the gap minutes after
a land batch (premise `land-to-live-compression-owner-directive`).

### `helm rearm [--apply] [--json]`
**Dry-run is the DEFAULT** — it reports, mutates NOTHING: (a) every live `helm
chat wait` waiter with its owning seat and start time, marked **STALE** if it
started before main's current HEAD commit time; (b) the web-service unit
(`helm-web`, active + since) and whether it predates HEAD; (c) every OTHER
long-lived helm process predating HEAD (proxies, daemons — by cmdline match) as
an **advisory** respawn candidate. It ends on a plain summary line: *N waiters
stale, web stale?, M advisory*.

`--apply` runs the OWNED re-arm pass, in order: **(1)** posts ONE owned
**ambient** ANNOUNCE row to `#main` (ambient = renders everywhere, wakes nobody)
saying the pass is cycling waiters and why; **(2)** SIGTERMs ONLY the stale
waiters — each owning agent gets its Monitor-exit notification and re-arms on
the new code at its OWN turn boundary (this is the OWNED version of the
beacon-killer incident class); **(3)** restarts the web unit **iff** it is active
AND stale; **(4)** NEVER touches proxies/daemons/seats — they print as advisory
only. Idempotent by convergence: staleness is recomputed from live state each
run, so once the signaled waiters exit a second `--apply` finds nothing stale (a
re-signal of a still-dying pid is a harmless no-op).

**Safety — failed-probe-is-not-absence.** Only a process whose cmdline argv
EXACTLY matches the waiter shape (a `helm` executable token immediately followed
by `chat wait`) is ever signaled; the bash Monitor wrapper carries the same
string inside a single `-c` argument, has no standalone `helm` token, and is
excluded by construction. Ownership is the `--seat` token; a stale waiter with
no readable seat, an unreadable start, or an unreachable HEAD (staleness
unprovable) is SKIPPED and reported, NEVER signaled. The argv shape and stat
starttime are re-checked immediately before each SIGTERM, so a waiter that
exited into a recycled pid during the announce round-trip is never hit; and if
the announce itself fails to land, the pass is **fail-closed** — nothing is
signaled or restarted. Reads `/proc` cmdline + stat only.

**The ANNOUNCE row carries the re-arm incantation.** An idle owner is *woken*
by the Monitor-exit event and re-arms at that turn; a busy owner keeps its
mid-turn hook delivery — so the deaf window is bounded to the re-arm turn. But a
resumed/cleared agent can lose its beacon context entirely, so the row spells
out the exact incantation
(`Monitor(command: "helm chat wait --seat <your-seat> --follow", persistent: true)`)
and names the seats it cycled. This is the rearm-INITIATED path (the waiter dies
from outside); the SELF-rotation recipe an agent uses to swap its OWN beacon
without a deaf window is the reverse — **arm the successor Monitor FIRST, verify
one live event lands through it, THEN `TaskStop` the old** (`helm chat wait` has
no handoff, so a stop-then-arm has a real deaf window).

**Proxies never self-propagate a land.** A per-instance or family cli-proxy
keeps running the config it loaded until respawned; `rearm` lists each pre-HEAD
proxy/daemon as advisory with the recipe
`helm seat down <seat> && helm seat up <seat>` (or the daemon's own restart) —
per-seat respawn stays the operator's call, never `rearm`'s signal.

```console
$ helm rearm
helm rearm — land-to-live: live processes still holding pre-HEAD code (dry-run; `helm rearm --apply` cycles the stale waiters)
  HEAD 1a2b3c4 committed 12m ago
  waiters (helm chat wait):
    STALE   pid 1656     seat codex-3                started 41m ago
    current pid 4174464  seat codex                  started 3m ago
  web-service: helm-web stale since 40m ago  [--apply restarts]
  advisory (pre-HEAD long-lived helm procs — respawn candidates, NEVER signaled):
    pid 88123    helm router         started 2h ago
helm rearm: 1 waiter stale, web stale, 1 advisory
```

### `helm beacons [--seat S] [--json]`

The inbox-beacon **registry census**. A seat's beacon is its ONLY wake path —
nothing external can re-invoke a PTY agent — so a seat whose beacon is gone
cannot be reached by anything, ever, and it looks completely normal from
outside.

**Liveness is a LIVE SESSION behind the shape, not an argv match.** A
`helm chat wait` process left behind by a DEAD session satisfies a shape check
perfectly. Each beacon is therefore judged on the session it was armed under:

| state | meaning |
|---|---|
| `live` | its launcher is alive **and** a live process still holds the session it was armed under |
| `ghost` | its **launcher is gone** (it was reparented to a reaper), or the process that held its session is **proven gone** |
| `unknown` | liveness could not be proven — including *"the launcher is alive but now holds a different session"*, which is what a healthy seat looks like after a compaction |

Two death proofs at different layers, and they are not folded together:
`orphaned` says the pipe's **reader** is gone, session state says the
**session** behind it is gone. A beacon can be orphaned while its session id
still resolves, and it still wakes nobody.

**Orphanhood is not `ppid == 1`.** A user session runs under `systemd --user`,
which sets itself as a *child subreaper*, so an orphaned beacon reparents to
the **user manager** and init never sees it. Measured across all 22 live
beacons on one box: zero had ppid 1, while the one genuinely orphaned beacon
had the user manager as its parent and had been beating into a dead pipe for
33 hours. A beacon whose parent cannot be named is `unknown`, and unknown keeps
the process.

**The alarm is three-directional**, and reporting one direction is how the
count reached 48 while every check said fine:

* a **DEAF SEAT** — no live beacon: nothing can wake it.
* a **GHOST WAITER** — a beacon whose session is dead: it consumes the seat's
  addressed rows into a pipe nobody reads, *and* it makes a dark seat read as
  covered.
* a **VACANT SEAT** — the exact inverse of a ghost: a **live** wake path for a
  seat with **no agent**. A ghost is a dead pipe for a live seat; a vacant seat
  is a live pipe for a dead one. Both are the same error — treating the
  instrument as the thing it measures.

**A beacon is a `helm chat wait` python process, not an agent**, so a live
beacon proves the wake path and says nothing about the seat. A credential-wall
pause deliberately leaves that process armed: the delivery actuator checks
proxywatch's live family-dark latch before touching presence or a cursor, so the
same waiter stops spending turns while the provider is dark and resumes after a
measured `HEALTHY` result. `helm beacons` still reports the process shape; the
proxywatch row and owner roster badge say `PAUSED-CRED-WALL`, and addressed rows
stay pending rather than disappearing into the paused pipe. The agent question
is asked with **different evidence than the rung it checks**: what a PANE
DECLARES ABOUT ITSELF (`HELM_CHAT_NAME`, or a `CLAUDE_CONFIG_DIR` directly
under this home's seats dir). A cross-check built on the session/roster binding
the `live` rung already uses would agree with it by construction.

**An unstamped pane is UNPROVEN, never VACANT.** A hand-launched `claude` joins
the roster under a derived name and stamps nothing — 8 of the 16 live panes on
the box this landed from are exactly that. So VACANT is refused whenever the
process the beacon actually writes into declares no seat of its own: an
unattributable pane may BE this seat's agent. Only when that process is
provably **somebody else's** agent is the house proven empty. Calling a working
seat vacant is worse than the bug it fixes — a reader would stop addressing
work to a seat that is working.

**It signals nothing.** A superseded beacon is stopped by its own seat's next
re-arm: `helm chat wait --seat <seat> --follow` is now STOP-then-START, and a
seat only ever stops its own. Reaping by pattern across a shared process table
is how a live seat's only wake path gets severed: a developer box routinely
runs seats belonging to other projects' fleets, and a pattern match over the
process table cannot tell them apart from this one's.

```console
$ helm beacons
helm beacons — the inbox beacon is a seat's ONLY wake path
  codex                  covered   live 7  ghost 0  unknown 0   (+6 surplus)
  gemini                 DEAF      live 0  ghost 1  unknown 0
  kimi                   UNPROVEN  live 0  ghost 0  unknown 1
  retired-seat           VACANT    live 1  ghost 0  unknown 0
  DEAF SEAT gemini — no live beacon: nothing can wake it. It must re-arm `helm chat wait --seat gemini --follow` before it can be reached at all.
  VACANT SEAT retired-seat — its wake path is LIVE and nobody is home: 1 live beacon consumes its addressed rows, and no live pane declares this seat, and the process behind its beacon (pid 42546) is seat codex's agent. A DM to it is swallowed exactly as a ghost's is — the inverse case, a live beacon for a dead seat. Only a relaunched agent (or that seat's own de-arm) ends it.
  UNPROVEN kimi — no beacon could be PROVEN live (1 unknown). Not a deaf seat and not a healthy one; the instruments could not answer.
  GHOST WAITER pid 798715 seat gemini armed 33h ago — its launcher is gone — the beacon was reparented to a reaper, so nothing reads the pipe it writes wakes into. It consumes that seat's addressed rows into a pipe nobody reads, and it satisfies a naive shape check, so it makes a dark seat read as covered.
helm beacons: 4 seats, 1 covered, 1 DEAF, 1 VACANT, 1 UNPROVEN, 1 ghost waiter, 10 beacons (6 surplus)
```

`surplus` is the accumulation, counted in BEACONS and never in matching
processes: each beacon is a process PAIR (a `bash -c` wrapper and its python
child), so a `pgrep` count is double the real figure. Only the python child is
a beacon here — matching the wrapper would kill the shell and orphan its child.
Surplus is reported rather than reaped: the seat's own next re-arm collects it.

```console
$ helm tidy
========================================================================
helm tidy [DRY-RUN — here is everything that would change]
========================================================================
[1/4] CENSUS   ...
[2/4] HOOKS SYNC   every home carries its full canonical hook set (0 changes)
[3/4] MCP SYNC     every home reaches the canonical MCP set (0 changes)
[4/4] WORKTREE GC  ...
helm tidy: dry-run complete — `helm tidy --apply` executes
```

## ready — the fleet-readiness gauge

### `helm ready [--json]`

**May forward work resume after a crash/reboot?** Five ratified signals,
each a READER of an authority that already exists — the gauge
composes, it re-implements nothing:

| signal | authority read | red means |
|---|---|---|
| `daemon` | the detected metaharness adapter's own pane probe (orca: one 5s-bounded socket round-trip) | the daemon is installed and not answering |
| `seats` | the spawn register + `seat_liveness` per registered seat | a registered seat's pane is GONE, or its agent exited under a live pane |
| `beacons` | `beacons.census()` — the attendance question | a seat is unreachable, vacant, or ghost-eaten: nothing can wake it |
| `families` | proxywatch's **latest recorded** sweep — never a fresh canary | *(no red arm)* — see the wall law below |
| `checkout` | two git reads over the shared checkout (the MAIN root of the tree this helm came from) | dirty, or off origin/main as last fetched |

**ADVISORY, ratified**: the gauge renders red/green per signal and overall;
it **never refuses and never gates a dispatch** in this iteration —
enforcement is a later decision, after one real reboot has exercised it. It
is the instrument for 0.3's reboot-and-resume gate.

**UNKNOWN never qualifies.** A signal whose instrument cannot answer — a
blind spawn register, a failed session-liveness probe, a stale proxywatch
record, a tree with no git — reads UNKNOWN carrying the instrument's own
reason, never a confident green. The composed verdict follows the same law:
one red = `NOT READY`; no red but one UNKNOWN = `UNKNOWN`; all green =
`READY`.

**A wall is a fact, not unreadiness.** A family proxywatch records dark
WITH A NAMED CAUSE (`QUOTA-402`, `RATE-LIMITED`, `AUTH-401`, …) is WALLED:
it rides a green row's note and keeps `READY` — work routes around a wall.
Only an UNMEASURED family blocks the verdict.

**Exit codes, distinct on purpose** (beacons' precedent — a fleet measured
broken and a gauge that could not measure must never be the same signal):
`0` READY · `1` NOT READY · `2` cannot prove.

The console renders the same gauge as the READY card on the home tab
(`/api/ready`, 30s single-flight server cache, 60s poll).

```console
$ helm ready
helm ready — may forward work resume? ADVISORY: this gauge renders; it never gates a dispatch
  GREEN   daemon    orca daemon answering — 19 panes listed
  UNKNOWN seats     1 of 7 panes unprovable — grok: stale-handle
                    -> `helm seat where <seat>` has the per-seat detail
  RED     beacons   unreachable: claude-6, claude-7
                    -> `helm beacons` names each fault; a seat re-arms its own beacon on its next turn (`helm seat resume <seat>` for a dead one)
  GREEN   families  4 families HEALTHY: codex, ds4pro, gemini, grok
                    WALLED: kimi AUTH-401 since 2026-08-04T21:41:05Z — a wall is a fact, not unreadiness
  GREEN   checkout  shared checkout ~/dev/helm clean at origin/main (1a2b3c4d5e6f, as last fetched)
helm ready: NOT READY — 3 green, 1 red, 1 unknown
```

(A real run, host names redacted — the gauge's first live read
found two seats whose beacons were dark and a stale pane handle the fleet
had not noticed. Honest reds are the product; a gauge that always reads
READY is decoration.)

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

### `helm chat [post <text...> [--dm SEAT] [--reply-to <id|n>] | reply <id|n> <text...> | read [--since N] [--follow] [--dm] | rooms | react <n> <emoji> | verify | dm <seat> <text...> | log-flush | node up|down|status] [--room R]`
One shared conversation log + notify + read/write loop, **owner in the room**.
Rooms live in RAM (`/dev/shm/helm-chat/<room>.jsonl`, dir 0700, default room
`main`; `HELM_CHAT_DIR` overrides) — ephemeral presence-chat, not the durable
record: past ~2 MB the oldest half rotates out, so `/premise` anything that
must outlive the room. `post` writes as `$HELM_CHAT_NAME` (else the best local
identity guess: session, then user). `read` prints the room (`--since N` skips
the first N messages); `read --follow` polls and prints new lines until Ctrl-C.

**The signed transport (v2).** Signing is **opt-in and unsigned by
default**: it needs BOTH the explicit cell binary (`HELM_CELL_BIN` — unset ⇒
off, never a `PATH` probe) and a live chat **room node** — a dregg node whose
data-dir lives on tmpfs, so a chat turn never lands on a disk-persisted
chain. With both, every post also rides a **signed self-write turn** on the
poster's cell there: the turn payload carries the message digest
(`chat:b2b:<blake2b-256>`), the RAM room carries the text (thin claim, fat
corroboration — the same pattern as premise attestation, see
[ATTESTATION.md](ATTESTATION.md)). Signed rows render clean (the web panel
shows a subtle ✓ tick, chain index on hover). A node/sign/join/faucet failure
falls open to the v1 row, tagged `[unsigned]` — the message never dies, but the
failure is **LOUD and sticky**: the row records a secret-scrubbed structured
`transport` diagnostic, the exact per-profile incident stays on tmpfs
(`/dev/shm/helm-chat-failures/<room-key>/.sign-failures.json` when the room
path itself is not tmpfs), and CLI/TUI/web/doctor all report `DEGRADED`
with reason, profile, first/last failure, age, count, and remediation until a
successful signed turn by that profile clears it. No signer configured is not
an incident: it keeps the same honest tag with zero signing traffic (no probe,
no unlock), and status says `unsigned (no signer)` rather than letting a
reachable node imply signed posts. Agents sign as `HELM_CELL_PROFILE` (else
`helm-agent`); the owner's web posts sign server-side as the owner's derived handle. `helm chat
node up` provisions
the room node (`helm-chat-node.service`, `dregg-cave-node` on
`/dev/shm/helm-chat-node`, port 8898, faucet ON — the node auto-funds joining
cells and helm tops up before each turn: **chat turns never die on
computrons**). The transport is node-agnostic (`HELM_CHAT_NODE_URL`; empty
disables) — the node migration (`scripts/node-migration.sh`) just
repoints it.

**The log-after leg.** Ordinary chat keeps RAM pure in the hot path (premise
`a2a-ram-only-disk-log-after`): `helm chat log-flush` is the only bulk
transcript writer, an out-of-band append of delivered history to
`~/.helm/helm/journal/chat-<date>.log` — idempotent (per-room high-water
mark; a rotation gap is logged loudly), run by the operator, a cron, or
`helm --human` at exit. `HELM_CHAT_LOG=0` disables that transcript log. The
narrow exception is the internal `chat.post(..., event_id=...)` API for
retryable machine announcements: after the RAM append and before rotation it
writes a small 0600 operation receipt under a durable, bus-keyed
`~/.helm/_global/.state/chat-event-receipts` root. An explicit
`HELM_CHAT_DIR` still keys the same host-wide durable root rather than putting
proof back on tmpfs; `HELM_CHAT_EVENT_DIR` overrides that root for isolated
estates. The receipt contains the first row and exists only to make a lost
acknowledgement fail closed and deduplicate across rotation/reboot; it does not
flush unrelated chat history.

**Emojis + reactions.** `:fire:` → 🔥 at post time on every surface (CLI,
web input, TUI input — ~140 shortcodes, `helm/emoji.py`). `helm chat react
<n> :tada:` attaches a reaction to message *n* (1-based; `-1` = latest),
rendered inline (`🎉×2`) in the web panel and TUI; the web panel gets
click-to-react on hover. Reactions ride the same transport as posts (signed
turns on v2). Nothing gates emojis to humans — **agents are encouraged to
emoji and react like anyone else in the room**.

**Replies (one level).** `helm chat reply <id|n> <text…>`
(or `post … --reply-to <id|n>`) threads a message under a parent: the row
gains `{reply_to, rts, rfrom}` — the parent's **stable row id** plus its
`(ts, from)` pair. A parent that predates the id law also records `rtext`, so
same-second twins resolve by exact `(ts, from, text)` rather than whichever
row indexed first. The reference is a row id, an unambiguous id prefix, or a message ordinal (`-1` =
latest). Rendering is **one level, never nested**: a reply shows a compact
`↳author "quote"` of its parent, a parent shows `↩N`, and an orphan (the
parent rotated out of the RAM room) renders as `(parent rotated out)` — in
`helm chat read`, in the journal, and in the web panel (where the quote is
clickable and `↩N` jumps to the first reply).

Signed replies **bind the parent**: the payload is a distinct algorithm tag,
`chat:reply:b2b:` over RS-joined, injectively escaped parent fields + text
(a pre-id parent also binds `rtext`) — a separate tag, never an in-band
prefix, because the text is
attacker-chosen and an in-band prefix would let a plain post mint a reply's
digest (free re-parenting). Plain posts stay **byte-identical** to v2, so
every signed row already on disk verifies unchanged. `helm chat verify`
re-derives each row's payload through the one shape dispatcher
(`payload_for`) and compares it to the payload the signed row records —
self-consistency (a hand-edited parent pointer or text is caught), *not*
remote re-verification: the node still cannot disclose a turn's payload
(`cell.verify_anchor` — "payload binding unavailable"). Rows signed *before*
that field existed report `legacy` (unverifiable, never a false alarm) — but
a signed row that IS a reply and records no payload is a **MISMATCH**, not
legacy: `reply_to` and the recorded payload shipped together, so deleting the
payload to dodge the check is itself the tell.

A reply names its parent by **row id**, so a parent that has rotated out is
an orphan — never its `ts|from` twin. (One seat posting twice inside a second
shares that key and rotation can split the pair; resolving the twin would
quote words the author never wrote under the reply.) A parent that never had
an id resolves only through an exact `(rts, rfrom, rtext)` match; ambiguity or
rotation renders an orphan rather than guessing.

**A reply wakes its parent's author.** `seats.deliverable()` reads `rfrom` —
the parent author stamped at post time — and treats a reply to a seat's row
as a direct address of that seat: mention-tier, any room, before mute (the
owner's WHY: replying *instead of* typing the @mention). Only the parent's
author wakes; for every other seat the pointer changes nothing, so a reply
stays quieter than the mention it replaces. (Tested as a law, not an
observation: `tests/test_chat_reply.py::ReplyWakeTest`.)

The notify loop: when the owner posts (web panel or `helm --human`), helm
drops a `<room>.owner-unread` marker and the shipped `owner-chat-unread`
reflex steers every local agent's **next turn** to read and reply — any
`helm chat read` that consumes past the owner's post clears it. Agents never
poll; the already-installed inject hooks deliver the nudge. Identical in
both transports.

```console
$ helm chat post "seat B: web slice landed :rocket:"
$ helm chat reply -1 "on it — rebasing the lane now"   # threads under it
$ helm chat post "…" --reply-to 4f2a1c                 # by row id (or prefix)
$ helm chat verify               # re-derive every signed payload
$ helm chat react -1 :tada:
$ helm chat read --since 40
$ helm chat read --follow        # the owner's orca pane sidecar, exactly this
$ helm chat node status          # room node + chain head + cell balances
$ helm chat log-flush            # the durable record, out-of-band
```

**Chat path (the AX↔UX law — the owner never types the CLI).** Say it and
your agent runs it:

- "**tell the fleet:** …" / "**post in helm chat:** …" → `helm chat post "…"`
- "**DM codex:** …" / "**tell ONLY codex:** …" → `helm chat dm codex "…"`
- "**reply to that**" / "**answer codex's question**" → `helm chat reply <n> "…"`
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

### The delivery lane — chat at tool boundaries (the meld-half port)

Meld carried a
separate tmpfs whisper channel because it had no room; helm has the room, so
the lane is a **read cadence over the same room** — zero new transports. (The
word "whisper" stays reserved for inject's first-turn brief and the v1 ledger
frames; this lane is called *delivery*.)

- **`helm chat join [--hook-json] [--seat S]`** — the SessionStart autojoin:
  writes the seat's RAM roster row (`.roster.json` in the room dir, keyed on
  `HELM_CHAT_NAME` so a launched seat joins under its family name — `codex`,
  `kimi`, …; an UN-named join gets a meaningful stable auto-name,
  `<project>-<family>` like `helm-fable`, deduped — never opaque
  `agent-<sid8>` hex) and hands the session its
  identity + protocol line as context. That line makes it a **mandatory first
  action** to arm the idle-wake beacon — `Monitor(command: "helm chat wait
  --seat <seat> --follow", persistent: true)` — because nothing external can
  re-invoke a PTY agent (native-wake-only-agent-armed), so the self-armed
  Monitor is the only thing that wakes an idle session. The line ends with the
  onboarding pointer — new seats read
  [NEW_AGENT_GUIDE.md](NEW_AGENT_GUIDE.md) for their bearings. Never posts to
  the room (presence lives in the roster panel, not the transcript).
- **`helm chat deliver [--hook-json] [--seat S]`** — the PostToolUse nudge:
  an agent deep in an autonomous turn is unreachable by turn-start injection;
  this delivers between tool calls. At most ONE row per boundary (oldest
  first, the rest collapse to `+N waiting`), 200-byte clip on a codepoint
  boundary, control-char scrub, labeled information-not-instruction. What
  delivers — the **beacon scope** (premise
  `beacon-scope-mentions-plus-home-room-owner-posts-not-all`): a `@seat`
  mention or a DM from **any** room, always; **anything** posted in the
  seat's HOME room (its roster `home_room`) — a boundary-scope tier the idle
  *beacon* drops by default (see `chat wait` below); `@all` broadcasts in
  `{home, main}` only — never fleet-wide across side rooms, and owner-rail
  posts are NOT a wake class of their own (owner steer: an owner
  post reaches a seat via an @mention or its home room); foreign-room chatter
  never (noise law). `helm chat seat mute
  <room>` tunes the noise classes off per seat (mentions + DMs still
  surface). **Multi-room**: the
  boundary considers EVERY live channel — the seat's DM lane first, then
  main, then the newest-activity rooms, bounded (16 rooms/pass, each read
  SCAN_CAP-capped, home + main pinned so foreign volume never evicts them) —
  so an @mention in a room the seat never joined delivers, labeled with its
  channel (`[helm chat #room → seat]`, `+N waiting — helm chat read --room R`).
  A tracked seat meeting a room born after its join backfills from the
  room's birth; an untracked seat EOF-baselines everywhere (pre-join backlog
  never floods). Before presence or cursor mutation, delivery reads
  proxywatch's persisted family-dark latch. A credential-walled family emits no
  boundary or idle wake, advances no cursor, and leaves every addressed row
  pending. The already-armed waiter re-reads that latch each poll: `UNKNOWN`
  during a known dark episode holds the pause, while a measured `HEALTHY` result
  clears it and drains the backlog without a restart. The primary observer file
  and its byte-equivalent last-good snapshot are one canonical state: deletion,
  corrupt JSON, or wrong-shaped JSON recovers the last latch instead of inventing
  HEALTHY; only a genuine first run with neither snapshot delivers unclassified.
  Rotation preserves every paused cursor's unread suffix even when that
  temporarily exceeds the room cap. Stop does not block or continue on the held
  inbox, and an armed paused waiter remains the dispatch owner even though its
  honest `.seen` beat ages. Every unpaused fire refreshes the seat's roster `last_seen` — presence needs no
  daemon. The hot path is ~one stat per quiet room; `HELM_CHAT_DELIVER=0` kills the lane;
  fail-open total. The same universal boundary silently records delegation
  interval evidence only when Claude Code supplies a nonempty subagent
  `agent_id` and the payload cwd is exactly a lane claimed by this seat/session.
  The record binds the exact resource, lease nonce and enclosing Claude holder
  PID/starttime; the activity namespace also hashes the canonical room, so a
  same-basename foreign clone cannot overwrite it. Main-thread tool calls
  cannot mint it. `SubagentStop` writes a full-session/agent tombstone
  independent of claim-lock contention; the next claim-locked read rejects and
  prunes it, so teardown cannot erase a concurrent agent's evidence and a
  finished subagent does not inherit the long-lived parent process. The
  owner-unread marker is untouched (only a real
  read consumes it).
- **`helm chat wait [--seat S] [--any] [--follow] [--ambient] [--timeout N]`**
  — the
  beacon: block until the next word addressed to the seat (a delivery — it
  advances the cursor, so the hook never re-nudges), or any new row with
  `--any` (cursor untouched). **`--follow`** is the idle-wake mode the join
  directive arms: it never returns on a match — it streams EACH new matching
  row as one line (one line = one agent wake) and returns only on
  `--timeout`. The beacon's default scope is **MENTION-ONLY**: `@seat`
  mentions, replies to the seat's rows, DMs and `@all` wake it — ambient
  home-room rows do NOT (each ambient wake burns a full idle turn; premise
  `mute-busy-home-room-trust-mentions`, owner directive). A row
  the strict beacon skips is still consumed — the home room becomes a pull
  surface (`helm chat read` on wake), exactly the muted-room semantics.
  **`--ambient`** opts a quiet-room seat back into full home-room wakes;
  single-shot seat mode keeps the full delivery scope (home room included) —
  it is a delivery, not a beacon. Seat
  mode (single-shot and `--follow` both) watches **every room** — a mention
  in a channel the seat never joined wakes it, per-room cursor per (seat,
  room, session) so the beacon and the boundary hook never double-deliver
  (`--any` stays one room's tap). A
  persistent Monitor armed on `wait --follow` is meld's SSE watcher, natively;
  fail-open + bounded poll, so a delivery hiccup never crashes the beacon.
- **`helm chat stop-guard [--hook-json] [--seat S]`** — the IDLE GATE (Stop
  hook; the work-arbiter capability, helm-native). Posture resolves
  once, checks run inline, and ALL blocking messages surface in ONE exit-2
  (fix everything in one shot); WARN lines ride along without changing the
  exit. Checks: **BLOCK** on undelivered @mentions/owner rows past the seat's
  cursor in ANY room (side-room rows carry their `[#room]` tag) — listed
  compactly, once per pending-fingerprint (a re-stop on the
  SAME rows passes; any new row re-arms — never an infinite block loop;
  `stop_hook_active` suppresses this block but surfaces the undelivered count
  as a WARN alongside claim evidence); **BLOCK** on
  live claim leases held by the stopping session. The block's HEADER only
  says why the stop is held; EACH LANE LINE OWNS ITS OWN INSTRUCTION — its
  exact release command, or plainly why none is offered — and no line
  inherits a promise from the header (the same-state one-liner a re-stop
  compresses to describes the sermon's structure, never its content). For
  every held LANE lease the guard makes **FOUR READS** of the claimed room,
  each through the primitive that already owns it: **WORKING TREE**
  (uncommitted bytes via the room-status authority), **LANDEDNESS**
  (ancestry OR patch identity vs the trunk), **REVIEW** (the owed dispatch
  frontier on this lane family, repo-scoped three ways — a row stamped with
  this room's repo identity counts, a row stamped with another repo's is
  excluded, and a legacy row carrying no repo identity contributes UNKNOWN
  rather than same-project proof), and **GATE** (a LIVE pid in the room's
  own in-flight marker dir — a liveness proof, never a receipt mtime). The
  reads are DIAGNOSTIC, NEVER PERMISSIVE, and every read that cannot be
  made degrades to a TYPED, COUNTED UNKNOWN — the line says "N of 4 reads
  could not be made" with each read's reason; an unreadable gate census or
  dispatch ledger is UNREADABLE, never a clean empty. **NO BLIND COMMAND**:
  a lane helm could not prove idle — any UNKNOWN, or even all four reads
  clean (the reads cannot see a delegate that has not written a byte yet,
  which is the first minutes of every delegated build) — gets NO
  copy-pasteable release command, only the reason; a MEASURED room gets its
  unfinished work NAMED beside its exact command, as a fully-informed
  line. A lane lease is EXEMPT (warn, lease retained) with one of two
  positive proofs:
  (1) a live child of this session's pid tree whose `/proc` cwd IS the exact
  room, or (2) a recent PostToolUse event carrying a documented subagent
  `agent_id`, exact claimed room/session/holder/lease, and the same enclosing
  Claude holder PID/starttime as this Stop. The WARN names which proof passed
  and retains the lease. Live-child samples remain valid only while that child
  incarnation lives; subagent entries are independent per agent and
  `SubagentStop` removes the completed one. Claim-lock contention, malformed or
  foreign-clone evidence, holder death/reuse, expiry, lease rotation, and every
  other UNKNOWN block. Kill: `HELM_STOP_GUARD_DELEGATION=0` disables both
  producers/readers and restores the strict block; **BLOCK** on **NO ARMED BEACON** — a launched fleet
  seat whose inbox beacon is gone is a seat *nothing can wake*, so every
  addressed row silently piles up (the restarted-integrator incident: the
  Monitor died with the old process, was never re-armed, and the seat ran
  blind ~1.5h accumulating 98 undelivered rows while missing a gate verdict it
  was waiting on). The block quotes the exact `Monitor(command: "helm chat
  wait --seat <seat> --follow", persistent: true)` call plus the `ToolSearch`
  escape for a DEFERRED Monitor. Three precision gates, each fail-open: the
  process must carry `HELM_CHAT_NAME` naming this seat (the launch seam's
  stamp — an ad-hoc session that auto-named itself is never blocked, it keeps
  the advisory warn), the seat must be roster-registered, and absence must be
  PROVEN by reading the process table for a live `helm chat wait` waiter
  (rearm.py's exact argv shape; any probe trouble means liveness is UNKNOWN,
  not absent). Non-wedging by construction: the latch stores the last OBSERVED
  state, so the transition into `missing` blocks exactly ONCE per (seat,
  session) — a re-stop passes, a restart gets a fresh block, and a beacon that
  later dies re-arms it. Kill: `HELM_STOP_GUARD_BEACON=0`; **BLOCK** on a
  **REVIEW SPIRAL** — the stopping seat has review-dispatched ONE lane at
  **3+ DISTINCT tips** inside a 12h window, which is round three, which helm's
  own store heuristic `review-begins-with-cat-file` forbids by name ("at TWO
  rounds the cure is a MELD, never round three"). The rule was in the
  integrator's injected context on every turn of the night he ran six
  serialized rounds on one lane, so it is enforced rather than repeated. The
  block quotes the literal cure with the real peer and lane filled in — `helm
  chat meld invite <peer> "<lane>: converge every open review finding in ONE
  exchange"`. TWO rounds get a WARN instead (the rule's stated cure point,
  where the fix is still cheap) — measured over the live ledger, 2 rounds is
  ordinary (13 of 122 sender/lane pairs) and 3+ is not (5 of 122, both known
  incidents among them). DISTINCT TIPS, not dispatch count: a round is a new
  tip, so two review dispatches at the SAME tip to two families are one round
  of deliberate cross-family fan-out and never count as a spiral. Only
  explicitly `--kind review` rows count (UNKNOWN is not a review), cancelled
  rounds are dropped (a withdrawn round consumed no reviewer), and only the
  stopping seat's OWN sends are billed. Latched on `(lane, round-count)`: one
  block per state, a re-stop passes, a further round re-arms it, and an
  unwritable latch DEGRADES TO THE WARN rather than walling the seat. An
  unreadable ledger yields no finding at all. Kill:
  `HELM_STOP_GUARD_SPIRAL=0`; **BLOCK — NON-DISTRACTION PROTOCOL** — the
  stopping seat is connected to 3+ live claim leases (minted by its session
  or naming it as holder) and has delegated to a SUBAGENT exactly zero times
  this whole session (SubagentStop tombstones and documented subagent
  PostToolUse records are the only delegation evidence; a live same-uid
  child process is a build, not a delegate). Owner canon — "NDP
  rises to the level of importance to merit a conditional stopbook" — over
  the measured incident: a wide queue held 8h with zero subagent calls,
  three seats near-idle, the owner ask list grew ~46 -> 73 overnight. The
  block names both hand-out verbs (spawn a subagent via the Agent tool, or
  `helm dispatch send <seat> <lane>`) and the reason (a TLA's scarcest
  resource is hot context on the critical path). Deliberately WIDER than
  NDP's own trigger, because the per-arrival version ("this row arrived
  mid-turn and was neither delegated nor dispatched before the turn ended")
  is not computable from state helm keeps — nothing binds an arrival to the
  delegation that handled it, so it would fire on every head-down seat that
  answered one question inline. Latched once per load-state bucket
  (N // 3): a re-stop passes, a drift (3 -> 4) stays quiet, a doubling
  re-arms, ONE delegation silences it for the session (tombstones are never
  unlinked); an unwritable latch degrades to the compact one-line WARN.
  UNKNOWN claims accuse nobody. Kill: `HELM_STOP_GUARD_NDP=0`;
  **WARN — CLAIM EVIDENCE** — the outgoing
  message's first count/percentage, named SHA, proof word, and landed claim
  must anchor to the matching successful measurement in THIS turn. Numbers
  anchor in result content; a SHA anchors in a successful resolution tool's
  input or content (contentless predicates count); landed needs one pure Git
  invocation that names `origin/main` and the SHA when claimed. The reader
  joins tool uses to their results, ignores typed non-message metadata, treats
  malformed message envelopes as unreadable and surfaces a latched
  `CLAIM-EVIDENCE SKIPPED` rather than clean, and derives the finding + latch
  identity from ONE transcript snapshot. The same record warns once; a later
  record with identical text re-fires. WARN-only, including under
  `stop_hook_active`; any rung failure is isolated from other blocks. The
  calibrated known misses are explicit scope, not hidden completeness: only
  the first numeric shape and final text block are inspected; no-UUID identity
  is tail-relative; guillemets, double-backticks, `no failures`, `not now`, and
  `yet` remain lexical edges. Revisit when measured warn-rate misleads. Kill:
  `HELM_STOP_GUARD_CLAIME=0`; **WHISPER** — the contextual continuation lane
  (stop-whisper): ONE budgeted line (240B) from the live signals — an
  UNREPORTED owner ask (top of the ladder: the owner-ask ledger's OLDEST row
  not yet `reported` — open or done-but-unreported — named one at a time with
  its `helm asks report` pointer; fp carries the row's status, so open→done
  re-fires once), then dispatch state reloaded from disk on each stop: ledger
  **UNAVAILABLE / obligations UNKNOWN** first, then the oldest historical row
  that **NEEDS REDISPATCH**, then the oldest **NEEDS CONFIRMATION** row
  (delivery unproven — verify at the recipient, never resend), then the
  oldest overdue **NEEDS CHECK-IN / PENDING VERDICT** row with its exact-tip
  verdict command (advisory only and
  never an automatic reassignment), stuck
  session (`stuck-streak`≥3: surface the blocker), a RED gate (record.py's
  command-log shows a test-runner whose LATEST run exited nonzero — fix or
  surface before stopping; a green rerun silences it), unlanded owner/mention
  rows aged >10m past their inbox block (land or route them), UNVERIFIED
  edits (code files edited, tree dirty, NO test/gate ran this session — run
  the gate; doc-only sessions never arm it), UNBANKED green (edits landed,
  every latest gate run green, tree still dirty — commit is the named next
  step), uncommitted
  drift (`dirty-streak`≥8: bank the green slice) — highest salience wins,
  each line ends in a pull-depth pointer; fires once per (signal, level)
  fingerprint as a soft hold (a re-stop on the same state passes; a
  worsening streak or new row set re-arms), rides an existing block when
  one fired; measurable via `_global/.state/stop-whisper-ledger.jsonl`
  (ids only); fail-CLOSED to nothing; **WARN** on a clean stop with the
  beacon-arm line (the Monitor command with the resolved seat); plus the
  silent mechanical legs — `helm index cap --apply` and the throttled,
  pressure-gated scratch reaper (`helm scratch gc`, `HELM_SCRATCH_GC=0`) —
  best-effort, never blocking, never printing.
  FAIL-OPEN TOTAL (a broken guard must never wedge the fleet); kill-switch
  `HELM_STOP_GUARD=0`, per-check
  `HELM_STOP_GUARD_INBOX/CLAIMS/CLAIME/WHISPER/BEACON/SPIRAL/NDP/PUNT/WIRING/INDEX=0`;
  bounded reads; no network. With inject (turn start), deliver (tool
  boundary) and join (session start) this completes the loop: an agent
  cannot idle past a NEW inbox row. The bound is stated where the gate is
  specified above and is worth repeating here, because the flat version of
  this sentence read as a guarantee the mechanism does not make: the inbox,
  beacon, spiral, NDP, wiring, punt and whisper blocks are each latched, so a
  re-stop on the same pending rows passes and the beacon block fires ONCE per
  (seat, session). An agent that ignores a block can go dark and nothing says
  so again until its state changes. The claim-lease block latches the same
  way (it was the one rung without a memory: the owner
  watched its identical multi-line wall print on two consecutive stops of an
  unchanged held set): one full block per held-set fingerprint — resource +
  lease id + TTL band — after which a re-stop on the SAME set compresses to
  a one-line WARN carrying the count and the expiring tally. A CHANGED set
  (a new lease, a release, a re-claim, or a remainder crossing the 120s
  alarm) re-prints the full block, an all-exempt stop clears the memory so
  the stop after a delegation/gate proof dies is loud again, and an
  unwritable latch degrades the block to the WARN. The latch governs the
  EMISSION only — the spec above stays true of the classification: positive
  proof only, re-verified every stop. Releasing the lease is its cure, and
  the delegation/gate proofs are the only carve-outs; `stop_hook_active`
  suppresses every block when the harness is already continuing off a stop
  hook while still surfacing the inbox and claim-evidence WARNs.
- **`helm chat seats [--all]`** — the roster table: presence (fresh <2m /
  quiet <15m / absent, off the last tool boundary), pending deliveries, each
  seat's active home + provenance (`#helm (derived)`, `#team (operator)`, or
  `all`), live claims, and launch-owned runtime metadata when known. The compact
  label keeps two independent axes visible — model family beside agent
  harness/backend, for example `[codex · pi/proxy]` versus
  `[codex · claude/proxy]`. A legacy row stays unlabelled rather than guessing
  from its `@name`. Absent rows hide by default (`--all` shows them).
  The report is a pure READ — it deletes nothing (the legacy auto-reap that
  rode it dropped stale-but-persisted seats on presence alone; retired).
  Junk rows leave only via `helm chat seat gc`, the one cleanup owner. The
  web twin is the **seats** panel in the ledger tab (`GET /api/chat/roster`).
- **`helm chat status [<one-line> | --clear] [--seat S]`** — the seat's
  ICQ-style away message: ONE glanceable line of "what am I on", written on
  the seat's roster row (through the roster writer's flock — no second
  writer path; scrubbed + clipped to 160 bytes). Bare `status` shows the
  seat's current line. Every surface composes the same line with the same
  precedence — **fresh explicit status > live claim** (`working lane/<x>,
  <ttl> left`) **> stale explicit status > home room** — so the web chat's
  fleet **presence bar** (dot 🟢 fresh / 🟡 quiet / ⚫ gone + seat + line,
  one row per seat, visible from every room, riding the existing `/api/chat`
  poll), `helm chat seats`, and the ledger seats panel all glance
  identically. An explicit line always shows its AGE (`▸ line (2d)`; the web
  bar dims a stale one) and DECAYS: past `STATUS_FRESH_S` (4h) it yields to
  a live claim — a holding lease is fresher evidence than an hours-old
  announcement, so a 3-day-old away message can never mask a seat verifiably
  working a lane. With no claim the stale line still shows, aged. The line
  persists until overwritten or `--clear`ed; setting your own is also a
  presence beat. Cross-seat writes (`--seat` ≠ self) stay allowed — a
  coordinator annotating a wedged seat is the point — but record the writer
  (`status_by`, post-attribution parity), rendered as `(by X)`; the beat
  lands on the writer, never the annotated target. Reader-side, whatever
  tier wins is scrubbed + clipped in `status_line` itself, `claims_list`
  launders resource/holder for EVERY claim surface (the seats footer,
  `helm chat claims`, the web ledger), and the roster report launders the
  display columns (home room, project, todo cell) — a planted claim
  resource or junk roster field cannot reshape a terminal anywhere the
  fleet is glanced. A corrupt row fails open to `?` instead of blanking
  the bar, and a junk `status_ts` — missing, non-numeric, OR dated into
  the future past a small skew allowance — counts as stale: an unknown
  (or unfalsifiably "fresh") age must never outrank a live lease.
- **`helm chat seat rename <sid|oldname> <newname>`** — bind a live agent to
  a memorable @name (`old` = seat name or an 8+-char session-id prefix). The
  roster row AND every keyed state file move together, so tracked delivery
  ground survives and the hook's `session_id` resolves to the new name from
  the next boundary. Refuses taken/reserved/unaddressable names; reminds you
  to re-arm a beacon armed on the old name. Web twin: the **rename** control
  on each seats-panel row (`POST /api/chat/seat`).
- **`helm chat dm <seat> <text...> [--seat S]`** (also `post --dm SEAT`) —
  a TRUE 1:1 (premise `exact-token-addressee-match`): the row lands in the
  recipient's private lane (`dm/` in the room dir — never a room, never the
  channel list), addressed by the EXACT seat token (a casefold roster snap
  only — never a substring or slug fold: `team.a` ≠ `team-a`). The
  recipient's beacon/boundary surfaces it first (`[helm chat dm → seat]`),
  it gates their stop like any pending word, renders as a DM everywhere, and
  signs like a post. Catch up with `helm chat read --dm`. A DM to a
  not-yet-joined seat waits in its lane and delivers at join. Web twin: the
  ledger tab's **message a seat** card — a named seat routes to
  `POST /api/chat/dm`, only `@all` still posts to `#main`.
- **`helm chat seat mute <room>` / `seat unmute <room>` / `seat mutes`
  `[--seat S]`** — the seat's own beacon filter: a muted room stops
  surfacing home-room chatter / `@all` at that seat; a direct `@seat` mention
  or a DM ALWAYS still surfaces (mute tunes noise, never direct address).
  Roster-stored; boundary, beacon, stop-guard and the roster report all read
  the same truth.
- **`helm chat seat rehome <sid|name> <room|main|none>`** — deliberately move
  an existing seat's inbox scope without relaunching it. A room admits exactly
  `{home, main}` for home-room chatter and broadcasts while direct mentions and
  DMs remain cross-room; `main`/`none` clears the home back to the legacy
  all-room scope. Newly admitted rooms baseline at current EOF, so destination
  history and traffic accumulated while the seat was away never replay. The
  operator choice survives later project-derived SessionStart joins.
- **`helm chat seat gc [--apply]`** — the roster's ONE cleanup owner (a verb
  someone runs, never automatic; dry-run default; no other code deletes a
  roster row). Prunes only rows with NO live evidence: no presence beat
  within the reap window, no transcript for ANY remembered session anywhere
  the session persistence census covers (`~/.claude`, `~/.claude-homes`,
  `~/.codex`, `~/.codex-homes`, **and helm's own seat homes** under
  `~/.helm/_global/seats/`), and no live same-uid process naming a
  remembered session or carrying the seat's `HELM_CHAT_NAME` — the `/tmp`
  throwaway class. Refusal is the default and every probe fails CLOSED (an
  unlistable process table, an unreadable same-uid process, or an incomplete
  census prunes nothing). `--apply` re-runs the FULL evidence probe fresh
  under the roster lock before each deletion (evidence landing between scan
  and apply wins), then removes the row plus its derived seat state
  (cursors, `.seen`, stop latches, the RAM DM lane).
- **`helm chat claim <resource> [--ttl N] [--lease ID]` /
  `release <resource> --lease ID` / `claims [--json]`** — the advisory TTL
  lease (meld claims, minus the cap-gate): refused while another holder's lease
  is live; expiry is MONOTONIC. The printed **lease id is a CONFIRMATION
  TOKEN**, not a secret and not a capability: extend and release validate
  `{lease, holding seat, granting session}` TOGETHER (session comes only from
  the ambient harness env, never a flag — a roster-visible SID opens nothing),
  which proves the caller is the DELIBERATE holder and stops a stray or
  fat-fingered release, a stale re-granted holder (ABA), and a copied-SID
  caller. It stops no *thief*: every seat on the box is the same uid and can
  read `.claims.json` outright. So `claims` prints the lease id for the rows
  **the calling seat itself holds** (never another holder's), and so does
  `helm work list` — a holder who lost the token to compaction or a restart
  reads it back instead of watching an honest lane stay locked for the rest of
  the TTL. `--json` prints the same rows machine-readably. The claim ledger is
  one per chat dir, not per room. For short-lived same-host mutual exclusion —
  files, ports, worktrees (the concurrent-lane collision class).

**Trust domain, loudly:** seat names are DISPLAY LABELS and every mechanism
above is advisory coordination between cooperating same-uid processes in a
0700 tmpfs dir — not a security boundary. The owner-rule delivers only rows
the server-side owner rails stamped (`origin: web|tui`); a CLI post claiming
an owner name delivers as an ordinary mention. Principal cryptography stays
dregg's. (Council — embargoed verdicts — SHIPPED: `helm chat council invite
<members> <topic> --tip SHA [--threshold K]` convenes an N-of-M quorum over
an EXACT artifact (`--tip` is required — an optional one fell back to
first-signal selection, letting the fastest member choose the question);
`verdict` seals one member's judgment, `council-status` prints the BAR
(threshold of N members) and deliberately NOT the signalled count — a count
beside the member list identifies signers by elimination — `reveal` lifts the
embargo at quorum and states the OUTCOME (more YES than NO carries; a tie does
not), `council-abort` ends it permanently. Nothing — not the verdicts, not
WHO, not even HOW MANY — is emitted or readable before quorum.)

```console
$ helm chat post "@codex-seat xrev the meldhalf branch when free"
$ helm chat seats                # who's live, what's pending, what's claimed
$ helm chat claim worktree-main --ttl 1800   # prints the lease id — keep it
$ helm chat release worktree-main --lease 5f3c9a2d41b0e6f2
```

### `helm chat meld` (aka `council`, `standup`) — the mindmeld preset

Hyper-speed a2a real-time convergence (premise meld-discipline): both parties
reply FAST with what they ALREADY know; a fork that needs research is NOT a
meld — it falls to async. The preset is THIN by law (one-comms-primitive): a
meld is a fresh room (`meld-<epoch>-<slug>`) plus a bounded synchronous read
discipline over it — no new transport, no daemon; the room IS the artifact,
visible live in `helm chat rooms` and the web channel list like any room.
Protocol lineage: a predecessor mindmeld protocol (epoch fencing, floor
markers, bounds-as-behavior — the scars are kept, the channel is helm's).

NAMING (owner canon: council is the number-one feature):
MELD is the GENUS — agents converging quickly — and `meld` stays the primary
verb. `standup` (informal 2+ convergence; today's 2-party mindmeld included)
and `council` (the big FORMAL convergence — agenda/quorum/recorded verdict;
its N-of-M verdict/reveal machinery SHIPPED) are SPECIES spellings routed
to the same preset, never replacements. The spelling you type echoes back in
every printed next-command, so a council-invoked flow speaks council. Every
verb takes `--seat S` — explicit per-command identity for one-off seats
(identity is otherwise env-first: `HELM_CHAT_NAME` wins over the roster,
aligned with every other chat verb).

- **`meld invite <peer> <topic...> [--wait]`** — the verb owns the wake
  atomically (premise a2a-wake-foolproof-layers): seeds the problem
  statement (ending `[HOLD]`, discipline line + `invited=<peer>` included)
  into the fresh room, then posts the `@peer` invite with the protocol head
  FIRST — room + join command inside the first 200 bytes, so the delivery
  clip can never eat the join instruction (a prior-harness incident). The invite is a
  durable row: a tracked peer's delivery lane backfills the newborn room
  from offset 0, so it lands at the peer's next tool boundary (busy) or
  beacon fire (idle) — never lost, only delayed. An UNTRACKED peer gets a
  loud roster warning instead of that promise (the
  invite asserted delivery to a seat with no lane). `never lost` is scoped to
  the DELIVERY lane, not to storage: rooms live in RAM
  (`/dev/shm/helm-chat`) with a write-behind disk flush, so a reboot or a
  tmpfs loss drops whatever the last flush had not yet written. Durable
  against a busy or idle PEER; not durable against losing the tmpfs. `--wait`
  collapses
  invite + the first recv into one call — every convener's literal next
  command.
- **`meld join <room>`** — parses epoch + convener + invited from the seed,
  posts the control-only `READY` **@convener** (the wake-back: a READY that
  lands silently strands GO forever). REFUSES a seat the seed did not
  invite — a different-seat join is how melds got hijacked/mis-consummated
   The seeded problem is the joiner's first recv
  chunk.
- **`meld recv <room> [--timeout S]`** — the blocking marker-aware read
  (default 90 s): returns the next PEER chunk carrying a real floor marker;
  skips own/unattributable rows fail-closed, stale epochs (a reused room
  never replays a dead meld), READY/GO control echoes, markerless chatter,
  and — the pinned-pair law — every row from a seat that is not the meld's
  other half (noted in the output, never silently). Bounds are BEHAVIOR:
  exchange cap (5) or timeout → exit 3 with the fall-to-async instruction
  printed; `[ABORT]` → exit 4, fail-loud. After your own `[DONE]`, recv is
  the COUNTERSIGN WATCH: it returns the peer's closing DONE (→ done-mutual,
  counted) instead of refusing — the closer is never blind.
- **`meld say <room> --marker YIELD|HOLD|DONE|ABORT <text...>`** — one
  bounded chunk, content + floor marker in the one text field. `[YIELD]`
  hands the floor, `[HOLD]` more coming, `[DONE]` leaves, `[ABORT]` kills.
  DONE/ABORT @mention the peer (act-moments — the closing must land);
  YIELD/HOLD stay mention-free, so a meld never floods the peer's delivery
  cursor with stale nudges (both parties sit inside recv, polling the room
  at 0.5 s).
- **`meld status`** — this seat's live melds (role, status, exchanges/cap)
  plus lifecycle provenance: `live-not-yet-durable`, `durable-live`,
  `replayed`, or `UNKNOWN`. Missing, unreadable, malformed, sequence-gapped,
  or contradictory durable lifecycle input is UNKNOWN — never a fabricated
  “no live melds” result.

LATENCY-PURE (premise comms-presets-optimize-their-novel-purity): every meld
post rides the v1 RAM append unsigned (`sign=False`) — no signing leg, no
node round-trip, no disk write mid-meld. Lifecycle transitions append a typed,
room-global monotonic event to RAM beside the actor snapshots; transport cursor
`idx` is deliberately absent from that stream. The existing out-of-band
`log-flush` first mirrors unseen contiguous lifecycle events append-only to
`~/.helm/helm/journal/meld/`, then writes rendered chat history. State therefore
survives only through the **last successful write-behind flush** — this is not
persist-before durability. `restore-journal --apply` strictly replays that typed
stream with `idx=0` while keeping old meld room history suppressed, so a reboot
restores lifecycle truth without resurrecting dead sidebar rooms. State remains
RAM on the hot path, keyed room × ACTOR (two seats share one chat dir — the
the legacy meld's host-global-state clobber, refuted in its live dogfood, is structural
here). The pinned PAIR is now a pinned SET: `invite` takes one
seat or a comma list, join admits on set membership and refuses every
outsider, recv accepts from any member, and a 2+ standup seals only when
every member has closed. A single-peer invite is the size-1 case — the
2-party pair, unchanged. Env:
`HELM_MELD_CAP`, `HELM_MELD_RECV_TIMEOUT_S`.

```console
$ helm chat meld invite kimi-b "converge the reflex naming"   # → room + next
$ helm chat meld recv meld-1784663842-converge-the-reflex-nami           # READY → GO
$ helm chat meld say  meld-1784663842-converge-the-reflex-nami --marker YIELD "propose: …"
$ helm chat meld say  meld-1784663842-converge-the-reflex-nami --marker DONE "state: converged; next: I land it"
```

### `helm multiplayer publish|read|presence|peers|leave`

Local human+agent shared-state transport. `publish` appends a client-owned opaque
CRDT update (`--stdin` keeps it out of shell history); `read --json` returns
updates after a generation-bound cursor. Helm never decodes or merges payloads,
and publish acknowledgements never echo them. `presence`/`peers`/`leave` use a
separate connection-scoped TTL snapshot, so attention state can expire without
touching document state. The default cave is
`HELM_MULTIPLAYER_CAVE`, then `HELM_CHAT_ROOM`, then `main`; the default actor is
`HELM_MULTIPLAYER_ACTOR`, then the normal chat/seat identity.

```console
$ helm multiplayer presence --cave helm --actor alice --connection phone --state editing
$ printf %s 'base64:opaque-update' | helm multiplayer publish board --stdin --cave helm --actor alice
$ helm multiplayer read board --cave helm --after 0 --json
$ helm multiplayer peers --cave helm --json
$ helm multiplayer leave --cave helm --actor alice --connection phone
```

The default adapter stores both channels in tmpfs, but the interfaces are
metaharness- and CRDT-agnostic. See [MULTIPLAYER.md](MULTIPLAYER.md).

### `helm launch [--seat S] [--home H] [--room R] [--no-install] [--] [claude args…]`
The metaharness seam (meld-launch's capability): wires the full hook estate
into the target home (idempotent), pre-writes the seat's roster row so
teammates can address it before the first tool call, exports
`HELM_CHAT_NAME=<seat>` (a STABLE addressable identity across sessions —
default `<host>-<cwd-basename>`), then execs `claude` with the pass-through
args. With no explicit `--room`/`HELM_CHAT_ROOM`, a git checkout derives its
canonical Helm project room (worktrees resolve to their main checkout; registry
identity wins, with a canonical-path fingerprint only when two registry labels
normalize to the same room; an unregistered checkout always gets a stable basename +
path fingerprint rather than claiming an unsafe global basename). The child exports
that room plus
`HELM_CHAT_ROOM_SOURCE=derived`; explicit `--room` wins, including explicit
`main`, and project-less launches remain un-homed. The fleet needs no wrapper
(`helm hooks install` covers every home); launch adds the stable name and the
per-home pin (`CLAUDE_CONFIG_DIR`).

### `helm work claim|release|gc|list|install-guard`

In-cave git coordination (design: `prd/2026-07-21-in-cave-git-coordination.md`
— the maintainer's tree + the hotel front desk): the shared checkout is the
**integrator's tree**; every other seat works in a private room
`<repo>-wt/<lane>` on branch `lane/<lane>`. Every leg is a reuse — the lease
IS `helm chat claim` (resource `worktree:<proj>:<lane>`, nonce = the release
confirmation token, 4h TTL, the stop-guard already refuses a stop with a lease
still held), the
registry IS `git worktree list --porcelain` ⋈ `.claims.json` computed at
read time (ZERO new state files), and `git worktree lock --reason
lease:<id8>` is the git-native do-not-disturb. `[--repo PATH] [--seat S]`
everywhere; from inside a repo the root resolves itself.

- **`helm work claim <lane> [--ttl N] [--lease ID]`** — check-in: lease
  first (held by someone else = the existing refusal, pick another lane),
  then the room (`worktree add -b lane/<lane>`; a registered room is reused,
  a parked branch re-opens; `--lease` extends — idempotent re-entry). Prints
  ONE machine line: `path<TAB>branch<TAB>lease<TAB>ttl` — keep the lease
  (`helm work list` reprints it if you lose it).
- **`helm work release [<lane>] --lease ID [--park] | --stale`** — checkout at
  the desk. The existing grant is strictly refreshed before any room mutation, so
  a wrong or expired confirmation token cannot even trigger a `--park` commit.
  DIRTY still has exactly two exits — commit and re-run, or `--park`
  (WIP-commit onto the lane branch; nothing is ever discarded BY THIS
  ACTUATOR). The scope matters: helm's worktree lifecycle never discards, and
  helm is not the only thing that removes rooms. The metaharness has its own
  worktree reaper, the two do not share a lock or consult each other, and a
  room orca removes was never offered to helm's rescue path. See
  ORCA_SEAM_AUDIT C2 for the same shape at the settings seam. Releasing
  surrenders the lease and unlocks the room, but retires room + branch only
  when Git proves the lane's work reached the trunk — by ANCESTRY (the tip
  itself is on trunk) or by PATCH IDENTITY (every commit is on trunk under a
  rebased sha). The second half is what lets a seat clean up after itself:
  helm lands work rebased, so an ancestry-only check said "not landed"
  truthfully and stranded the branch of every agent that followed the rule. A
  patch-identity retirement preserves the tip at `refs/helm-retired/<branch>`
  before removing the branch, so it is reversible by construction; the release
  line names which proof retired the lane and how to restore it.
  Unlanded work keeps BOTH room and branch; its TRIAGE line names tip, age and
  git committer identity (shared across seats, not seat provenance) and the integrator is told. Lane infers from inside its own
  room. Lost the lease id? The refusal names the surface, and `helm work list`
  prints it for the rows you hold. `--stale` is the DEAD-HOLDER escape: it
  releases a claim whose holder is demonstrably dead, and the lease token is
  deliberately NOT required — the safety is the liveness proof, not a token a
  dead process can never produce. Only the claim is released and the room
  unlocked; room/branch cleanup stays `helm work gc`'s, since a stale holder's
  room may still carry unlanded or dirty work.
- **`helm work gc [--apply]`** — housekeeping, dry-run default (gc.py
  culture), across guarded `<repo>-wt/` lanes and harness-minted
  `.claude/worktrees/` rooms. The verdict table: live lease → keep; locked
  out-of-band → keep; detached/mid-operation → manual keep; clean + LANDED
  (ancestry OR patch identity) → remove room and retire branch, saying which
  proof and — on patch identity — preserving the tip at
  `refs/helm-retired/<branch>` first; clean + unlanded → TRIAGE and keep both;
  landedness UNKNOWN → TRIAGE and keep both; DIRTY → rescue-commit then keep
  both (the rescue is itself unlanded). Apply re-checks occupancy, METAHARNESS
  PANES, branch identity, dirtiness and landedness immediately before removal. Missing lane/harness records are also
  named in dry-run and re-authorized immediately before metadata-only removal of
  that exact registry record; a checkout that revives in the last interval is
  never recursively deleted. This verb never runs global `git worktree prune`,
  so one approved phantom cannot authorize deletion of an excluded unreadable
  live record. Unmanaged phantoms belong to `helm worktree gc`, and peek records
  to `helm work peek --drop`. A pane outlives its shell, so /proc
  alone cannot clear a room: a room orca or herdr still holds a terminal in is
  kept, and so is a room whose metaharness could not be asked — an
  unanswerable host is a refusal, never an empty answer. The sole occupied
  exception is an empty Orca shell-ready bash
  (`--rcfile .../.config/orca/shell-ready`, Linux `Ss+`, no children), which may
  be hung up (SIGHUP — bash ignores SIGTERM in that state) before an otherwise-
  proven reap; every uncertain process state keeps the room, and no pane-bound
  room reaches that leg. Each APPLY pass prints and posts one `removed=N kept=N
  triage=N` summary. A stale `lease:` lock with no live lease falls through to
  the sweep. `helm gc` carries the report row; this verb is the actuator.
- **`helm work list`** — the room board: lane, holder + remaining, dirty,
  ahead/behind the base, lock, path (registry ⋈ claims, computed).
- **`helm work install-guard [--apply]`** — the deterministic rail is four
  composed hooks — `reference-transaction` + `post-checkout` + `pre-commit` +
  `pre-push` — in Git's effective repo-local hooks directory. The transaction
  hook refuses shared-checkout branch creation, switch/detach,
  non-fast-forward trunk movement, and plumbing mutation/deletion of a branch
  occupied by another registered worktree **before any ref changes**. The
  checkout hook remains a pointer-only safety net for an otherwise-created
  branch. The pre-commit hook runs four rungs in a fixed order, and each
  rung's skip or absence isolates to itself — one law's bypass never disarms
  a sibling: first the WARN-only vacuous-assertion
  advisory over staged Python test functions (`helm/vacuous_assertion.py`),
  then the REFUSING conflict-marker rung (`helm/conflict_marker.py`: staged
  merge-conflict marker lines — column-0 `<`/`>`/diff3-`|` runs at the
  per-path `conflict-marker-size` attribute length, default seven, on sight;
  the lonely equals separator only when bracketed by an opener above and a
  closer below, so RST/markdown heading underlines pass; diff-aware and
  rename-aware, so a marker HEAD already carries is named but never blocked,
  a pure rename included; staged BYTES are read by index OID with literal
  `-z` paths — never a worktree file, a textconv view, or a glob expansion —
  and unreadable staged content refuses as UNKNOWN; its own one-commit skip
  is `HELM_CONFLICT_MARKER_SKIP=1` — measured 2026-08-01, a full conflict
  block inside a committed docstring survived commit + push + review because
  it parses),
  then the WARN-only hardcode rung (`helm/hardcode.py`: context-identity
  shapes baked into portable logic, staged-diff scoped, never blocking),
  then last the never-track STAGED-SET scan (`helm/nevertrack.py`: never-track
  path prefixes + private-needle content scan, against the index — not the
  worktree), which refuses the commit on a hit. The pre-push hook runs the
  host-path guard (`helm/hostpath_guard.py`, pattern-based host-path shapes
  in outgoing commits; one-push skip `HELM_HOSTPATH_SKIP=1`). The advisory parses each whole
  staged test blob with `ast` but reports only functions touched by the staged
  diff. It warns when an empty/absent observable has no unconditional positive
  control on that same observable, when the matching control is behind
  `if`/loop/`try`/a comprehension, when only local spy instrumentation is
  asserted while the production result is discarded, or when the test has no
  structural assertion. Legitimate absence tests suppress their own warning with a
  reason-bearing comment on the test definition or the intentional assertion:
  `# noqa: VACUOUS_ASSERTION — <reason>`; string/docstring/nested-helper
  lookalikes grant nothing.
  Every rung covers every tree sharing the common hooks dir, lane worktrees
  included, because the one gap a suite-run guard cannot close is the moment
  between `git add` and the commit becoming history (a real leak once
  shipped with a green suite exactly there). The needle half reads the
  commit's DIFF, never the whole file: a needle this commit ADDS refuses, a
  needle already in HEAD's copy prints a NOTE that names the file and says it
  needs its own scrub commit. Blocking that second case refused honest work
  for content the commit could neither write nor remove — measured
  live: one needle in `helm/web_ui.html`'s history refusing every commit
  to a 3,000-line file until the seat reached for the bypass — and a guard
  that refuses honest work teaches its own bypass flag and then guards
  nothing. A binary staged change has no line structure to read, so the WHOLE
  blob is scanned there: the guard over-blocks rather than under-sees. The
  never-track path prefixes are unconditional — those files must not be
  tracked at all, whatever any diff says. Install snapshots every scanner
  file beside the shared hook and bakes those stable paths into the scripts;
  installing from a lane never makes that editable/disposable lane the live
  detector. A missing installed snapshot fails OPEN but LOUD. `HELM_NEVER_TRACK_SKIP=1` skips the scan for one commit — an owner
  decision for false positives, not a lane convenience. Lane rooms may
  branch normally; commits on each room's own branch
  remain legal. Existing executable hooks run first and are preserved
  byte-for-byte (including symlinks and executable mode) as `.helm-user`
  companions; non-executable hooks stay inactive. Installation is serialized,
  per-file atomic, rolls the full set back on partial failure, and is
  idempotent. A repo-local `core.hooksPath` is honored; a path outside the
  checkout/common git directory is refused rather than mutating shared or
  foreign hooks. `HELM_WORK_INTEGRATOR=1` is the explicit override for the
  shared-tree rail (it does NOT skip the never-track scan — the privacy law
  binds the integrator too). `helm doctor` warns when a rail-managed repo's
  hooks are missing or stale. Prints a
  no-write plan by default; `--apply` installs.

```console
$ helm work claim webui            # path  branch  lease  ttl — keep the lease
$ helm work release webui --lease 5f3c9a2d41b0e6f2      # dirty? --park saves it
$ helm work gc                     # the verdict table, dry; --apply enforces
```

### `helm asks add <text> --needs "<what only the owner can supply>" [--source S] | done <id> <evidence> | report <id> <chat-post-id> | list [--open] [--json]`
The OWNER-ASK LEDGER — the durable fix for dropped owner asks (root cause:
asks lived in memory-only panes + siloed scratch, and report-back went to
AGENTS, not the owner). One append-only jsonl at
`~/.helm/_global/owner-asks.jsonl`: every mutation appends a full snapshot
row `{id, ts, ask, source, status, done_ref, report_ref, last_updated}`
(last line per id wins — O(1) single-write appends, history never lost, no
rotation: durable record, not telemetry). This is the canonical fleet
owner-ask list; agents SELF-ADD the moment the owner asks (a2a-self-add
culture). **`add` REQUIRES `--needs`** — name the HUMAN-ONLY input (a
credential, a physical act, an account only they hold, a genuine vision
call) or the add refuses with rc 2: if you cannot name one in a few words,
it is not an owner ask — put it on the task list and do it (11 of 19 rows
in this queue once were not owner gates, and they buried the 4 that were).
**`done` does not close a row** — `report <id> <chat-post-id>` is
the ONLY closer, and its argument is the chat post that told the OWNER
(owner-surface-is-the-bar: work merely finished is invisible work). Any row
not yet `reported` rides the stop-whisper's TOP rung (see `stop-guard`
above) until the owner has actually heard it. A missing ledger is known-empty;
an unsafe/unreadable ledger is **UNAVAILABLE / owner debt UNKNOWN** on both
`list` (nonzero) and stop-whisper, never silently rendered as zero. Mutations
still never traceback, and a failed `add` says NOT RECORDED loudly.

### `helm task add <title...> [--owner SEAT | --mine] [--note N] [--ref R]... [--id NNN] [--owner-asked] | list [--all] [--owner SEAT] [--json] | show <id> [--json] | resolve <token> [--json] | claim <id> [--owner SEAT] [--force] | update <id> [--title T] [--note N] [--owner S] [--status S] [--origin owner|agent] [--ref R]... | close <id> <reason...> | comment <id> <text...>`
The FLEET TASK LEDGER — shared work items with real ids every seat can
resolve. One append-only jsonl at `~/.helm/_global/tasks.jsonl`, same
event-sourced shape as the owner ledgers: every mutation appends a full
snapshot `{id, ts, last_updated, title, status, owner, note, refs, source,
origin, closed_reason, comments}` and the last line per id wins.

**IDS KEEP THE NUMBER THEY WERE BORN WITH.** A row is `task/<n>`, so the
week of chat rows citing `#263` resolves BY CONSTRUCTION — no lookup table
and no alias verb. `resolve` takes any spelling a seat would type (`263`,
`#263`, `task/263`, `task-263`) and answers UNPARSEABLE (rc 2) separately
from ABSENT (rc 1), because a typo and a number nobody filed are different
questions. New rows CONTINUE that sequence rather than forking a second one,
minted under the ledger lock so two seats filing in the same instant cannot
put two different works behind one citation.

**FILING IS NOT OWNING.** A row is UNOWNED unless `--owner` or `--mine` says
otherwise, and unowned-and-open is the POOL `offer_rows()` hands an idle
seat — pre-assigning every row would empty that queue by construction. An
OWNED row joins that offer only when its owner is measured absent from the
live roster (the stranded-work rescue, marked `[assigned: seat]`); a live
owner's row is never offered, and an UNREADABLE roster offers the pool alone
rather than guessing who is gone. What
is refused is `in_progress` with no owner: work in progress by nobody is
incoherent, while work nobody has started yet is the normal case. `claim`
refuses to take a row from a live holder without `--force`.

**CLOSED ROWS INCLUDE TOMBSTONES** for items retired before this ledger
existed. 55% of the fleet's measured citation load points at those, so a
tombstone — id, title, `status: closed` — is what makes a week-old citation
resolve to a sentence instead of nothing. Tombstones are never work, never
offered, and never counted as backlog.

**THE ORDERING IS A PUBLIC CONTRACT**, `tasks.sort_key`: numbered rows in
NUMERIC order (task/45 before task/263 — never lexical, where `"263" < "45"`),
slugged rows after them alphabetically. It is public because a consumer needed
it and found only a private name: the task endpoint in helm/web_core.py bound
itself to the module's private sort helper because this entry described the row
schema and left ordering implicit. A module that does not say what it exports gets read for what it has.

**A CLOSED ROW'S TITLE IS A POINTER; ITS `origin` IS THE LOAD-BEARING FIELD.**
The 172 tombstones carry titles transcribed by hand out of the private list
they came from — there is no second copy to diff them against and no reviewer
who can catch a drifted word. `origin: corpus-2026-08-05` is machine-written
and verifiable; the sentence beside it is human-carried. A reader who needs
certainty about what a tombstone WAS should chase the origin plus the chat
archive, never the title alone. And never backfill a title from inference: a
title from the authoritative list is the record, a title from someone's reading
of the number is a fabrication wearing the same shape.

Deliberately carries **NO board projection and NO owner-gate queue**: 120
engineering tasks filed where 3 owner rulings live is the burial
`ownerasks.py` already measured once at 19 rows. A missing ledger is
known-empty; an unreadable one is **UNAVAILABLE — the backlog is UNKNOWN,
not zero** on both `list` (nonzero) and any surface reading it.

### `helm decide file <title> [--asker SEAT] [--ref R]... | list [--open] [--json] | show <id> [--json] | verdict <id> <choice> [--comment T] | deliver <id> | comment <id> <text> | board-sync`
The OWNER DECISION QUEUE — the ask ledger GENERALIZED (owner steer:
the store review queue proved the primitives; decisions should
ride them too, "so that I can just go review a durable queue" instead of
answering every fork inside one seat's input box). An **ask** waits on a
human-only input; a **decision** waits on a RULING among named options. Same
mechanics, sibling file: append-only snapshot rows at
`~/.helm/_global/owner-decisions.jsonl` `{id, ts, title, context, options[],
asker, refs, status, verdict, comments, owner_pushed_ts, delivered_ref}`.

`file` reads the CARD BODY on stdin (quoted heredoc): a phone-readable
context paragraph, then option lines `* <label> :: <one-line consequence>`
(`*!` marks the one recommendation; no option lines = an approve/reject
card). The `::` consequence is required mechanically, the same bar `--needs`
sets for asks: an option whose trade-off is unstated makes the owner
reconstruct it himself. `--asker` is the verdict's RETURN ADDRESS (defaults
to this process's seat; a missing OR malformed address refuses AT FILE TIME
— the card has no address-edit verb, so a bad address filed today is a
verdict undeliverable forever). Filing drops **one compact line** in the
filer's room — the attention budget is a pointer, never the card body — and
**pushes the card's HEADLINE to the owner's own phone** through
`helm/notify.py` (title, option KEYS AND LABELS, card id, and how to reply —
never the context paragraph or the consequences). **THE DECISION TRAVELS IN
THE PUSH AND THE PUSH NAMES NO CONSOLE.** The push channel is the only leg
that reliably reaches the owner's phone; helm web binds 127.0.0.1, which on
a phone is the phone, so pointing the owner there was always a dead end —
and helm deliberately builds no remote/mobile plumbing for this UI. The option KEYS
travel with the labels so a one-character reply resolves through the same
`_pick_option` a full label does. That push is the whole point of the
queue rather than a courtesy: a room is read by SEATS, so without it a card
waits on the owner NOTICING, and overnight that is a queue of rulings nobody
is blocked on because nobody knows it filled up — the same law
`notify.py` was written for one layer down (an alarm about the fleet being
unreachable must not depend on a fleet member being reachable). REACH IS
RECORDED, NEVER ASSUMED: `owner_push` returns True for BOTH a delivery and a
deliberate opt-out, so `configured()` is read BEFORE the call and
`owner_pushed_ts` is stamped only when a headline actually left the box (the
latch defect fixed in `helm/beacons.py` for fleet alarms; a card is worse
placed to survive it, having no next edge to correct itself). A card that
was never reached is therefore CARRIED: the next card's push names the
backlog in one extra sentence — batched, never one buzz per row — and a
carried card is not carried twice. The room line and `decide list` both say
which state a card is in, because an open card the owner was never told
about looks exactly like one he is ignoring and only one of them is waiting
on him. UNSET `HELM_NTFY_TOPIC` is a deliberate opt-out that makes no
network call and is reported, not treated as delivery. NOTE FOR TEST
AUTHORS: filing a card is an OUTBOUND NETWORK ACT — any suite that files one
must scrub `HELM_NTFY_TOPIC`/`MELD_NTFY_TOPIC`, and a guard in
`tests/test_ownerdecisions.py` fails the suite that does not. The card
itself waits on the WEB queue: the console's **work** tab — the
owner⇄fleet surface, where the task backlog and store review will join it
as sibling sections — with a count on the home deck that links
there. Option buttons, comment box, retry-delivery all route through the
SAME functions these CLI verbs call. **`decided` does not close a card** —
delivery does, and **delivery binds to the LEDGER, not the lane**: the
verdict and its `delivered_ref` are durable rows (`helm decide show` always
pulls the ruling) while the DM to the FILING SEAT (exact-token lane,
beacon-woken, waits for an unjoined seat) is the lossy ATTENTION layer.
Chat is tmpfs — a reboot, lane rotation, or seat GC can eat the DM — so
`deliver` on a delivered card RE-VERIFIES the lane: recorded row present is
the idempotent no-op; absent REOPENS delivery, and the reopen snapshot
(status back to `decided`, dead ref cleared) lands **BEFORE** any resend —
so a failed resend leaves a card that is visibly decided/delivery-pending
on every queue surface, never terminally `delivered` on dead evidence. The
web queue also projects recently-delivered cards whose evidence is gone
back into view (`dm_lost`, recency-bounded: an OLD vanished row is
overwhelmingly seen-then-rotated, and flagging it forever would flood the
queue with stale re-deliver noise; older losses stay discoverable via
`show`, which warns read-only). Delivery is **AT-LEAST-ONCE**, stated: a
crash between DM and append, or a lane lost after both, re-DMs on retry —
a duplicate notification is cheap, a lost ruling is not. `comment` is the non-closing lane (it DMs the asker
too). The board's `owner_gated_queue` key is a PRESERVED-PASSTHROUGH
projection (`board-sync`, auto-run on file/verdict/deliver): rows the
projection wrote (stamped `src:"ledger"`) re-derive from open cards +
unreported asks — a single source that cannot drift — while hand-written
rows (owner gates awaiting migration to `helm asks add`, informational FYIs
whose canonical home IS the board) pass through verbatim, never silently
discarded. A missing ledger is known-empty; an unreadable one is
**UNAVAILABLE / owner decisions UNKNOWN**, never zero.

```console
$ helm decide file store GC policy --ref lane/store-gc <<'EOF'
The store holds 400 retired entries; scans cost 2s per resolve.
* Archive to cold file :: resolves fast; history one file away
*! Leave in place :: zero risk; scans stay slow until indexed
EOF
decision 3f9a1c2e filed for the owner: store GC policy (Archive to cold file/Leave in place) — verdict will DM builder-1
```

### `helm landgate --lane L --tip SHA --tree SHA --gate ID [--base REF] [--repo P]`

READ-ONLY. Answers "may THIS seat land THIS lane right now" and lands nothing.

The board's single-writer contract and the integrator's sole-lander role were
the same question, asked one layer apart, and the owner asked it first: is one
agent an unnecessary single point of failure? The meld answered
YES for the MECHANISM and NO for the ROLE — leadership is one account of what
is landing and why, while the ACT of landing needs SERIALISATION, which
`landlock:helm` already provides. So "qualified to land" becomes a PREDICATE
anyone can compute rather than a role someone holds.

**The five clauses**, each reported on its own line, refusal named by clause:

| clause | what it proves |
|---|---|
| (i) landlock held | this seat, not another, holds `landlock:helm` |
| (ii) gate binds the **post-rebase tree** | the receipt describes what BECOMES TRUNK, not what was reviewed |
| (iii) cross-family APPROVE at the **reviewed tip** | a human reviewed this exact tip |
| (iv) changed-file set **disjoint** from other approved-unlanded lanes | a cheap pre-filter against cross-lane collision |
| (v) freeze admits | the land window is open |

**(ii) and (iii) bind different trees on purpose.** The verdict is about what a
human reviewed; the gate is about what becomes trunk. Every dispatch already
says the integrator "rebases the chain and runs the final exact-tree gate
once" — that gate exists today only because it is one agent's last keystroke,
and decentralising without naming it would delete the strongest check there is.
Collapsing them to one tip is the mistake this verb makes impossible.

**(iv) is a pre-filter, not a composition proof.** Three lanes once edited the
same `vcs.py` spawn count (22→25, 22→23, true merged 26) and each was
individually sound; a mutex cannot see that. But disjoint file sets do not
prove composition either — a signature change in `x.py` and a new caller in
`y.py` are disjoint and still break, and only (ii) catches that. Do not later
drop (ii) as redundant on the grounds that (iv) covers it.

**UNKNOWN never qualifies.** An unreadable claims ledger, an ambiguous gate
token, an undiffable tip all REFUSE. This is deliberately the opposite polarity
to `helm/board.py`, whose guard fails OPEN because a board write is recoverable
and the guard targets an honest mistake: a land is irreversible on shared
trunk, so what cannot be proven is refused.

The verb REPORTS; it does not gather. It reads an empty queue and no supplied
verdict state, so a QUALIFIED from the bare CLI is necessary, never sufficient.

### `helm gate run [--label T] [--timeout S] [--repo P] [--box NAME] [--json] [-- <argv>...] | show <id> [--json] | list [--limit N] [--json]`

**A suite result becomes a claim only by being MINTED here.** Before this verb,
a gate verdict entered helm as prose: the only thing any code did to the
evidence string was check its LENGTH (256 printable characters). A reader could
not learn which interpreter produced it, which tree it ran against, or whether
it was run at all.

That is not theoretical. **This box has two pythons and they disagree about
whether the suite passes** — `python` is GraalPy 3.12.8, `python3` is CPython
3.14.4, and on 2026-07-30 the same tree HUNG under one and returned OK under
the other twenty minutes apart. Two seats reported honest, contradictory
results and neither was flaky (premise
`a-suite-result-is-not-a-claim-without-its-interpreter`). The protocol delta —
state your interpreter — was adopted socially the same day and enforced
nowhere.

**The interpreter is bound by construction, not by parsing.** `run` spawns the
suite as a child of the helm process itself, using its own `sys.executable`, so
there is no window in which the recorded interpreter and the running one can
differ. To gate under the other interpreter, invoke helm under it:
`python3 -m helm gate run` versus `python -m helm gate run`. That makes the
choice explicit at the only place it was ever ambiguous.

`helm gate run --help` is a **safe existence probe**: it prints usage and exits
zero before entering the FIFO, starting a child, or writing a receipt. Unknown
Helm options and malformed timeout values likewise refuse before any of those
effects. Options after the exact `--` separator belong to the child command and
are preserved verbatim rather than parsed as Helm options.

**Whole-suite runs enter one repository FIFO before they read the tree or
start a child.** The queue is keyed by Git's common directory, so every linked
worktree shares the same order. Each invocation records its own immutable
position and exact launcher PID generation; only the oldest live position may
start, and the one-slot bound is explicit. Poll frequency affects wake latency,
never order. Dead, recycled-PID, and zombie waiters become ambient `GATE ORPHAN`
room lines and are skipped. A running child is bound by exact PID generation and periodically
revalidates that binding; the hold itself has no TTL to expire beneath a proven
live suite. During mixed-version rollout, the FIFO head also acquires the legacy
`gatelock:<project>` claim derived from the same Git common directory. A daemon
strictly refreshes that compatibility claim through post-run reads and
stale-base analysis, then stops while receipt append validates the exact lease
under the claims lock. Strict claim, refresh, commit, and release operations wait
boundedly through transient global-lock contention, but still fail closed when
the lock remains unavailable or the claim ledger is malformed. The short TTL
recovers if the launcher dies; a missing, replaced, or unreadable claim refuses
the receipt rather than minting
through lost exclusion. Thus a FIFO-aware runner cannot overlap an older binary
or a manual legacy holder, even under the same seat/session identity, while FIFO
positions remain the ordering authority among upgraded callers. A one-byte
inherited launch barrier prevents the supervisor from starting any suite process
until its exact PID generation is durably bound into the FIFO row. Before that
bind, its argv carries the row's immutable position token and launcher PID; if the
launcher dies, queue recovery scans the same-uid process table for that exact pair,
resumes a stopped supervisor into cleanup, and retains the row until it dies. An
independent pipe-free watchdog is spawned before suite admission and watches exact
launcher and supervisor PID generations plus executable process state; `T`/`t`
launchers are unavailable before their short compatibility lease can expire, while
`Z`/`X` launchers are dead even before reaping. It supplies the same `SIGCONT` +
`SIGTERM` cleanup immediately even when no later FIFO transaction occurs. A dedicated
guard enters a delegated per-position cgroup before spawning the inner supervisor.
All suite descendants inherit that kernel-owned membership despite new sessions or
process groups, while retaining the launcher's PID namespace so process-table tests
observe production semantics. `cgroup.kill` removes the entire membership — including
the simultaneous guard+supervisor `SIGKILL` case. A watchdog spawned outside the
cgroup kills and removes it after launcher or guard death. Unrelated launcher children
never enter the cgroup and are never swept. A terminal `Z`/`X` supervisor cannot bind
or renew. The guard remains an outer subreaper for ordinary cleanup, while cgroup
membership is the final containment owner; pipe collection stays bounded even if an
external holder survives.
The launcher retains the slot through post-run tree reads and
receipt append; it posts ambient `GATE
FINISH ... NEXT @seat` while the row is still `finishing`, then releases, so the
next `GATE START` cannot appear first. Chat is observation only: posting failure
never changes admission.

Queue wait does not consume `--timeout`; the clock starts at child creation.
Timeout and interruption kill the supervised child tree before release,
including descendants that created a new session or whose group leader exited.
Custom `-- <argv>`
commands remain outside this expensive whole-suite FIFO and remain non-binding.
Unreadable queue state or an unavailable queue lock refuses a new start rather
than treating the queue as empty.

A receipt records the interpreter, `HEAD`, `HEAD^{tree}`, **whether the
worktree was dirty**, the argv, the runner's own `Ran N` / `OK` / `FAILED`
summary, and the wall time. `run` prints one canonical evidence line:

```
gate:0a1b2c3d4e5f6a7b | CPython-3.14.4 | tree=9f8e7d6c5b4a | whole-suite | Ran 5115 (skipped=8) OK
```

Everything after the token is for a human to read; **nothing after it is
trusted.** Paste the whole line into `helm dispatch verdict`, which resolves
the `gate:` token — see the binding rules there.

* **The tree is read before AND after the run, and both halves must agree.** A
  suite child that modifies a tracked file leaves the worktree dirty
  *afterward*, and a receipt taken only beforehand says clean — so it never
  proved execution against the immutable tree it names. A receipt whose HEAD or
  tree moved during the run describes no single tree; a receipt with no
  post-run read at all is refused rather than assumed unchanged.
* **Dirty is part of the claim, not a warning.** A green suite over uncommitted
  edits proves something about a tree nobody else can check out, so such a
  receipt can name a commit but never bind one.
* **The exit code is part of the claim.** A child that prints a plausible `OK`
  footer and then exits nonzero has two sources disagreeing, which is the
  clearest UNKNOWN there is. The contradiction is recorded rather than
  resolved: picking a winner would mean ignoring one of two honest signals.
* **A receipt whose id does not match its own content does not resolve.** The
  id is a hash of every field a reader relies on, and it is recomputed on the
  way in — so a hand-written ledger line with an invented id, and a real
  receipt edited after minting, are both rejected. The check is **total**: a
  row the recompute cannot even evaluate (`"interpreter": "not-an-object"`) is
  dropped like any other mismatch rather than raising, because one malformed
  line must not be able to hide every honest receipt behind it. `list` reports
  how many rows were skipped — a ledger that drops rows silently is how a
  count reports zero and gets believed.
* **No summary is UNKNOWN, never OK.** A run killed by a timeout, a GraalPy
  hang, or an import error before collection prints no verdict line at all.
  A truncated `Ran 300` keeps the truthful count and withholds the unobserved
  verdict.
* **A `--` command records interpreter UNKNOWN and can never bind a verdict.**
  helm did not choose that interpreter and cannot see inside a shell script, so
  the receipt does not answer the question the gate exists to answer. An honest
  refusal, never a pass whose input was missing.
* **An ambiguous id prefix refuses** rather than picking the newest: a token
  that names two runs names neither.
* **`--box NAME` routes the whole suite to a consented inventory box** (the
  job-routing seam — the helm-native spelling of `fab gate`, composing
  alongside it, never replacing it). The box resolves UNIQUELY against
  `host`/`ssh_host` in the `helm/boxes.py` provider chain (labels are display,
  not address; an ambiguous name refuses) and must be reachable — the exact
  boolean, not a provider's `'false'` string. **Executing a job is a separate
  consent, above probing**: `HELM_STORAGE_MATRIX_HOSTS` /
  `HELM_BOXES_SSH_CONSENT` authorize reading a box, never running shipped
  code on it — and the job capability is exactly one thing, the owner's
  `HELM_BOXES_JOB_CONSENT` (`all` or a comma list of the actual `ssh_host`
  channels); ABSENT refuses. A display/lookup host alias never grants execution
  on a different channel. The inventory's `fab_accept` is **not** that
  capability: it is fab's transient
  eligibility proxy (up ∧ cargo-ready ∧ build storage above the floor,
  recomputed on every render — "eligible, not a routing guarantee"), so a
  `yes` cannot authorize arbitrary code and a `no` cannot veto a grant the
  owner did make; it rides along as an eligibility NOTE beside the result.
  An incomplete inventory (any provider failure) refuses as UNKNOWN **before
  resolution** — a surviving lower row proves neither uniqueness nor
  authority while the chain is broken — and every refusal names its lever.
  What ships is the
  COMMITTED tree as a git bundle pinned at the CAPTURED sha (a temp
  `refs/helm-job/<challenge>` ref, streamed from disk) — the same commit
  object, re-verified by sha on the far side — because `gate import`'s ladder
  resolves the receipt's head in the importing repo and compares trees
  byte-for-byte; a dirty worktree refuses up front, the tree must CARRY helm
  (the remote command is the shipped tree's own runner), and the caller tree
  is read AGAIN after the remote run — movement refuses the import. The
  remote box runs the shipped tree's OWN `python3 -m helm gate run` in a
  scratch dir (tmpfs only under the ram-scratch headroom law with the
  absolute inode floor, else disk). Every blocking stage owns a `setsid`
  process group; HUP/INT/PIPE/TERM cleanup applies bounded TERM→KILL to the
  whole group, then removes scratch. The far side owns a hard session deadline
  because local ssh timeout/Ctrl-C/network loss does not signal sshd's non-pty
  child; client keepalives bound dead paths, and stdout/stderr retain only a
  fixed memory ceiling while still being drained. Thus code that ignores TERM,
  leaves descendants, loses its reader, or loses its client cannot strand the
  routing session. `suite_cap`, PSI and the FIFO are judged by the box that
  owns them — a remote refusal is relayed through the terminal-safe laundering
  boundary and nothing imports. Every protocol
  marker carries a per-invocation random challenge, so a replayed transcript
  cannot mint — but the challenge rides in the session's own argv, so the job
  under test can read it, and the FRAMING is structural instead: payload
  sections travel base64 (no `:` in the alphabet), a section's interior is
  data by position, and a control field written twice refuses the whole
  transcript. The
  session reports its own `uname -n` and the receipt's host must equal it
  (a receipt about another machine refuses), and a byte-identical
  already-imported receipt refuses as replay — a fresh run always mints a new
  one. The fetched receipt enters the ledger only through `gate import`'s
  strict seam; a FAILED suite imports honestly and the exit stays
  bind-derived, exactly as local. `--box` with a `--` command refuses: an
  interpreter helm did not choose cannot bind, so routing it would spend a
  box on an unbindable claim.

`show` prints one receipt in full; `list` is the recent ledger. `run`'s exit
status **is** the binding — 0 only when a verdict citing the receipt would be
accepted — so a run this verb calls green can never be one the land path
refuses.

**What this buys, and what it does not.** Every stored receipt is
self-consistent, so an edited, corrupted, or hand-written row is rejected. It
is **not unforgeable, and no content hash can be**: the id is a pure function
of the row, so anyone able to write the ledger can compute a matching one, and
every seat on this box runs as the same user. `helm gate` is an INTEGRITY
instrument — it makes an honest claim reproducible and a careless one
detectable. A seat that deliberately writes the ledger is a dishonest agent,
which is a different problem whose answer is signing the receipt with the cell
signer the way `helm lr land` signs land candidates. That is a live open
question, not a closed one.

### `helm dispatch send <recipient> <lane> <message...|stdin> --ref TIP --kind build|review --new-work|--supersedes ID [--key K] [--force] | add <recipient> <lane> --ref TIP --kind build|review --new-work|--supersedes ID [--force] | verdict <id-or-unique-prefix> <full-reviewed-tip> --approve|--fix|--supersede|--concur <evidence> | cancel <id-or-unique-prefix> <reason...> | rebind <id-or-unique-prefix> --to <seat> [--force] [--reason R] [--repo PATH] [--json] | retip <id-or-unique-prefix> --ref NEW_TIP --reason R [--repo PATH] [--json] | list [--open|--overdue] [--json] | mix [--hours N] [--sender SEAT] [--json]`

**EXACTLY ONE of `--new-work` or `--supersedes <dispatch-id>` is REQUIRED on `send` and `add`.** The lane is a free-text LABEL and always was; work IDENTITY is the CHAIN. `--supersedes` names the row this one continues, and the row inherits that row's chain root; `--new-work` roots a fresh chain at this row's own id.

One missing relation cost three separate things in a single night, pointing in opposite directions, which is why patching any one of them alone kept failing:

* **124 open land loops with zero terminal rows**, 43 of 46 READY rows pointing at commits that no longer existed. A close-out discharged 10 with proof and had to REFUSE 11 more where the verb WOULD have accepted a candidate — any later approved trunk commit trivially "contains" a change already on trunk, so accepting would have been a verb-blessed lie.
* **Two seats built the same work in parallel** and neither could know. The integrator then told one to stop and was wrong: their lane touched a file the landed fix never did.
* **The meld spiral guard counted a lane string**, so it overcounted a finished spiral and undercounted a continuing one. When lane `gate-mints-its-own-evidence` landed, `gate-epoch-is-append-order` opened immediately to close a hole in it — round 10 of the same work under a new name, invisible to any same-lane rule.

A weaker proposal — require `--supersedes` only when the same lane already has an open FIX row — was rejected: it misses renamed continuations, which is the third failure above.

**The writer checks duplicate intent in the same locked snapshot it appends against.** A second OPEN child naming the same `--supersedes` parent is strong identity evidence and is refused unless `--force` explicitly declares a legitimate fork; even when forced, the existing child id is printed as a warning. `--new-work` has only the weaker lane-label signal: an OPEN row in the same repository with the same lane label warns but records, because labels can be reused by unrelated work. A closed child is not a duplicate live obligation. If the ledger snapshot is unreadable, the duplicate check is UNKNOWN and the write is refused rather than silently minting through blindness.

**Every state of the parent, stated.** A verb designed on its primary case ships holes that are just the same object later in its own life:

| parent state | behaviour | why |
|---|---|---|
| OPEN | ALLOWED | a continuation may start before the previous round closes; that is also how a duplicate becomes visible |
| CANCELLED | ALLOWED | abandoning a round does not un-happen the work. `rebind` proves it by linking the replacement before retiring the old row, so duplicate refusal never destroys the source obligation |
| VERDICT (any polarity) | ALLOWED | the ordinary next round |
| id does not resolve | REFUSED | UNKNOWN, and unknown never silently means new work |
| ambiguous id prefix | REFUSED | two matches; continuing either would be a guess |
| second OPEN child names one parent | REFUSED unless `--force` | usually a duplicate resend; a legitimate fork remains expressible, but must be explicit and prints the existing child id |
| LEGACY parent (no chain) | ALLOWED, root = the parent's own id | the child's honest statement is "my chain begins at the row I can name". The parent's stored row is never rewritten |
| parent chain malformed | REFUSED | present-and-broken is UNKNOWN, never demoted to LEGACY — LEGACY is the branch that permits |
| ledger UNREADABLE | REFUSED, nothing recorded | rooting a fresh chain here mints exactly the unlinked row the field exists to prevent |

Resolution is ONE HOP — each row stores its resolved root — so a cycle is impossible by construction and round ten under a tenth name still answers with round one's id.

**History stays LEGACY and is never retro-fitted.** Rows written before the field carry no chain at all. `review_spiral` keeps grouping those by lane exactly as before, and `lr discharge` keeps its old author/repo rule for a legacy contrary — otherwise every pre-existing contrary would become permanently undischargeable, which is the same debt in a new costume (one proven-handoff extension: a cross-sender approve qualifies only when the record itself proves the lane changed hands — the discharge section states the proof). A chained contrary requires its candidate to share the chain root exactly; a legacy or malformed candidate can never prove that, so it never qualifies.

**`--kind` is REQUIRED on `send` and `add`.** It records whether a dispatch asks the recipient to BUILD something or to REVIEW something already built — the one fact needed to answer "how is my fleet's capacity allocated", and the one the ledger could never state. It cost a full night: an integrator told to run build lanes sent 15 review rounds on his own work and no surface reported it, because none could. Inferring it from the lane name was measured against the real ledger and misclassified 5 of 17.

It is required rather than optional because optional made the alarm **vacuous** — `mix_alarm` is deliberately silent on an all-UNKNOWN window, so the exact failure the feature exists to catch reproduced by simply not typing the flag. The library still accepts an absent kind: every row written before the field carries UNKNOWN, and `mix` reports those in their own bucket rather than assuming a value.

**A review send checks the current approval tier, advisory-only.** `dispatch send --kind review` reads the one live, explicitly human-certain typed-store prior whose `policy_kind` is `approval-tier`, under the normal active-project shadowing law. `seat:<token>` selectors are literal exact seats; `family:<token>` selectors use only the canonical roster row's self-verified resolved `runtime.family`. Minted proxy families, seat/agent/subagent names, harness types, and UI labels grant no family authority: a Claude-labelled subagent actually running a Codex model is Codex for this gate. Missing, malformed, absent, or unverified runtime evidence is UNKNOWN and refuses authorization rather than being guessed. A conclusively out-of-tier recipient produces a warning with the policy reason, source prior, and valid selectors, but the send continues. Historical v1 family proof on already-landed close events replays unchanged; runtime-only enforcement is forward from v2 writes. `add`, build sends, and the library `send()` primitive remain advisory-only.

**Base freshness belongs to the integrator, not the author.** A review send now
names the measured relation between current trunk and `--ref`, but never refuses
on it. That check is diagnostic only: three different authors rebased, checked
ancestry, gated, and reported truthfully, while another lane landed between the
check and report each time. No author-side check can close that interval. Authors
preserve the immutable content tip the reviewer sees and do not spend another
full gate trying to chase trunk. The integrator chooses landing order, rebases
approved lanes onto each other in that order, obtains the final exact-tree gate
once per lane, and lands ff-only. A moved tip still needs a superseding exact-tip
review row; `range-diff` all-equals proves content identity but never transfers a
verdict or a tree-specific gate by itself.

**Branch identity is future-write evidence, frozen on the row.** A named local
branch binds its canonical `refs/heads/...` identity as before. When `--ref` is a
raw SHA, the writer now asks which local branches point **exactly** at the resolved
commit and binds the branch only when there is exactly one. Zero matches, multiple
matches, a nonzero probe, timeout, or spawn failure leave `ref_branch` absent but
do not refuse the already-proven exact commit: the moved-branch check is an
optional veto, never authority to invent a branch. This applies to every local
branch, including trunk. A raw SHA that is the unique tip of local `main`/`trunk`
binds that branch, so a later trunk advance can make an APPROVE refuse as "moved
under this review"; trunk is not silently exempted from the identity the write
actually observed. FIX and SUPERSEDE remain writable because branch movement can
be the result of those verdicts.

The binding is immutable dispatch history. An idempotent keyed retry still
resolves the supplied ref to prove it names the same requested tip, but any fresh
branch-topology answer is non-semantic: the standing row keeps its first binding.
`rebind` skips the optional branch census and copies the old row's `ref_branch`
value — including an absent value — instead of re-inferring it from the carried
SHA. A later independent dispatch gets a fresh write-boundary observation. If a bound branch is renamed or deleted, Helm does not chase the new
name: the stored ref is unreadable, movement is unanswerable, and the optional
veto fails open without blocking the verdict. Existing rows are never backfilled
from today's topology;
historical SHA dispatches and other rows without `ref_branch` remain
unanswerable exactly as written.

The live measurement that chose this protocol over a review-send refusal was
structural, not marginal: 154 nonterminal land loops; among the 70 READY or
REVIEWED rows, zero refs were provably ff-able from `origin/main` (6 known stale,
64 already unobservable because their objects were gone). Refusing stale review
refs would make the existing queue impossible to review and would only narrow the
same race window. `helm seat` therefore no longer prints an author-side `git
rebase` command when a reused home is behind; it reports the drift and the same
landing-order ownership instead.

**`mix`** answers the capacity question: per sender, how many build vs review vs unknown dispatches in the window, and to whom. It fires `CAPACITY-INVERTED` when one sender has sent several reviews with ZERO build lanes — a fleet fully occupied reviewing one author is maximally busy and minimally diverse, which is why the heartbeat, `lr stalls` and the land count all read healthy through it. A single genuine build lane clears the alarm; it is deliberately a yes/no question, not a ratio, because a ratio invites gaming toward a number when the failure being caught is the total absence of delegated authorship.

**The recipient is exact, and alias evidence can only REFUSE.** `send`/`add` share `resolve_recipient` with `chat dm`: a canonical seat token (case-snapped) proceeds, an unrelated valid token stays a legal pre-join address, but positive alias evidence — a pane/worktree label or owner shorthand provably naming one canonical seat (or ambiguously several) — REFUSES with the suggestion (`did you mean helm-claude-2?`) before any ledger row, operation key, notice, or DM exists. No path returns a replacement recipient: the evidence module (`helm/seat_identity.py`) exposes only `alias_refusal() -> str | None`, so silent retargeting is structurally impossible. Evidence derives from roster/spawn/live joins over Git-registered deterministic seat worktrees; `HELM_SEAT_ALIASES` declares the narrow machine-local fallback (see ENVIRONMENT.md). The same law covers `helm seat spawn/where/resume` unknown-seat errors (closed world: a fuzzy hint may decorate the refusal).

**The body may come from STDIN.** Omit `<message...>` and pipe it, or use a **quoted-delimiter heredoc** such as `<<'EOF'`. This is the literal route, not a convenience: argv bodies and **unquoted** heredocs both substitute backticks and `$()` **in the shell, before helm receives anything**, so helm cannot detect or repair the mangling afterward. Five dispatches were silently corrupted this way in one evening across two model families before the door existed.

The DISPATCH ledger is the durable obligation behind work handed to another
seat. The shipping surface is deliberately small — an append-only event grammar,
immutable verdicts, no exactly-once machinery. `verdict` and `cancel` accept an ID
token only when exactly one canonical ID starts with it (the 12 characters rendered
by list/LR surfaces are normally usable). An exact historical short ID does not
outrank a colliding longer ID: that token is ambiguous and refuses before append.
Mutation events store the resolved canonical full ID; event replay remains literal
exact-ID matching and never prefix-matches history:

* **`send`** persists the obligation FIRST, then attempts exactly one DM. The
  optional `--key` names the operation (namespaced by canonical sender + Git
  repository; derived from the semantic request fields when omitted). **One
  operation sends at most once, ever**: retrying an existing operation never
  re-DMs — a prior attempt whose delivery evidence is missing is AMBIGUOUS,
  not absent, and resending is exactly the duplicate-message hazard the
  reduced core refuses to automate away. Ambiguous delivery stays open as
  **NEEDS CONFIRMATION**: verify at the recipient, never resend blind.
* **`add`** records a handoff performed by another transport; it still
  requires `--ref`, so no new row is ever born without the exact tip its
  verdict must name. Its delivery starts NEEDS CONFIRMATION.
* **`verdict`** closes a REVIEWED obligation. It takes the full exact reviewed
  commit id and refuses unless it equals the row's dispatched tip — a stale
  verdict can never close moved work. Exactly one polarity is required: an
  omitted `--approve|--fix|--supersede|--concur` would create immutable UNDECLARED state
  that no later event can repair, so the writer refuses before touching the
  ledger and recommends `--fix` when the reviewer is unsure. `--approve` also
  requires evidence containing a verified `gate:<token>`. The receipt may run
  at the exact reviewed tip, or at one strictly-later whole-suite head when the
  standing dispatch repository proves the reviewed tip is its ancestor and the
  receipt postdates the dispatch. The receipt's scratch-worktree path is never
  the ancestry authority; unreadable/divergent/reverse proof refuses. FIX/
  SUPERSEDE remain valid without a receipt because they authorize no land and a
  later gated approval can discharge them. The refusal prints both full IDs and
  the 1-based first differing character, so a mistyped SHA is distinguishable
  from an ID-prefix resolution failure. Identical verdict retries are
  idempotent; conflicting ones are refused. Existing historical UNDECLARED and
  ungated rows are immutable and replay unchanged — this boundary prevents new
  instances and never guesses old intent.
* **The evidence is BOUND, not read.** If it carries a `gate:<id>` token (see
  `helm gate` above), that token is resolved against the minted receipt ledger,
  and the verdict is **REFUSED** when the receipt names neither the reviewed
  commit nor a later head proven to contain it, fails the strict post-dispatch
  ordering, ran on a dirty worktree, is not `OK`, names nothing at all, or came
  from a `--` command whose interpreter helm never chose. Each of those is a claim already
  proven false, and there is no compatibility argument for recording one.
  Evidence with **no** token is accepted only for FIX/SUPERSEDE and recorded
  `UNVERIFIED`: preserving a negative review is actionable and does not
  authorize landing. A new APPROVE without a verified receipt is refused before
  append — recording it would create immutable evidence that can never reach
  READY. Verdicts written before this refusal replay exactly as stored,
  including historical ungated approvals: that is the true reading of them,
  and replay never invents a binding history it did not have.
* **`UNVERIFIED` does not reach READY, and that is where the enforcement
  lives.** Recording a claim as unchecked enforces nothing on its own — `lr`
  derived READY from APPROVE polarity alone, so an untokened approve came back
  `gate=UNVERIFIED` *and* `land_state=READY`. An approve with no bound receipt
  now stays **REVIEWED**, and `lr show` prints the reason. Grandfathering is
  per **row** and keyed on the **writer**, never on the clock: `mark_verdict`
  stamps its capabilities BY NAME (`gate_caps: ["receipt-v1"]`) on every event
  it writes. Three states, and they stay distinguishable through replay:

  | `gate_caps` | reading | effect on READY |
  |---|---|---|
  | absent | an old writer — the true reading of every row from before the feature | old derivation, unchanged |
  | a recognized set | apply exactly what those capabilities imply | `receipt-v1` ⇒ must bind a receipt |
  | present but unreadable | **UNKNOWN** | never READY, bound receipt or not |

  Two earlier designs failed on the same axis. A **wall-time boundary** cannot
  say whether a receipt was *obtainable*: the boundary instant passed while
  the feature was still unlanded, so the real ledger already held three
  verdicts stamped after it that no gate verb existed to serve. An **integer
  policy** then invited `2` to mean "newer" and "stricter" at once, so a
  `>= 1` check would silently accept a row that never met the stricter rule.
  A future signed-receipt policy instead adds `signed-receipt-v1`, and the
  land check asks for the exact capability it needs.

  The third state is the one that decides the lane: a corrupted stamp is
  **never** demoted to "old writer", because that would let one bad byte
  launder an ungated approve into READY. Un-readying real work is visible and
  recoverable; silently authorizing a land is neither.
* **`cancel`** honestly ABANDONS an open obligation with a reason — the only
  truthful terminal when a verdict will never come (recipient departed, work
  moot, superseded). It binds no reviewed tip, refuses a dispatch that already
  carries a verdict (that one is already honestly closed), and drops the row
  from every open / overdue / land read — so a stranded dispatch stops nagging
  the idle-watchdog without laundering a review that never happened.
* **`rebind`** moves one OPEN obligation to a new recipient in a single
  operation: the old row is cancelled as REBOUND (the trail names where the
  obligation went, never a bare abandonment) and a NEW row opens preserving
  lane / ref / kind / note / deadline, chained `--supersedes` the old — never
  `--new-work`, because a rebind is the SAME obligation addressed to a
  different seat. It is EVIDENCE-GATED through two independent arms: either
  proxywatch measures the current recipient starved / hung / down, or
  autocompact proves its fresh context window exhausted. An unreadable
  proxywatch is not evidence and does not suppress a current context reading;
  only no evidence across both arms refuses. `--force` overrides with a
  mandatory `--reason`, recorded in the cancel trail.
  `--repo` overrides the row's recorded repository for validating the
  carried ref (legacy rows only); `--json` prints the `{old,new,reason}`
  move for a machine caller. The original DM body does NOT travel — the
  ledger stores only its hash — so the CLI says so on stderr and names who
  to re-brief.
* **`retip`** re-points one OPEN obligation at a NEW TIP in place — same row,
  same recipient, same chain — the mirror of `rebind` for the case where the
  BASE moved rather than the reviewer (a land rebased the lane under an
  unstarted review). One strict seq-ordered `retip` event moves the row's
  `tip`/`ref` and appends the hop (old tip, old ref, when, why, identity) to
  the row's `retips` history; the seq-0 event keeps the original tip forever,
  so this is never a history rewrite and never a successor mint — the
  supersedes chain is reserved for real ROUNDS of work, and a rebase is not a
  round. REFUSED on any non-open row (a verdict BINDS the tip it was written
  against), on a same-tip no-op, without a `--reason`, on a `--repo` naming a
  different repository, on a ref that does not resolve, on DIFFERENT WORK
  (review rows: the per-commit ordered patch-id sequence must match — a rebase
  changes every sha and no patch — and when both tips already sit on trunk the
  reviewed patches themselves must match, because two empty ranges say nothing
  about the work; build rows: the new base must descend from the old; an
  UNANSWERABLE identity proceeds stamped `unverified`, never silently either
  way), on a row whose OPEN successor already supersedes it (the
  obligation's live frontier is the child — re-pointing the parent would mint
  duplicate live work, the duplicate-successor law), and when the successor
  frontier CANNOT BE READ (a not-closed row replaying a malformed supersedes
  is chain-UNKNOWN and might name this parent; a frontier the check could not
  read never reads as clear), and on a row with NO DERIVABLE CURRENT TIP (a
  legacy needs-redispatch shape — a hop must anchor to the tip it moves, and
  an anchorless hop would replay inert). REPLAY ENFORCES THE SAME LAWS: a
  hand-appended retip event after a verdict, past an open OR unreadable
  successor frontier, misnaming the row's DERIVED current tip as its
  `old_tip` (replay's own reconstructed state is the witness, and a row
  whose tip cannot be derived anchors nothing), or carrying no strict
  `identity` stamp (`verified`/`unverified`, kept durably on the hop) is
  inert at fold. The stamp is the writer's recorded TESTIMONY, never
  re-proven at fold: an unkeyed ledger-resident hash cannot gate a forger
  who recomputes it over their own fields, so no `proof` field exists on the
  acceptance path (deleted round 3, pinned inert by test), and a forger with
  append access already sits outside what any ledger-resident scheme can
  reach — they could as easily rewrite the seq-0 event itself. The
  EXACT retry of a committed retip (same tip, same reason) reconciles onto the
  achieved row without a second event, even after the row has since gone
  terminal (reconciliation runs before the OPEN-only gate) — a lost response
  is never punished.
* Four proof-specific post-verdict annotations preserve the immutable verdict:
  **`discharge`** records a later land-authorizing, Git-proven resolution of a
  contrary FIX/SUPERSEDE. It applies the same approval-tier and gate requirement
  as READY, so a raw or ungated `polarity=approve` cannot retire debt the land
  path itself refuses. **`withdraw`** records that a FIX/SUPERSEDE change was proven
  absent from trunk; **`close-landed`** records that an UNDECLARED reviewed change
  was proven present without inventing approval; **`abandon`** records that the
  exact reviewed commit is mechanically MISSING, while land state remains UNKNOWN.
  Each is written only through its gated `helm lr` verb; every later event after
  one terminal annotation stays inert.

There is **no ack or bind** — those verbs are gone; retargeting a stranded
recipient is `rebind`, above, and it is evidence-gated. Historical
ref-less rows (the short-lived v2 schema) stay visible as **NEEDS
REDISPATCH**: redispatch the work with an exact `--ref`; the old row remains
as history. Rows the old schemas already wrote keep replaying truthfully, but
only rows stamped before the reduced core landed (`LEGACY_COMPAT_BOUNDARY`)
can drive those historical transitions — an event appended today, however
well-shaped (including non-string timestamps), is inert at replay, and a
type-corrupt row is skipped without blinding the ledger.

Rows are append-only events in `~/.helm/_global/dispatches.jsonl`, separate
from owner asks while sharing the same hardened event-ledger primitive:
stable flock, incomplete-tail repair before append, one bounded `O_APPEND`
write, file + directory-entry fsync, partial-write rollback, 0600 private
regular files, symlink/hardlink refusal, and per-row fail-safe replay. An
absent ledger is known-empty; an unsafe/unreadable ledger is **UNAVAILABLE /
obligations UNKNOWN** (CLI nonzero and stop-whisper loud), never silently
rendered as zero.

Deadlines are advisory. An overdue row says **NEEDS CHECK-IN** and rides the
stop-whisper after owner asks; it never reassigns work, because a long turn
is observationally identical to a dead seat. The stop-whisper ranks
redispatch needs, then unconfirmed delivery, then overdue check-ins. UTC
timestamps use calendar semantics; future or malformed timestamps read as
NEW, never false-overdue.

### `helm lr list [--all] [--json] | show <id> [--json] | stalls [--json] | foldcheck <tip> [--gate gate:TOKEN] [--repo PATH] [--remote R] [--branch B] [--no-fetch] | legacy-completion-hints [--json] | land <id> [--json] | compose <id> [<id>...] [--trunk REF] [--repo PATH] [--dry-run] [--json] | close <id> --reason landed|superseded|withdrawn|out-of-scope|stranded|subsumed|delivered-report|discharged|resolved [--evidence LINE] [--artifact-ref REF] [--report-ref CHAT_REF] [--tip FULL_SHA] [--repo PATH] [--trunk REF] [--live | --needs-restart WHAT] [--dry-run] [--json] | annotate-delivered-report <id> --artifact-ref REF --report-ref CHAT_REF --evidence LINE [--json] | discharge <id> <full-superseding-tip> <evidence...> [--json] | withdraw <id> <evidence...> [--json] | abandon <id> --reason TEXT [--repo PATH] [--json] | close-landed <id> --trunk REF [--repo PATH] [--json]`

**`foldcheck` is the five fold checks as ONE refusing rung**, in the order a
fold needs them: TIP EXISTS, TREE == GATE, FF-ABLE, CLEAN, ORIGIN HAS IT. Each
answers PASS, REFUSE or **UNKNOWN**, never a bool, and all five must PASS —
so the verb exits 1 on REFUSE *and* on UNKNOWN (not-measured is not consent;
both are reasons not to announce a land) and 2 on an unknown flag. The fifth
is the one that gets skipped when tired and the one that catches an
announced-but-unpushed land, so it RE-FETCHES and never answers from a local
ref: `--no-fetch` makes it UNKNOWN rather than a cheaper PASS, because a
rewound origin reads through a stale remote-tracking ref exactly like a
current one. **The fetch happens ONCE, before BOTH ancestry rungs.** FF-ABLE
and ORIGIN HAS IT ask opposite questions about the same `<remote>/<branch>`
ref, and while the fetch lived inside the fifth they answered from two
different moments: a clone whose tip is pushed, plus a second clone that then
pushes a DESCENDANT of that tip, produced all five PASS over a tip current
trunk could not fast-forward to. So `--no-fetch` — and a fetch that FAILS —
makes FF-ABLE UNKNOWN as well: same ref, same staleness, same law. An offline
run therefore no longer prints "trunk moved; rebase and re-gate"; it never had
the standing to, and when the stale ref already reads NOT an ancestor the
UNKNOWN says so as a hint (never the reverse, which reads as consent).
`--gate` takes a RECEIPT HANDLE (`gate:<16-hex>`), never a tree —
the tree is read out of the receipt store, so a caller-supplied tree is
UNKNOWN by construction: proving that two strings match is not evidence that a
suite ever ran. Every verdict carries the value that decided it (both trees
spelled out, the count and the first dirty path, the ref that was compared)
rather than a restatement of the rule.
### `helm mcpd serve [--port N]`

**The stateless MCP endpoint** (stdlib-only, an owner-ruled hybrid). One localhost POST route serving `server/discover` | `tools/list` | `tools/call` over the **same verb layer the CLI fronts**: the CLI remains the universal floor and the diff oracle (`test_mcpd` pins both front-ends equal on one input — the owner's own reasoning: two implementations of one spec are a differential test neither can fake). Compliance shape: mirrored-header validation (`-32020`), version gate (`-32022` with the supported list), unknown method/tool (`-32601`), Origin validation (DNS-rebinding defence), `resultType` on every result, `ttlMs`/`cacheScope` + deterministic ordering on lists, two-tier errors (protocol vs `isError` execution text a model can self-correct from). **What MCP is NOT here**: the attention transport — beacons/wake stay `Monitor` + `helm chat wait`; an MCP notification informs a client process and never wakes an idle model. Slice 0 = shell + read-only `store_resolve`; slice 1 adds `chat_read`/`chat_post` + one work verb behind per-seat bearer auth, with server-side signing (the owner's signing verdict) behind a flippable seam so the proxied-provenance fallback stays one flip away.

### `helm lr list [--all] [--json] | show <id> [--json] | stalls [--json] | legacy-completion-hints [--json] | land <id> [--json] | compose <id> [<id>...] [--trunk REF] [--repo PATH] [--dry-run] [--json] | close <id> --reason landed|superseded|withdrawn|out-of-scope|stranded|subsumed|delivered-report|discharged|resolved [--evidence LINE] [--artifact-ref REF] [--report-ref CHAT_REF] [--tip FULL_SHA] [--repo PATH] [--trunk REF] [--live | --needs-restart WHAT] [--dry-run] [--json] | annotate-delivered-report <id> --artifact-ref REF --report-ref CHAT_REF --evidence LINE [--json] | discharge <id> <full-superseding-tip> <evidence...> [--json] | withdraw <id> <evidence...> [--json] | abandon <id> --reason TEXT [--repo PATH] [--json] | close-landed <id> --trunk REF [--repo PATH] [--json]`

**`compose` stands N READY lanes on ONE measured tip** in a detached `<repo>-wt/compose/` room: members compose in caller-given order, each member's patch-id is re-measured across the cherry-pick with one instrument for both sides, and every refusal NAMES the member (conflict, drift, already-landed by patch-identity, partial stack, capped-screen UNPROVABLE). It runs NO suite — the one gate the caller runs on the composed tip is the only per-tree evidence there is (an APPROVE carries on CONTENT; a gate rebinds the TREE). On a red composed gate, localize by PREFIX: re-compose the first K members; the longest green prefix lands and the first red member evicts to its own re-gate. `--dry-run` composes, measures, reports, and removes the room.

The **land-request lifecycle**, projected from the dispatch ledger plus Git's
observation of trunk. One dispatch already binds author, lane, the exact
review commit (`--ref` tip), and the reviewer's verdict, so a land loop is
those facts read together rather than a second writable workflow record.
`list`, `show`, and `stalls` are read-only; `land` writes diagnostic evidence,
while `close` (and the still-separate `abandon`) append narrow proof-specific
terminal annotations without rewriting the standing dispatch facts.

`close <id> --reason R` is **the one terminal verb**. Nine reasons (the
register is `landreq.CLOSE_CLI_REASONS`, test-pinned), one

proof ladder each, every rung tri-state (unreadable is UNKNOWN and UNKNOWN
refuses), the trunk pinned to one sha at ladder entry and recorded:
**landed** proves the reviewed change on the pinned trunk (ancestry or exact
patch identity — directly, or through the mechanical rewrite-translation
sidecar `lr migrate --apply` records, rendered `CLOSED (LANDED_REWRITTEN)`);
it admits approve and UNDECLARED verdicts and refuses FIX/SUPERSEDE (a
contrary on trunk is a CONTRARY, not a resolution). An OPEN BUILD row has no
verdict of its own, so a proof-v2 variant walks its exact `supersedes` ancestry
to an authorized APPROVE review descendant and proves that child's reviewed
tip on trunk. The BUILD row's own tip remains the starting base and never
serves as reviewed-content proof; the parent becomes `BUILD — CLOSED (LANDED
via APPROVED REVIEW)` and leaves every open-obligation surface. **landed**
also REQUIRES the LIVE step, `--live | --needs-restart WHAT`: trunk and the
running fleet are different facts, so the closer declares CLI-class (`--live`
— a fresh helm process off main is live at land) or process-class
(`--needs-restart WHAT` names what still holds pre-land code until it
re-arms; `helm rearm` measures that); helm never guesses, an undeclared
landed close is refused at the writer, and a process-class row prints as an
OPEN LOOP. Historical
proof-v1 and `close-landed` events replay unchanged. **superseded** runs the
full discharge ladder (durably-later same-chain same-author/repo APPROVE on
the exact superseding tip, containment, trunk proofs) widened to every
verdict polarity, with a mandatory refutation rung for approve/UNDECLARED
rows: a reviewed change that itself reached trunk is a landed outcome, never
a supersession. **withdrawn** proves the change ABSENT from the pinned trunk
and stays falsifiable — a later land re-exposes the row as CONTRARY
(`close_contradicted`), owed by the integrator. **out-of-scope** retires a
moot OPEN row only, writing a plain `cancel` event through the cancel
boundary after a liveness block (a lane-family ref holding the tip, or a
dirty lane-family worktree, refuses — rebind instead); verdict rows have no
attestation door. **stranded** terminates a destroyed substrate only after
the full liveness ladder: the repo proves it can read a known-good object
(positive control), the tip is provably pruned (bare `cat-file -e`, rc 1),
no recorded translation resolves to a live object, no lane-family ref
survives (generous stem matching — a match names the ref and refuses), and
no lane-family worktree exists. **subsumed** selects one later confirmation
row by `--evidence <id-or-prefix>`: both rows are review dispatches on the same
chain/repo; the original is APPROVE or FIX, while the confirmation remains an
authorized APPROVE. A pre-chain original is admitted only when the
confirmation's supersedes-derived chain root names that exact legacy row. The
confirmation passes the approval policy scoped to that repository plus the
normal gate decision. APPROVE debt retains the marked `Subsumption verified ...
on trunk ...` reimplementation; FIX debt instead requires the exact
`Subsumption verified FIX findings were answered on trunk: ...` sentence with a
concrete resolution. Either marker is first or begins a new sentence after a
gate receipt, and the confirmed work is on current pinned trunk. SUPERSEDE is
not admitted. The original is absent, and the original author and confirmation recipient each
resolve to exactly one different runtime family. The event
anchors both accepted verdict events, structured write-time snapshots of both
family derivations, and the tier/gate authorization that admitted the
confirmation. Replay validates those captured inputs only; a later policy or
runtime-roster change cannot resurrect retired debt. Prefix/full-ID retries
resolve to the stored confirmation identity and still bind the recorded
repository/trunk ref. **delivered-report** applies only to an OPEN dispatch whose
recorded `kind` is exactly `build`; legacy/missing and non-build kinds refuse.
It requires three distinct clean one-line fields: `--artifact-ref` (a compact
`<scheme>:<nonempty-value>` typed reference with a URI-style scheme, no
whitespace, and at most 512 characters), `--report-ref` (the full 12-character
lowercase-hex stable Helm chat row id, at most 256), and concise `--evidence`
(at most 256). This
proves syntactic identity, not artifact or chat-row existence. It records a
strict `close` event and projects
`DELIVERED_REPORT`: terminal, owed by nobody, never stalled, and debt-absorbing
for its chain. The dispatch base/tip is preserved, while reviewed tip, verdict
polarity, gate, Git landing, and trunk proof remain unclaimed. These references
are not transport `delivery_ref`. Retry identity is the exact
artifact-ref + report-ref + evidence tuple; any difference refuses.
**discharged** closes only an OPEN never-verdicted row (the polarity-less
domain: a build row whose successor was minted first has no verdict of
its own and sat doorless while its work ran on trunk) whose work is on
trunk under a successor's landed, gate-verified APPROVE — the authority is
the discharging row's, re-derived from the ledger at write AND replay, never
taken from the event. **resolved** is the polarity-wrong door: a
FIX/SUPERSEDE verdict row whose OWN reviewed tip reached the pinned trunk by
ancestry (patch identity routes to subsumed; absence is withdrawn's
question), confirmed by one later same-repository cross-family review
verdict — approve or supersede, never fix — whose evidence OPENS with
`Resolution verified on trunk: ...`; the overridden reviewer is recorded on
the close event, so the contest path stays auditable.


`legacy-completion-hints` is a read-only audit over currently CANCELLED rows
whose recorded kind is exactly BUILD and whose `cancel_reason` contains the
standalone word `delivered` (case-insensitive). Matching is reason-field-only:
a lane, note, ref, or another serialized field cannot make a row appear. Every
result carries `classification=UNVERIFIED` and
`delivered_report_eligible=UNKNOWN`, remains CANCELLED, and may be an explicit
negative (`delivered neither ...`) or code delivered/landed under a successor
rather than a report artifact. JSON prints the shared caution once as `why` and
the per-row records under `rows`, rather than duplicating identical prose on every
match. The query appends nothing and never recommends promotion; it only makes the
historical prose cohort distinguishable for audit.

`annotate-delivered-report` is the separate append-only correction path for
historical cancelled BUILD rows. It requires the same three references, accepts no OPEN,
review, legacy-kind, or already differently-retired row, and never interprets
the cancellation reason prose. The original `cancel` event and reason stay in
history and on the projection; a strict `close-correction` event explicitly
binds that standing cancel before the canonical state becomes
`DELIVERED_REPORT`. Heuristics only ever REFUSE the Git-backed ladders; their
authoring proofs remain Git's, while delivered-report is authored only by its
three explicit structured references. `--tip` remains
superseded-only; `--repo`/`--trunk` are accepted by the Git-backed landed,
stranded, and subsumed ladders. `--dry-run` runs the full
ladder, appends
nothing on either path, and mirrors the refusing rung's exact message.
Retries reconcile idempotently per reason (landed's identity is repo +
trunk REF, the sha deliberately excluded so a retry after trunk movement
returns the row, exit 0). A row is retired once: every second terminal —
old or new — refuses.

`discharge`, `withdraw`, and `close-landed` are **deprecated aliases of
`close`** for exactly one release (`--reason superseded` / `withdrawn` /
`landed`): each prints a deprecation line and maps its arguments onto the
one close path, writing the same `close` event. Historical `discharge` /
`withdraw` / `close-landed` events replay forever. `lr land` is **not** an
alias and is not deprecated — it is the separate at-integration receipt
verb, fired at the moment the merge exists locally, pre-push, where its
fail-open signed witness and fail-closed deletion guard both mean
something; `close --reason landed` is the post-observation terminal.

`land <id>` is the integration annotation: it emits a signed, chain-ordered
`helm.land` coordination turn over `[lane, branch, reviewed_tip, patch_id,
trunk_sha]` through chat's existing signer/revive/faucet path, then stores the
returned turn/receipt/chain in the local receipt index. It is totally
**fail-open**: no signer, node, or index write can block the land. The current
dregg replay surface cannot disclose a turn's payload, so a structurally valid
row is named `local-unverified`; it is useful diagnostic/correlation evidence
but can **never strengthen or veto Git's lifecycle result**. Wrong topics,
forged fields, malformed complete rows, and competing bindings remain visible
as rejected/conflict diagnostics without overriding independently readable Git.

`discharge <id> <superseding-tip> <evidence...>` closes only the **debt** on an
active contrary FIX/SUPERSEDE row. It refuses unless a durably **later** dispatch
ledger event records an APPROVE verdict **on the same work chain**, by the same
author/repository, for that
exact superseding tip, Git proves the original reviewed change is inside that tip
(ancestry fast path, exact per-commit patch identity fallback), and Git proves the
approved tip reached the authoritative trunk. A configured origin whose tracking
ref is absent/unreadable is UNKNOWN, never permission to fall back to local trunk;
without a configured origin, stray `refs/remotes/origin/*` refs are ignored and
local trunk remains authoritative. Negative or unknown proof refuses; a diagnostic land
receipt can never discharge. **The chain is what makes "same work" checkable**:
any later approved trunk commit trivially contains a change already on trunk, so
plausibility could never distinguish the resolution of THIS round from an
unrelated later one — a close-out had to refuse 11 rows the verb would have
accepted. A contrary that carries a chain requires an exact chain-root match and
a legacy or malformed candidate never qualifies; a LEGACY contrary keeps the old
author/repo rule, so history stays dischargeable. **A cross-sender candidate is
admitted only on a PROVEN lane handoff, never a loosening**: on a chained
contrary, the candidate's declared supersedes ancestry must reach a row the
debt's author sent (the candidate already shares the contrary's chain root); on
a legacy contrary, the author row that ancestry reaches must itself sit in the
contrary's lane-family stem — chaining to any unrelated row of the author's
proves the author's NAME, not their WORK — or, chainless both ways, the
candidate must work the same stem family AND its sender must visibly hold
another dispatch in that family. One family-suffix vocabulary feeds both this
admit stem and the abandonment family veto. Unreadable or ambiguous proof is
UNPROVABLE and refuses, and the refusal names each near-miss's failed leg
beside the same-author requirement. The append-only event names
both dispatches and the
full tips. The original verdict, contrary fact, trunk target, and evidence remain
visible under `show` and `list --all`, while default list/stalls/board stop billing
the row; its dwell freezes at the discharge timestamp. Identical retries are
idempotent and a conflicting second discharge is refused.

`abandon <id> --reason TEXT [--repo PATH]` is the honest terminal for reviewed
work whose exact commit evidence was destroyed by a history rewrite or prune.
It is deliberately **not** a landing proof: ABANDONED means the reviewed work is
written off and its land state is permanently **UNKNOWN**. The mandatory reason
records why that judgement was made. Only `kind=review` rows qualify; build rows
always refuse.

The safety gate is three-valued and repository-bound. Helm asks the row's own
canonical `repo_id` for `<reviewed-tip>^{commit}` using `cat-file --batch-check`.
EXISTS refuses and names the truthful existing route (`land`, `discharge`,
`withdraw`, or `close-landed`); a repository or Git failure is UNKNOWN and
refuses; only Git's explicit MISSING answer may append. The same one-object
probe runs again under the dispatch-ledger lock immediately before append, so
a competing terminal or an object that reappears between preflight and mutation
cannot be overwritten. A second, independent trunk-message scan is an
**asymmetric refusal interlock**: an exact structured `land:`/`Merge lane` hit
or even an ambiguous incidental lane-family match blocks abandonment, while no
message may ever author a LANDED state. Legacy labels encoded workflow rounds,
roles, and ad-hoc handoff words in free-text suffixes, so the refusal scan widens
from the exact name through every useful hyphen prefix (at least two tokens or
eight characters), while recognized short `-rN`/role suffixes preserve legacy
stems such as `delim-r1`. This caught confirmed live false candidates ending in
`-review` and `-contract`; the same census exposed `-xrev` as why an enumerated
suffix list cannot be the identity rule. Exact-name silence would have written
false ABANDONED events over landed family work. Structured tag refs close one
important hole that prose cannot: Helm lists `refs/tags`, token-matches the same
lane-family identities (including `gate/<family>*` round tags), peels each match
to a commit, and refuses when that commit is an ancestor of authoritative trunk.
Lightweight and annotated tags follow the same path. A matching landed tag is
still correlation rather than proof that the reviewed tip landed, so it can only
veto abandonment; an unreadable tag list, target, or ancestry is UNKNOWN and
refuses, while the absence of a matching tag authorizes nothing. Successful new
abandon events
record `structured-message-and-tag-scan` v2; replay keeps accepting the historical
message-only v1 receipt.

The message scan still has an explicit limit: a trunk message may paraphrase the
work without sharing any lane-family token, and no token grammar can prove that
absence. A mention-free result is therefore only the lack of a veto, never proof
that work is absent; shared writes—especially FIX-polarity debt—still require an
independently audited subject and a reason that admits the write-off. A heuristic
may block an irreversible write; it may never authorize one. No clone search,
commit-map, reflog, patch identity, or lane-name heuristic is allowed to guess
where equivalent content landed. The same refusal-only family identity covers
the other places work can survive: any derived `lane/<family>` branch not
contained by authoritative trunk blocks, and a Git-registered worktree on such a
branch with nonempty `status --porcelain` blocks. Git worktree porcelain resolves
the actual path; Helm never guesses one from the lane label. Unreadable
branch/worktree evidence is UNKNOWN and refuses. A missing or clean worktree, an
absent or non-trunk-ancestor tag, and a merged or absent lane branch authorize
nothing — they merely do not veto the independent MISSING-object judgement. Work
lives in at least five places: reviewed tip, structured family tag, lane branch,
uncommitted worktree, and trunk under a rewritten SHA; any positive or unreadable
signal abstains from the write.

The immutable `abandon` event retains the original verdict, polarity, reviewed
tip, gate, author/reviewer, and chain. It records the canonical repository,
MISSING proof mode/version, reason, and `land_state=UNKNOWN`. Identical retries
are idempotent; a different reason and every competing terminal annotation
refuse. Default list/stalls/board stop billing it, while `show`, `list --all`,
and the web closed section retain **ABANDONED — LAND STATE UNKNOWN** forever.

`close-landed <id> --trunk REF [--repo PATH]` is the exact terminal mirror for
an immutable **UNDECLARED** verdict whose reviewed change nevertheless landed.
It never backfills polarity and never implies approval. `--trunk` is mandatory;
a legacy row with no repository binding also requires explicit `--repo` (helm
never searches clones by commit id). The named branch/remote-tracking ref is
resolved once to a full SHA, then Git must prove either exact ancestry or
per-commit patch equivalence. The dispatch ledger records the canonical Git
common-dir, full trunk ref, sampled trunk SHA, proof mode and version. The row
remains `REVIEWED (UNDECLARED)` but gains the monotonic terminal overlay
`CLOSED BY LANDING`; dwell freezes and default list/stalls/board stop billing it,
while `show` and `list --all` retain both facts. Negative or unknown proof refuses
without appending. Normal trunk movement, a later revert, ref rewrite, or Git
becoming unavailable cannot erase the historical closure receipt. Diagnostic
`helm lr land` receipts and free-text verdict prose are never authority.

States are `OPEN`, `AWAITING_REVIEW`, `REVIEWED` (verdict polarity undeclared),
`CHANGES_REQUESTED` (FIX), `READY` (APPROVE), `MERGED_LOCAL`, `LANDED`,
`SUPERSEDED`, and `ABANDONED` (reviewed artifact missing, land state UNKNOWN).
Each non-terminal billable wait carries a DWELL time, which makes
`stalls` a workflow-gap finder rather than an age report: it surfaces loops
past their per-stage threshold and names who owes the next move. An undeclared
verdict has no honest threshold and is reported as unmeasurable rather than
guessed. A CANCELLED dispatch is abandoned, not a land loop, and is excluded.
If Git nevertheless finds a FIX/SUPERSEDE tip on trunk, the verdict-derived
state remains intact but the row is marked **CONTRARY**, stays operationally
non-terminal, and is surfaced in list/show/board as owed by the integrator until
a proof-gated discharge. Afterwards it is terminal and owed by nobody, but still
renders CONTRARY + DISCHARGED in historical views. The contradiction is
information: it is neither hidden nor laundered into a normal approved land.

One row KIND wears that exact signature healthily: a **confirmation row** —
the resolved door's own confirming round, a `kind=review` dispatch sent
`--supersedes` a contrary parent whose verdict polarity is in the door's own
admitted set (`landreq.CONFIRMATION_POLARITIES` — approve or supersede,
NEVER fix, whose meaning is that the work is NOT resolved) and whose verdict
evidence OPENS the literal resolved-door sentence
`Resolution verified on trunk: ...`, read by the door's own parser
(`landreq.confirmation_row`; both gates are shared with the close rung, not
paraphrased, and the phrase is load-bearing — a supersede verdict without
it, or a FIX verdict wearing it, stays a loud CONTRARY). Its reviewed tip is the landed cure
carrier BY DESIGN — the tip reached trunk before the verdict was filed,
which is the round's whole point — so the classifier stamps it
`contrary_discharge="c"` instead of alarming: every surface renders a quiet
"confirmation" (the discharge instrument, never a debt), and the same
citation discharges its supersedes-parent through the ladder arm ("b") the
way an on-trunk APPROVE successor would. Before this kind existed in the
classifier's vocabulary, every use of the resolved door minted one permanent
CONTRARY row and each cure round added another, so the owner's contrary
count could never reach the truth. Display only, exactly as with "a"/"b":
the `contrary` fact, `owed_by`, and stall billing are untouched.

Landed-ness is read by **patch identity**, not object ancestry alone:
reachability is the fast path, but a negative falls through to `git cherry`
because normal lands cherry-pick the reviewed commit and mint a new sha. The
parser finds the line for the exact reviewed commit rather than trusting the
first line of a multi-commit range. Ancestry itself is tri-state — rc 0 means
ancestor, rc 1 means not-ancestor, and rc 128/timeout is unknown — while an
independently readable patch comparison may still answer the landing question.
Unreadable evidence never invents a land or a stall. Unavailable dispatch
storage reports land loops UNKNOWN rather than none.

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
in effect, cred token families (the shared-family/revocation-bomb audit —
content hashes only, byte-copies = FAIL), and git presence (absent = WARN
with the exact per-distro install command; jj/jujutsu noted as a
git-compatible alternative on the radar). Exit 1 only on FAILs.

### `helm proxywatch [--post] [--json] [--force] [--install-timer]`
Every fifteen minutes, compose local proxy/seat evidence for every minted seat with
fresh upstream evidence for every family represented by those seats. The local
rungs remain unchanged: CONFIG drift, the silent-DROP latch, stale-pane HANG
candidacy, the invalid-key PROBE, refusal LOG streaks, and the fused TURN ladder.
TURN still calls a seat HUNG only when the pane is live, the transcript is stale,
there are zero in-flight connections, context is below the compact bar, and the
process is not fresh. An open request is THINKING; any unreadable pane, socket,
context, or spawn input is HUNG-UNKNOWN, never HUNG.

The unauthenticated PROBE sends a deliberately invalid key, never a real
credential. A `401`/`403` proves the local listener and auth path answer;
connection refusal is DOWN, a client timeout is HANG, and a tiny HTTP 200 is the
EMPTY200 fault that makes an auth failure resemble a dropped completion.

UPSTREAM starts with one deterministic, locally healthy primary per represented
family. Every request is capped at eight tokens; a completed dark response gets
one confirmation, and a still-dark primary then triggers every locally healthy
sibling, whose completed failures can each receive the same confirmation. HTTP
200 counts as HEALTHY only when it contains a valid assistant message whose
answer is exactly `OK`, or a non-empty thinking-only message stopped by the
eight-token `max_tokens` cap. Empty bodies are EMPTY200; error-bearing envelopes,
non-assistant content, HTML, and other invalid success envelopes are
MALFORMED200. Named provider 4xx/5xx responses are completed failures. A client
timeout or unreadable request never retries because the first request may still
complete.

A dark primary is corroborated across every locally healthy sibling. If any
corroborator is unreadable the family is UNKNOWN; otherwise any healthy sibling
makes it HEALTHY, unanimous readable dark causes retain their name, and differing
readable causes become FAMILY-MIXED. Current detail and summed measured latency
are recomputed each pass; an entirely unmeasured pass keeps latency unknown.
`since` is stable while the named state is exact and resets when that name
changes. Observation loss cannot prove recovery: UNKNOWN preserves the dark
latch, the last named dark state, and its original `since` until measured evidence
returns; only measured HEALTHY closes the episode. That same latch now drives
chat delivery: every dark family row prints `beacon=PAUSED-CRED-WALL`, and its
seats' armed waiters retain addressed rows without waking. The recovery pass
writes `HEALTHY`; the next existing waiter poll resumes automatically. No second
pause ledger or manual unmute exists, so alert, roster badge, and actuator cannot
drift onto different state machines. UNKNOWN persists as UNKNOWN (the last named
cause is separate episode context), so proxywatch and the roster name the same
current evidence. Family identity comes from the exact session's self-verified,
launch-owned `runtime.family` first (so a custom label such as `pi-codex` still
follows the Codex wall); foreign mirrors cannot steer delivery, co-named sessions
keep independent families, and ordinary `family[-N]` names are only the
compatibility fallback. Proxywatch's authenticated canary uses that same family.

`--post` uses the existing durable chat outbox and reports only on semantic
change. All named dark causes share one alert fingerprint during a latched
outage, so cause churn stays quiet; measured recovery emits one recovered edge
and re-arms the next dark edge. `--force` posts a report but never manufactures a
family transition. `--install-timer` wires the fifteen-minute cadence. Exit 1 when
there are findings.

### `helm proxy-fork-watch [--json] [--post] [--force] [--install-timer]`
Run the upstream-compatibility checker shipped in the durable CLIProxyAPI source
clone. The default clone is
`~/.helm/_global/proxy-fork/CLIProxyAPI`; `HELM_PROXY_FORK_DIR` is the only
override (there is deliberately no legacy fallback). Helm refuses a missing
clone, a missing checker, or any local branch other than
`helm/upstream-tracking`, then invokes exactly `go run -mod=readonly
./cmd/helm-upstream-check` with `GOWORK=off`, a finite timeout, no shell, and the
clone as cwd.

Bare and `--json` are **read-only**: no notification state and no chat post.
Checker exit 0 and 1 carry strict, newline-terminated JSONL (one schema-1 record
per fork branch); malformed/duplicate/mixed-upstream output, exit 2, timeout,
and clone validation failures render as stable `ERROR[category]` reports.

`--post` holds one exclusive lock across checker + prior-state read + decision +
post + atomic write. The single state file is
`~/.helm/_global/.state/proxy-fork-watch.json`; every attempt records its start
and finish, overall `CLEAN|DEBT|ERROR`, checker exit/commit/upstream, branch
results, stable error category, and post outcome. The announced fingerprint
contains only semantic debt (branch set, status, conflict files, failing
packages, or error category) — never timestamps, SHAs, checker commit, or test
targets. The first post always speaks, including clean; later posts speak only
on semantic change, including one recovery. `--force` speaks regardless. A
failed chat post does **not** advance the announced fingerprint, so the next
cadence retries rather than consuming the notification.

Each announcement is exactly one physical line in room `helm`, authored as
`proxy-fork-watch`, with no mention. It names `FIRST`, `CHANGED`, or `FORCED`,
compact per-branch status, and the state path. `--install-timer` atomically
installs a bounded, low-priority `Type=oneshot` user service plus a persistent
daily timer under `~/.config/systemd/user`, then daemon-reloads and enables it.
The service always runs `%h/.local/bin/helm proxy-fork-watch --post`; it never
captures a worktree path or references the deployed CLIProxyAPI binary.

Tests validate the units and installer subprocess contract hermetically. A live
`systemd --user` execution is intentionally **not automated** by the test suite;
installation and the next real timer firing remain the operator-level exercise.

### `helm eval arms [--seat S] [--json] | register [--seat S] [--out PATH]`
The cc-codex vs pi-codex eval's guard rails, which exist **before** its runner
on purpose.

`arms` asks the one question the eval design says the whole eval lives or dies on: are the
two arms COMPARABLE — same endpoint, same model? Measured 2026-07-29, they were
not, and nothing said so: `cc-codex` reaches codex through helm's CLIProxyAPI on
a subscription OAuth while pi had one provider configured (openrouter). Run as
specified, the eval would have compared Claude-Code-over-subscription with
pi-over-OpenRouter and credited the whole difference to the **harness**. Exit 1
when NOT pinned, naming each mismatched field and what the difference would have
been mistaken for. The pi arm is read from the **installed extension**, not from
what the generator would emit — a generator asked about itself agrees every time.

`register` writes the eval's decision rule **before** any run, stamped with the
arms fingerprint it was registered against, so a run against different arms
cannot inherit it. It refuses on unpinned arms: pre-registering a rule for an
experiment that cannot be run leaves a file that later reads as due diligence.

### `helm pi extension|run|launch|resume|status`
The pi harness seam. `extension` GENERATES a pi extension that points pi at a
helm seat's own CLIProxyAPI, so a pi run and a Claude Code run reach the same
upstream, through the same proxy, on the same credentials — which is what makes
a cc-vs-pi comparison a measurement of the **harness** rather than of two
different providers. Prints the shim by default; `--apply` installs it under
pi's extension dir.

`helm pi run [SEAT]` is the executable proxy path. It defaults `--model` to the
matching `helm-SEAT/MODEL`, refuses a model from another provider, labels the
child as `pi-<seat>` with canonical `family · pi/proxy` runtime metadata, mirrors
that fact into the roster before exec, and then replaces itself with pi. An
explicit model is allowed only inside the same `helm-SEAT` provider. `launch`
and `resume` remain key-free printable recipes.

**No TypeScript lives in this repo** — the shim is generated on demand, so helm
stays a stdlib-only Python package. **The key is never written down**: the
provider reads `HELM_PI_PROXY_KEY` directly from the child process environment.
The `run` path narrowly reads the first key from the seat's `0600` `api-keys`
stanza and assigns it only to that environment; it never prints the value,
writes another copy, or places it in argv.

**Pi/Claude is deliberately proxy-only in this estate.** Pi technically supports
Claude Pro/Max OAuth and can present Claude Code identity headers; helm does not
use that path. The standing account rule is stricter than Pi's capability:
Claude runs only through the first-party `claude-code` CLI under Max OAuth, never
through `@anthropic-ai/sdk` or an API key. Therefore a Pi seat doing
Claude-family work is labelled `pi/proxy` and cannot satisfy the cross-family
review gate in-process; route the gate to a separately addressable external seat.
Whether a third-party client may ever spend the owner's Max subscription directly
is an owner-held account/ToS decision, not a missing adapter to implement.

`status` reports whether pi is installed, where its sessions live, how many
projects helm sees through them, and which shims are generated.

### `helm wiring [--verbose] [--json] [--gate]`
The **built / reachable / actuated / exercised / verified** ladder. Static import
reachability still answers whether any entry point can call a module;
`UNREACHABLE` means code nothing will ever run, however good its tests are.
Reachability, not "has an importer" — two dead modules importing each other
satisfy the latter unanimously and are still dead. Dispatch through
`_lazy("mod", "fn")` and a literal `import_module("helm.x")` count as real
edges, because they are helm's actual verb surface.

`NO ACTUATOR` is the separate installed-estate question: does an executable Git
hook, enabled user-systemd unit, active crontab line, Claude Code hook, MCP
command, or registered chat verb actually invoke each declared detector/action?
Comments, shipped-but-uninstalled units, disabled timers, and an `ALLOWED` prose
claim are not evidence. External-import exemptions such as `nevertrack` and
`hostpath_guard` name checkable obligations; an absent installed hook is reported
instead of being permanently hidden by the exemption. Missing actions fold into
one readable warning near the top of `helm doctor`; this is report-only and never
a commit or Stop refusal. The standing declarations include corpus backup,
worktree GC, dispatch-mix observation, and dispatch-rebind (the evidence-gated
re-route built for an unattended caller that does not exist yet), so the census
also names working tools that nobody scheduled.

`--gate` asks the narrower question the Stop hook asks: did **this working
tree** add a module nothing can reach? That rung refuses an idle stop, so it
never interrupts work — only finishing while leaving dead code behind — and it
is scoped so no seat is ever blocked on debt it did not create. Wire the
module, or declare it in `helm/wiring.py` `ALLOWED` with the reason it is
unreachable. `HELM_STOP_GUARD_WIRING=0` disables the rung. Exit 1 when anything
is unreachable.

Found `helm/board.py` on its first real run: the integration board's locked
write path, built to replace the ad-hoc `json.dump` that had already lost an
update, with no verb and no caller for two days.

### `helm punt [--text T | --transcript P] [--json]`
The **dressed declination** detector. The `punt-tell` reflex already catches a
punt that announces itself — "later", "for now", "todo:", "park it". This
catches the expensive shape: the one that arrives wearing a reason.

> Three things I did **not** do: restart the live chat node (would disrupt the
> fleet mid-work), install the staged signer (unproven end-to-end), or chase
> the node build further while you're away.

Every clause reads as judgment; all three were punts. Measured minutes later,
the seats supposedly mid-work had **no live process at all**, so there was
nothing to disrupt; "unproven" described nobody having run the proof; and
"while you're away" is a fact about a calendar, not about the work.

A finding needs **both halves in one sentence**: a first-person declined action
AND an excuse from a named class — `owner-presence`, `assumed-disruption`,
`unproven-as-excuse`, `size-or-cost`, `someone-elses-lane`. Honest prose is
full of negation ("this does not fix the node half"), so the pairing is the
discriminator; a rung that fired on plain negation would be switched off within
a day, which is the fate of every gate that cries wolf.

The Stop hook reads the turn's last assistant message and refuses an idle stop
on a hit **when the owner-ask ledger has nothing open**. That escape hatch is
the rule, not a loophole: real blockers exist, and owner canon is that anything
truly deferred is LOUDLY FLAGGED, never silently parked. `helm asks add "<what
you are not doing and why>"` discharges it — turning a sentence the owner has
to catch by reading into a row he can see. `HELM_STOP_GUARD_PUNT=0` disables.
Exit 1 when prose carries a finding.

Owner: *"'i won't do X because y (where y is based on clock time,
my location status, or assumed interruptions it would cause, without actually
checking...)' is 100% of the time a punt."*

### `helm board show [--json] | landed <name> <sha> <note...> | set <key> <value...> [--new]`
The integration board — the owner console's lane truth. `landed` prepends one
row through the **locked, idempotent** write path; the lock is exactly what a
hand-rolled `json.dump(board, open(path, "w"))` does not have, and that pattern
has already produced a lost update (two agents read-modify-wrote the same file
seconds apart) and can produce a torn file (the truncate lands before the
bytes). `HELM_BOARD` overrides the path.

`set` updates an existing top-level **scalar** and refuses two things it cannot
know to be safe. **A new key** needs `--new`: helm does not own the console that
renders this file (the board's own `_contract` names it as helm-claude's
generator, and nothing in this repo reads the board), so an invented key may be
written and never displayed — a row that reads as recorded and is invisible to
the person it was recorded for. **Clobbering a list or dict with a scalar** is
refused outright: `landed` is an audit trail, and replacing it with a word
atomically under a lock is the most convincing possible way to destroy it. A
refused set leaves the file byte-identical.

### `helm note set <key> <headline...> [--detail <body...>] [--goto <tab-or-url>] | list [--json] | retire <key> | restore <key> | rm <key>`
**Fleet notes** — what the fleet leaves the OWNER. One note per key, replaced
by whoever writes it next (last-writer-wins), rendered as the first card on the
cockpit's **home** tab, newest first, each with who wrote it and how long ago.

**Freshness is a display tier, never a deletion.** A note past
`fleetnotes.FRESH_WINDOW_S` (48h) is `stale`: the card folds it behind an
expandable summary and the CLI rules it off below a divider, but it is still
listed, still on disk, and still one click away. Both surfaces read that ONE
boundary so they cannot disagree about what is current. A note whose stamp will
not parse is **never** dimmed (`age_s` is `null`, not zero) and sorts FIRST —
hiding a note on the strength of a number nobody could read is the failure this
tier exists to prevent, and sorting undatable notes first is what lets the split
preserve the feed order exactly.

**`retire` is the undoable exit arc; `rm` is destruction.** `helm note retire`
drops a note out of the current section at any age and keeps it on disk, and
`helm note restore` puts it back — the author declaring a note done, as opposed
to the clock declaring it old. `helm note rm` DELETES and nothing brings it
back. The two are deliberately different verbs: a surface whose whole promise is
"dimmed, never dropped" cannot offer deletion as its retirement path.
Each note is one headline line, optional native click-to-detail, and an explicit
pointer to `ledger`, `chat`, `roster`, `board`, or an absolute HTTP(S) URL. This
is `headlines-click-to-detail`: tell the owner
what happened at a glance, then direct the action to the surface that owns it.

```console
$ helm note set reboot-recovery "Reboot recovery is complete" \
    --detail "All 4 casualties closed; exact rows are in the ledger." --goto ledger
helm note: reboot-recovery set by helm-claude-2
$ helm note list
  reboot-recovery          Reboot recovery is complete  — helm-claude-2, 0m ago
  (1/64 keys · /home/you/.helm/_global/fleet-notes.json)
```

Headlines over **90 characters warn but still write**; the owner gets the full
line, while the browser keeps it to one glanceable row. Existing long or
multiline text-only notes need no migration: their first line becomes a bounded
headline at read time and the untouched original automatically moves under
`<details>`. `helm board landed` is the first compositional producer: a genuine
new board landing replaces the stable `board-landed` note with its derived
headline, board note as detail, and `board` pointer. A duplicate board retry
repairs a missing projection but does not refresh an already-matching timestamp,
so the board remains the single source rather than a second list agents must
remember to maintain.

The predecessor was a cell on the web cockpit's multiplayer demo board, and it
failed the owner twice over. It lived in **tmpfs** (`/dev/shm`), so a reboot ate
the notes that exist to explain the reboot; and it lived behind a nav tab called
**cave**, a word borrowed from dregg where it means the *attestation node* —
which is what the cockpit's **ledger** tab shows, one tab over. Two meanings,
adjacent tabs, and helm canon already says *node, not cave*. So the notes moved
to disk and the tab was retired (2026-07-30); the relay, its adapters and
`helm multiplayer` are untouched.

**Bounded, and loud about it.** At most **64 keys** and **2000 characters**
across one note's headline + detail, both REFUSED rather than trimmed — a new
key past the cap names the oldest note to retire. A fleet-writable card on the
owner's front page with no ceiling
becomes a wall he stops reading, which is the same failure as losing it. Keys
are identifiers (`[A-Za-z0-9._-]`, e.g. `reboot-recovery`) and text carrying
control or bidi characters is refused at the seam, so nothing laundered reaches
the owner's page or a terminal. Writes go through one exclusive lock (read and
write inside it, `pk.write_json` atomically) — two agents leaving notes in the
same second is the ordinary case here. Reads are **totally fail-open**: a
missing, truncated or corrupt file reads as fewer notes, never a raise, because
one of the readers is the owner's home page. `HELM_FLEET_NOTES` overrides the
path. Web: `GET /api/notes` (read-only — there is deliberately no browser
mutation; these are the FLEET's notes to the owner).

### `helm evolve`
One observe/propose cycle over the stores: what to drain, which beliefs need
evidence, what's missing. **Proposes, never mutates** — the anti-rulesurf
gate is constitutional (see [EVOLUTION.md](EVOLUTION.md)).

### `helm gc [--dry | --apply] [--install-timer]`
The retention policy plane over the derived exhaust — the ancestor hit 32MB
of guard-ledger before learning this, so helm declares budgets on day one.

**`--install-timer` wires the hourly cadence, and it is not optional
furniture.** A retention policy that nothing SCHEDULES is a policy that does
not exist: gc knew every budget from the start and had never once run, so a
sustainability audit found 11,192 items over budget (11,191 chat cursors plus
a 31.5MB usage-history file against a 5MB budget) — on a fleet whose work
actuator was *also* gated on an undrained pile, which is the same bug wearing
different clothes. The standing rule that fell out: **nothing may gate on the
size of a pile that no automatic drain empties.** Install the timer on any
host that runs a fleet.
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

### `helm scratch [small|big|durable [--name N]] | gc [--apply] | status [--json]`

The **mount plane**. Two facts made this a verb rather than a rule: (1) a tmpfs
`nr_inodes=` cap is invisible to every bytes-based check, so the fleet hit
`No space left on device` on a `/tmp` that `df -h` called **36% used** —
1,048,562 of 1,048,576 inodes used, 14 free, while `/dev/shm` on the same box
carried 11,501,004 uncapped; (2) the files were agent scratch, including whole
`git clone` verification trees and a repo fork, i.e. repos cloned into RAM with
nobody reaping. RAM scratch is *good* (helm's own chat bus lives in `/dev/shm`
by design) — so the fix is not "use disk", it is to stop making agents guess and
to reap what dies.

* **`status`** measures every filesystem helm writes on **both axes**, bytes AND
  inodes, names any explicit `nr_inodes` cap, and prints the routing table.
  `helm doctor` carries the same rows and WARNs past 85% on *either* axis, with
  the top offending directory and both real fixes named.
* **routing** — the substrate picks the mount, never the agent: `small` (a
  probe/fixture) stays on the fast ambient tmp; `big` (a clone/fork/verification
  tree — thousands of inodes at once) goes to an **uncapped** RAM mount while it
  has headroom, else disk; `durable` always goes to **disk**, because tmpfs dies
  with the boot. Under pressure even `small` reroutes off the hot mount.
  `HELM_SCRATCH_DIR` overrides the root outright; `helm launch` exports the
  routed `TMPDIR` into every seat (opt out with `HELM_SCRATCH_TMPDIR=0`) so
  `mktemp`/`tempfile`/git temp files land correctly with nobody thinking about
  it. An operator's own `TMPDIR` always wins.
* **`gc`** reaps dead-session scratch — dry-run default. **Liveness before
  age**: a tree is reapable only when no live same-uid process references its
  session id and no live cwd sits inside it; age alone never reaps. Only a child
  named by a session id or `pid-<n>` is even a candidate, a tree holding a
  harness transcript subtree is structurally off-limits (that is training
  corpus), and probe trouble reaps nothing. The TTL escalates with real
  pressure — 7d at rest, 6h past 85%, 1h past 95% — and every pass is bounded
  (candidates, victims, stats, processes). The automatic pass rides the existing
  **Stop hook**, throttled to once an hour, a no-op at rest, and writes one
  auditable `pk.event` line that `helm doctor` surfaces. Kill: `HELM_SCRATCH_GC=0`.

```console
$ helm scratch status
  chat bus       /dev/shm     tmpfs   bytes   5% (41.8G free)  inodes   0% (11455520 free)
  helm home      /            ext4    bytes  44% (944.0G free)  inodes   9% (108661670 free)
  tmp            /tmp         tmpfs   bytes  31% (30.1G free)  inodes  86% (146975 free, nr_inodes=1048576 CAP)
  routing (the substrate picks the mount — never the agent):
    small    -> /dev/shm/helm-scratch     /tmp is under pressure (86%) — routed to /dev/shm
    big      -> /dev/shm/helm-scratch     /dev/shm: uncapped RAM with headroom (5% bytes, 0% inodes)
    durable  -> ~/.cache/helm/scratch     disk (durable never lands on volatile tmpfs)
helm scratch: worst mount 86% (warn)
```

### `helm storage-matrix [--measure] [--json]`

The owner-facing cross-box storage comparison behind the web **boxes** tab.
The bare verb is READ-ONLY: it prints the last durable snapshot and never
creates I/O, shells the inventory helper, or crosses SSH. `--measure` is the
explicit actuator. It runs one bounded, comparable Python-stdlib probe on local
durable scratch, `/dev/shm`, and every remote box the box-provider chain
(`helm/boxes.py`) reports: owner-declared `HELM_STORAGE_MATRIX_HOSTS` aliases,
SSH-config hosts the owner consented to via `HELM_BOXES_SSH_CONSENT`, and the
Linux build/standby rows of the optional external inventory helper when one is
installed (absent is silent). Unreachable rows, retired rows, and unconsented
SSH-config CANDIDATES stay in the matrix with their reason instead of
disappearing — a candidate is listed so the owner can see the machine exists,
and is never probed without the opt-in. `HELM_STORAGE_MATRIX_HOSTS` limits or
explicitly requests remote SSH aliases without baking this estate's hostnames
into portable code.

Every successful tier uses the same method: a 64 MiB same-file sequential
read/write pass, 4 KiB deterministic random read/write operations, and 24 fsync
samples. Final fsync time is included in write rates; short writes refuse rather
than being counted as I/O. Cache eviction is best-effort and recorded separately
for sequential and random reads. Exact read values remain visible when eviction
is unavailable, but the web matrix marks them `cache not cleared` and excludes
them from cross-tier bar scaling. The result is a workload-specific bounded
snapshot, not a device specification. Each probe file is removed in `finally`,
less than 192 MiB free refuses before writing, SSH is non-interactive and
bounded, and one stable lock prevents two measurements from loading the estate at
once.

The complete-or-partial schema is atomically replaced at
`~/.helm/_global/.state/storage-matrix.json` only after every target has an
outcome. A catastrophic run leaves the prior snapshot untouched; a partial run
preserves useful rows and names unavailable ones. `--json` prints the same
validated projection read by the web boxes tab, including exact measurement
age and a seven-day stale marker. `HELM_STORAGE_MATRIX` overrides the artifact
path for tests or an operator-managed state root.

```console
$ helm storage-matrix
storage matrix — measured 3h ago · 6/10 tiers
  box-a / Local disk     write   2737.6 MiB/s · read   4448.7 · random   294081/   24067 IOPS · fsync p95   0.395 ms
  box-b / Local disk        UNAVAILABLE — inventory reports this box unreachable

$ helm storage-matrix --measure --json
{ "schema": 1, "status": "partial", "rows": [ ... ] }
```

### `helm capabilities [--public] [--json]`
The **wired-substrate self-index**: what helm actually gives you, as
`verb -> what it does -> what it is wired via -> live or absent`, grouped
into core and powerpack. Its point is that a capability nobody can find is
the same as a capability nobody built, so the index is generated from what is
WIRED rather than from a hand-kept list that drifts.

The same index surfaces one line at the reasoning moment through `helm
inject`, keyed to the problem rather than the verb — a2a or converge
surfaces meld and chat-deliver; "solved this before" surfaces recall; deep
code analysis surfaces a code-analysis powerpack when it is wired. `--public` withholds
private-hold powerpacks.

### `helm fleet [--json]`
**Composition truth** — every live agent process mapped to its seat, session
id, daemon, stamps and credential home, all LIVE-PROBED at call time.

Answer fleet questions by running this, never from memory or from a roster
you read earlier. Process state is exactly the class of fact that goes stale
between the moment you learn it and the moment you act on it, and a confident
wrong answer about who is alive is worse than no answer — it sends work to a
seat that cannot take it.

### `helm watchdog [--json] [--quiet]`
Scans proxy seat error logs for the **context-window wedge signature** and
raises one loud a2a alert per wedge, so a human can clear and relaunch the
stuck seat. A wedged seat is silent by construction — it cannot report its
own wedge — which is why this is a scan rather than something the seat emits.

Fires ONCE per wedge and re-fires only if the count climbs; a recovered seat
re-arms. That is deliberate: an alert that repeats on a known condition
trains its readers to filter it, and the next real one goes unread with it.
Run it periodically (a Monitor or cron) as the live backstop. `--quiet`
detects without posting, `--json` is the machine form.

### `helm web [--port N] [--open]`
The same truth, warm, in a browser: eight views (knowledge home, quota,
storage, sessions, configs, chat, roster, ledger) served self-contained on
`127.0.0.1:7433`. `--open`
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
provider auth). Adopted-by-symlink homes are host-local:
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
who observed what, when — host-a active, host-b dormant.

```console
$ helm ship
helm ship --dry (nothing touched):
  home:   ~/.helm
  git:    not initialized (would `git init -b main`)
  remote: none configured — would COMMIT ONLY (add --remote <private-url>)
  would ship: 82 authored files, 0.9MB
  derived (never ships): .state/ x1 adopted-symlink x1 registry.json x22 seats/ x1
  adopted symlink homes (host-local): example-project
  secret scan: clean (11 patterns over 82 files)
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
`seat add|up|down|launch|spawn|where|resume|resume-turn|smoke|autocompact|list|status|doctor`) is
wired into the dispatcher.

**`helm seat spawn <seat>` — the harness-agnostic SELF-ONBOARDING spawn.**
A hand-spawned seat is a bare idle pane: no beacon, no work, not
addressable. `spawn` closes that gap with one verb and three paths behind
`harness.detect()`: **headless** (no orca/herdr — the standalone default:
launch.sh runs detached under its own setsid session, io to `spawn.log`,
and the onboarding rides as the script's positional arg, i.e. claude's
first prompt at boot); **orca** (adapter `terminal create` + `terminal
send --enter` of the onboarding); **herdr** (`agent start` + `pane run`).
Common to all: dup-name REAP first (a prior bare same-name seat is
killed/closed), mint hygiene (child-stamp stripped, skills linked,
`--dangerously` canonical), and a roster register (`spawn.json` +
chat-roster mirror) so any agent can `helm seat where <seat>` — harness,
handle/pid, worktree, room, liveness. The onboarding directs the seat to
arm its `helm chat wait --seat <seat> --follow` beacon FIRST, read its
home room, announce, and take `@<seat>` work. `--print` shows the exact
per-harness calls without spawning; `--room R` homes the seat (else the
room is recovered from launch.sh); `--cwd DIR` sets the worktree.

**Per-seat home worktrees (the isolation default).** A spawn with no `--cwd`
lands the seat in its OWN checkout — `<repo>-wt/seats/<seat>` on branch
`seat/<seat>`, created-or-reused through the metaharness seam
(`harness.ensure_home_worktree`, implemented for headless/none, herdr, and
orca). The old default was "wherever the operator stood", i.e. the SHARED main
checkout, so every seat edited, built and stashed one tree: dirty main blocks
every land, seats collide, and `git stash` on a shared checkout is not
swarm-safe. A seat home is deliberately NOT `git worktree lock`ed (a lane lock
is a task lease; a seat home is long-lived) and it is not a lane room — it
folds to the same project room via `--git-common-dir`, so isolation costs no
room scatter. `--cwd DIR` still wins, and a provisioning failure degrades to
the old shared-checkout behaviour with a loud warning rather than aborting the
spawn. `--print` shows the resolved path without creating it.

Two family modes are live: `codex`
(mode "proxy" — a subscription OAuth cred translated read-only into the
proxy) and `kimi` (mode "proxy-key" — an API-key provider behind the same
proxy via its openai-compatibility block; the key comes from `KIMI_API_KEY`
or `--key-from <env-file>` at add time and is baked into the seat's 0600
config, never printed).

A seat is a **first-class fleet chat member and dregg identity**. `seat launch`
exports `HELM_CHAT_NAME=<family>` (keeping the existing
`env -u ANTHROPIC_API_KEY` scrub), so the seat's SessionStart join registers it
in the roster under its family name and `@codex` / `@kimi` fleet posts deliver
to it between tool calls. The same line exports the dregg-native signer
(`HELM_CELL_BIN=~/.local/bin/dregg-client-sign`) plus
`HELM_CELL_PROFILE=<family>` and `DREGG_PROFILE=<family>`: the first signed post
creates the named SDK profile and faucet-materializes its own cell, so agent
activity advances the cave as that seat — never through an inherited owner
profile. The delivery lane (deliver + subagent-end + join) lives in the seat's `claude/`
config dir; `helm hooks install` wires it there and `helm hooks status` reports
seat coverage (see `helm hooks`). A seat already running an OLD session must be
relaunched with a fresh `helm seat launch` to pick up the identity, signer, and
delivery hooks — a live session's environment/settings are fixed at start.

`seat add` and `seat launch` also derive the current project room by default;
`--room` and an inherited explicit `HELM_CHAT_ROOM` win. Generated `launch.sh`
presets preserve `HELM_CHAT_ROOM_SOURCE=derived` across `helm seat resume`, so a
refresh cannot turn a project default into an explicit room and undo a later
operator rehome/clear. Resume also preserves the seat's `--multi` shape and
refreshes `spawn.json` with the new pane handle.

**`helm seat autocompact` — pre-empt the proxy-model 100% context hang.** One
bounded pass reads Claude Code's own latest main-chain usage record from every
proxy seat transcript, divides by that family's declared context window, and
at 80% injects `/compact` into the identity-proven pane. The actuator uses the
seat's authoritative `spawn.json` handle and verifies it against the matching
adapter's live inventory. The same resolver owns duplicate-seat reap and
`seat where`; copied launch text and mutable pane titles are never identity.
Missing/headless/stale-register panes produce a loud room alert instead of a
guessed injection. Before sending, the actuator proves the current composer. An
exact pretyped `/compact` outside an open turn authorizes Enter only (never a
duplicate retype); `RUNNING` still refuses because submitted-message history uses
the same prompt glyph. Arguments and every other non-empty draft remain
untouched. An empty composer in `CONTEXT_FULL` may receive the normal `/compact`
injection because compaction is
that state's remedy. Adapter success alone is not actuation: bounded pane rereads
must prove the command drained or the pane transitioned before a locked success
latch is written. A static/unreadable composer is reported as failed or unproven
and retried next cadence. Verified actuation permits one fire per context episode
and re-arms only after context drops or the authoritative registered session
changes; elapsed time and a newer transcript alone never release the latch.

An already-full seat takes a separate, fail-closed rung. Recovery requires
either repeated line-leading context-overflow 400s or a newly caused post-send
400 whose exact fingerprint remains above its pre-send count on the next cadence;
proxywatch `HUNG`, silence, historical errors, and generic HTTP 400s cannot act.
Helm asks `cv prune --window … --thinking --json` to mint a bounded copy with
revive enabled, independently verifies that copy's recorded context fits the
budget, and resumes that exact id in the prior registered worktree. Success is
published only after the replacement pane is live and the seat's claim-session
bindings have moved without changing their lease nonce, fence, or expiry.
`/clear` is the last resort only when pruning cannot create a copy. Once a copy
exists, validation or relaunch failure preserves both sessions, latches the
episode for manual recovery, and never falls through to `/clear`.

`--dry-run` reports decisions without injecting or latching; `--seat S` limits
the pass; `--threshold N`, `--json`, and `--quiet` shape it. `--install-timer
[--interval SEC] --apply` installs the external systemd user cadence (60s by
default). `seat launch`, `seat spawn`, and `seat resume` refresh and enable that
timer automatically; a system without systemd gets a warning and can schedule
the same bounded command another way.

Claude-model seats are scanned too. The old skip ("native seats autocompact
fine") was right only about CC's *native* autocompaction, which continues the
turn it interrupts; deliberate self-compaction at 80% is what the owner wants
for every helm agent, and it is safe only behind the resume leg below.
`HELM_AUTOCOMPACT_CLAUDE=0` restores the skip for a fleet that has not
installed that hook.

**`helm seat resume-turn --hook-json` — the RESUME leg of a compaction.**
A compaction ends a turn and nothing starts the next one, so before this
existed a seat compacted and then slept until a human typed into its pane
(bug class `compaction-has-no-resume-leg`: `autocompact` fired correctly, the
PreCompact handoff hook wrote correctly, and jointly they parked the seat).
Wired as a `SessionStart` hook on every home and every seat, it acts **only**
when the payload's `source` is `compact`.

The hook itself never touches the pane — a `SessionStart` hook runs *before*
the session resumes, so an inline injection lands ahead of the composer.
It does file reads only, then forks a **detached child** that waits out the
composer settle (`HELM_RESUME_TURN_SETTLE_S`, default 3s — measured against a
real Claude Code TUI on a pty) and injects through the metaharness seam,
re-proving pane identity inside the seat lifecycle lock at send time. The
directive is the seat's **own** freshest handoff entry (`NEXT:`), so it resumes
on real work rather than a generic "continue"; with no handoff written for that
compaction it injects a re-ground line instead. A pane that cannot be
authoritatively identified is never guessed at: it gets a loud chat alert
naming the seat.

The loop guard makes compact→resume→compact impossible by construction: a
second `SessionStart(compact)` within `HELM_RESUME_TURN_DEBOUNCE_S` is the same
episode re-firing (silent no-op); a genuinely new compaction within
`HELM_RESUME_TURN_SPIRAL_S` of a resume stops injecting and alerts; and
`HELM_RESUME_TURN_MAX` resumes per rolling `HELM_RESUME_TURN_WINDOW_S` caps the
slow case. `helm seat resume-turn --status` prints what the leg last did per
seat; `HELM_RESUME_TURN=0` disarms it.

### `helm router [up|run|down|status|line|probes]` + `helm seat launch|smoke --multi`

**Mixed models in ONE claude-code process** (proven 2026-07-21, raw conductor
logs): a Task subagent's `.claude/agents/*.md` frontmatter `model:` string
goes to the wire per-request, and the proxy routes each request by model
name. The one law: **never set `CLAUDE_CODE_SUBAGENT_MODEL` for a mixed
fleet** — it blunt-pins every subagent over the frontmatter. Two surfaces:

- **`helm seat launch <family> --multi`** — the proxied-parent shape (e.g.
  codex parent + mixed codex subagents): the launch line drops the
  `CLAUDE_CODE_SUBAGENT_MODEL` pin and probe agents (per-model frontmatter)
  are minted into the seat's config dir. `helm seat smoke <family> --multi`
  adds the mixed fan-out leg: two subagents pinned to different models
  through an ephemeral router, PASS only when the router's **conductor log**
  shows both models on the wire (never the subagents' word).
- **`helm router`** — the claude-PARENT shape. `ANTHROPIC_BASE_URL` is
  process-global, so a Claude parent with non-claude subagents needs one
  local endpoint speaking both worlds: the router forwards `claude-*`
  requests **verbatim** to api.anthropic.com (claude-code's own OAuth
  Authorization header, body, and headers untouched — no substitution, no
  re-auth, and NEVER an Anthropic API key), and conducts non-claude models
  to their seat's CLIProxyAPI with the seat token. `helm router line` prints
  the parent launch line (`env -u ANTHROPIC_API_KEY -u ANTHROPIC_AUTH_TOKEN
  -u CLAUDE_CODE_SUBAGENT_MODEL ANTHROPIC_BASE_URL=http://127.0.0.1:8320
  claude`); `helm router probes` mints the example per-model agents into a
  project's `.claude/agents/`. Every request appends one paste-safe JSON
  line (model/route/status — never header values) to
  `~/.helm/_global/router/router.log`: the conductor log.
