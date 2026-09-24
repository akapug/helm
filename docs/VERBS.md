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
The real project list, newest activity first: name, light, last activity,
session count, harnesses, path. The light column is the project's resolved
light: an authored colour when somebody has set one (marked `*`), otherwise the
status the scan saw (`active`, `dormant`, `external`). `--all` includes shelf repos (on disk, no
observed agent activity) and retired projects. Explicitly forgotten registrations
are excluded from membership and listed separately by `helm projects forgotten`.

### `helm projects state [<name> (green|yellow|orange|red|clear) [--reason TEXT] [--apply]]`

Every project has ONE light. The scan derives a value for it (`active`,
`dormant`, `external`) from what it saw; a person can author a colour, and the
authored colour **supersedes** the derived one wherever the light is read — the
`helm projects` column and the dot that leads each project row on the web Work page.

| colour | meaning |
|---|---|
| `green` | open more lanes for work that is already built up; never speculative (what `active` always meant, said on purpose) |
| `yellow` | normal work, no extra lanes |
| `orange` | work, constrained: critical path only, one delegate at a time (what to set when a credential is scarce) |

Under `orange`, both doors also print a **measured burst exemption** when the
asking seat is homed on a credential whose own window will expire headroom
unspent (`helm burn burst`). It lifts the one-delegate clause for that seat on
that credential until that window resets — never the light's own verdict.
| `red` | start nothing new here; finish or park what is running |

**A project's light outranks a credential flag.** A family's burn flag says how
much capacity there is; the project's light says whether work may start here at
all. A green credential is capacity, never permission.

The light binds at the two doors where work starts, and both ask one decision
(`registry.admits`), so they cannot grade a colour differently:

| door | green | yellow | orange | red |
|---|---|---|---|---|
| `helm work claim <lane>` (a new lane) | admits | admits, says the reason | admits, says the reason | **refuses** |
| `helm work claim <lane> --lease L` (a renewal) | admits | admits, says the reason | admits, says the reason | admits, says the reason |
| `helm dispatch send/add --kind build --new-work` | admits | admits, says the reason | admits, says the reason | **refuses** |
| a build continuing a chain (`--supersedes`), or any review | admits | admits, says the reason | admits, says the reason | admits, says the reason |

**The four words mean what the credential burn flags already mean** (`helm burn`): the
colour is the INTENSITY, and only `red` refuses, and only NEW work, because its own
sentence is "finish or park what is running". `orange` is what to set when a credential
is scarce: work continues, on the critical path, one delegate at a time. What KIND of
work a project wants (maintenance only, a single lane) belongs in the reason, which
every door prints.

A renewal is never refused because a lease is a lock on a room, not work:
refusing one would strand a room mid-edit without stopping anything.

Only an **authored** light binds. The scan's values never refuse — `dormant`
means nothing happened lately, which is no reason to stop the first thing that
does. A path no project claims, or a registry that cannot be read, admits and
says so: a corrupt file is not the owner saying no.

There is no bypass flag, and `helm dispatch --force` does not open a stopped
project (it overrides the *recipient* checks, which are about a seat). The way
past a light is to change the light, which records who did it and why on the
card the owner reads.

With no arguments the verb lists every authored light with who set it, how long
ago and why. Setting a colour is **dry-run by default**; `--apply` writes it. A
colour requires `--reason` (what decided it, and what would change it back);
`clear` takes none and hands the light back to the scan — it does not set green.
The setter is recorded from `HELM_CHAT_NAME`.

The owner's door is the project row: tap a project's light on the web Work page
and its **light** section shows the current value, who set it and why, with a
button per colour and a reason box. It posts to `/api/projects/state`, which
calls the same writer as this verb, so the page refuses exactly what the verb
refuses.

The light is an authored field, stored in `registry-authored.json` and never
regenerated by a sync. Authorship is resolved from that file alone: a `state`
block appearing inline in the scan-rebuilt `registry.json` is not a light, is
never promoted into the authored file, and leaves the scan's value showing.

### `helm projects residency [<name> (lan-only|may-leave-lan|clear) [--reason TEXT] [--apply]]`

Whether a project's turn text may leave the operator's LAN for an outside
scorer. It is an AUTHORED registry field, never a list of names in code, and
it fails CLOSED: only an authored `may-leave-lan` opens the door. No field, an
unknown project, a turn with no project, a malformed value, a copy that sits
only in the scan's projection, and an unreadable registry all read `lan-only`.
Like `gate` and `state` it is never migrated from the projection, so no
discovery pass can mint it. `clear` removes the field, which is `lan-only`.
With no arguments it lists every project's residency (`*` = authored). A value
needs `--reason`; without `--apply` it is a dry run. `helm relevance` reads it
in the worker, immediately before the only outside call.

Two operator notes:

- **A helm older than this field clears it.** Its `helm sync` (and anything
  else that saves the registry: `helm lineage add`, `helm lineage external`) rebuilt
  each entry from the fields it knew. Every registry writer now carries the
  authored keys it does not own, but a seat or worktree still running an
  older helm drops the field, and the project reads `lan-only (no field)`
  again. After any such run, check `helm projects residency` and re-author.
- **Register every client checkout as its own project.** A turn's project is
  the registered path that contains its working directory, deepest first, so
  an unregistered client checkout nested inside a `may-leave-lan` project's
  tree inherits that project's residency. A registered checkout wins by the
  longer prefix and reads its own field, which is `lan-only` until written.

### `helm projects forget <name> [--apply] | restore <name> [--apply] | forgotten`

`forget` archives a registration whose absolute path is proven gone. An existing
path, permission failure, symlink loop, or malformed registry refuses; an
unreadable entry is not automatically garbage. Both `forget` and `restore` are
**dry-run by default**; `--apply` changes registry membership, never repository
files, knowledge homes, or compose activation artifacts.

The full record is retained under `forgotten_projects` in the existing authored
registry, together with a note of which EXECUTABLE authored fields (the
never-migrated ones, `gate`) that record was carrying from the authored layer.
The name and path remain reserved: ordinary sync, shelf scanning, and
external-anchor replay cannot resurrect the forgotten registration. `forgotten`
lists these archives; `restore` reinstates one, refusing a conflicting name/path.
Restoring a still-missing path is allowed and may restore an UNKNOWN authority
answer. Discovery never guesses a migration from a basename or remote URL.

`restore` republishes an executable field as a DECLARATION only where the archive
itself records that provenance. An archive written before that law — by a helm
that did not treat `gate` as an authored field — carries the merged record's block
with no such note, so its `gate` is restored into the PROJECTION rather than
promoted into authority, which is where the helm that wrote the archive left it.
An authored declaration already standing in the file is never overwritten by
archive bytes.

### `helm projects repoint <name> --from PATH (--to PATH | --undo) [--apply]`

An explicit operator decision about **location**, not proof of repository
identity. Both paths must be normalized absolute paths. `--from` must match
current membership; forward repoint requires that source to be gone and the
named target to resolve to an existing directory. Recurring non-Git directories
are valid registry projects. Missing/file targets, uncertain path resolution,
conflicting registrations, or independently edited destination metadata refuse.
Dry-run is the default; `--apply` publishes the change. No directories are moved.

`project_bindings` in the existing authored registry holds the current binding,
per-location registry snapshots needed for reversal, and immutable departure
records. Those records are also what lets a binding written by a helm older than
the current path-stamp separator still apply: the historical `name@<stamp>` key is
decoded from the binding's own location history, never from the hash alone, and
the next `save` re-spells it.

**One record per (project, location).** The authored layer holds exactly one
record for a project at a location, and every reader and writer — `repoint`,
`forget`, `restore`, `save`, the gate's command resolution and its receipt
admission — resolves it through one door (`registry.authority_record`). A key is
a place to look for that record, never an identity of its own: the plain name,
the computed `name/<stamp>`, and the historical `name@<stamp>` all map to the
record only where the entry filed under them RECORDS that location (the
historical spelling only where the binding or archive history proves it). So a
full name-shaped key with no such record is a project NAME, left whole, and a
lawful name spelled like another project's stamp at some other location can
neither veto that project's repoint nor be captured as its departed record. Two
keys holding the SAME record for one (project, location) — the shape two helm
versions write between them, a stamping-era `name@<stamp>` and a later repoint's
computed `name/<stamp>` for the same location, same command, same provenance —
are ONE record: they resolve, and the next `save` folds them into one key. Two
keys that DISAGREE about that record refuse, naming both. A repoint still refuses
over the project's OWN unaccountable record — an entry spelled as its historical
stamp for the source or target and recorded at that very location that nothing
in the file accounts for — because the move would otherwise mint the provenance
that captures it; re-key the entry and repeat.

The name, authored notes/edges, knowledge home, and activation files
are preserved. Preserving activation files does **not** validate their evidence
for the new location. Historical activity stays attributed to its original
location; it is not copied into the new location's counts. Sync discards
observations of suppressed historical locations instead of resetting the binding
or relabeling that activity. Projection rebuilds and external anchors follow the
authored binding. Stale/missing-generation saves refuse rather than claiming an
edit succeeded against another binding generation.

`--undo` reverses the **latest transition**, using the expected current `--from`
path. It retains later authored edits and archives the departed location's
observations. The restored location may be missing, reinstating UNKNOWN.
Reversing an undo is a redo: this is a one-step toggle, not an undo stack.
Repoint and undo refuse forgotten entries; explicitly restore membership first.
Forget/restore retain binding history. Collision checks are point-in-time checks,
not a durable filesystem uniqueness guarantee; strict composition UNKNOWN
semantics, including earlier unresolved registrations, remain unchanged.

The authored write is the sole binding publication: interruption leaves the old
or new complete binding, never a projection published ahead of its authority.
`ship pull` serializes with registry writers and validates imported bindings
before resync. Invalid imports remain recoverable in Git and read UNKNOWN;
validation refusal is not a claim that the import was rolled back. Corrupt
local authored bytes are backed up, but saves refuse to replace them with empty
authority.

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
helm store get <id>                        one entry, full record; <id> may be
                                           typed (`lexicon:seam`) or carry the
                                           tag its injected line shows
                                           (`premise:`, `move:`, `term:`, `ref:`)
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

    an id is ONE kebab token: prior, premise, heuristic and reference ids
    with whitespace are REFUSED at add (and at every other mint door), and
    the refusal names the kebab id to use; a lexicon term keeps its spaces,
    because a term is the phrase a prompt carries.

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
helm store keywords <id> [--add CSV] [--remove CSV] [--set CSV] [--type T]
                                           retrieval keys; no flags = print them
helm store gates <id> [--add CSV] [--remove CSV] [--set CSV] [--type T]
                                           the rule's PRECONDITIONS, as entry ids.
                                           A rule arrives with its gate: when the
                                           rule is a DETERMINISTIC delivery (the
                                           pinned lane, a notice route) each
                                           gate's gloss rides in the SAME whisper,
                                           marked "GATE of <rule>"; a gate the
                                           budget cannot carry is DROPPED LOUDLY
                                           with a marker naming it, never
                                           silently. When the rule fired on
                                           keywords, its gates are NAMED in the
                                           lane footer instead ("; their gates:
                                           prior:<id>"), by ids `get` reads. The
                                           two share probe vocabulary, so the
                                           rule's words pull the gate as strongly
                                           as the rule. Every id must resolve to
                                           a live entry; no flags = print them.
helm store gloss <id> [--set TEXT...] [--clear] [--type T]
                                           the SHORT line that FIRES in place of
                                           a long statement (the gloss law under
                                           `add` above). --set takes the whole
                                           trailing argv as the text and writes
                                           through the writers' own _commit, so
                                           a gloss whose rendered line exceeds
                                           LINE_CAP is REFUSED with the exact
                                           overage and the file is untouched;
                                           --clear empties it; no flags prints
                                           it. The DERIVED law stands: confirm
                                           --edit and a re-mint over a retired
                                           id still scrub a stale gloss.
helm store rescope <id> <project|fleet|-> [--type T]
                                           RECORD THE PROJECT THIS ENTRY IS
                                           ABOUT (task/2435). A registry
                                           project name scopes it to that
                                           project, `fleet` marks owner policy
                                           that applies everywhere, and `-`
                                           clears the record so the entry
                                           falls back to its root's default —
                                           the project root it sits under, else
                                           fleet unless a helm-global statement
                                           names a helm lane, train, task or
                                           row (a seat name scopes nothing).
                                           A registry name is recorded exactly
                                           as given, internal spaces included,
                                           and an invalid name is refused
                                           without a write. A seat is injected
                                           fleet entries plus its own
                                           project's, so this is the ONLY door
                                           that makes an adopted-root entry
                                           project-scoped.
helm store evidence <ts> <id> <delta> <reason...>  move a belief (logged + clamped)
helm store supersede <ts> <old-id> <new-id> [reason]  TOMBSTONE old (file kept)
helm store retire <ts> <id> [why...]               retire (file kept as the record)
helm store demote <id> [--undo] <reason...>        flip always->jit with a receipt
                                                   (--undo = provenanced restore)
helm store events [--limit N]                      the mutation-receipt trail
helm store counts                                  per-root type inventory
helm store doctor [--fix]                          retrieval-field audit; --fix repairs
                                                   the mechanical classes. An id with
                                                   whitespace is listed with the kebab
                                                   id --fix re-keys it to (a new file
                                                   under that id, a delete_eligible
                                                   tombstone at the old id with
                                                   replaced_by, every supersede link and
                                                   gate naming it re-pointed), or HELD
                                                   with the reason: attested (move it
                                                   with `helm premise --supersede`),
                                                   pinned, no mechanical reading, or the
                                                   id already taken. A phrase id whose
                                                   kebab form is its own key is respelled
                                                   in place. More than 6 authored probes
                                                   is a warning (WIDE KEYS). Every repair
                                                   is validated and staged before any
                                                   lands, so one refused row withholds
                                                   all of them (rc 1, nothing written);
                                                   the rename leg is the filesystem's
                                                   boundary — a rename failure after
                                                   the first lands a prefix and the
                                                   message names what landed, what did
                                                   not, and any stage residue; an
                                                   attested row is rewritten only when
                                                   its seal verifies (unreadable or
                                                   malformed proof = unverified, never
                                                   absent), else left byte-identical
                                                   and named
```

The stem-common floor (the stop-list behind `add`'s auto-stems and the
doctor's `stem_drift` class) counts DISTINCT VERIFIED STATEMENTS: a seal that
verifies is the provenance, an unsealed row casts no vote, four verified
statements sharing a stem make it common by design because verified rows are
the store's authority, and a store with no verifying carrier has an unmeasured
floor (the doctor says `stem floor unmeasured: 0 attested carriers`; nothing is
demoted).

Commonness has a PROMPT side too (task/2978). `helm inject` records each turn's
substance into the prompt census (`_global/.state/prompt-census.json`: word
forms and turn counts only, never text; windowed and bounded). A stem is never
minted from a numeric or <= 2-character word, nor from a word the census sees
in >= 40% of recent turns. The doctor's `stem_unfit` class names GENERATED
stems (the tail the mint guard appended after the author's string) that fail
those legs, and `--fix` removes exactly those, with a receipt; authored cells
are never edited. Below 400 recorded turns the census is unmeasured: only the
shape leg runs, and the doctor says `prompt census unmeasured`.

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
An entry whose ONLY match is one single-word probe is held when the prompt
census measures that word in more than 4% of recent turns (task/2978); a
phrase match, or two or more probes, is never held, and an unmeasured census
holds nothing. `--explain` lists what was held and why.

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

### `helm inject [--project P] [--json] [--explain] [--hook-json] [--compare-report] [--moment-report [--days N]] [--replay TURNS]`
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

A turn that a harness notice began — a chat wake delivered as a Monitor
event, a finished agent or background command, all one `<task-notification>`
envelope OPENING the prompt — is matched on its **substance**
(`helm/promptshape.py`): anything queued behind the leading envelopes is kept
verbatim, and within them only the agent's `<result>` body and the event's
text are kept, with each chat wake line cut to `author: text`. The envelope's fixed text (ids, output path, status, the
summary naming the Monitor or command, the agent note, usage, the
PushNotification sentence, Monitor-expiry and `[helm chat]` notice lines) is
set aside before the JIT keyword match and the prompt reflex regexes run, so
an entry keyed on a word the harness writes into every envelope no longer
fires on every wake. A typed prompt is matched byte-for-byte as before, even
when it quotes an envelope. The notice's ledger row carries `"notice": true`.
The harness's fixed lines INSIDE the kept bodies are set aside too
(task/2978): a `<result>` line "This agent's report was delivered to you ..."
or "This agent has not reported yet: ...", and a Monitor's "[N events
suppressed — ...]" line. A leading subagent hand-back (`<agent-message>`) is
reduced to its report: the "[Subagent hand-back] ... The report follows:"
frame is dropped and the report dedented. A turn whose substance is empty
matches no keyword. A fixed notice KIND can still route: a Monitor-expiry
event delivers every entry whose keywords declare `notice:monitor-expired`
(`helm store keywords <id> --add notice:monitor-expired`), scope-fenced and
cooled like any delivery. A `notice:` cell is a route, never a word probe.

A lane whose lines are shorter than their entries — a cut line (ending in
`…`), a gloss, a first sentence — ends in ONE footer, `[helm] full text: helm
store get <type>:<id>`, where each cut line once carried its own pointer; a
lane of whole lines has none. `<type>` may be the line's own tag (`store get`
reads `premise:`, `move:`, `term:`, `ref:`). A cut line keeps the rule text it
carried beside that pointer, so the footer only removes bytes. Gate riders
ride only on DETERMINISTIC lines — the pinned lane and notice routes; a
keyword line's gates are named in the footer (`; their gates: prior:<id>`) and
are not delivered, so they record no cooldown. Every id the lane prints is a
spelling `helm store get` reads back, including an id whose 60-character slug
ends in a dash.

With a session, the habituation guard extends to the JIT lane: a fired entry
**stays sent** for the life of that session (state per session under
`_global/.state/inject-seen/`, stale files self-pruning) unless its score
jumps to ~2x its score at last fire. There is no turn window — a seat forgets
at **compaction**, not on a timer, and the SessionStart legs clear this state
at exactly that boundary, so everything re-fires to a seat that actually lost
it. (A 15-turn window used to re-send every entry about every 16th turn
forever: 94.2% of measured JIT re-deliveries were that window expiring rather
than a real score escape.) Identical pinned content is also sent once per
context, with a content fingerprint so changed guidance re-fires immediately.
The codex-only delegation and claim-start tail tracks each line's content: a
line fires when budget is available, then returns its budget to any unseen
sibling and stays silent until a new session or context loss such as compaction
or `/clear`. Reflexes remain exempt because
they describe a signal live on this turn. Suppression happens pre-cap so freed
slots reach the next candidates; the ledger distinguishes cooled JIT ids
(`suppressed`), pinned-content suppression (`suppressed_pinned`), and codex
nudge suppression (`suppressed_nudges`). `--explain` reports each verdict.
The boundary itself is on the row too: `turn` restarts at 1, and `epoch` says
WHY it restarted — the SessionStart `source` that reset it, and a `vouch` of
`vouched` / `unvouched` / `unknown` for whether a PreCompact record was there
to judge. Three values, because "no record was found" and "the record could
not be read" are different facts and only the first is evidence. `epoch` rides
the reset row alone and is absent everywhere else, including on every row
written before it existed — its absence says the row cannot answer, never that
nothing vouched.
Plain stdin (no session) never cools. inject is also the **coinage 3-strikes** recorder: quoted 1-3 word
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

**The budget keys on the ARRIVAL** (trigger design lanes 1-2, `helm/moments.py`):
the hook cannot see a turn's phase, so it classifies who began the turn — typed,
a peer's wake, an agent message or hand-back, the seat's own background result,
a machine broadcast (every row from a watcher label), a Monitor expiry — and
spends that kind's cap. An empty, duplicate or replayed notice (a row older than
the seat's last turn or than an hour) takes a **zero-byte fast path** before the
store loads and still writes its 0-byte ledger row. The pinned contract waits for
a turn with a reader; WHO rides the first typed turn of a context; correction,
owner-feedback and coinage fire on typed turns only; a turn over its cap shortens
every long-tail line and never drops one by rank. A store entry declares the
moment it answers with a `route:<id>` keyword cell (the id is a row of
`moments.ROUTES`; the older `notice:<kind>` reads as `route:arrival.<kind>` and
`helm store doctor` prints its re-key). Every ledger row carries `arrival`,
`timed_out`, `fast_path` and `wall_ms`; a turn past the 8 s soft deadline leaves
a `timed_out` row instead of dying at the wrapper's 10 s kill.
`--moment-report` renders, per route, EXPECTED (must-hit arms replayed now plus
raw-text signatures seen in the window), DETECTED and DELIVERED, RED when a
detector misses what it should catch, plus p95 hook wall time and the typed-turn
timeout rate against their bars (rc 1 on any RED). `--replay TURNS.jsonl
[--recorded] [--gold G --labels L]` runs the tree's own construction over a
recorded turn file with no state written (`--recorded` reproduces the recorded
numbers first).

```console
$ echo "how should we drain the memory backlog?" | helm inject --project myproject
TERM drain: routing raw memory intake to typed homes ...

$ helm inject --compare-report
comparison backend off (set HELM_CF_ENDPOINT). No comparison backend is configured ...
```

### `helm relevance [status | report [--hours N] [--json] | show --session S | warm [--project P] | remeasure ... | serve ... | score-turn]`

The long-tail RE-RANK: one probability per (turn, candidate store line), so the
keyword lane's candidates can be kept or dropped by relevance instead of by
keyword order. Its settings are `<helm home>/_global/relevance.json`
([example](relevance-config.example.json)); an absent file is mode `off`.

- **One scorer per turn.** `helm inject` renders the top candidates (8 by
  default; routed lines are never scored) exactly as the lane would inject
  them and starts ONE detached worker (`helm relevance score-turn`, the job on
  stdin from a file unlinked before the hook returns).
- **Two scorers, chosen by residency.** The outside evaluator scores a turn
  only when its project's authored residency is `may-leave-lan`
  (`helm projects residency`) and the text matches no secret or address
  pattern. The pattern for a secret includes any run of 32 or more hex
  digits, so a turn quoting a full commit sha is never sent out: most helm
  turns are scored locally for that reason, which errs closed and is
  accepted. The local head behind the scorer service scores every other turn
  and is the evaluator's fallback. When the evaluator scores a turn, the local
  head scores it too and both land in the ledger (the teacher's labels).
- **The wait is bounded, the score is not.** In `live` mode the hook waits at
  most `wait_s` (1 s). A score that arrived re-ranks the lane; one that did not
  leaves keyword order and the lane's first line is the one word `[keywords]`.
  `wait_s` above 3 s is ignored (the hook's whole budget is 10 s). The worker
  still finishes and writes the per-turn cache
  (`_global/.state/relevance/<session>.json`, swept after 7 days untouched),
  which a later read takes without calling a model:
  `relevance.turn_scores(session)`. Today that read is `helm relevance show`;
  the per-tool-call surfaces that will make it are not built yet.
- **The turn is read in the form its labels had.** The notice envelope,
  hand-back frame, harness banners and helm chat headers are removed, and a
  turn past 3,000 characters keeps its first 2,000 and last 900
  (`relevance.label_form`); the evaluator's threshold and the local head were
  both fixed on turns read this way. The local head then cuts the turn to its
  own 1,000 characters, and the service cuts every row to 512 (no labelled
  row was longer than 408).
- **Shadow first.** `shadow` scores every turn and changes no delivered byte;
  the ledger (`_global/.state/relevance-ledger.jsonl`) records what the
  classifier would keep beside what the keyword lane delivered, and whether
  the score would have arrived inside the bound.
- **Counted per tier.** Every outcome is `in_time`, `late` or `failed`
  (`_global/.state/relevance-counters.json`). `report` prints the fallback rate
  per tier, the scoring p50/p95, the classifier's keeps beside the keyword
  deliveries, and the local head's agreement with the evaluator.
- **Local by choice is gated on the claim.** A `may-leave-lan` project listed in
  `local_by_choice` is scored locally only while a `remeasure` receipt has
  PASSED for the head the service serves. `remeasure --items F --labels F
  [--exclude F[,F...]] [--apply]` re-scores the frozen head on a stable held-out
  split of the evaluator's labels through the live service and writes the
  receipt; the criteria (`--min-turns`, `--min-auc` on the 90% lower bound,
  `--min-recall` of the evaluator's keeps) ride in the receipt. A
  `--limit-turns` sample never passes.
- **The service.** `serve --head HEAD.json --port N [--host ADDR]
  (--embed-url URL | --llama-server BIN --model GGUF [--threads N]
  [--parallel N]) [--cache-dir DIR]` runs the scorer: it embeds the turn once,
  embeds each row once per content hash (cached across restarts), and applies
  the head as one dot product. It carries no authentication: bind it to the
  LAN. Its URL is the `relevance` key of `endpoints.json`. `warm` embeds every
  current store line ahead of use. A user unit ships as
  `scripts/helm-relevance.service`.

```console
$ helm relevance
helm relevance: mode shadow
  scorer     http://<host>:<port> — ok, head <version>, threshold 0.26, 1842 cached rows
  evaluator  typesafe-ai/jev, threshold 0.32, credential present
$ helm relevance report --hours 24
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
receipt; **dry-run by default**; on `--apply` MEMORY.md is locked, re-read fresh,
and atomically rewritten. Writers using the same projection lock serialize and
changes visible at the protected re-read survive; an external unlocked writer
racing the final replacement is outside that guarantee. The self-firing
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
ancestor-era entries the store still holds by their successor-era twins (an
old-era premise superseded by its helm successor).

**Never on weak signal.** A proposal needs BOTH a directed lineage edge AND a
match between an ancestor-era entry and a successor-era one: *same-slug* (equal
base after stripping the era token — `old-rollover` / `helm-rollover` →
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
  [same-slug] oldtool-rollover-policy -> helm-rollover-policy   (helm supersedes oldtool)
      old: "on rollover, re-home the oldtool session under the freshest account"
      new: "on rollover, helm re-homes the session under the freshest account"
      ->  helm store supersede <ts> oldtool-rollover-policy helm-rollover-policy lineage: helm supersedes oldtool + same base slug 'rollover-policy'
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
`stalled` (stalled-turns ≥ 6 — TURNS with no forward op, counted by
`helm record`; a turn a notice began, or one with no tool call, never counts),
`thrash` (loop-streak ≥ 3), `drift`
(dirty-streak ≥ 8), `stuck` (stuck-streak ≥ 3, escalating), `refused`
(refusal-streak ≥ the owner's dial, by default
`reflex.REFUSAL_STREAK_THRESHOLD` — fed by the friction ledger rather than the
recorder, see `helm friction`); generic `counter`
reads any counter via `--counter NAME --threshold N`. `nested-spawn` never
fires on a turn: the SubagentStart hook (`helm saguide`) hands it to every
build-capable subagent before its first step, honouring the project the reflex records
(see HOOKS.md for the hook's own scope). The seed
pass (`helm sync`) re-keys an authored reflex listed in `reflex.REKEYED` only
while its file still carries the exact legacy trigger, and notes why in the
file. `--latch` fires ONCE per
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
`uncommitted-drift`, `stuck-commonsense` and `guard-friction`, all latched,
editable like any reflex.

A counter steer may carry two slots, `{count}` and `{subject}`: the counter's
own integer, and the token its feeder planted beside it as
`<counter>-subject`. A subject that is not a short no-whitespace token is
replaced by a fixed phrase, so the fixed-text law holds.

```console
$ helm reflex add "checkpoint-green | checkpoint the green slice before refactoring" --pattern "refactor"
$ helm reflex add "spin-guard | stop retrying; read the error" --signal stuck --threshold 3 --latch
$ helm reflex smoke
```

### `helm friction [--days N] [--seat] [--json] | record <guard> [--reason TOKEN] [--session ID] | dial [N] [--json]`
Refusals per guard over a window (default 7 days), most-refusing guard first;
`--seat` adds each guard's per-seat split. It reads the **friction ledger**,
`_global/.state/friction.jsonl` in the helm home: one JSON line per guard
refusal carrying a random `id`, `ts`, `guard`, `seat`, `session` and a short
`reason` token —
never the refused command, the message body or the guard's diagnostic, because
a diagnostic is built from raw input and raw input can carry a secret. A value
that is not a short token is stored as null.

Four places append to it. The hook wrapper `bin/helm-hook` — the door every
standalone gate's exit 2 leaves through, an external gate's included — counts
through `helm friction record` with the gate's name as `guard` and the event
as `reason`; the count runs detached, so the refusal never waits for it. The
in-process dispatcher (`helm hooks run`) appends when a GATE handler's exit 2
escapes (`guard` is the handler's name, `reason` is `<event>:<tool>`); an
allow never loads the ledger module, and the wrapper does not count a
`dispatch-*` entry a second time. Every managed git
hook that refuses (`pre-commit` under both profiles, `pre-merge-commit`,
`pre-push`, `commit-msg`) appends through `helm friction record` from an EXIT
trap, with the refusing rung as `guard` and the hook's name as `reason`; a
user hook's failure names no rung and is not counted. A chat post refused for
a padded short sha appends as `shaguard`. Every write is fail-open and
bounded: a ledger that cannot be written changes neither a refusal nor its
exit code.

An absent ledger is zero refusals. An **unreadable** one prints `UNREADABLE`
and exits 1 (`--json` carries the reason with `total` and `guards` null) — it
is never reported as zero. `record` is the door a shell hook counts through:
it prints nothing and exits 0 whether or not the line landed, and exits 2 only
for arguments it does not know.

The reflex layer reads the same ledger: the most refusals ONE guard handed the
calling seat in 24 hours is the `refusal-streak` counter, and the seeded,
latched `guard-friction` reflex fires when it reaches **the owner's dial**: how
much of a mistaken guard a seat suffers before it is told it may spend 30
minutes or one delegated agent repairing the tool.

`helm friction dial` prints that number with who set it and when; `helm
friction dial N` sets it, where N is a whole number from 2 to 50 and anything
else is refused with exit 2 and nothing written. The number is stored with its
author and time under `friction_dial` in the `host` block of the authored
registry layer (`registry-authored.json`), beside the other things only a
person decides. Until someone sets it, the dial is
`reflex.REFUSAL_STREAK_THRESHOLD`. A number the owner set outranks both that
default and a `threshold:` written into a `refused` reflex entry. A stored
value that cannot be used (a settings file that does not parse, a number
outside the bounds) is never guessed at: the default is in force, the verb
says so on stderr and exits 1, and the console card prints the same sentence.

The console's Work page carries the same two reads as a card: refusals per
guard for the last 7 days, from the function this verb prints from, and the
dial with a minus and a plus button. A press posts to `/api/friction/dial`
with the number the card was showing; if the dial has moved since, the press
is answered `stale`, writes nothing, and the card shows the number in force.

```console
$ helm friction --seat
$ helm friction --days 1 --json
$ helm friction dial
$ helm friction dial 8
```

### `helm record [--hook-json] | status [--session S] | install [--dry] [--home NAME]`
The session-keyed tool-outcome recorder — the sensing half of the behavior
leg. A `PostToolUse` hook pipes each tool event's full JSON in (claude first;
codex when its hook surface lands); helm keeps tiny per-session counters under
`_global/.state/reflex-state/<session_id>/` that dynamic reflexes, the
stuck-hook, mentor observe triggers, and evolve's behavior observers read:
`counters.json` (passive-streak; stalled-turns, closed at each turn's start
by the inject hook — a forward op is an edit, a spawn, a write to the seat's
own task list (TaskCreate/TaskUpdate/TodoWrite), a `git commit` (the parsed
subcommand, so `git grep commit` is not one) or a helm coordination write
such as `task add`, `chat post`, `chat claim` or `store revise`; both hooks
write it under the session dir's lock;
dirty-streak with cached last-dirty — `git
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
includes reference-only hits. A scoped grep timeout/exec failure exits nonzero
and says the search is unavailable; it never reports a confident zero. If the
match list succeeds but a snippet read fails, the hit is retained and marked
incomplete while the command still exits nonzero.

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

### `helm burn [--json] | burn why <family> [--json] | burn burst [--json] | burn runway [--json] [--window <hours>] | burn declare <family> <colour> --until <iso> [reason...]`
The fire-danger rating for the fleet's usage: one flag per model family, plus
the overall reading and what it means for how much may run at once.

**One snapshot, folded once, read by everybody.** The watchdog pass folds every
reading it already took — the pooled codex budget, the native usage log, the
upstream family states, the owner's declarations — into
`<helm_home>/_global/.state/burn-flags.json`, beside the codex budget snapshot
and for its reason: anchored on the helm home, so one env var isolates it. The
read path NEVER probes and a snapshot past the watchdog's own staleness bound
yields nothing rather than an old colour, so no consumer can answer GREEN off
a file nobody refreshed.

**THE ASKING FAMILY IS ADMITTED AND RANKED LAST, never dropped.** For a
`review` or `verify` ask, `--from <family>` no longer refuses that family: the
row is admitted, printed with `family same`, carries E1 and E2 as its reason,
and sorts behind every other admitted family THE OWNER HAS NOT RATIONED — the
ORANGE term stays outside the family term, so a preference never spends a
reserved window. What decides whether a read is
independent is the reader's fresh context and different resolved model
(`helm/review_independence.py`), not its vendor — so a cross-family reader is
still taken first whenever one is usable, and the board gets a same-family
read instead of nothing when none is.

**The colours are behaviour, not decoration.** GREEN open more lanes for work that is already built up; never speculative · YELLOW
normal work, no extra lanes · ORANGE critical path only, one delegate
at a time, prefer another family · RED start nothing new on this family,
finish or park what is running · GREY NOT MEASURED — behaves as yellow,
renders grey, and never renders green.

**Four axes, because the four repairs differ.** MONEY (wait for a reset),
REACH (bounce a proxy), POLICY (change the model mix), DECLARED (read what the
owner said). Each flag names the axis that set it, so a colour never hides
which question it answered.

**An unread account never worsens a colour.** It caps it — GREEN becomes
unreachable — and names the repair. Half of one family's accounts can read
`reauth-needed` because helm's own token copy expired rather than because the
vendor said no, and folding that as pressure would ration the fleet on our own
broken reader.

**Every flag says when it changes.** `expires_at` is anchored to the reading's
own instant, from the window whose reset would actually move the family's
band — a five-hour window resetting in two hours does not move a family that
is red on its week. Where no instant is known the surface says *unknown when*
and never leaves a blank, because a blank reads as *soon*.

`helm burn why <family>` prints the reading, its four axes, its provenance and
its age. `helm burn declare` records an owner colour: it may only WORSEN a
measured reading, it expires at the instant he states, and it renders as
DECLARED wherever it is shown — a declared colour is never rendered as a
measurement. Exit 3 when the snapshot is absent or stale; exit 2 on usage.

**The stable read API** for the routing verb and the scheduler's admission
function is `burnflags.cached_flags()` and `burnflags.family_flag(family)`.
Neither re-derives a colour.

#### `helm burn runway [--json] [--window <hours>]` — the rate a level cannot see

The money axis reads levels: the account with the most headroom, the share of
accounts past half their week. A fleet can hold most of its codex week and
still run dry days before the first Pro account resets. The rate decides when
the last account walls, and a level cannot see it. `helm burn runway` reads the
rate.

**Percent per hour, per codex account.** Each posting watchdog pass
(`helm proxywatch --post`) appends one line to
`<helm_home>/_global/.state/codex-budget-history.jsonl`: every pooled
account's windows, keyed on its member. The rate is the least-squares slope of
the account's **longest** window over the stated window (`--window`, default
6 hours), counting only the points after the account's last reset. An account
with fewer than 3 readings, or with less than one hour between them, is
**UNKNOWN, never 0**.

**One member, one series.** On a Team plan the account id is the workspace,
and every member carries it. A series is keyed on the account id **and** the
user id. A Team credential with no user id keeps its own series, keyed on its
pool file. It is **left out** of the fleet supply, because it can be a second
copy of a member that is already counted. The providers usage history is
**not** read for codex: its rows name a codex home, not a member.

**The runway is in tokens.** A Team account holds a small fraction of a Pro
account's week, so percentages cannot be added across plans. Supply is each
account's remaining percent times its tokens per percent: the proxy-usage
ledger's tokens on that account, divided by the percent it moved while it
spent them. An account that has not yet moved 3 points takes the measured
weight of its own plan, and says so. The rate is the ledger's codex tokens per
hour over the same window.

**The horizon is the next Pro reset.** The Pro accounts carry the baseload, and
a Team reset restores too little to end a shortage.

**The verdict feeds the fold, not a second fold.** `short` (runway under the
horizon) steps the codex money axis UP one colour. `use-it` (runway at least
1.5 times the horizon while an account projects to strand at least 20% of its
week) steps it DOWN. `even` and `unknown` do not step. The step is a row of
`burnflags._RUNWAY_STEP_TABLE`, and it has two limits:

- **A projection never reaches RED.** RED refuses the family's work, and the
  fold admits RED only on a measured wall or cap.
- **An unread account keeps GREEN out of reach**, for a step as for a reading.

An account that is unread, unproven or without a weight makes the supply a
floor. A floor proves neither a shortage nor a surplus, so it steps nothing.

**One line and one push per wall.** When an account's longest window reads
100%, the pass posts one line to #helm and one push to the owner's phone
through `notify.owner_push`. The line names the seats that spent the account
over the last 3 hours, how many accounts are still open, and the runway. The
latch is (member, the reset instant): a wall speaks once, and the next week's
wall speaks again. A failed channel stays owed and is the only one retried. A
five-hour wall alone is not a wall event, because `poolwall` speaks for it.

The bare `helm burn` carries the pass's reading as **one line**. The verb reads
the history and the ledger live and makes no vendor call. It exits 0 on a
reading, 3 when there is no history yet, and 2 on usage.

#### `helm burn burst [--json]` — the per-credential question the colour was hiding

A colour answers a POOL question: most of the fleet's credentials are walled, so
an arbitrary NEW delegate probably cannot get served. That is true and useful,
and it says nothing about whether a seat **already homed on a named credential**
may spend that credential's own measured headroom. `helm burn burst` answers
only the second, from the homed credential's own live row in the observation
log — never from the fold.

**The measurement is one number per window.** For each account-wide window,
`surplus = (100 - used%) - (percent of the window still to run)`: the points of
quota that **will expire unspent** if the seat keeps to that window's own
average pace. The exemption is granted when the **tightest** window's surplus
clears a floor derived from the same ceiling knob as the warning band
(`(100 - ceiling)/2`, 5.0 by default).

One number carries both halves of the owner's rule. 15% of a week left with 14
hours to run is `+6.7` — real headroom, and perishable. The same 15% with six
days to run is `-70.7` — the pacing policy is right and there is no exemption.
The minimum across windows is load-bearing: a weekly window with room to burn is
worth nothing behind a session window at 95% with four hours left.

**A grant expires with its measurement**, at the sooner of the binding window's
reset and the reading going stale — `window-reset` or `reading-stale`, named on
the record. An exemption that outlived either would be a standing licence
wearing a measurement's clothes.

**It lifts the COUNT and nothing else.** `orange` wears two rules as one word:
*one delegate at a time* (capacity) and *do not start new work on a dark family*
(reach). Only the first is a question a credential can answer. A project's light
still decides whether work may start — capacity is never permission — the lanes
opened must be work already built up rather than speculative, and any delegate
NOT on this credential is still governed by the flag.

**Nothing unmeasured becomes headroom.** No home, no row for the account, a row
past the freshness bound, a status carrying no gauges, a window with a deadline
and no readable length: each yields no exemption and names which one it was
(`not-homed`, `unread`, `stale`, `unreadable`, `unsized-window`, `no-surplus`).
A model-scoped window (`7d-fable`) never enters the surplus — a sub-limit on one
model is not the credential's window — and a walled one is printed under the
grant so it cannot be read as covered. Exit 0 granted, 1 no exemption; a refusal
is an answer, not a missing snapshot.

**Where it is read.** `registry.admits` appends the granted sentence to the note
both work-starting doors already print under an ORANGE light, so the seat that
was about to ration itself to one delegate reads the measured exemption at the
moment it decides. The note is appended only on a grant and never changes the
verdict.

### `helm route <kind> [--from <family>] [--project P] [--row <id>] [--explain] [--json]`
Who should take this work right now — answered from the flags and the owner's
own stored rulings, not from a guess and not from a measurement the asking
agent pays for out of its own context.

`kind` is one of `review`, `build`, `verify`, `delegate`, `research`,
`council`.

**The defect it closes.** An agent deciding where to send a review holds a
context window full of the work and empty of the fleet. The measured cost of
guessing, the hour this verb was specified: one integrator wrote a whole
family off on a dark reading it had taken FOUR HOURS EARLIER and ran every
adversarial review as an expensive delegated agent, while that family sat
idle with its own reading healthy. Nothing was broken and everything the
answer needed was already on disk.

**Six nodes, the reviewers ladder applied to families.** The kind chooses
candidate families from the owner's rulings; the project's own bench narrows
them; the flag drops a RED family, rations an ORANGE one to the critical path
and admits a GREY one WHILE SAYING SO; the usability join answers who is
live; the cap says how many delegates may start; and the rank puts
judgment-bound work up the SMARTS axis and volume-bound work up the SPEED
axis. With `--row`, the eligibility read is `helm reviewers` itself — that
verb becomes one edge of this graph rather than a second census, and where
the two answers part, the node that parted them is printed.

**No routing sentence is written in Python.** The edge table carries a node,
an effect and a STORE ID; the reason you read is resolved at render time
through the same typed-first resolver every entry-taking verb uses. Edit the
store and the answer's reasons change. An id that does not resolve prints
UNRESOLVED and makes the whole reply PARTIAL — it is never silently dropped
and never replaced by a sentence an agent wrote about what it thought the
owner meant. One edge ships UNEXPRESSED with its sources named, because the
reader its threshold would need does not exist.

**It never spawns, probes, writes or blocks.** Every reading is a file
another pass already wrote. It RECOMMENDS and never acts: the write doors
gate themselves, so never asking costs answer quality and never safety.

**The cap.** `may_run = cap x delegate_factor - running`, where `cap` is four
per master and THREE for codex-family seats, and `running` is the live
subagent fan-out. A seat whose transcript tree cannot be read reports the cap
and says UNMEASURED — it never subtracts an invented zero, because that would
render a saturated seat's full cap as free headroom.

**Exit codes.** `0` an answer, `1` measured-nobody, `3` PARTIAL or
could-not-tell, `2` usage. Both 1 and 3 are non-zero so a gating caller
refuses identically; the split exists for the agent, which owes a different
next move to "park it" than to "re-measure".

**Not `helm router`.** That is the HTTP relay. `helm route
up|run|down|status|line|probes` refuses at exit 2 and names the relay, and
the relay's own usage names this verb.
### `helm accounts [--json] | show <id> [--json] | set <id> … | rm <id> | seed [--apply] | line | teach`
The OWNER-DECLARED account inventory: what we pay for, how many of each, what
each one is FOR, what it must NOT be used for, and how an agent reaches it.

It exists because the knowledge had nowhere to live. The owner kept being asked
how many accounts of each kind we have and what each is good for, while the
answers sat in free prose on an integration-board key nothing reads, in a
hand-written ledger inside a task comment thread, in one paragraph of the typed
store, and in the seat catalog — which knows the port and the model for the
paid X/grok subscription and has nowhere to say its quota is too small to build
with. Each of those is unqueryable, so every agent asked again.

**Authoritative about inventory and intent, never about anything observable.**
A declared row says a subscription EXISTS, costs about seven dollars a month,
is for reading the live timeline and is not for building. It never says an
account is active, healthy or has headroom — those are MEASURED and belong to
the quota provider. Declared rows JOIN the measured ones through
`--measured-as <measured account name>` and decorate them; a declared row may
never remove, reorder or overwrite a measured cell. Three states stay three: a
matched row decorates the quota table, a declared row with no measurement reads
"declared, not measured", and a measured row nobody has described gets a
one-click "describe this account" affordance.

**No secrets, structurally.** There is no field for a key, token, password,
cookie or recovery code — not empty, absent — so the page has no box to paste
one into. On top of that every free-text field is shape-scanned and a
secret-looking value is refused with a plain sentence that names the field and
the masked token and never echoes what it was handed. A key SHAPE is a known
prefix at a token boundary followed by real key material, so ordinary English
(`task-`, `ask-`, `risk-`, `disk-` all carry `sk-`) is not refused as an API
key; a credential word followed by a value — `Bearer abc123` — is.

**One field may hold an address, and it is masked wherever a human reads it.**
`measured_as` carries the quota provider's own account name, which on this host
is usually an address; without it a declared row cannot find its measured row
at all. The card, `helm accounts` and `helm accounts show` all print
`mask_identity`'s one letter and domain (`p…@example.test`). `--json` carries
it whole because that is the machine surface the join reads. A measured account
nobody has described yet is named the same way: the page is sent the mask to
print and an opaque handle to post back, never the provider's own spelling, and
the handle is what the "describe" button carries — a mask is not a key, because
two accounts can mask to one string.

**A save MERGES over what is on disk.** A field the writer does not carry keeps
its stored value; an explicit empty clears it. That is what lets the web form
round-trip fewer fields than the schema has without deleting `renews_on`,
`confirmed` and `seeded_from`, and what makes `helm accounts set <id> --confirm`
a one-field edit rather than a rewrite.

**An unrecognised flag REFUSES.** Every branch of this verb guards its tail,
so `helm accounts seed --bogus --apply` exits 2 naming `--bogus` and writes
nothing, and `helm accounts --json --bogus` does not answer as though the token
existed. `set` is the exception and for a reason: its values are unquoted
sentences, so it closed-set refuses an unknown `--option` instead.

**Provenance is helm's and absence is news.** `seeded_from` records where a row
came from, and the page may not write it — a payload carrying it is refused.
A save or confirm that carries no required field at all, about a row no longer
in the inventory, answers that the row is gone instead of asking for a vendor;
the card reloads on that answer exactly as it does on a conflict.

**The owner never types this verb.** He adds and edits accounts on the web
cockpit's quota tab: one headline per account, detail on click, an Add button
and per-row Edit/Remove, and a revision stamp so two open tabs cannot clobber
each other. The CLI is for agents. `helm accounts line` prints the one-line
pointer, and `helm accounts teach` prints the two `helm store` commands that
put that pointer on the JIT lane so it fires on account-shaped turns only.

A row helm cannot parse is REPORTED and SKIPPED on read yet PRESERVED on the
next save — a hand-edit is legal here, and a save that quietly dropped a line
someone typed wrong would be the worst thing this surface could do. An
inventory file that cannot be read at all reads as UNKNOWN, never as "we have
no accounts". At most 64 accounts; `HELM_ACCOUNTS` overrides the path.

```
$ helm accounts
helm accounts (2 declared · ~/.helm/_global/accounts.json):
  x-premium            x · X Premium (about $7/month) · x1 · $7/mo · Grok with live X…
      for: Grok with live X/Twitter access — getting the latest from Twitter, which other agents are blocked from
      NOT: building; too little Grok quota
      reach: owner-only, ask until a seat is wired
  vendor-y-team        vendor-y · Team · x4 · p…@example.test · batch evaluation runs
      for: batch evaluation runs
      NOT: anything the owner has to wait on
      reach: vendor-y  · measured as p…@example.test
  totals: 2 account(s), 5 unit(s), $7.0/month (1 unpriced)
```

### `helm creds`
The live account scorecard: every account across providers, with headroom,
credential state, reset window, and the use-it-or-lose-it weekly verdict.
Degrades to a one-line notice on machines with no quota provider — sessions
and resume still work.

**codex rows read the PROXY POOL, not the codex CLI home** (task/2480, which
closes task/2283 for this family). The codex seat proxy serves creds from its
hot-reloaded auth-dir and refreshes its OWN copy; a codex CLI home is
refreshed only while codex is running in it. Measured 2026-09-14: all seven
codex homes on this host carried access tokens 39-77 days old, none matching
the pool, so every codex row read `needs_reauth` during a live wall — true
about bytes nobody serves, silent about the budget the fleet was spending. An
account that is not pooled still falls back to its home, and says so.

**Every window, and the FULLEST one binds.** A codex row now prints a
continuation line — `windows: 5h 52% resets 1.5h · 7d 100% resets 134.4h` —
and its headroom/reset columns come from the fullest account-wide window, not
from the 5h one. That inversion is the bug the owner hit: a 7d window at 100%
under a 5h window at 52% reported 48% headroom while every pooled account was
weekly-capped. Rows for other families are unchanged, byte for byte.

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

### `helm cred [list | backup [--all] [--apply] | switch-guard [--install] [--apply] | heal [--apply] | sync-orca --home H [--apply] [--replace-own-chain]]`
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
| `list` (default) | DIR NAME &#124; ACTUAL ACCOUNT &#124; verdict (`AGREE` / `DRIFT` / `UNKNOWN` / `N/A`) &#124; FRESHNESS &#124; backup depth. Pure read — the owner-visible truth surface. `AGREE` says the identity is right, never that the token is alive, so FRESHNESS asks the second question (below). `--json` carries it as each row's `orca` object (verdict, reason, both expiries, `same_family`, `chain`; no token byte). |
| `sync-orca --home H [--apply] [--replace-own-chain]` | The Orca → credhome sync by hand — the same function `helm launch` runs before exec. **Dry-run by default.** Exit 1 when the home is stale and was not synced, the identity disagrees, or the home is `CHAIN-UNPROVEN`. `--replace-own-chain` is the operator's word that the home's chain is Orca's to replace: it also writes an `OWN-CHAIN` or `CHAIN-UNPROVEN` home, behind every other fence (pre-image, no other home on Orca's family, no live claude, the writer lock). A launch never passes it. |
| `backup [--all] [--home H] [--apply] [--quiet]` | Plan a snapshot of a home's stable `.credentials.json` bytes + `oauthAccount` block; **dry-run by default**. `--apply` writes it under `~/.cred-backups/<folded-email>/<ts>/` (dirs `0700`, files `0600`, owner-only from creation). Concurrent identity/token changes, symlinks, unreadable files, and failed pre-image capture refuse. Skips an identical newest snapshot; keeps 20 per account. `--quiet` is hook mode. |
| `switch-guard [--home H] [--apply]` | **Run with `--apply` before a `/login`.** Dry-run is the default. Applied mode backs the home's current account up, then prints the login command. Manual backup behavior is unchanged. |
| `switch-guard --install [--apply]` | Retire the old `SessionStart`/`Stop` backup **and heal** hooks; no active per-turn credential hooks are installed. Dry-run by default. Applied cleanup uses content-revision CAS over the realpath-deduplicated union of discovered Claude credential homes and minted seat homes. An unread census refuses before any settings write. Only exact recognized owned commands are removed; foreign hooks, mixed shell commands, unknown forms and unrelated settings keys survive. Ambiguity or unread settings returns failure visibly; recognized entries may already have been removed. Arbitrary outside config inventories are out of scope. Upkeep belongs to `helm doctor --ensure`; this command installs **no scheduler**. |
| `heal [<home>] [--apply]` | Put the correctly-named account back into a DRIFTED home from a safely selected snapshot (selection below). **Dry-run by default.** |

**FRESHNESS against Orca.** Orca keeps each account's live token at
`<orca user data>/claude-accounts/<id>/auth/.credentials.json` and refreshes it
while its panes use the account; a credhome copied from it weeks ago still reads
`AGREE` and cannot log in. The column compares a named credhome (a dir directly
under `~/.claude-homes`; the default home reads `N/A`) with Orca's copy of the
SAME account, matched by the email in the home's `.claude.json` against the Orca
dir's authoritative `oauth-account.json` — never a directory name — plus
`organizationUuid` / `accountUuid` whenever both sides carry them (one email can
sit in several organizations), and only in a dir carrying Orca's
`.orca-managed-claude-auth` marker naming that dir. A dir whose own
`.claude.json` names this account while its `oauth-account.json` names another
is `UNKNOWN`, never a match.

| FRESHNESS | meaning |
| --- | --- |
| `FRESH` | the home's access token expires no earlier than Orca's copy (or is byte-identical). |
| `STALE-vs-ORCA` | Orca's copy is strictly fresher AND the home's own chain is provably spent: no `.credentials.json` or refresh token, Orca's family, or a refresh lifetime already passed. |
| `OWN-CHAIN` | Orca's copy expires later, but the home is on an independent login chain: its refresh lifetime is still live and more than 5 minutes from Orca's, so the two are different grants. Access expiries across two chains prove nothing, so it is never synced, and keepalive rolls it as before. |
| `CHAIN-UNPROVEN` | Orca's copy expires later, and nothing tells the home's chain apart from a copy Orca rotated away: the refresh lifetimes are within 5 minutes, or one is missing. One grant Orca refreshed away and a login minted minutes apart look the same, and so does a home whose token Orca once held (Orca's dir may have logged in again). A sync would overwrite what may be a live login, and a launch would present what may be a spent token (the reuse that revokes Orca's live family), so a launch or `sessions resume` is REFUSED with that reason, the sync writes nothing, and keepalive skips it. The reason names both cures: a fresh login in the home, or `sync-orca --home H --apply --replace-own-chain` when the operator knows the home's chain is Orca's to replace — including the common case of a synced home whose Orca account refreshed while its seat was down. |
| `NO-ORCA-COPY` | Orca's store was read and no dir holds this account. |
| `UNKNOWN` | the home's identity or credentials, Orca's store, or Orca's copy could not be read; several Orca dirs claim the account, or one does while another is unreadable; or the only claimant is another organization/account uuid. Never passed off as synced. |
| `DISAGREE` | the Orca dir's `oauth-account.json` names this account and its own `.claude.json` names another. |

The sync writes **Orca → home only**, and only a `STALE-vs-ORCA` home (or, by
hand with `--replace-own-chain`, an `OWN-CHAIN` or `CHAIN-UNPROVEN` one): Orca's
files are opened read-only, and a home as fresh or fresher, or on its own live
chain, is never overwritten automatically. It refuses to write while Orca's refresh family is
live in ANY other home — `~/.claude` included, which holds exactly that family
for the account Orca has switched it to — because one refresh token in two
homes is the reuse-detection bomb `helm doctor` reports as FAIL; the reason
names that home and the fresh-login command. It also refuses while a live
claude process holds the home (one writer per home; unprovable counts as held),
while the keepalive credential-writer lock is held, on a DRIFTED home, or when
the `helm cred backup` pre-image cannot be captured. It re-measures inside the
lock, re-checks Orca's `.credentials.json`, `oauth-account.json` and
`.claude.json` stat keys, writes `.credentials.json` through a `0600` temp file
+ fsync + rename, then verifies the bytes and the mode read back.
**After a sync the seat on the home is a second refresher of Orca's family.**
Whichever side refreshes first rotates the token away from the other, and the
other presenting it trips reuse detection, which revokes the whole family —
both copies. So while the seat runs, do not switch Orca to that account: the
switch copies the same family into `~/.claude`, and the first refresh on either
side kills both. The launch line says so. `helm cred list` promises a sync only
where the sync's own dry run would write. helm adds no third refresher:
`helm keepalive` skips a home that shares Orca's family or is stale against it,
and takes the same writer lock.

Retirement also recognizes the historical `timeout 5 <absolute helm path> cred
backup --quiet` command with its exact fail-open suffix (`|| true`) on
`SessionStart` and `Stop`. The path retains the installer's shell quoting.
This compatibility does not admit arbitrary flag subsets, reordered arguments,
extra shell work, or unknown wrappers.

Establish and verify a top-level `helm doctor --ensure` schedule **before
deploying code that removes credential handlers from merged hook dispatch**,
not merely before cleaning standalone settings entries. The separate
`helm seat doctor --ensure` proxy watchdog is not credential-recovery coverage.

Selection starts with the account's newest snapshot. If its family clashes with
another home's live credentials or recorded lineage, heal may instead select
only the **newest snapshot recorded as captured from the target home**. That
candidate must have a known, unexpired access-token expiry, a known family, no
live or historical foreign-home family claim, and no identity-discontinuity
evidence. If it fails, heal retains the refusal; it never walks farther back
past a rejected target-home snapshot. Missing source-home provenance is not
guessed. Account ambiguity, holder checks and transactional restore safeguards
still apply. This lets estate-wide backups preserve independent logins without
masking a safe target-home pre-image. The 20-snapshot per-account retention
limit remains; a pruned pre-image cannot be recovered by selection.

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

### `helm attribute [--by project|model|cred|seat] [--since Nd|Nh] [--limit N] [--project P] [--json]`
Historical token-effort attribution — the HR-capacity frame: how much effort
(in tokens) has a project / model / cred / seat consumed, so cred homing can
follow need. A BOUNDED on-demand rollup over the session catalog (the bound is
always printed — a scoped result never masquerades as a whole-corpus total).
Effort = output + cache-creation tokens (claude) / output tokens (codex),
**never** raw cumulative input. Cred attribution is a path-boundary match
against `-homes/` dirs only: codex rollouts under `~/.codex-homes/<name>/`
attribute cleanly; claude sessions live in the ONE shared store and stay
**UNATTRIBUTED** rather than being guessed — that bucket is always visible,
so a coverage gap never hides inside an attributed total.

Proxy-family seats (codex, kimi, gemini, grok) join through the **sidecar
meter** (`helm proxy-usage`): every request a seat's own sidecar forwarded is
a ledger row naming the seat, the model and the pooled account (`source`,
the account email or a `cred:<hash>` label under the contract stated at
`helm proxy-usage`) that spent it, with the proxy's own input and output
totals.
`--by seat` groups on the sidecar and `--by cred` on `<family>:<account>`;
**input is printed beside effort** for every row because a retained reader
re-sends its whole context on each call, so input is the meter that moves on
a pooled account. A sidecar the meter could not read is never a zero: under
`--by seat` it is a row carrying `UNREADABLE — <reason>`, under every other
dimension a `sidecar unread:` trailer. The ledger itself has an UNKNOWN
channel: a meter ledger that exists but cannot be read, or carries a
malformed row, renders `sidecar meter: UNREADABLE — <reason>` and marks the
rollup INCOMPLETE (`incomplete: true`, `meter_unknown` in `--json`) instead
of printing zero; malformed rows stay uncounted. A seat whose LATEST read
marker is `FAILED-PERSIST` counts what reached the ledger and carries
`PARTIAL — N record(s) lost — <reason>` on its `--by seat` row (a
`sidecar meter: PARTIAL` trailer elsewhere), and the rollup is INCOMPLETE on
the same channel; a later READ marker for that seat clears it. The meter names no project and no
dispatch row (the proxy records nothing a client controls per request), so
`--project` leaves it out and per-row attribution is not offered.

```console
$ helm attribute --by cred --since 7d
helm attribute — effort by cred, 143 sessions + 412 sidecar requests (last 7d, limit 200; measure: output + cache-creation tokens per session, output and INPUT per sidecar request)
  codex:pool@example.com     3.1M  in   210.4M  412 requests
  UNATTRIBUTED             123.4M  in        0  118 sessions
  codex:seat@example.com    12.1M  in        0  25 sessions
  sidecar unread: codex-9 — UNREADABLE — port 8326 closed
```

### `helm proxy-usage [--json]`
Read every proxy-family sidecar's usage queue NOW and append it to the
proxy-usage ledger (`proxy-usage.jsonl` under the global helm home). Each
sidecar is helm's CLIProxyAPI fork with `usage-statistics-enabled: true` in
its generated config and management routes enabled by the spawn's
`MANAGEMENT_PASSWORD` (the seat's own `mgmt.token`, 0600, never printed);
the reader pops `GET /v0/management/usage-queue` on loopback with a
two-second timeout. A pop is destructive and the queue dies with the
sidecar, so every popped record is a ledger line at once (`kind` request,
with the sidecar pid so a restart is visible) and each pass leaves a `kind`
read line per seat. One status line per seat: `READ` with the record count,
`UNREADABLE — <reason>` (no management secret minted — the sidecar
predates the meter and reads after its next respawn; port closed; management
routes not enabled; management key refused), or `FAILED-PERSIST — N of M
record(s) not persisted — <reason>` (or `FAILED-PERSIST — read marker not
persisted, 0 of M record(s) lost — <reason>` when every record landed and
only the marker was refused): the pop is destructive, so a ledger append
the lock, the write or the read marker refused is its own state and never
reads as READ; the read marker on disk carries the same state and its
`persisted` count. The exit code is 1 when any sidecar was unreadable or any
append failed (in `--json` too). `helm proxywatch --post` runs the same pop every
fifteen minutes, which is what keeps the meter continuous; this verb is the
door for reading it by hand. The ledger keeps the producer's own field
spellings (`token_breakdown`, `source`, `auth_index`, `model`, `alias`,
`request_id`, …). The meter's contract, stated once here and in the
module docstring of `helm/proxy_usage.py`:
- **Kept verbatim:** `source`, only when the pool file this record's own
  `auth_index` names — a readable JSON file in the sidecar's declared
  `auth-dir:`, indexed by the producer's formula (sha256 over
  `<type>:<absolute path of the auth file>`, the file's JSON `type` trimmed
  of surrounding whitespace and lowercased, the path under the auth-dir
  exactly as the config spells it) — carries that `source` as its `email`.
  A per-record identity join, not membership; the record's shape and
  `auth_type` decide nothing.
- **Hashed:** every other `source` — an api key, an OAuth auth's fallback
  credential, a record with no `auth_index`, a record whose file is faulted
  or gone — persists as `cred:` plus the first eight hex of its sha256, so
  one credential's requests still group and its text is not written.
- **The one bound:** the pool is read at pop time and the index is derived
  from the file's path, so a path whose auth file was replaced between
  enqueue and read is joined against the file now at that path, and an
  older fallback credential equal to the new file's email is written
  verbatim. Only the producer stamping the account on the record at enqueue
  closes that window; this reader cannot. "Its text is not written" above
  holds up to this bound and no further.
- **READ:** the usage records were collected and persisted. Account naming
  is per record under the rules above and does not move the status.
- **Pool faults and unresolved:** each read event and `--json` row carries
  `pool` — `admitted` (pool files joined), `faults` (the auth-dir
  undeclared, absent or unenumerable, or a file by basename that is
  unreadable, not JSON, not an object, without `type` or without `email`;
  a faulted file admits nothing while its readable siblings still admit
  theirs; a fault carries basename and reason, not file content) and
  `unresolved` (records hashed although they carried an `auth_index`) —
  and the verb prints it as a trailer under the seat line whenever a fault
  or an unresolved record exists:
  `pool: 1 admitted, 1 fault (x.json: unreadable (EISDIR)), 2 unresolved`.
  The pool read is total and happens before the queue is popped: any
  failure in it is a fault, and a failure of any kind after the pop is a
  FAILED-PERSIST read event carrying the popped count, so no pool read
  loses a popped record.
- **Dropped before the ledger:** `api_key` (the inbound seat token),
  `response_headers`, `fail.body` and the client address triple.

```console
$ helm proxy-usage
helm proxy-usage — 3 sidecar(s) read; ledger /home/me/.helm/_global/proxy-usage.jsonl
  codex-2      codex  port 8319  pid 41022   17 record(s)
  codex-3      codex  port 8320  pid 41077   0 record(s)
  kimi         kimi   port 8318  pid ?       UNREADABLE — port 8318 closed
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

### `helm homes [prepare <claude|codex> <email> | provision [<name>] | verify [<name>] | archive <name> | restore <name> | migrate <name> | archives] [--provider claude|codex]`
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

**What a claude home carries is one list, `homes.BENEFITS`.** Each entry
names a benefit, its source, how it is provisioned, how its absence is
measured, and the command that closes the gap: the shared session store, the
skills hub, the skill deck, the default home's MCP server set, the hook
contract and the auto-memory base. `prepare` runs one pass over that list;
`provision [<name>]` runs the same pass over an existing claude home (every
named one when no name is given). The pass is additive: a value the home
already holds is kept, and a rewritten `.claude.json` leaves a
`.claude.json.bak-mcp-<stamp>` backup beside it. The hook contract and the
auto-memory base are listed but written by `helm hooks install`, whose write
gate admits only the homes it saw at start; the pass names them as absent.
`helm doctor` walks the same list over every home and prints one
`home benefits` row per home that lacks something, naming each gap and its
command, and a separate row when it cannot tell. A benefit added to the list
reaches every new home and is reported on every old one.

**A prepared claude home links the skills hub the moment it is made.**
`prepare` mints `<home>/skills -> <hub>` (the canonical source `helm skills
sync` wires, `HELM_SKILLS_CANONICAL` or the authored host `skills_canonical`)
through the same primitive seat mint uses, and says so in its note. A home
without that link runs every session launched on it with NO helm skills, and
nothing complains — `/learn`, `/premise`, `/afk` are simply absent — which is
how one prepared credhome served a rehomed seat for a day before anyone
noticed. On an existing home nothing is overwritten: a `skills` link that
points elsewhere is named in the note and left alone (a link that resolves to
the hub through another path is named as indirect), a REAL `skills` dir is
left whole; `helm skills sync --apply` is the normalizer for both. A hub the
host NAMES but that cannot be used — the configured path missing, the
authored layer unreadable — is said as `skills hub UNAVAILABLE (...)` with
the reason; only a genuinely unconfigured hub is quiet. The
`HELM_SKILL_DECK` symlink farm applies only to a home whose `skills` is
absent or a real dir — never through ANY symlink there, since a deck entry
symlinked through a link lands inside whatever the link names (the shared
hub, or a foreign target), not in this home. `helm doctor` names any home
that is missing the link.

```console
$ helm homes prepare claude you@example.com
helm homes: prepared claude home you-example-com
  note: skills -> /home/you/.helm/_global/skills-hub
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
(the usage-cap fall-through stays intact; it prints what it replaced). It
writes through the same locked door as every other pool writer (the pool's
`.pool.lock`, held across its scan, its removals and its write, the lock
`cred-follow`, `pool` and `unpool` take), so a follow and a seat add can
never interleave; and it REFUSES, writing nothing, when the file it would
mint (`codex-<email>-<plan>.json`) already holds a DIFFERENT account — the
name is not the account, and the standing credential is somebody's — and
likewise when that file holds a KNOWN account while the credential being
added names NO account id at all (`unknown-identity-cannot-replace-known`):
an identity that cannot be named cannot prove it is the holder's.

**One admission rule for every pool writer.** That four-way rule (known-same
refreshes, known-different refuses, unknown-into-known refuses,
unknown-into-empty admits) lives in one function, `codexhomes._admit_identity`,
and BOTH locked writers ask it: the seat-add door above and the source-file
door behind `helm codex pool`, `helm codex sync-orca` (either source, the
orca-managed home included, `--watch` too — a refusal is printed as rc 1 and
the watch polls again) and `helm seat cred-follow --apply`. Before task/2517
the source-file door admitted on its own, and the first `sync-orca` after an
orca-managed credential lost its account id replaced the known pooled account
under the same name with one nobody can attribute; now it refuses, names the
holder, the source and the reason, and leaves the standing file byte-identical.

`capacity` is the one policy readout: per-pooled-cred `email tier seats` +
the fleet total (what the POOL holds — an unpooled ultra contributes 0),
plus the codex* seats live on the roster.

### `helm seat cred-follow [--apply] [--json]`
**The codex pool follows whichever account Orca has ACTIVE, with no daemon
and no human in the loop.** Orca writes the active managed account id to
`<userData>/codex-runtime-home/shared-runtime-auth-provenance.json`
(`{"owner": "managed", "accountId": …}`) the moment the owner switches, and
keeps that account's codex auth at
`<userData>/codex-accounts/<accountId>/home/auth.json`. Two JSON reads, so
this rung is cheap enough to ride **every proxywatch pass and every
`helm seat doctor --ensure`** — the pool learns a switch within one pass
instead of when somebody notices. (`helm codex sync-orca` answers the same
question through Orca's daemon; `accounts.list` carries a provider refresh on
every call — measured 15.5-16.4s — which is why nothing ever ran it
unattended.) `ORCA_USER_DATA_PATH` moves the root.

Dry run by default, and **the dry run IS the table**: one row of Orca's active
account against the pool — `PRESENT` (already pooled) · `IMPORTED` (this
`--apply` wrote it) · `MISSING` (`--apply` cures it) · `DISABLED` · `COLLISION`
(the file name it would mint is held by a **different** account) · `INCOMPLETE`
(the credential names no email or no plan) · `CHANGED` (the destination moved
between the proof and the locked write) · `MISSING-AUTH` · `UNREADABLE` ·
`SKIPPED` (provenance `owner` is not `managed`) · `UNKNOWN` (no or unparseable
provenance — never a refusal). rc 1 on a measured gap, 0 when the pool follows
Orca or there is nothing to follow.

**A proof taken outside the boundary cannot authorize a write.** The pool is
shared mutable state with several unattended writers — the proxywatch pass,
`helm seat doctor --ensure`, and an operator's `helm codex pool` — so proving
a destination free, or proving it parked, is a fact about the *past*. Every
pool write in helm now runs under one exclusive lock (`<codex seat
dir>/.pool.lock`, beside the hot-reload dir so the proxy's watcher never sees
it), and the follow **re-reads the destination identity and the `disabled`
flag inside that lock, immediately before writing**. A destination that moved
in between is `CHANGED`: the row says what it found, rc 1, and nothing is
written. Without it, two follows could each read a free destination, and the
second would replace the first's credential; and a park taken after a
`disabled` read could be cleared by the writer that was already in flight.

**A file name is not an identity.** A pooled file is called
`codex-<email>-<plan>.json`, which is injective over (email, plan) and **not**
over account ids: one owner can hold two codex accounts reachable at one
address on one plan. So this rung resolves its destination **by account id**
and refuses anything it cannot prove:

* the account is already pooled under *any* file name → `PRESENT`, untouched;
* it is not, and the name it would mint is **free** → the plain name is
  written (no account-id suffix is ever appended — the door that mints a
  second file deliberately is `helm codex pool <home>`, which names the file
  after the home);
* it is not, and that name is held by **another** account → `COLLISION`: the
  row names both account ids and the standing file, rc 1, and **nothing is
  written**. Without this gate an unattended pass replaced the other account's
  credential and cleared its `disabled` flag;
* the credential names no email or no plan → `INCOMPLETE`, naming the missing
  field. The translator spells an absent field `unknown`, and every incomplete
  credential in the world lands on `codex-unknown-unknown.json`.

**The kill switch survives a refresh.** `disabled` is the proxy's own
kill-switch and an operator's deliberate act. A pooled record that already
carries `disabled: true` keeps it through every re-pool, including
`helm codex pool` and `helm codex sync-orca` — nothing in helm re-enables a
parked credential, and the pool write says so in its `WARN` line.

**It imports once and then lets the proxy own its copy.** Measured
2026-09-14 03:47Z: forty seconds after an import the pool copy's
`refresh_token` and `access_token` both differed from Orca's (compared by
sha256) — the proxy refreshes an expired token on sight and the provider
**rotates** the refresh token on use, so two holders of one refresh token
diverge from the first refresh. Whether Orca's older refresh token keeps
working is **unmeasured**. So: an account already in the pool **by account
id** is left untouched even when its email or file name differs (measured on
this host, one account pooled as `codex-a@example.com-team.json` while its
auth.json translates to `codex-b@example.com-team.json` — the same two addresses
that make the name collide); a member marked `disabled` is reported and never
flipped; no other pool member is read for
deletion; Orca's own files are never written; and no token material reaches
any surface.

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

### `helm codex resets [--dry-run] [--consume <account>] [--json]`
**The earned rate-limit reset credits, read and spent.** A codex account earns
a small number of reset credits; redeeming one clears that account's rate-limit
windows immediately, ahead of the natural reset. The credits are scarce, they
belong to the owner, and a redemption cannot be undone — so this verb is three
doors, and only one of them sends anything.

* **bare** — one row per pooled codex account: credits available, the weekly
  window's percentage, and how far away the natural reset is. Read-only.
* **`--dry-run`** — what the *automatic* policy would do this instant and why,
  per account, spending nothing. It calls the same decision function the
  unattended rung calls, so the dry run is the policy rather than a
  description of it.
* **`--consume <account>`** — the explicit manual door. It requires the account
  by name and never picks one for you — including when a name is not unique:
  the display name falls back email → truncated account id → pool file, and on
  a Team plan the account id is the WORKSPACE's, so two email-less members of
  one workspace render the same name. That refuses, naming the candidates by a
  masked handle, rather than spending the sibling's credit.

**The automatic policy.** The proxywatch pass already probes every pooled codex
identity once per tick and publishes the budget snapshot; the reset rung reads
that and spends ONE credit for an account only when **all** of these hold:

| gate | why |
|---|---|
| the reading is fresh | a stale or unreadable reading is not a measurement of zero |
| the account-wide **weekly** window is spent | at 100% used, **or** at the wall's edge with a 429 helm itself already recorded for that credential naming that window — taken only from the proxy sidecars whose config declares the pool's own auth-dir, because a roster names a credential by its pool FILE and a same-named file in a foreign proxy is a different credential — the usage endpoint's percentage lags the refusals. A 5h wall alone never qualifies; it refills by itself |
| the vendor calls the wall a **rate limit** | a reset credit lifts a rate-limit window. `workspace_member_credits_depleted` and `workspace_owner_credits_depleted` are states of the workspace's credit balance, and a credit spent against one buys nothing and cannot be recovered. An unrecognised reason, or no reason at all, refuses too — the 429 cannot stand in for it: measured across every cooling pooled credential on this fleet, the vendor's 429 body reads `usage_limit_reached` for the rate-limited accounts and the credits-depleted ones alike, so it says WALLED and never says WHY |
| exactly **one pooled credential** serves the reading | on a Team plan the account id is the WORKSPACE id, shared by every member, so a match on it alone sends the redemption with a sibling's bearer. The binding is the member identity (account id **and** chatgpt user id); anything ambiguous refuses |
| the natural reset is more than an hour away | closer than that, a credit buys only that hour and is gone |
| a credit is **spendable** | the balance is read under the same resolved credential, only for an account that is otherwise ready, and the vendor's `available_count` is taken as the LARGER bound: each credit entry states its own status and whether this plan honours it, and the smaller answer wins |
| the pass has not already spent one | ONE redemption per pass across the whole pool: several accounts reach the weekly wall within hours of each other, and the per-account cool-down cannot see a sibling. The rest are deferred to the next pass, not refused |
| no attempt inside the cool-down | one exhaustion, one attempt — the cool-down exceeds the snapshot's own staleness bound so a single wall cannot be seen twice. A 401/403 is exempt: it moved no credit and is certain about it — exempt from the two hours, never unbounded, because an outcome released from the cool-down is released from the only thing bounding how often its request repeats. A refusal is re-driven on the NEXT pass and never twice inside one, and after four refusals in a row on one key — one hour, the age a budget reading may carry and still be acted on — the refusal is a standing state of the credential rather than the transient a token rotation cures, and the ordinary cool-down takes over. The cool-down reads the LAST WORD about each idempotency key, so the write-ahead `pending` row the same pass wrote is superseded by the row that settled it; a `pending` row nobody survived to settle is the last word about its key and still holds the wall |
| the attempt ledger is readable **and lockable** | "nothing has been attempted" is the sentence that authorizes a spend, and no failed read may speak it. The lock spans re-read → cool-down → write-ahead append, so two overlapping passes cannot both spend for one wall |

**Every attempt is journaled** under `<HELM_HOME>/_global/.state/codex-reset-attempts.jsonl`
with its `redeem_request_id` idempotency key and its outcome — never a token,
never an account id. Rows are keyed on a digest of the pooled credential's own
member identity, so a reading that loses its display email keeps its history. A
consume whose outcome the vendor never settled — a timeout, an unreachable
host, a 5xx, **and a 2xx helm cannot classify** (an unreadable body, or a
`code` this helm does not know: a 200 from the redemption endpoint is where a
credit most likely left the balance) — is never retried under a fresh key; the
next attempt re-drives the SAME key, which either finds the credit already
redeemed or redeems it once. A key is closed by one of the four documented
codes and by nothing else. The rung posts one room line when it
spends a credit, and one when it wanted to and could not — the second LATCHED
on which account is blocked and why, because a wall a reset cannot lift stands
for days while this pass runs every fifteen minutes, and an unlatched line
would repeat it a hundred times a day. A spend is never latched: it is an
event, and the cool-down is what bounds how often one can happen. An attempt
the vendor REFUSED is latched with the walls, because the cool-down exempts it
and so bounds nothing about it: the room hears that a credential is refused
once per state, not once per pass. A pass that
can act on NOTHING for a systemic reason — no vendor base url, or a pool this
host cannot enumerate — is in that latched set too, so silence from this rung
means healthy rather than inert. The rung never moves proxywatch's exit code.

**The ledger is host-local**, and so is that mutual exclusion: two hosts that
both held a copy of this pool would keep two ledgers and one wall could cost
two credits. The rung acts only over the pool the host it runs on enumerates.

```console
$ helm codex resets
helm codex resets (3 accounts):
  account                        credits   weekly         natural   balance read
  other@example.com              2         100% used      134.4h    listed
  third@example.com              ?         95% used       111.1h    no-credential — no single pooled credential serves this account (ambiguous-credential)
  you@example.com                0         41% used       92.0h     listed
$ helm codex resets --dry-run
helm codex resets --dry-run (reading 3m old; NOTHING IS SPENT — a read-only credit listing IS sent for each account nothing local refuses):
  other@example.com              CONSUME       ready                    the weekly window is spent (measured at 100% used), 2 credit(s) spendable
  you@example.com                NO-ACT        weekly-not-exhausted     the weekly window is 41% used, not spent
```

### `helm keepalive [--home NAME|PATH] [--early HOURS] [--apply] [--ensure-timer]`
Roll idle claude homes' OAuth tokens forward before their refresh chains rot.
**Dry-run by default:** without `--apply` it makes no network call, lock/log
file, backup, or credential write. Applied mode captures a stable pre-image
*before* the rotating grant and refuses the grant if capture fails; then it
persists the rotated pair atomically at `0600`. Codex homes remain read-only and
surface as stale-risk. A claude home whose refresh family is the one Orca's
managed copy of the account holds, or which is stale against that copy (see
`helm cred` FRESHNESS), is skipped — before the plan and again after the
pre-image — because Orca refreshes that chain and a helm grant would spend
Orca's copy or present a rotated-away token; a `CHAIN-UNPROVEN` home is skipped
for the same reason; an `OWN-CHAIN` home is rolled as before. `--home H --apply` takes the same credential-writer lock as the sweep
(and as the Orca credhome sync), and skips while it is held. Applied outcomes are logged to
`~/.cache/helm/keepalive-log.jsonl`; token and exception values appear nowhere.
Every logged row records `by` — the seat identity that ran the pass, or `hand`
for a human-typed run — so the log can answer whether the loop is turning.

`--ensure-timer` installs or verifies the HOURLY systemd user timer
(`helm-keepalive.timer`, identity `HELM_CHAT_NAME=keepalive-cron`, session vars
unset) and exits without granting anything. It is idempotent: a second call on
an unchanged tree reports `already installed ... unchanged`. A hand-installed
crontab line that runs this verb is REPORTED as superseded and never edited —
the crontab is the operator's file, so `crontab -e` is the removal. Without the
cadence this verb only runs when somebody types it, and helm's unrefreshed copy
of a perfectly healthy account reads on the quota page as an account that needs
a re-login (see `due-refresh` below).

`helm doctor` carries the matching rung: it reports the timer's absence, any
hand crontab line, and the age of the last recorded grant, warning once that
age passes the ~8h access-token lifetime the loop exists to stay ahead of.

**The cred state this cadence feeds.** An expired access token is two different
situations and the provider now names both. A home whose refresh chain helm can
still grant on reads `due-refresh`, whose status names `helm keepalive --apply`,
the chain, and when the cadence last ran — it never says reauth, because the
owner owes nothing. A home whose chain is spent or absent keeps `expired-token`
and its re-login. `due-refresh` is still BLOCKED for allocation: a dead access
token 401s whoever presents it. A refresh token carrying no
`refreshTokenExpiresAt` counts as refreshable, because keepalive never consults
that field and the token endpoint is the authority.

## configs — every config, every home

### `helm configs [list | show <path> | cascade <cwd> [--harness claude|codex|pi] [--home DIR] | injection [--seat SEAT] [--session SID] | edit <path> | backups | restore <backup-path>]`
The config model over every claude/codex/pi home and project tree — MCPs, hooks,
skills, rules, memory files, settings, owner-authored `commands/*.md`, and
Codex `rules/*.rules`. `list` groups every discovered config file by scope and
deduplicates canonical symlink aliases; `show` prints one recognized regular
file; `cascade` resolves what a seat launched at `<cwd>` actually loads.
`injection` is the same opt-in observation as the roster's Config action: query
selectors stay separate from backend-verified identity, source conflicts and
unavailability stay explicit, and v3 agent-runtime exact UTF-8 samples stay
separate from frozen v2 explicit-env exact samples and v1's historical character
approximation. V3 config attribution comes only from one targeted, bracketed
actual-agent pid generation and its procStart-bound session; unrelated estate
scan failures cannot poison the hook. The hook event cwd remains the turn/cohort
cwd, while hook ambient `HOME` and transcript paths are never config authority.
A present-empty provider config variable is invalid rather than permission to
fall back to the process `HOME`. `edit`
(new content on stdin) is the safety-first writer: validate + exact-byte backup,
same-directory atomic exchange, file/directory fsync, concurrent-revision
checks, and rollback, under allowlisted roots only. Symlinks, directories,
devices, non-UTF-8 and oversized files are refused; existing encoding,
newlines, mode and ownership are preserved. `restore` puts a backup back
(validated, and re-backed-up first).

```console
$ helm configs cascade ~/dev/myproject --harness claude
$ helm configs injection --seat seat-a --session 01234567-89ab-cdef-0123-456789abcdef
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

**The auto-memory base rides every home install.** A credential home whose
`projects` is a symlink to the shared tree (`<base>/projects`) gets
`env.CLAUDE_CODE_REMOTE_MEMORY_DIR = <base>` in its `settings.json`. Without
it, the seat's auto-memory dir is `<home>/projects/<slug>/memory/`, which
resolves under a `.claude` segment. Claude Code then refuses the write as a
memory write and holds it on a permission prompt, and the seat stops until a
person answers. `permissions.additionalDirectories` and a path `Write(...)`
allow rule do not clear that check. With the variable set, the memory dir is
`<base>/projects/<slug>/memory/`: the same directory on disk, with no link in
the path, so the write is allowed. The estate owns this one env key: absent
or drifted is rewritten, every other env key is kept, and a home whose
`projects` is a real dir gets no key (a key it no longer needs is removed).
Seat config dirs whose `projects` is a real dir, and project installs, get
nothing. `hooks status` and `helm doctor` name each home whose key is missing
or wrong. Only sessions started after the install read the new value.
The variable is not documented by Claude Code, so a release can drop it while
every key stays in place. `helm doctor` checks that the installed program
still contains the name, and `helm doctor --probe-memory` proves one real
memory write lands under the base. Run the probe after every Claude Code
upgrade. A side effect of the variable: subagent memory moves too.
Project-local subagent memory goes to
`<base>/projects/<slug>/agent-memory-local/` instead of the project's own
`.claude/agent-memory-local/`, and user-scope subagent memory goes to
`<base>/agent-memory/` (shared by every linked home) instead of
`<home>/agent-memory/`. Nothing in the estate uses subagent memory today.

**Seats are covered too.** A full `install` (no `--home` filter) wires the
same full hook contract into every multimodel seat's isolated
`CLAUDE_CONFIG_DIR` (`<helm_home>/_global/seats/<family>/claude`): inject,
delivery + lifecycle gates, both handoff producers, and resume-turn. Thus a
launched codex/kimi/… seat
receives `@<family>` and owner posts under its family name and cannot idle past
NEW ones (the stop-guard blocks once per pending-fingerprint; a re-stop on the
SAME rows passes) — same merge-preserving laws, a seat's own settings (theme, model,
permissions, any foreign hook) untouched. `status` shows a `seats (fleet
delivery)` block and a `seat delivery: N of M seats` line beside the per-home
coverage table (hook present / helm resolvable / fail-open intact, plus the
`deliver`/`join`/`stop` lane columns). Status also reports whether each installed
resolved hook spec matches the current canonical rendering: drift prints a
per-home `OUT-OF-DATE` line instead of also being labeled missing. Unreadable
settings keep the missing-contract alarm and additionally print `currency
UNKNOWN`.
Non-hook defaults and lane-room repair never count as rendering drift, and the
inject-only coverage number falls only for inject drift. Doctor mirrors the
narrow count as `inject coverage: N of M claude homes`. Codex-harness NOTIFY hooks are reported as
recipe-pending until [HOOKS.md](HOOKS.md) carries a mechanical shape.

When both installed PostToolUse recorder and delivery members are present or
explicitly requested, installers and sync use the installed pair door,
`helm hooks run PostToolUse --installed --hook-json`. It is not a logical
handler registry entry: the ordinary event dispatcher remains separate.
Record-only projects and delivery-only seats stay single-member until the
other member is requested; narrowed repair never resurrects a standalone
beside a healthy pair. Conversion requires exact owned commands and compatible
all-tool groups, with recorder immediately before delivery when both already
exist. Contiguous exact-owned recorder duplicates followed by contiguous
exact-owned delivery duplicates collapse to one pair; a single-member population
still uses the existing every-entry repair without seeding its sibling.
Requested standalone writers retain their type, matcher, inner-budget and native-
deadline repair, including historic or modern installed drift. Explicit custom
requested specs remain standalone declarations, not substitutes for the default
pair. Conversion is checked again after those repairs. Reversed phases,
intervening foreign execution, unrequested drift and opaque metadata make a pair
ineligible, not an installation failure. A Bash-scoped recorder can therefore
stay byte-identical while requested delivery relocates to its own wildcard group.
Ordinary delivery orphans still share one canonical group; opaque orphan leaf
metadata is retained in distinct relocated leaves rather than discarded. This
is a deliberate data-preservation exception to the old relocation behavior.

Non-whole-command member-like leaves are never overwritten, deleted, or lane-room
rewritten on PostToolUse. Requested canonical wiring is installed beside them,
with a persistent **REFUSED** row naming the home and ownership category, never
raw command bytes. This is partial-success installation, not unchanged-file
refusal or runtime equivalence: possible duplicate recorder execution requires
operator resolution (an echo-only marker does not prove duplicate execution).
A no-write retry or structurally covered status still reports that refusal;
CAS retries derive it from the winning snapshot, not the first plan. Failure
hooks stay separate. Foreign group/leaf shapes ignored by the standalone writers
are preserved and prevent conversion across their position; they do not hide
healthy canonical coverage beside them. Non-object settings/hooks or non-list
PostToolUse events still refuse, as do ambiguous/damaged composite contracts and
custom requests that would split/replace a composite.

Project-scope reporting reads `settings.local.json` as well as `settings.json`;
home-only status/census does not alias the project file into a home. A healthy
composite projects delivery into the existing logical-spec ownership inventory
for project/home duplicate reporting, without changing removal authority or
registering the composite in the dispatcher. Damaged or wrong-event composites
do not supply that healthy-member projection. The existing standalone ownership
inventory remains a registration report, not a certification of runtime health;
recorder is not added to its logical-spec population.

The installed budget is recorder 10s + isolated whisper preparation 2s + delivery
2s + existing 5s reserve = shell 19s, with native deadline 24s. This preserves
budgets, not a measured speedup. Recorder's logical dispatcher budget remains
5s; standalone recorder and PostToolUseFailure use installed 10s/native 15s,
including canonical sync. The installed runner owns stdout publication and its
receipt. Failures it regains control of before publication can produce one
composed visible alarm. External termination or failed/partial stdout leaves
publication **UNKNOWN**: the outer shell reports only to stderr (debug-only
at exit 0, not visible agent context) and never appends a second repair JSON.
It does not buffer stdout or promise visible
recovery after the process can no longer publish safely.

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

### `helm hooks latency [--json] [--since T] [--until T]`
Read-only report of retained hook stage telemetry; `--json` emits the native
structured report. Reading never creates, rotates, or repairs telemetry files.
`--since`/`--until` bound the census to a half-open window. These rows record
ABSOLUTE instants, so T must carry a zone (a trailing `Z`, or an offset); an
unzoned cut is REFUSED at rc 2 rather than guessed at, because placing an
unmarked string on that scale means choosing a timezone for the caller and a
wrong choice returns a believable empty census. A window filters per ROW, so a
span whose START is inside and whose END is outside counts as CENSORED and one
whose END is inside counts in `orphan_ends`; nothing about real termination may
be inferred from that.
Populations separate event, execution mode, stage, observer, seat, and outcome.
Completed spans use nearest-rank p50/p95 wall durations; empty completed
populations have null quantiles. Nested stage spans overlap: do not sum them.

Counts expose invalid/malformed rows, conflicts, duplicates, orphan terminals,
timeouts, cancellation, and censored START records. A missing END means
**completion-unobserved**, not running or completed. Retention/rotation is not a
historical event census; snapshot coherence or uncertainty is explicit. Observed
drops are lower bounds, and unreported loss remains unknown, never proven zero.
Nonblocking logger locking bounds lock waiting, not filesystem open/write time.

`startup_ms` stays null without a compatible external launch observer. The first
in-process marker measures initialization after entry, not interpreter startup;
pre-marker launches and the unmatched total-event denominator remain unknown.
Stage telemetry is not full startup-share acceptance. Do not make budget
decisions before at least 24 hours of live rows **and** adequate coverage; this
report alone does not establish live acceptance. It neither discovers a seat
roster nor includes payloads, commands, working directories, or raw exceptions.

### `helm injectbudget [--fleet] [--whole] [--verbose] [--json] [--session ID] [--path P]`
What SHARE of a seat's context its own hooks injected, bucketed by hook NAME,
plus the within-compaction-window repeat rate for store entries. Read-only.
Every hook on the per-turn loop is individually small and individually
justified; the quantity that can go bad is the composition, and this is the
surface that carries it. The share prints on every run and not only on a
breach — a number that appears once it is already bad teaches no reader what
normal looks like.

The population is decided by PROVENANCE: a record counts when the harness
typed it so (`attachment.type` beginning `hook_`, carrying its own
`hookEvent`), never by matching text, so a turn that merely quotes an injected
premise is not counted as one. The cost is the `rendered` block, never the
transcript line: a hook record carries its payload in `content`, in `stdout`
AND in `rendered` beside a kilobytes-long `command`, so sizing by line bytes
overstates hook cost by the file's own redundancy and counting entries by a
regex over the raw line triples every count. Transcripts that record no
`rendered` block fall back to the hook's stdout, which is flagged in the
headline as ESTIMATED; outside UserPromptSubmit a JSON stdout is a protocol
envelope and only its `additionalContext`/`systemMessage` is charged.

Counts are segmented at compaction boundaries. A re-delivery after a boundary
is the injector restoring what the seat provably lost — `inject._ledger`
drops the fire record there on purpose — so a cross-boundary repeat count
scores the design working as intended as waste.

`--fleet` censuses every seat transcript written recently and reports them
worst share first, never a mean: an average over a quiet fleet hides exactly
the outlier the instrument exists to find. A window holding less context than
the floor is reported and NOT ranked, because a session whose SessionStart
cost is not yet amortised reads as almost all hooks and is not a finding.
A read that reaches neither a boundary nor the start of the file returns no
budget verdict at all — the share of a suffix is UNKNOWN, not clean.

Default reads only the CURRENT context window (single-digit MB against
transcripts reaching hundreds); `--whole` reads the session and segments it at
every boundary. `helm doctor` carries the condensed fleet form. Deliberately
NOT wired to the per-turn loop: an auditor that becomes the cost it audits has
refuted itself.

### `helm fixedtext [--json]`
What each CHECKOUT hands a seat before its first turn: the personal instruction
file, every `CLAUDE.md`, `.claude/CLAUDE.md` and `CLAUDE.local.md` from the root
down to the working directory, and the project memory index, each with its size,
heaviest checkout first, and **every seat the roster homes there**. Read-only.

**The unit is the checkout, never the seat.** The harness keys a project's memory
directory by working directory, so every seat standing in one checkout loads the
same index and any one of them can grow it for all of them. This verb caps and
prunes nothing: a ceiling on shared text is an agreement between the seats that
load it. `helm injectbudget` is the other half, what helm's own hooks add per turn.

**A file counts only where a seat's harness loads it.** A codex model served
behind the proxy still runs inside the claude harness and is handed the claude
files; `AGENTS.md` is counted only at a checkout where the roster records a seat
in the codex harness. Elsewhere it is printed `NOT COUNTED`, so it is seen and the
total stays what a seat is actually handed.

A file that does not exist is not loaded and is not listed; one that exists and
cannot be sized is printed `UNREADABLE` and its row says `PARTIAL`, never a total
that is quietly too small. An unreadable roster prints `UNREADABLE` and exits 1.
`helm doctor` names the heaviest checkout on every run.

### `helm skills [dupes]`
Census of every skill across every home, read-only. `dupes` flags multi-homed
names (identical-everywhere = safe to consolidate vs diverged), same-content-
different-name copies, and hygiene issues (e.g. a skill dir with no SKILL.md).

### `helm skills sync [--apply]`
Universal skill distribution — the deliberate mutation verb the census
anticipated. One canonical skills source (an external skills hub — a
gitignored symlink farm into its source repo, never a repo's own tracked dir;
`HELM_SKILLS_CANONICAL`, else the host's authored `skills_canonical`; none is
configured by default, so sync is opt-in), and every claude-code config dir —
each real credhome under `~/.claude-homes` (alias symlinks fold onto their target), the
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

Idempotent: a wired estate reports zero changes, so re-running after adding a
skill to canonical is the whole maintenance story. Birth is the other door:
seat mint (`helm seat add`), `helm homes prepare` and `helm launch --home`
each link the new config dir to the same canonical the moment it is made
(`skillsync.link_canonical`, one primitive), so a re-run is a repair for a
home that was made some other way, not a step every new home needs. When a
seat mint links from the minting host's own skills BECAUSE the canonical
source could not be read (authored registry unreadable, configured path
missing), the link is made and one line still names it — `skills come from
the host fallback (...) — the canonical source is unusable: ...` — never a
quiet success over a degraded source; a deliberately unconfigured host that
takes the fallback stays quiet.
`helm doctor` prints one FAIL per config dir whose `skills` entry is missing
or resolves elsewhere, with this verb as the fix.

The census is honest about what it could not see: a root or subtree the
discovery cannot list (EACCES on a seat's `instances` dir, say) is reported
as `UNLISTED <path> (<errno>)` on stderr and the run exits 1 — never a clean
"N already canonical" tally over the dirs that happened to be visible, since
a config dir hidden there was neither merged nor wired.
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
guessed (blindly minting a raw entry for a server that a plugin already
provides would create the exact duplicate-shadow the census warns about).
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
out the exact re-arm incantation, to be run from the seat's main conversation
and never from a subagent
(`Monitor(command: "helm chat wait --seat <your-seat> --follow", timeout_ms: 1800000)`,
with the rule that it expires every 30 minutes and is re-armed in the same turn),
and names the seats it cycled. The incantation is the IDEMPOTENT arm, not
`--replace`: every agent that reads the row may act on it, and `--replace` run
by a subagent SIGTERMs the main conversation's beacon and routes the seat's
wake lines to that subagent, which leaves the seat deaf once the subagent ends
(task/2542). A dead incumbent is not what `--replace` is for: a bare `--follow`
from a seat with replacement authority already stops a dead or ghost incumbent
(`beacons.arm` keeps the replacement pass for those states). `--replace` is
needed only to rotate a LIVE waiter, so the row names it for that, done
deliberately from the main conversation. The cost is real and accepted: rearm
may spare a seat's final stale waiter so it keeps one wake path, and a bare
`--follow` beside that live waiter keeps the pre-HEAD process instead of
loading the landed code; rotating it is that deliberate `--replace`. This is the
rearm-INITIATED path (the waiter dies from outside). A second ordinary
`wait --follow` for the same live seat+session and the same normalized wait
behavior reports the incumbent PID and exits cleanly instead of killing the
Monitor that just proved it works. A same-session room/any/ambient/timeout
mismatch is left untouched and says to use `--replace`; deliberate SELF-rotation
is explicit — arm `helm chat wait --seat <seat> --follow --replace`; the
successor process performs the owned stop-then-start pass. A stop-then-arm
sequence still has a real deaf window.

**Proxies never self-propagate a land.** A per-instance or family cli-proxy
keeps running the config it loaded until respawned; `rearm` lists each pre-HEAD
proxy/daemon as advisory with the recipe
`helm seat down <seat> && helm seat up <seat>` (or the daemon's own restart) —
per-seat respawn stays the operator's call, never `rearm`'s signal.

```console
$ helm rearm
helm rearm — land-to-live: live processes still holding pre-HEAD code (dry-run; `helm rearm --apply` cycles the stale waiters)
  HEAD 265e8af committed 12m ago
  waiters (helm chat wait):
    STALE   pid 1656     seat codex-3                started 41m ago
    current pid 4174464  seat codex                  started 3m ago
  web-service: helm-web stale since 40m ago  [--apply restarts]
  advisory (pre-HEAD long-lived helm procs — respawn candidates, NEVER signaled):
    pid 88123    helm router         started 2h ago
helm rearm: 1 waiter stale, web stale, 1 advisory
```

### `helm beacons [--seat S] [--json]`

The inbox-beacon **registry census**. A seat's beacon is HELM'S ONLY wake path
to it — helm has no second re-invocation path — so a seat whose beacon is
gone cannot be reached **from here**, and it looks completely normal from
outside. The census reads beacons, so every verdict is about OUR leg; a seat
with a declared wake path outside helm is task/1308, not something these
instruments can see.

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

**The alarm is four-directional**, and reporting one direction is how the
count reached 48 while every check said fine:

* a **DEAF SEAT** — no live beacon: helm cannot wake it (the claim is about
  OUR leg; an external declared wake path is task/1308).
* a **GHOST WAITER** — a beacon whose session is dead: it consumes the seat's
  addressed rows into a pipe nobody reads, *and* it makes a dark seat read as
  covered.
* a **VACANT SEAT** — the exact inverse of a ghost: a **live** wake path for a
  seat with **no agent**. A ghost is a dead pipe for a live seat; a vacant seat
  is a live pipe for a dead one. Both are the same error — treating the
  instrument as the thing it measures.
* a **DEAF-IN-EFFECT SEAT** — a live wake path, an agent that IS home, and a
  row addressed to it that has waited past the grace without being consumed.
  The three above are all facts about a PROCESS; this is the only one about a
  DELIVERY, and it is the case where every liveness surface is right and the
  seat is still not being woken. Re-arming the beacon fixes nothing, because
  the waiter is not what failed: the repair is to make the pane take a turn
  (`helm seat resume-turn --nudge --seat <seat>`), which composes the same
  directive the alarm would and forks the same child. `--deliver` is that
  child's own interface and its parser refuses without `--session` and
  `--text-file`; it is not the operator's door. The operator's door for a
  directive a repair typed and could not submit is `helm seat composers
  --submit HANDLE`. For a record written under a repair's authorization (it
  carries the seat's repair key) it meets the same act door as the child's own
  recovery: Enter is pressed only while the seat's family is not paused and a
  row is still owed, and, when the repair named its attempt, only while that
  attempt is still the one its episode names and inside its horizon. A repair
  forked with no named attempt (a manual `--nudge`) has no attempt or horizon
  for the door to re-ask, so only the pause and the owed row apply. Otherwise
  it presses nothing and names the door and the reason. A record that carries
  no repair key (the compaction resume, and a `--nudge` recorded before this
  door existed) keeps the payload-only check: same handle, same text, same
  generation. The census (`helm seat composers`) and the
  recovery task a refused recovery writes say the same thing first: a strand
  whose attempt was retired or passed its horizon is not called eligible, and
  the owner is told that `--submit` will refuse it and that the held text must
  be resolved by hand. A pause or a drain gets the same note while it holds.

**Turn age does not decide this one.** A seat can complete turns and never
consume a row, so "no turn since the delivery" is false exactly when the defect
is present; what the rows themselves carry is how long they have waited, and
that is what the rung reads. The grace is long on purpose — the symptom is
measured in hours and an alarm that fires early is one the fleet learns to
ignore.

**A beacon is a `helm chat wait` python process, not an agent**, so a live
beacon proves the wake path and says nothing about the seat. A credential-wall
pause deliberately leaves that process armed: the delivery actuator checks
proxywatch's live family-dark latch before touching presence or a cursor, so the
same waiter stops spending turns while the provider is dark and resumes after a
measured `HEALTHY` result. The same actuator checks the seat's OWN proxy-pool
wall first (`helm/poolwall.py`): a codex proxy seat whose pool has every
credential cooling down gets the proxy's `429 · no available credential for
<model> ...: N cooling down (reset in <duration>)` on every wake while its
family stays healthy on other accounts. The seat's proxy.log carries that
refusal with the proxy's own timestamp, so the wall's expiry is that instant
plus the printed duration, and every wake path — the beacon, mention
keystrokes, resume-turn's injection and nudge, the boot-brief re-arm — is
held for that seat with one reason naming the reset instant. The pool's latest
request decides: a completed request after the refusal ends the wall early,
and an unreadable log or an unparseable reset never walls. The seat renders
`BLOCKED_ON_QUOTA` with the reset instant, the proxywatch pass posts exactly
one `#helm` line per wall per seat, and a fresh refusal after the reset is a
new wall with one new line. `helm beacons` still reports the process shape; the
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

**It signals nothing.** A bare `helm chat wait --seat <seat> --follow` is an
idempotent ensure: exactly one committed, attributable LIVE waiter for the same
seat, session, and normalized room/any/ambient/timeout behavior is retained and
reported by PID. Concurrent first arms publish short-lived `arming` markers and
serialize one per-seat election. The lock and registry key use the same
casefold-canonical seat identity as chat addressing, so case-only spellings such
as `Kimi`/`kimi` cannot split one seat into competing elections. Active-waiter
display casing comes from the declared seat; census reconciles a legacy row's
case-only spelling to the enrolled roster key before writing attendance. During
registry migration, one canonical/newest row per PID feeds election, signalling,
and census, so an older raw-cased duplicate cannot shadow the live incarnation.
A contender is never mistaken for a ready incumbent or a census-live wake path.
A pre-marker waiter is elected by strict `(starttime, pid)` age, so two launchers
cannot each stand down for the other and leave zero. If the readiness marker,
election lock, or incumbent proof cannot establish a safe election (including an
unreadable process-incarnation key), the arm says **DEGRADED**, signals nothing,
retries active registration, and runs the newcomer even if that retry also fails
— a duplicate wake path is safer than zero. `--replace` is the
explicit STOP-then-START rotation, and a seat only ever stops its own. A same-
session behavior mismatch stays untouched and requires that flag. Different-
session, ghost, unknown, and duplicate committed states are not laundered into
the idempotent case; they retain the owned replacement path. Reaping by pattern
across a shared process table is how a live seat's wake path gets severed:
a developer box routinely runs seats belonging to other projects' fleets, and a
pattern match over the process table cannot tell them apart from this one's.

```console
$ helm beacons
helm beacons — the inbox beacon is helm's only wake path to a seat
  codex                  covered   live 7  ghost 0  unknown 0   (+6 surplus)
  gemini                 DEAF      live 0  ghost 1  unknown 0
  kimi                   UNPROVEN  live 0  ghost 0  unknown 1
  retired-seat           VACANT    live 1  ghost 0  unknown 0
  DEAF SEAT gemini — no live beacon: helm cannot wake it. It must re-arm `helm chat wait --seat gemini --follow` to be reachable here.
  VACANT SEAT retired-seat — its wake path is LIVE and nobody is home: 1 live beacon consumes its addressed rows, and no live pane declares this seat, and the process behind its beacon (pid 42546) is seat codex's agent. A DM to it is swallowed exactly as a ghost's is — the inverse case, a live beacon for a dead seat. Only a relaunched agent (or that seat's own de-arm) ends it.
  UNPROVEN kimi — no beacon could be PROVEN live (1 unknown). Not a deaf seat and not a healthy one; the instruments could not answer.
  GHOST WAITER pid 798715 seat gemini armed 33h ago — its launcher is gone — the beacon was reparented to a reaper, so nothing reads the pipe it writes wakes into. It consumes that seat's addressed rows into a pipe nobody reads, and it satisfies a naive shape check, so it makes a dark seat read as covered.
helm beacons: 4 seats, 1 covered, 1 DEAF, 0 DEAF-IN-EFFECT, 0 MISROUTED, 1 VACANT, 1 UNPROVEN, 1 ghost waiter, 10 beacons (6 surplus); seat-joined 7/8 live beacons; origin UNSTAMPED on all 8 (no stamp producer exists yet, so main versus subagent is not measured)
```

The summary's last clause holds TWO different joins. `seat-joined N/M` is
SEAT attribution: live beacons whose seat row names a live pane for that seat.
The `origin` clause is ORIGIN attribution: whether the main turn or a subagent
armed each beacon, read from a producer stamp on its registry row. No stamp
producer exists yet, so while every unattributed row is unstamped (reason
`absent`, or a row written before the field existed) the clause says
`UNSTAMPED`, not "unattributed". A fleet with any stamped row prints the full
fraction (`origin T top-level / S sidechain / U unattributed (N unstamped) of
M live`), which is the denominator MISROUTED rests on. `--json` carries the
same `ownership` block with `seat_joined`, `unstamped` and `origin_unstamped`.

Under `--post`, a DEAF seat whose pane DECLARES the seat (`HELM_CHAT_NAME`,
census `agent` true) AND that owes work gets one bounded re-arm nudge per
DEAF spell. It owes work when an addressed row is pending (the reader the
DEAF-IN-EFFECT path uses) or when it owes dispatch work (`dispatches.owed`).
A DEAF seat that owes nothing is never typed into, and the census still
reports it DEAF. Automatic typing is also limited in scope. It reaches only
seats of the project the timer runs for (a seat's project is its roster
checkout, resolved like any other), and only a verified native claude
runtime. An owing DEAF seat of another project, or on a proxy or codex family,
is reported with what it owes ("wake is a paid turn; not auto-nudged") and is
never typed into. The pane is
asked to re-arm its beacon with the exact `Monitor(...)` call. The nudge uses
the DEAF-IN-EFFECT repair path, its bounds (debounce, spiral, per-hour cap) and
its episode latch. It is refused while delivery to the seat is paused. The
actuator checks again that the seat still has no beacon. The latch settles
when the seat reads covered again. A seat whose pane the census could not name
(`agent` unknown, such as a dead roster row) is never typed into.

`surplus` is the accumulation, counted in BEACONS and never in matching
processes: each beacon is a process PAIR (a `bash -c` wrapper and its python
child), so a `pgrep` count is double the real figure. Only the python child is
a beacon here — matching the wrapper would kill the shell and orphan its child.
Surplus is reported rather than reaped: the seat's own next re-arm collects it.

```console
$ helm tidy
=================================================================helm tidy [DRY-RUN — here is everything that would change]
=================================================================[1/4] CENSUS   ...
[2/4] HOOKS SYNC   every home carries its full canonical hook set (0 changes)
[3/4] MCP SYNC     every home reaches the canonical MCP set (0 changes)
[4/4] WORKTREE GC  ...
helm tidy: dry-run complete — `helm tidy --apply` executes
```

## ready — the fleet-readiness gauge

### `helm ready [--json]`

**May forward work resume after a crash/reboot?** Five ratified signals
(#163), each a READER of an authority that already exists — the gauge
composes, it re-implements nothing:

| signal | authority read | red means |
|---|---|---|
| `daemon` | the detected metaharness adapter's own pane probe (orca: one 5s-bounded socket round-trip) | the daemon is installed and not answering |
| `seats` | the spawn register + `seat_liveness` per registered seat | a registered seat's pane is GONE, or its agent exited under a live pane |
| `beacons` | `beacons.census()` — the attendance question | a seat is unreachable, vacant, or ghost-eaten: helm cannot wake it |
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
                    -> `helm beacons` names each fault; `helm seat resume <seat>` re-seats one with no pane, and a seat whose pane is alive
                       can only be re-armed by that pane — no turn arms a beacon, the stop rung asks each seat to arm on its clean stops,
                       and prompting a live pane that is not yours is the owner's call
  GREEN   families  4 families HEALTHY: codex, ds4pro, gemini, grok
                    WALLED: kimi AUTH-401 since 2026-08-04T21:41:05Z — a wall is a fact, not unreadiness
  GREEN   checkout  shared checkout ~/dev/helm clean at origin/main (845dc0aff83e, as last fetched)
helm ready: NOT READY — 3 green, 1 red, 1 unknown
```

(A real run, 2026-08-04, host names redacted — the gauge's first live read
found two seats whose beacons were dark and a stale pane handle the fleet
had not noticed. Honest reds are the product; a gauge that always reads
READY is decoration.)

## substrate — the attested-truth leg

### `helm cell <join|send|status>`
The agent-to-agent substrate, helm-named: a thin wrapper over the signer
binary with `HELM_*` env mapped in (see [ENVIRONMENT.md](ENVIRONMENT.md)).
`status` reports node liveness, chain head and the signer; `join` and `send`
pass through to the signer, and they are the only verbs it dispatches. A missing binary is a graceful
"substrate unavailable" line, never a traceback — and nothing else in helm
depends on it. See [ATTESTATION.md](ATTESTATION.md).

```console
$ helm cell status
helm cell: node LIVE at http://127.0.0.1:8899 — chain head 43 ...
```

### `helm saguide`

    helm saguide --show          print the canonical initial-context block
    helm saguide --hook-json     the SubagentStart hook path (reads stdin)

`--hook-json` hands every fresh build-capable helm subagent the brief at
spawn (a read-only `agent_type` — Explore, Plan and the like,
`saguide.READ_ONLY_AGENT_TYPES` — gets nothing; an unknown type still gets
it): the test route, the beacon, the sibling sweep a fix or a review owes,
and the reviewer's own patch for a MECHANICAL finding. `helm/saguide.py` is
the one home of these rules. `--show` prints the long form the brief points at —
one source, so the guide and the agent cannot drift.

**It fails open on every path, deliberately.** A spawn must never die because
its physics could not be composed, so a malformed payload, an unreadable stdin
or a broken decode all exit 0 with the spawn intact. That includes the READ
itself: stdin is decoded as UTF-8, and a payload carrying one invalid byte used
to raise before the handler's own except could run, exiting 1 with a traceback
onto a spawning agent's stderr.

## posture — the owner declares where he is

One flag file, written and removed by the owner's word, and read by anything
that would otherwise assume. The owner's own door is the web console's **away
mode** card (he does not type verbs); these verbs write and read the same flag:

    helm away          declare yourself away
    helm away off      lift it
    helm away status   read the flag and the owner's fleet notice (writes nothing)
    helm back          lift it, four fewer keystrokes

Both doors write through `away.declare` / `away.lift`, so a change made on the
card reads at `helm away status`, and `helm away` shows on the card. A flag set
from the card records the owner's name and the door (`declared by <owner>
(web)`); one set from a terminal records the calling process as below.

**The door is a phone, and that is the whole shape.** Away matters exactly when
the operator is not at a terminal, so the surface is one short word each way —
`back` is a verb rather than a flag because it is four fewer keystrokes at the
moment you least want to type.

**Nothing infers posture, and that is not negotiable.** No clock, no activity heuristic, no quiet-for-twenty-minutes
rule, no second sentinel — enforced by a test over the module's own source.
Twenty minutes of silence is a man thinking, and a surface that fires on it
interrupts exactly the person it exists to serve. Away is DECLARED and REVOKED,
both by a human typing a word.

**One sentinel, and the writer owns it.** The flag lives at `owner-away` in the
chat store — room-free, because away-ness is not a fact about a room the way an
unread marker is. `helm/away.py` writes it and every reader imports
`marker_path`/`is_away` from there; a sentinel belongs to whatever writes it, or
two modules agree on a filename by coincidence and drift the day one is edited.

**The flag is the state, and it is the only state.** While the file exists the
posture holds. Its readers are the progress chart, the away mode card, `helm
away status` and the per-turn inject, and all of them read this file. Nothing else is
written on a transition: a second representation of one boolean is how two
surfaces come to disagree about where a person is, and an unread record that
can contradict the marker under interleaving is a contradiction surface rather
than a notice. Its lifetime is the chat store's, and the verb makes no
durability promise in either direction — it resolves a PATH and never asks the
filesystem underneath what kind it is.

**The marker is published in one operation.** Content is written to a temp file
and LINKED into place, so the flag never exists half-written and a failure
leaves only the temp — there is no cleanup path that could delete a marker
somebody else has since published. Link rather than rename, because link is the
atomic CREATE-ONLY primitive: declaring away twice reports the standing
declarer instead of silently replacing them.

**A guessed identity is not a declarer, and a disputed one is not either.** The
name is resolved through helm's identity layer with the caller's session, so a
rostered seat can be recognised, and only a DECLARED or ROSTERED name is
recorded. A DERIVED name is minted rather than known, and a posture flag is a
claim about a person, so it records as an unnamed process instead. The same
answer is given when the two identity sources DISAGREE — asked through helm's
own disagreement predicate, which covers both a session rostered to another
seat and a name already held by a different live session; a hand-written
"is my session bound elsewhere" test is a proper subset of that and passes
exactly the inherited-name case it would need to catch.

**The bar is a 0700 directory you own, and that is a deliberate limit.** The
parent must be a non-symlink directory you own at exactly mode 0700 — exactly,
because a guard that admits more than its stated contract is worse than a
missing one. No component of the path ABOVE it may be a symlink, owned by
anyone but you or root, or writable by others without the sticky bit: a
pathname is not its last component, and a swap anywhere above the leaf
re-points the whole path to a directory you would then create and pass your own
owner test on. The marker itself is read with `O_NOFOLLOW`, so a symlink left
at the marker path is authoritative away-ness with no attribution rather than
a name read out of whatever it points at.

It is still not hardened against a local attacker who can win a path RACE
inside your own private directory — that needs a directory fd held across the
whole publish. The worst outcome of losing such a race is that a chart renders
when it should not, and anyone positioned to win it can already edit the code
that draws the chart.

**Its lifetime is the chat store's.** By default that store is RAM, so a reboot
clears the posture, which is the right default: a posture declared before a
reboot is a stale claim about a person. But `HELM_HOME` or `HELM_CHAT_DIR` can
redirect the store onto disk, and then it survives and nothing resets it —
whoever redirects the store owns what that means.

**Posture from a terminal is attributed, not gated.** An admission gate could
not tell a person from an unnamed or deliberately unlabelled agent, so it
refused the honest agents and passed the ones worth stopping, and it could
refuse `helm back` and strand someone in away posture. So every declaration
records WHO made it, and every reader shows that: the inject line for a flag
not set from an owner door says so ("AWAY was declared ... by seat-x
(declared), not from an owner door"). Lifting is one unlink and cannot be
refused by any identity.

**The owner's fleet notice.** Beside the flag, in the same private directory,
the card keeps ONE notice to every agent (`owner-notice`, `helm/ownernotice.py`):
his words, the time he set them, his name and the door. It is a different
fact from away-ness (it may stand while he is here), so lifting the flag does
not clear it; `helm back` prints it when one is still standing. Only his web
card writes or clears it: the writer takes an OwnerDoor that only the card's
handler mints, and a record naming no owner door is ignored by every reader.
The card offers one preset, "Away: coordinate through a2a (helm chat /
dispatch); do not narrate in local chat; save output tokens." A notice cut at
the inject's byte cap ends "… (full notice: helm away status)".

**The fence, and what it does not stop.** Every agent runs as the owner's uid.
The argv-guard refuses a seat's command that POSTs to the away card, that
mints an OwnerDoor (`owner_door(`, `ownerasks.owner_door`) in code it runs, or
that writes the flag or the notice by hand (a redirect, `tee`, `cp`, `mv`,
`install`, `ln`, `touch`, a python write), that deletes the NOTICE by hand
(`rm`, `unlink`, `truncate`, `shred`, `mv` it away, a python remove; the
flag's delete stays open to anyone, as `helm back` is), and a Write or Edit of
either file. It reads the command through the same data fold the GitHub
Actions rung uses, so a commit message, a chat post, a dispatch, a store
statement or a gh comment that merely names the mint passes, while eval,
`source`, `bash -c`, `sh -c`, `python -c`, `printf -v`, `xargs` and a
substitution still count as code. A search or a read of either file passes.
That stops a seat forging his word by
ACCIDENT. A DELIBERATE same-uid process (a script file run later) can still
render as him; that residual is stated in `ownerasks` and `ownernotice`.

**How a seat learns it, and why nothing is woken.** No chat row is posted and
no seat is mentioned, so a beacon (`helm chat wait`) sees nothing. The per-turn
inject reads the flag and the notice at each seat's next working turn and
leads with a `[helm posture]` line ONCE per change in that context, kept in
the inject's own per-session seen-memory: the turn after is silent, lifting
renders a one-time "The owner is BACK" to a context that saw him away, and two
changes between one seat's turns render the latest state once. A machine
broadcast or a Monitor expiry defers the line to the next working turn. An
unreadable flag or notice renders as UNKNOWN, never as "here". The line leads
the turn outside the arrival byte cap and under its own (640 bytes); the fire
ledger records `posture` as delivered, in-context, deferred or unseen.

## chat — the human-included groupchat

### `helm chat [post <text...> [--dm SEAT] [--reply-to <id|n>] | reply <id|n> <text...> | read [--since N] [--follow] [--dm] | rooms | react <n> <emoji> | verify | dm <seat> <text...> | receipts <broadcast-id>[@occurrence] | log-flush | node up|down|status] [--room R]`
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
falls open to a RAM row without a confirmed receipt; this does **not** prove
that an attempted turn was uncommitted. A launched process timeout reports
`signing_timeout`. An explicit node refusal reports `send_failed` and carries
the node's own text: the signer exited nonzero with no stdout, and its last
stderr line was `node refused the turn: <text>`. The node sends that only for
a turn it did not commit. The signer prints `no reason given` for an answer
that had no error text, and that answer can be malformed, so it stays unknown.
Other unsuccessful send results report `send_outcome_unknown`. **The top-up is
reactive.** Helm sends first and never asks the faucet before a send: chat
turns are fee-free on a node with the coordination class, so a pre-send grant
paid for nothing and burned the faucet's own transfer fee. Only when the node
refuses the send because this cell cannot pay its fee (`rejected: insufficient
balance on cell <this cell>: need N, have M`) does helm ask the faucet once and
send once more — safe, because a refused turn committed nothing. If that
top-up fails on a dry faucet the row reads `faucet_source_dry`; any other
failure keeps the node's own refusal first. The bound signer also funds its
own cell BEFORE it submits, and when the faucet refuses that it exits with
`faucet refused funding: <json>`: that turn was never submitted, so the row
says so and reads `faucet_source_dry` for a dry source, `send_failed`
otherwise — never `send_outcome_unknown`. Helm replays nothing else: after
anything but an explicit refusal the node may already have accepted the turn,
so inspect the node's receipts before deciding whether to retry, and a retried
send is never retried again. Idempotent join recovery remains, but it does not
guarantee a signed result. The
failure is **LOUD and sticky**: the row records a secret-scrubbed structured
`transport` diagnostic, the exact per-profile incident stays on tmpfs
(`/dev/shm/helm-chat-failures/<room-key>/.sign-failures.json` when the room
path itself is not tmpfs), and CLI/TUI/web/doctor all report `DEGRADED`
with reason, profile, first/last failure, age, count, and remediation until a
successful signed turn by that profile clears it. No signer configured is not
an incident: it produces zero signing traffic (no probe, no unlock), and
status says `unsigned (no signer)` rather than letting a
reachable node imply signed posts. Agents sign as `HELM_CELL_PROFILE` (else
their own seat, else `helm-agent`), through the same identity gate as a
coordination turn: a process that provably is seat X while `HELM_CELL_PROFILE`
names someone else posts UNSIGNED, stamped `DEGRADED X/identity_conflict`, and
never signs with the other profile's key; while the roster cannot be read, the
same seat is stamped `identity_unreadable` instead, since an unreadable identity
is not permission. The transport status asks the same gate, so that seat reads
DEGRADED, never SIGNED on another profile's receipt, and `helm hooks install`
and `helm seat launch` list its pane as posting UNSIGNED. An explicit profile
still wins. The
owner's web posts sign server-side as the owner's derived handle. `helm chat
node up` provisions
the room node (`helm-chat-node.service`, `dregg-cave-node` on
`/dev/shm/helm-chat-node`, port 8898, faucet ON — helm tops a cell up only
after the node refuses a send for its fee; failed signing still leaves the RAM
message available).

**`helm chat node prepare [--data-dir D] [--bin B]` is the unit's
ExecStartPre.** It restores the node's chain descriptor (every data-dir file
but the store) from the snapshot beside helm's state, or mints one around a
snapshotted key, and never re-keys a live data dir. One runs at a time: it
holds `chat-node-prepare.lock` beside the snapshot for its whole run, and a
second prepare refuses at once, naming that lock, rather than waiting. **`D`
must not be a mount point**: the data dir is assembled in a sibling directory
and renamed into place, which rename(2) cannot do onto a mount point. The
shipped unit's data dir (`/dev/shm/helm-chat-node`) is a plain subdirectory
and is unaffected.

**The faucet is a cell, and it can run dry.** `POST /api/faucet` never mints —
value enters a cave only by genesis issuer-moves — so on a genesis-less cave
the faucet is an ordinary cell paying grants out of its own balance. The node
points its fee well back at it so that fees recirculate, but on the fee-loop
build that serves the chat node today they do not come back: measured
2026-09-23, the node's whole supply fell from 10260 to 1896 computrons and no
cell holds the difference, so every grant is a net drain.
**LOW comes before DRY.** Below `HELM_CHAT_FAUCET_LOW_GRANTS` grants (default
5; a grant is the largest refused requirement recorded at this node in the
last week, else 697), `helm doctor` WARNs and `helm chat node status` says
**FAUCET LOW**, naming the cell, its balance, how many grants are left and the
refuel that restores it (`helm chat node refuel --amount N`, N leaving the
faucet at twice the line). The grant path reads the faucet after every grant
and after every refusal on the faucet's own cell, and the first LOW reading
posts ONE wake to `main`, to the integrator (resolved from the roster, never
spelled) and to the seat `HELM_CHAT_NODE_OWNER` names. The wake is latched per
node and cell in `_global/.state/chat-faucet-low.json` under an flock, closes
on the first reading that is not LOW (so a refuelled faucet re-arms) — a
grant, `helm chat node status` or either path of `refuel` is such a reading,
so a faucet refilled by hand or from another host re-arms the wake the next
time anyone looks; `helm doctor` reads the faucet and never touches the
latch. It is not latched when its post fails, and an unreadable balance
neither opens nor closes it. When that balance
falls below what a funded turn costs, `helm doctor` and `helm chat node status`
say **FAUCET DRY**, naming the cell, its balance, the largest grant it refused
and the repair. That floor is the greater of a measured funded turn and the
largest grant this node refused WITHIN THE LAST WEEK: a recorded requirement
names the node it was observed at and expires, so a cave rebuilt at a NEW url,
or any cave after that week, does not inherit a previous season's number. A
rebuild at the SAME url inside the week DOES keep it: the record is keyed on the
node url and its season, never on cave identity, and the faucet cell id is
derived deterministically. Inside one node and one week the largest requirement
still stands and a smaller later refusal cannot lower it. A post the node
refused for its fee, whose top-up the faucet then refused for that reason,
carries the `faucet_source_dry` diagnostic; a post refused for anything else
keeps `send_failed` and the node's own text, whatever the faucet holds. The
low-cell row names who tops a cell up — the signer, before its next send —
and only on a node where chat turns carry a fee (`HELM_NODE_COORD_FEE` above
0); where they are fee-free a zero balance is healthy and doctor does not
warn about it. A
balance helm cannot read is UNKNOWN — never DRY, never healthy.
`helm chat node refuel [--amount N] [--apply]` is the repair: it states what it
would move, from which locally-keyed cell to the faucet, and moves nothing
without `--apply`. It is a no-op against a faucet that is not LOW, and it
refuses — loudly, still naming the move — when no configured signer can sign a
Transfer. **The source keeps a fee margin.** The signer tops a short SENDING
cell up from the faucet to pay its own turn fee, so moving the source's whole
balance asks the dry faucet to pay for its refill — which is how the first
live refuel failed. Without `--amount` the door moves the source's balance
less two fees; an `--amount` that would leave less than one fee is refused
with the arithmetic, at plan time and again against the source balance
re-read at the moment of the move.
`--apply` re-reads the faucet balance at the moment of the move and submits
nothing if it has become funded (or unreadable) since the plan was made. That
NARROWS the window in which two refuels overfund the faucet and drain the
source twice; it cannot close it, because nothing in the signer's argv carries
an idempotency key or an expected destination balance. Closing it needs that
key in the signer/node protocol. **A transfer's outcome is only what a receipt
proves**, and a receipt is an identity AND a statement of commitment: a hash
names a turn without saying it was applied, and a queued answer can already
carry one. `moved` is printed on a clean exit whose answer states commitment
and carries a turn or receipt hash, and the sentence names that hash; a nonzero exit is UNKNOWN and never "refused", because a signer can
commit a turn and still fail resolving its receipt, and an exit-zero answer
carrying no receipt is UNKNOWN too. **An UNKNOWN is then settled from the
balances**, because the fee-loop node's `/api/receipts` ignores the turn-hash
filter and returns the chain head, so the signer can never find its own
receipt there: COMMITTED (exit 0, "confirmed from balances") when the faucet
rose by exactly the amount and the source fell by the amount plus a fee no
larger than the margin; NOT COMMITTED (exit 1, safe to re-run) when neither
balance changed across three re-reads over 4.5 s — unless the signer TIMED
OUT, which may have left a submit queued at the node, so there it says "no
change seen within 4.5 s" and stays UNKNOWN; otherwise it stays UNKNOWN (exit 1)
with both deltas printed — a retry on an unread outcome MAY move the value
twice. The door never retries on its own.
`HELM_CHAT_FAUCET_CELL` names the faucet cell explicitly when helm has not
observed it from a refusal or the node's boot log.

The transport is node-agnostic (`HELM_CHAT_NODE_URL`; empty
disables) — the node migration just repoints it.

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

**A refused room never takes the batch down with it, and a failing flush is
LOUD.** Measured 2026-08-11: one meld whose durable lifecycle journal diverged
from RAM raised at the top of `log-flush`, which discarded the flush of all 156
rooms — including the room the whole fleet coordinates in. The 3-minute timer
fired ~160 consecutive times over eight hours, wrote zero rows, and alerted
nobody; 110 of those rooms were flushable the entire time. Two things changed:

* **Per-room isolation.** Every room that can flush does. A room that cannot is
  QUARANTINED BY NAME and the run continues. The coherence rule is unchanged —
  a meld whose lifecycle leg could not be mirrored still gets no partial
  rendered-row flush — but it is now honoured by name instead of by aborting
  everything after it. `log_flush(report=…)` fills an optional dict with
  `quarantined`, `stranded_rows`, `flushed_rooms` and `lifecycle_down`; the
  return value stays the row count for existing callers.
* **A repeatedly-failing durability timer wakes somebody.** A flush that
  quarantines every room, or whose lifecycle leg is down, is a FAILURE even
  though it raises nothing and returns the same `0` a healthy quiet fleet
  returns — so the outcome is classified from that report, not from an
  exception. The SECOND CONSECUTIVE failure DELIVERS A MESSAGE to the obligated
  seat (`HELM_LANDER`, else the folding seat): a DM when the roster says that
  seat has joined, an addressed `#helm` post when it says it has not, and both
  when the roster cannot say — a message parked in a lane no seat holds has
  woken nobody. Re-alerts double (2, 4, 8, 16…) so a persistent outage keeps
  getting louder without training the fleet to filter it, and the "alerted"
  mark is written only when a message actually landed, so a failed send is
  retried on the next run. A clean run closes the alarm and re-arms it. A
  `--room R` run is deliberately NOT counted: that is what an operator types
  while repairing, and it must not reset the streak that watches the timer.
  The CLI prints the refusal on stderr and exits 1 on a failed run.

**Reading the flush age.** `helm doctor` reports how long ago the journal last
reached disk against the timer's own cadence (WARN past three missed runs, FAIL
past an hour), names any quarantined rooms with the row count stranded in RAM,
and FAILs on a failure streak. `helm brief` carries the same fact in WAITING ON
YOU once it is stale, quarantined, or switched off — a cursor eight hours
behind a three-minute timer is the cheapest detector there is, and on the day
it mattered nothing was reading it.

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

**A broadcast states its basis.** `helm chat post` refuses a body that
addresses every seat (`@all`, `@fleet`, `@everyone`) unless ONE of
`--measured`, `--inferred`, `--unverified` comes before the body, the same
three words `helm dispatch verdict` already owes. The word heads the row
(`[MEASURED] @all ...`), so every reader sees how far to trust a broadcast
before acting on it. Any other post MAY carry one. The words are read from
leading position only, never out of the body, and a subsystem or the web
console posting through the library is not asked: this is the door an agent
speaks through.

```console
$ helm chat post --measured "@all LAND 190 is on trunk, pull before you branch"
$ helm chat post --inferred "@all the proxy looks walled, hold new sends" 
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

### `helm chat retire-rooms [--apply] [--idle-days N]`
Archives meld rooms with no post for N days (default 7) off the live bus.
The chat directory is FLAT and the delivery hook lists every entry in it on
every tool call, so a room nobody uses still costs every seat: its file, and
a delivery+wake cursor pair per consumer. Retirement copies the raw room and
its meld actor snapshots to `~/.helm/helm/journal/retired-rooms/<room>/` and
fsyncs them FIRST, records the newest row time in
`journal/retired-rooms.json`, and only then removes the room and every cursor
it carried. It runs under the room's append lock and the cursor locks rotation
takes, and re-proves the idleness under them, so a post that lands after the
scan keeps the room. Only `meld-*` rooms are candidates. The restore honours
the record: a journaled row at or before that time is never restored, so a
reboot does not bring the room back (a row posted after the retirement, to a
room reborn under the same name, still restores), and meld replay skips the
retired lifecycle. Dry-run by default; `--apply` retires.

### The delivery lane — chat at tool boundaries (the meld-half port)

Meld carried a separate tmpfs whisper channel because it had no room; helm has
the room, so the lane is a **read cadence over the same room** — zero new
transports. (The
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
  --seat <seat> --follow", timeout_ms: 1800000)` — and to re-arm it in the same
  turn each time it expires (every 30 minutes; that expiry is the seat's
  check-in, and its ID is never written down) — because nothing external can
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
  posts are NOT a wake class of their own (owner steer 2026-07-21: an owner
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
  proxywatch's persisted family-dark latch, and before that the seat's own
  proxy-pool wall (the per-seat `429 ... cooling down (reset in ...)` refusal,
  dated from the seat's proxy.log). A credential-walled family or pool-walled
  seat emits no boundary or idle wake, advances no cursor, and leaves every
  addressed row pending. The already-armed waiter re-reads that latch each poll: `UNKNOWN`
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
  `mute-busy-home-room-trust-mentions`, owner directive 2026-07-29). A row
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
- **`helm chat stop-guard [--hook-json] [--seat S] [--detail]`** — the IDLE GATE (Stop
  hook; a predecessor's stop-arbiter capability, helm-native). Posture resolves
  once, checks run inline, and ALL blocking messages surface in ONE exit-2
  (fix everything in one shot); WARN lines ride along without changing the
  exit. Checks: **BLOCK** on undelivered @mentions/owner rows past the seat's
  cursor in ANY room (side-room rows carry their `[#room]` tag) — listed
  compactly, once per pending-fingerprint (a re-stop on the
  SAME rows passes; any new row re-arms — never an infinite block loop;
  `stop_hook_active` suppresses this block but surfaces the undelivered count
  as a WARN alongside claim evidence). A room whose consumption cursor cannot
  be read contributes no rows, because replaying it from zero would bring
  consumed rows back as owed, and it is NAMED rather than skipped: one
  **WARN** on both stop paths lists each such room with the reader's reason,
  never blocks (an unreadable inbox is never a hold), and a stop carrying it
  is not called "inbox clean". A room listing that fails is named in the same
  WARN, because the scan then read only the pinned rooms; a rotation that
  covered a bounded slice of the foreign rooms is ordinary and names
  nothing; **BLOCK** on
  live claim leases held by the stopping session. The block is written for a
  reader with ten seconds: a HEADER of at most twelve words saying why the stop
  is held, then ONE LINE PER LANE carrying the lane, its remainder in human
  units (`3h58m`, never `14299s`) and EITHER its exact release command OR a
  short reason none is offered — and no line inherits a promise from the header
  (the same-state one-liner a re-stop compresses to describes the sermon's
  structure, never its content). **`--detail`** prints the long form of every
  lane sentence instead — the four reads by name, a dispatch row's full account
  — and bypasses the same-state latch, so the diagnostic the blocked stop
  replaces with one clause stays reachable by a named verb rather than printed
  at every stop; it is RENDERING ONLY, and no rung's verdict, read or write
  depends on it. The blocked stop names that verb ONCE, in a footer, however
  many lanes are held. For
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
  seat whose inbox beacon is gone *and* that sends or receives canonically owed
  dispatch work is a seat nothing can wake, so every addressed row silently
  piles up (the restarted-integrator incident: the Monitor died with the old
  process, was never re-armed, and the seat ran blind ~1.5h accumulating 98
  undelivered rows while missing a gate verdict it was waiting on). One
  dispatch snapshot feeds `dispatches.owed`; sender and recipient use exact
  canonical matching. An unreadable ledger fails closed because UNKNOWN is not
  a measured zero; a readable zero makes the beacon optional. The block quotes
  the exact `Monitor(command: "helm chat wait --seat <seat> --follow",
  timeout_ms: 1800000)` call, its expiry rule, plus the `ToolSearch` escape for a DEFERRED Monitor.
  Three precision gates still fail-open: the process must carry
  `HELM_CHAT_NAME` naming this seat (the launch seam's stamp — an ad-hoc session
  that auto-named itself is never blocked, it keeps the advisory warn), the seat
  must be roster-registered, and process absence must be PROVEN by reading the
  process table for a live `helm chat wait` waiter (rearm.py's exact argv shape;
  any probe trouble means liveness is UNKNOWN, not absent). Non-wedging by
  construction: the latch stores armed/optional/missing-owed/missing-unknown,
  so each missing state blocks once, a re-stop passes, and a measured zero
  re-arms the gate when work later appears. Kill: `HELM_STOP_GUARD_BEACON=0`; **BLOCK** on a
  **REVIEW SPIRAL** — the stopping seat has review-dispatched ONE lane at
  **3+ DISTINCT tips** inside a 12h window. Round three is the tripwire;
  typed verdict observations choose the prescription. **FINISH** allows this
  stop with a convergence advisory only when every comparable round's
  `finding_count` strictly falls and every newest reviewer explicitly records
  `prior_relation=regression-of-cure` for its positive remaining count.
  **MELD** remains the prescription for flat/rising counts or `uncured`/`new`
  findings. Missing, malformed, conflicting, or unlinked observations are
  explicitly **UNKNOWN**, never convergence, and retain MELD. Legacy prose
  and APPROVE polarity never manufacture a count.
  **What the rounds FOUND is counted beside how many there were.** Each round
  carries a FINDING KEY derived from what the reviewer already wrote: the
  verdict's `worse_than_main_paths` plus every project-relative path token
  standing in its evidence, each path entering the key beside its own basename
  so that two spellings of one file intersect. Nothing new is required of the
  writer — a required marker is adopted by a small fraction of the rows that
  owe it, so the key reads the rows that already exist. A round whose key
  INTERSECTS a key already raised on the chain is a **SPIRAL** round, and three
  of those render the MELD prescription unchanged. A round whose key is
  DISJOINT from every earlier round is a **NEW-DEFECT** round; three of those
  render **UNDER-ARMED** instead — each round found a defect the previous arms
  could not see, so there is nothing open to converge, and the prescription is
  to lift the shipped function into the arms rather than re-implement it in the
  harness (see `docs/MODULE_REGISTRIES.md`) and to ask the author for the
  generating cause. A round whose key cannot be derived is **UNKNOWN**: it
  counts toward NEITHER and is never folded into SPIRAL, and the render names
  how many rounds it could not read. A still-open round is UNKNOWN, and so is
  a CLEAN round, which accuses no file at all. Two rounds of either kind render
  nothing new. UNDER-ARMED ADVISES and does not block, as FINISH does not:
  finding a new defect each round is the behaviour a review exists to produce,
  and the block still fires on every chain where one path comes back. Existing terminal-verdict
  and landed-conversation suppression are unchanged; FINISH grants no landing
  authority. The store heuristic `review-begins-with-cat-file` names the
  tripwire ("at TWO rounds the cure is a MELD, never round three"). The rule was in the
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
  stopping seat's OWN sends are billed. All reviewers' observations at those
  tips must agree; fan-out counts are never summed. Prior rounds must be
  connected by the canonical `supersedes` walk in the same chain and repository
  (build hops and lane renames are allowed). Zero findings does not vacuously
  prove regression causality; tied round order is UNKNOWN.
  A blocking chain takes priority over a higher-round converging chain.
  Latched on `(chain, lane, round-count, prescription)`: one
  block per state, a re-stop passes, and a further round or a same-tip
  FINISH-to-MELD evidence transition re-arms it. An
  unwritable latch DEGRADES TO THE WARN rather than walling the seat. An
  unreadable ledger yields no finding at all. Kill:
  `HELM_STOP_GUARD_SPIRAL=0`; **BLOCK — UNTESTED COMPOSITION** — the worktree
  the stopping seat is standing in (plus any lane rooms it leases) and another
  LIVE worktree of the same repo, driven by a DIFFERENT holder, have both
  AUTHORED the same code file; BOTH halves are verified by the strongest
  evidence that repo produces; and nothing has ever run against the two
  together. Owner canon 2026-08-23: "two green halves with an untested
  composition ... the biggest failure mode across projects."
  THE SIGNAL IS GIT PLUS /proc, not helm bookkeeping, and that is a measured
  correction: keying on `work.lane_overlaps` + the claim ledger was tried and
  killed because the sibling project — the project whose incidents are the rationale —
  has zero helm lane rooms and zero rows on every structured helm surface, and
  because `lane_rows` also missed helm's own `helm-wt/seats/<seat>` rooms. So
  the room set is `git worktree list`, and LIVE is a union: a live process
  whose cwd is inside the room (universal; measured 7 of helm's 89 worktrees,
  2 of that project's 6) OR a held helm lane lease (where the ledger has rows).
  The HOLDER comes from the lease, else from the occupants' own
  `HELM_CHAT_NAME`; UNKNOWN never claims a name.
  VERIFIED IS TIERED PER REPO: an OK gate receipt on one of the branch's own
  commits where the repo mints receipts (1,685 of 1,690 green heads resolve in
  helm), else — where it has never minted one (0 of 1,690 in the sibling project) — the
  git-only reading of "banked": own commits with the room CLEAN. The emitted
  text says which, and never says GREEN about a repo with no receipts.
  A CONFLICTING merge is STILL a seam: git is loud about the text and silent
  about the behaviour, and a hand-resolved merge is an untested composition by
  construction. (Every code-overlapping pair in the sibling project conflicts, so the
  earlier clean-merge-only filter made the rung unable to fire there.)
  Discharged either way the owner named: an arm run against the COMPOSED tip —
  the receipt binds the merged TREE, so `git merge --no-ff <peer>` + gate
  clears it even if the merge is thrown away, and 3 of 40 clean pairs on the
  helm board already had one — or a `Seam: <peer branch>` commit trailer
  naming who owns the seam (a git trailer, not a chat row, because chat is
  tmpfs and dies at reseed; the match is bound to the counterpart's name, so a
  blanket trailer discharges nothing). A conflicting pair has no composed tree
  and is told so rather than handed a gate command that cannot clear it.
  ADMISSIBILITY IS THE WHOLE DISCHARGE: a receipt counts only if it is
  whole-suite, clean before and after, and unmoved across the run. FOCUSED and
  custom-argv receipts are inadmissible here — a focused claim is anchored to
  its scope rather than its tree, which is why it cannot authorize a land — and
  a half is GREEN only when its CURRENT TREE was tested, so a gate on an
  earlier commit no longer vouches for a branch that has moved past it (the
  key is the tree, not the head, so the empty trailer commit that DISCHARGES a
  seam does not cost the half its greenness). A trailer discharges only when it
  names the counterpart AND an owner: a bare `Seam: <peer>` names the seam and
  nobody.
  Latched on the COMPOSED seam set — each pair plus what it composes to — so a
  NEW peer re-arms it and so does a MOVED half, which is a different untested
  composition; an unwritable latch degrades to the WARN.
  A LANDED BRANCH IS NOT A HALF. Neither room's work may already be on the
  trunk, asked through `_merged` — the same authority `helm work release`
  prints LANDED-by-ancestry and LANDED-by-patch-identity from, because our
  protocol lands work REBASED so almost nothing arrives under the sha its
  author wrote and ancestry alone answers a truthful NO. Measured false
  positive that added this: the rung fired on its own lane against a seat
  branch carrying one two-week-old commit that `git cherry` marked with a
  MINUS. Asked of the BRANCH, not each commit, following that instrument's own
  reasoning — a partly-landed stack IS still a half and reads NOT_ANCESTOR.
  Without it the rung's noise scales with the number of unreaped rooms, which
  on a 74-worktree board is the difference between firing on two and twenty.
  EVERY INPUT HAS A COULD-NOT-TELL BRANCH: an unreadable worktree registry, a
  failed /proc occupancy census, and an unreadable receipt ledger each surface
  as an audible UNKNOWN warn instead of silently deleting peers, and a stale
  roster row may NAME a holder but may never buy the same-seat exemption.
  UNKNOWN (no trunk, unreadable receipt ledger, unreadable tier probe) is an
  audible warn, never a block. An UNKNOWN peer HOLDER never buys the
  same-seat exemption — skipping a peer is an exclusion, and helm's own
  `seat-liveness-is-environ-not-cwd-and-not-comm` requires exclusions to be
  earned by a positive signal — so the rung stays WIDE where it cannot tell.
  THE KNOWN BLIND SPOT: keyed on the WORKTREE, so two seats sharing ONE room
  produce no comparable pair and are invisible to it (one room is one branch,
  one authored set, one half) — which is exactly helm's shared checkout, where
  the roster puts four claude-native seats. Per-worktree stays the key; the
  shared-room case is a different mechanism. What this rung does there is
  DISCLOSE: a **WARN — SEAM RUNG BLIND SPOT** tells a seat stopping in a room
  several seats answer to, once per arrangement, that the rung cannot look
  between them, because a can-tell-nothing must not read as a clean bill. It
  never blocks, and an unwritable latch makes it stay quiet rather than repeat.
  Kill:
  `HELM_STOP_GUARD_SEAM=0`; **BLOCK — NON-DISTRACTION PROTOCOL** — the
  stopping seat is connected to 3+ live claim leases (minted by its session
  or naming it as holder) and has delegated to a SUBAGENT exactly zero times
  this whole session (SubagentStop tombstones and documented subagent
  PostToolUse records are the only delegation evidence; a live same-uid
  child process is a build, not a delegate). Owner canon 2026-08-03 — "NDP
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
  way since 2026-08-04 (it was the one rung without a memory, and the owner
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
  web twin is the **seats** page under Fleet (`GET /api/chat/roster`).
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
- **`helm chat seat rename <sid|oldname> <newname> [--dry-run] [--alias-hours H]`**
  — bind a live agent to a memorable @name (`old` = seat name or an 8+-char
  session-id prefix). The roster row AND every keyed state file move
  together, so tracked delivery ground survives and the hook's `session_id`
  resolves to the new name from the next boundary. Refuses taken/reserved/
  unaddressable names; reminds you to re-arm a beacon armed on the old name.
  THE OLD NAME STAYS REACHABLE FOR A WINDOW (default 24h; `--alias-hours 0`
  records none): the renamed row carries the rename event (`renamed`:
  old/at/until) and inside it `@old` mentions, replies, reactions, DMs and
  new dispatches reach the renamed seat under its new name, `wait --seat
  old` arms the new row, and a process still carrying `HELM_CHAT_NAME=old`
  (the retained session) is admitted AS the new seat instead of refused as
  a takeover. `helm chat seats` shows each live alias with its expiry; a
  second rename inside an open window carries the earlier hop forward under
  its original expiry. The rename is refused before any write when the
  actor store cannot be read or `<newname>` already names a different actor
  (a rename may not merge two identities). When an actor record exists, the
  durable rename journal records its exact actor id, revision and canonical
  label under the actor-directory lock and remains until both the roster/state
  leg and actor relabel commit. Recovery replays only that exact epoch, treats
  an already-committed relabel as an idempotent success, and retains the
  journal rather than overwriting a divergent actor or target-name collision.
  Legacy roster-only journals and renames with no prior actor record keep their
  original recovery path; case-only label changes use the same journal instead
  of bypassing it. While that journal remains, the Actor directory refuses a
  **first admission** of its source or target label so the incomplete rename
  cannot fork lineage; known canonical/alias binds and unrelated first
  admissions continue unchanged. The reservation consults only the pending
  journal in the current `HELM_CHAT_DIR` / current chat root; it does not scan
  other chat roots, even when they share an Actor store. The check occurs only
  on the new-admission branch under the Actor lock and never runs recovery
  inside `bind`; run `helm chat seats` to finish the normal recovery pass, after
  which the fence disappears with the journal. If the journal is unreadable or
  its names are inconsistent, existing names still bind but first admissions
  fail closed because no candidate can be proven unrelated to UNKNOWN
  source/target names.
  `--dry-run` runs every guard, prints the five surfaces the rename would
  change (roster, identity admission, chat addressing, beacon, dispatch) and
  writes nothing. Rows already addressed to the old name do not move — that
  is `helm seat reassign`. Web twin: the **rename** control on each
  seats-panel row (`POST /api/chat/seat`).
- **`helm chat dm <seat> <text...> [--seat S]`** (also `post --dm SEAT`) —
  a TRUE 1:1 (premise `exact-token-addressee-match`): the row lands in the
  recipient's private lane (`dm/` in the room dir — never a room, never the
  channel list), addressed by the EXACT seat token (a casefold roster snap
  only — never a substring or slug fold: `team.a` ≠ `team-a`). The
  recipient's beacon/boundary surfaces it first (`[helm chat dm → seat]`),
  it gates their stop like any pending word, renders as a DM everywhere, and
  signs like a post. Catch up with `helm chat read --dm`. A DM to a
  not-yet-joined seat waits in its lane and delivers at join. Web twin: the
  seats page's **message a seat** card — a named seat routes to
  `POST /api/chat/dm`, only `@all` still posts to `#main`.
- **`helm chat receipts <broadcast-id>[@occurrence]`** — the sender's
  persisted delivery census for one broadcast row. It reports the recipients
  frozen when the row was appended, each exact session sink, observed
  tool-boundary delivery, and beacon wake attempts. A missing effect remains
  `UNKNOWN`: delivery does not prove wake success or turn execution. Rotation,
  rename, disown, and roster GC preserve historical recipient identity; the
  optional physical occurrence disambiguates duplicate exact message IDs.
  Id-less rows have no sender-observable receipt selector and are not accepted.
- **`helm chat seat mute <room>` / `seat unmute <room>` / `seat mutes`
  `[--seat S]`** — the seat's own beacon filter: a muted room stops
  surfacing home-room chatter / `@all` at that seat; a direct `@seat` mention
  or a DM ALWAYS still surfaces. Mute gates idle beacon waking only; it never
  clears tool-boundary delivery, pending, stop, or pull obligations.
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
  is live; expiry is MONOTONIC. `--ttl` reads the same forms and refusals as
  `helm work claim --ttl` below (one shared reader; the default here is 900s),
  and a malformed one is refused rc 2 before any identity is resolved.
  The printed **lease id is a CONFIRMATION
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
  the TTL. On `claims`, the id goes only on the row whose stored holder is
  exactly the calling seat: a row published under the same resource and holder
  after the scrub (`lane ` held by `worker `) gets no id, and where two of the
  calling seat's own rows publish one resource, neither row gets an id, because
  the table cannot tell which id belongs to which row. `helm work list` reads a
  lane's holder, TTL and liveness only from the claim stored under the lane's
  own resource, so a row that merely publishes as that resource (`lane `)
  cannot name another seat beside the calling seat's id or file the lane under
  STALE. `--json` prints the claims rows machine-readably, and each row's
  `holder_exact` and `resource_exact` say whether the stored holder and
  resource are the published ones. The claim ledger is
  one per chat dir, not per room. For short-lived same-host mutual exclusion —
  files, ports, worktrees (the concurrent-lane collision class).

**WHO MAY ACT (task/994).** Identity lives in ONE layer, `helm/actors.py`, and
every actuator hooks into it. It answers with an OPAQUE CAPABILITY, not a name,
and refuses in one pass when the identity is MALFORMED (a hostile
HELM_CHAT_NAME), DISPUTED (the declared name and the session's roster row name
different seats), DERIVED (nobody declared it and the roster never bound it —
a name MINTED from session+cwd), or MIS-ASSERTED (`--seat` named a seat this
process is not).

SPEECH IS NOT AN ACT, and that line decides which verbs refuse. A plain room
`post`/`reply`/`react` is SPEECH: the row is read, and nothing about it says
another party did, owes, or saw anything — `helm chat seats`' auto-name exists
so an un-named join can say its first word, and that keeps working.

The verbs that now REFUSE rather than acting under a minted name are the ones
whose record ANOTHER PARTY reads as testimony: `dm` (including `post --dm`),
`ack`, `claim` and `helm work claim`, `catchup --apply`, `wait --follow` with
no `--seat` (arming a beacon consumes an inbox), the council acts, and the
stop-guard's auto-claim rung. Each refusal names the fix: export
`HELM_CHAT_NAME`, or bind the session with `helm chat join`.

`wait`/`deliver` WITH `--seat <someone-else>` used to be waved through for a
process that declared no name of its own — the guard refused the named case and
opened for the one that had proven nothing, so an unnamed shell could arm a
seat's beacon and drain its cursor in silence. Acting for another seat is now
STATED: add `--on-behalf` (a supervisor arming a beacon for a seat it is
standing up), and the run says so loudly on stderr. A process that DOES declare
an identity still cannot name another seat — `--on-behalf` is not a way around
that, because a named process is not acting on behalf of anyone, it is lying
about who it is.

TWO THINGS DELIBERATELY DO NOT REFUSE, because their authority is not a name.
`release` is authorized by the LEASE TOKEN — `_binding_ok` requires {lease,
holder, granting session} together and only the holder was handed the nonce, so
presenting it is the proof and `--seat` merely addresses the holder row. And
`status` never refuses the write; it WITHHOLDS the attribution, because `by` is
recorded and a minted name there credits a stranger, while refusing the verb
would lock a live seat out of its own status line.

A MALFORMED name is refused at every tier, speech included — a hostile
`HELM_CHAT_NAME` is no name, and it must not reach a row by falling through to
the derived floor.

The verbs that DO NOT ask who you are, deliberately: every read
(`read`, `seats`, `claims`, `pending`, `helm work list|peek|gc|stash|
install-guard`) and RECOVERY — `helm work release --stale` takes no identity at
all, because its authority is a liveness PROOF about the dead holder, not the
caller's name. An operator with no `HELM_CHAT_NAME` can still read everything
and still unwedge a fleet.

`--seat` IS AN ASSERTION EVERYWHERE, never a selector. It may state who this
process believes it is and be refused for being wrong; it may never name a
seat into being. `helm chat catchup --seat <someone-else> --apply` and
`helm chat wait --follow --seat <someone-else>` used to go through and are now
refused.

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
Protocol lineage: a predecessor's meld script and skill (epoch fencing, floor
markers, bounds-as-behavior — the scars are kept, the channel is helm's).

NAMING (premise council-is-the-number-one-feature, owner canon 2026-07-23):
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
  clip can never eat the join instruction. The invite is a
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
- **`meld say <room> --marker YIELD|HOLD|DONE|ABORT [<text...>]`** — one
  bounded chunk, content + floor marker in the one text field. WITH NO TEXT
  THE CHUNK COMES FROM STDIN, the same door `post` and `dm` use, and that is
  the safe route for prose carrying backticks or `$( )` — the shell expands
  an argv body before helm ever sees it, and this verb is in the argv guard's
  body-verb table precisely because meld chunks are prose written fast:

  ```
  helm chat meld say <room> --marker YIELD <<'EOF'
  ...chunk...
  EOF
  ```

  Stdin is read only when something is waiting on it, so a scripted caller
  with no body gets usage rather than a hang; a positional body AND a piped
  one together is a refusal, never a silent discard of one. `[YIELD]`
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
predecessor meld's host-global-state clobber, refuted in its live dogfood, is
structural here). The pinned PAIR is now a pinned SET: `invite` takes one
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

### `helm launch [--seat S] [--home H] [--room R] [--model M] [--no-install] [--] [claude args…]`
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

**Before the exec, a launch onto a named credhome passes the Orca sync** — the
same function as `helm cred sync-orca --apply`, whether the home came from
`--home` or from an inherited `CLAUDE_CONFIG_DIR` (the pin `helm seat spawn`
puts in a native pane's command). It prints one line: `FRESH` / `NO-ORCA-COPY`
/ `OWN-CHAIN` with the account, the sync it performed with the pre-image path
and the shared-chain warning (do not switch Orca to that account while the seat
runs), a stale home it could NOT sync and why, or `UNKNOWN` ("launching on the
home as-is, NOT synced"). A `DISAGREE` identity and a `CHAIN-UNPROVEN` home are
REFUSED before the roster row is written: exit 1, no session. `HELM_CREDHOME_ORCA_SYNC=0` skips it and says so.
`helm sessions resume --go` runs the same sync for a claude row pinned to a
credhome.

**A `--home` launch also ensures the home's `skills -> hub` link before the
exec** — the same primitive that links a seat config dir at mint and a
credhome at `helm homes prepare`. A missing link is created and said
(`skills linked: H/skills -> <hub>`); a correct one is silent; a link that
points ELSEWHERE is named with the normalizer and left exactly as found
(`H/skills -> X is NOT the skills hub — left untouched; ... helm skills sync
--apply`), and a REAL `skills` dir is left whole. A hub that is configured
but cannot be used is said (`skills hub UNAVAILABLE for H (<path> is
configured but MISSING)`). The session still launches in every case — told
what it will not see rather than silently seeing no skills. No hub
configured is no line.

`--model M` is carried onto claude's argv unless the passed-through args already
carry `--model`. With no model at all, the launch names the model the home's
`settings.json` will select (read, never written), so a seat that lands on a
settings default is visible at launch.

### `helm ownership census`

Re-derive, from BOTH ledgers at the moment of the call, who still holds open
rows and whether that seat is actually working. **READ-ONLY — this verb
reverts nothing.**

**A ROW IS HELD BY LIVENESS, NOT BY A NAME.** A dispatch `recipient` and a
task `owner` are bare seat names, and a name does not stop being a name when
the seat behind it dies. `DARK` means no TERMINAL RESPONSE inside the
threshold **and** no live descendant work, and both halves are load-bearing: a
seat waiting 34 minutes on a remote gate has emitted no terminal response and
is emphatically working, so the second conjunct is what makes the first safe
to act on.

**NO SOURCE CLAIMS THE HARNESS ACCEPTED A STOP, BECAUSE NONE CAN.** A plugin
`Stop` hook registered outside helm's dispatcher vetoes after every helm
handler has allowed, and helm sees its own dispatch and nothing after it. So
the strongest honest evidence is a TERMINAL RESPONSE — the last assistant
record whose `stop_reason` is `end_turn`, read from the seat's own session
transcript, bound by the session id in the FILENAME (never newest-by-mtime,
which on this box is another seat's turn) and timestamped from the record
itself. The one failure schedule it cannot distinguish is written on
`helm/turnresponse.py`: a hook vetoes and the harness then stalls before
appending the continuation, leaving a stale terminal record. A seat in that
state older than the threshold reads DARK, which is correct for a harness that
died, and younger than it reads HOLDING for at most the threshold.

**A RECORD TOO YOUNG TO HAVE SETTLED IS `PENDING`,** and reads UNKNOWN rather
than HOLDING or DARK: a Stop hook may still be running and about to veto. The
bound is the LONGEST Stop-hook timeout configured in the seat's own launch
settings, read rather than hardcoded, so a seat that raises its timeout is not
certified early by a constant nobody updated.

**A BOUNDED SCAN ANSWERS FROM THE HORIZON OR NOT AT ALL.** Transcripts reach
tens of megabytes, so only the tail is read. A window holding no terminal
record whose OWN OLDEST RECORD already predates the threshold has established
absence over that threshold, and the unscanned remainder cannot change it. A
window too short in wall-clock terms reports UNKNOWN and names the span it
did reach — a bound that skips work never produces a confident answer.

**A BEACON IS NEVER SUFFICIENT ON ITS OWN** and appears only as evidence in
the report. One seat read COVERED while 11.2 hours silent behind a proxy
cooldown and held 23 rows; a beacon-keyed lease would leave every one of them
there forever. The deciding predicate takes no beacon argument at all, so
there is no parameter through which one could keep a row.

**UNKNOWN NEVER REVERTS.** Every read that cannot answer — an unreadable
roster, an unreadable claims file, a descendant probe that raises or answers
None — returns `UNKNOWN` rather than `DARK`, because judging a live seat dead
gives one piece of work two owners, while leaving a dead seat's row costs
delay on an advisory surface.

**EVERY EXCLUSION PRINTS ITS OWN SIZE.** `HELD` dispatch rows are SET ASIDE,
never judged: a HELD row was parked by a seat that stated a named external
dependency, so an absent owner there is the documented condition rather than a
symptom. An aside category with no rows still prints its zero, so a reader can
tell an empty category from one nobody looked at. Rows naming nobody at all
are counted too, and `account()` lets a caller assert the columns cover the
whole file.

**THE THRESHOLD IS AN UNCALIBRATED PLACEHOLDER, AND THE TRIPWIRE SAYS SO.**
The two-hour constant was first derived from a bimodal `last_seen`
distribution — busiest live seat minutes quiet, quietest dark seat hours,
nothing between. **THAT DERIVATION IS WITHDRAWN:** every "minutes quiet"
reading belonged to a seat whose BEACON was polling, so the distribution
described beacon state rather than seat activity and the constant was
calibrated against the wrong population. It is kept as a deliberately
conservative placeholder, chosen for the failure that costs less, and it must
be RE-DERIVED against terminal-response evidence before any caller uses this
predicate to WRITE. The tripwire therefore reports which clock it is actually
reading: a distribution from a corroboration-only source certifies nothing
about the line and says NOT APPLICABLE rather than "clear".

Seat names are laundered through `seats_common._seat_label` at one door
before rendering: a roster key is unvalidated at the join seam, and the
tripwire is the most prominent column on the surface.

### `helm work claim|release|gc|list|install-guard`

**`claim` checks the project's light first.** A new lane in a project whose authored light is `red` is refused before anything is checked out; `yellow` claims and prints the reason; a renewal (`--lease`) is never refused. See `helm projects state`.

In-cave git coordination (design: the maintainer's tree + the hotel front
desk): the shared checkout is the **integrator's tree**; every other seat works
in a private room
`<repo>-wt/<lane>` on branch `lane/<lane>`. Every leg is a reuse — the lease
IS `helm chat claim` (resource `worktree:<proj>:<lane>`, nonce = the release
confirmation token, 4h TTL, the stop-guard already refuses a stop with a lease
still held), the
registry IS `git worktree list --porcelain` ⋈ `.claims.json` computed at
read time (ZERO new state files), and `git worktree lock --reason
lease:<id8>` is the git-native do-not-disturb. `[--repo PATH] [--seat S]`
everywhere; from inside a repo the root resolves itself.

VALUES COME FROM THE NEXT TOKEN AND MAY NOT BEGIN WITH A DASH, and BOTH
departures are REFUSED rather than accepted-and-dropped. These verbs read a
valued flag by an exact token match, so `--ttl=30` is a token nothing looks
for; and the reader refuses a dash-leading next token on purpose, so that
`--seat --apply` cannot mint a seat called `--apply`. Either way the flag
would read as ABSENT — the lease would carry a default the caller believes
they set, and the call would still exit 0 — so the guard refuses both shapes
at the door and names the spelling that reaches the reader.

`--superseded` is the exception in both directions, and for one reason: it is
the only flag whose reader parses `--superseded=REASON`, which is the escape
for a reason that legitimately begins with a dash. Because that escape exists,
`release --superseded -dash` is answered by the verb's own guard, which names
it — a better sentence than a generic unknown-flag refusal, which cannot know
which flag was starved — so that one shape is left to it.

- **`helm work claim <lane> [--ttl N] [--lease ID]`** — check-in: lease
  first (ADMITTED ACTOR required — a lane lease durably assigns responsibility
  and can strand another worker, so a name minted from session+cwd is refused;
  export `HELM_CHAT_NAME` or `helm chat join`. `--seat` ASSERTS that identity,
  it never selects one. The read-only verbs below need no identity at all),
  (held by someone else = the existing refusal, pick another lane),
  then the room (`worktree add -b lane/<lane>`; a registered room is reused,
  a parked branch re-opens; `--lease` extends — idempotent re-entry). Prints
  ONE machine line: `path<TAB>branch<TAB>lease<TAB>ttl` — keep the lease
  (`helm work list` reprints it if you lose it). `--ttl` is seconds, bare or
  with ONE unit suffix `s`, `m`, `h` or `d` (`3600`, `3600s`, `90m`, `4h`,
  `1d`); without it the lease runs 4h. Zero, a sign, a fraction, an empty or
  missing value, any other suffix, and a length past any instant the host can
  express are REFUSED with rc 2 and one stderr line naming these forms,
  before an identity or a room is touched — never defaulted, never rounded.
  `helm chat claim` and `helm multiplayer presence` read `--ttl` through the
  same reader (`seats_common.ttl_flag`), each with its own default.
  A renewal (`--lease`) takes the same forms.
- **`helm work release [<lane>] --lease ID [--park] [--superseded REASON] |
  --stale`** — checkout at
  the desk. `--lease` needs the same admitted actor `claim` does; `--stale`
  needs NO identity, because its authority is a liveness proof about the dead
  holder rather than the caller's name — recovery must work for an operator
  who declares none. The existing grant is strictly refreshed before any room mutation, so
  a wrong or expired confirmation token cannot even trigger a `--park` commit.
  DIRTY still has exactly two exits — commit and re-run, or `--park`
  (WIP-commit onto the lane branch; nothing is ever discarded BY THIS
  ACTUATOR). The scope matters: helm's worktree lifecycle never discards, and
  helm is not the only thing that removes rooms. The metaharness has its own
  worktree reaper, the two do not share a lock or consult each other, and a
  room orca removes was never offered to helm's rescue path. The settings
  seam has the same shape: helm and orca both edit Claude's settings file,
  and an advisory lock owned by one product cannot bind the other. Releasing
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
  room.
  `--superseded REASON` is the THIRD STATE, for work that will never land
  DELIBERATELY — a class solved independently and more strongly on trunk, or a
  lane whose one surviving finding is declined by canon. Landed and not-landed
  were the only two answers, so such a room stood forever on a predicate
  nobody had asked the right question. It is narrower than it sounds: TWO ACTS
  hide behind the landedness proof — removing the ROOM, which costs one `helm
  work claim` to rebuild, and DELETING THE BRANCH, which is what can put
  commits beyond reach. This opens only the first. The room retires, the
  BRANCH IS KEPT with every commit still reachable on it, and the line carries
  the landedness evidence that did NOT authorize the retirement beside the
  stated reason, so an assertion can never be misread later as a proof. It
  therefore needs no landedness proof: it never touches what that proof
  protects. Every earlier guard still refuses first — a DIRTY room refuses the
  assertion exactly as it refuses a proof, and the reason is validated before
  anything mutates. A reason that starts with a dash needs the
  `--superseded=REASON` spelling; a typed flag with no value REFUSES rather
  than falling back to an ordinary release. On a lane Git PROVES landed the
  flag is not used and the line says so, so nobody learns to reach for an
  assertion where a proof exists. `--stale` and `--superseded` refuse each
  other: one is a liveness proof about a dead holder, the other is a live
  caller's evidence about the work. Lost the lease id? The refusal names the surface, and `helm work list`
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
  ahead/behind the base, lock, path (registry ⋈ claims, computed). A HELD
  room whose committed work is already on the trunk prints a `LANDED — <proof>;
  the lease is still held` line under its row with the exact release command
  (the token for your own lease; for another seat's, where its holder reads
  it), because a land releases no lease and `+0/-N` left the reader to infer
  it. The verdict is `work.lanes_landed` — the same one the web board's
  kanban reads: `_merge_state` (the proof `helm work release` retires on:
  ancestry, or patch identity) bound to the resolved tip and trunk shas, plus
  the lane's reflog biography, because a lane claimed a minute ago sits AT
  the trunk exactly as a merged one does and has authored nothing. The
  biography is read for what the lane WROTE and whether its tip still carries
  any of it: a fast-forward of the trunk in writes nothing (unstarted), and a
  branch RESET back to the trunk after committing is judged by its newest
  dropped commit — on the trunk under any sha and it landed before the
  reset; on neither and it is UNLANDED, because that work is only in the
  reflog, which a release deletes. It is kept per lane by a stat stamp of the
  refs it read (both spellings of the base, since a remote appearing flips
  the trunk's name), so an unmoved repository is answered with no git call.
  A LANDED row whose room is dirty says so (`the room is DIRTY (commit or
  --park first)`); the web board asks the same reader for its landed lanes
  (`work.rooms_dirty`, one `git status` per landed room) and keeps such a
  lane in its building column too.
- **`helm work install-guard [--apply] [--profile rail|leak]`** — two
  profiles, recorded in the repo's git config as `helm.guard.profile` so
  install drift and `helm doctor` judge the repo by what it declared. WITHOUT
  `--profile` THE TARGET RESOLVES IN THREE STEPS, in this order: (1) what the
  repo DECLARED; (2) else what its installed hooks are RUNNING, so a repo put
  under a profile before it recorded anything stays under the one it is
  running; (3) else the default its own identity carries — `leak` in a project
  repo, `rail` only in a checkout of helm's own source — which is the FRESH
  repo's answer, for a repo with no hooks and no declaration. The rail is
  helm-the-repo's shared-checkout law, so it is opt-in everywhere else — a team
  USING helm never has to know how helm is made. AND A FLAGLESS INSTALL NEVER
  NARROWS: if the resolved target would retire hook slots or scanner snapshots
  the running profile uses, it REFUSES — exit 1, before the first byte — naming
  the installed profile, the target, and the exact `--profile` command that
  narrows on purpose. An explicit `--profile leak` on a rail repo is the
  operator's decision and is allowed, with its retirement notes. Every printed
  remedy — the `helm work claim` staleness notice, `helm work gc`, `helm
  doctor`, and the SKIPPED lines inside the generated hooks themselves —
  carries `--profile <resolved>`, so a stale hook's own advice cannot narrow
  the repo that runs it — and the profile it carries is never the one that
  would narrow: when the declaration disagrees with the hooks on disk in the
  narrowing direction (declares `leak`, runs the rail), the remedy names the
  profile the repo is RUNNING, says the declaration disagrees, and names
  `--profile leak` as the door that narrows on purpose, so following the
  tool's own advice keeps every rung armed and re-records the declaration to
  match. A declaration that widens what is running stands, as it does for the
  flagless verb. The profile a repo is
  moving OFF is a separate question with a separate answer: what it recorded,
  and when it recorded nothing, the artifacts actually on disk (any hook slot,
  composed `pre-commit` body or scanner snapshot that only a rail install
  writes counts as rail). Switching retires every hook slot and scanner
  snapshot the outgoing profile ran and the incoming one does not, and the
  install note names the switch and what departed. A declaration
  short-circuits that observation; with nothing declared and the disk
  unreadable the install REFUSES — exit 1, before the first byte is written,
  nothing recorded and nothing retired — because recording a profile over
  hooks nobody could read certifies a law against whatever is still armed. The
  refusal names the exact path to make readable and says that every retry
  refuses until it is: `--profile` names the INCOMING profile and does not
  settle the outgoing history, so it does not recover that state, while
  recording `helm.guard.profile` yourself does. Every repository identity is
  accepted in any of its three
  spellings — a checkout root, a worktree root, or either one's gitdir — and
  normalized to the checkout root before it is classified, so a store that
  keeps a repo as its gitdir (a land close reading guard drift, for one) judges
  the same repo the same way; two separate clones remain two repositories. An
  identity that names no working tree git can reach — a separate gitdir that
  records nothing pointing back at its checkout, a bare mirror — is classified
  as NEITHER profile: an undeclared install there refuses instead of deriving a
  law from a tree nobody can read (`--profile` decides it), drift reports
  UNKNOWN rather than STALE, and no land close treats that as drift.
  `leak` is the legs EVERY repo owes regardless of process — the
  `pre-commit` conflict-marker rung and never-track staged-set scan
  (bulk-data shapes: exports, dumps, snapshots, archives, address crowds —
  addresses at RFC 2606/6761 reserved names such as example.com or `.test`
  do not count toward one — blobs over 1 MiB; private needles), each with its own skip and each warning
  rather than disarming the other when its snapshot is missing, the
  `pre-merge-commit` merge-result scan described below, and
  the `pre-push` host-path scan — for project repos that are
  not run on the shared-checkout rail, where the rail's venue and citation
  rungs would refuse ordinary work. A `leak` install needs no particular
  branch checked out (the base-branch precondition is the rail's), and
  switching profiles is a transition: hooks the old profile planned and
  the new one does not are retired in the same transaction (a user hook the
  rail had wrapped in as `<name>.helm-user` is restored to its name, bytes
  and mode intact, so nothing of the user's goes dark), the profile
  is written last so a rolled-back install leaves the declaration true,
  and the bulk-data override counts only from a STAGED `.gitattributes`
  (never `info/attributes` or a global file): `helm-bulk=ok` waives every
  bulk finding for the path, `helm-bulk=quotes` waives only the export/dump
  self-declaration (source that quotes a header) and leaves the ceiling,
  the magic bytes and the address count judging. `rail` (the default only in a
  checkout of helm's own source, and opt-in by `--profile rail` anywhere else)
  is the deterministic
  rail: six
  composed hooks — `reference-transaction` + `post-checkout` + `pre-commit` +
  `pre-merge-commit` + `pre-push` + `commit-msg` — in Git's effective
  repo-local hooks directory. A MERGE COMMIT NEVER REACHES `pre-commit`: git
  builds the commit of a merge it completes itself (`git merge --no-ff`, or any
  non-fast-forward merge without conflicts) through `pre-merge-commit`, so both
  profiles install that hook, and it runs the refusing conflict-marker rung and
  then the never-track scan (each with its own skip,
  `HELM_CONFLICT_MARKER_SKIP=1` / `HELM_NEVER_TRACK_SKIP=1`) over the merge
  result staged in the index. Git writes `MERGE_HEAD` only after that hook
  passes, so what the merge ADDS is measured against `HEAD`, the target branch:
  a blob the target branch already tracks is never re-flagged by a later
  merge, and a blob only the merged branch carries is refused, the merge is
  not committed and `HEAD` does not move. A fast-forward creates no commit and
  runs no hook; `--no-verify` skips it. A merge git does NOT commit itself
  (`git merge --no-commit`, `git pull --no-commit`, any conflicted merge, and
  a merge the merge door refused, which git leaves in progress) is concluded
  with `git commit` and reaches `pre-commit` with `MERGE_HEAD` present. THE
  TWO DOORS AGREE ON BULK: there too a blob's size and bulk shape are judged
  against `HEAD`, the first parent, so a blob over the size line that `HEAD`
  did not already carry at that path is refused by both doors with the same
  answer, and a legitimate one (a trunk fold that grows a landed file past
  the line) is declared `helm-bulk=ok`; a path `HEAD` already carries over
  the line is refused by neither door. ON THOSE TWO LEAK RUNGS THE DOORS
  DIFFER ON NEEDLES, CONFLICT MARKERS AND E-MAIL ADDRESSES, and the merge
  door's refusal says so: the
  merge door judges them against `HEAD` alone, while `pre-commit` keeps the
  every-parent law below, so content a merged parent already carries is
  admitted by `git merge --no-commit` then `git commit` (or `git commit` on
  the refused merge) — the recovery route for a fold of history that already
  holds it — and content no parent carries is refused there too. Nothing is
  recorded between the two doors. THE MERGE DOOR RUNS ONLY THOSE TWO RUNGS:
  under the rail profile `pre-commit` also runs the refusing `lane-discipline`
  (which admits a commit carrying `MERGE_HEAD`), `in-flight-gate`,
  `seat-name`, `docref` (citation), `world-prose`, `retired-name` and
  `split-budget` rungs and the WARN-only `silent-cap` advisory, so
  a merge git commits itself never meets them, while the same merge concluded
  with `git commit` can be refused by one of them. Under the leak profile both
  doors run the same two rungs. The transaction
  hook refuses shared-checkout branch creation, switch/detach,
  non-fast-forward trunk movement, and plumbing mutation/deletion of a branch
  occupied by another registered worktree **before any ref changes**. The
  checkout hook remains a pointer-only safety net for an otherwise-created
  branch. The pre-commit hook runs four rungs in a fixed order, and each
  rung's skip or absence isolates to itself — one law's bypass never disarms
  a sibling: first the WARN-only vacuous-assertion
  advisory over staged Python test functions (`helm/vacuous_assertion.py`),
  then the WARN-only silent-cap advisory over staged Python added under
  `helm/` (`helm/silent_cap.py`: a slice or a `*_CAP`/`*_MAX`/`*_LIMIT` bound
  whose result reaches a serializing, file-writing, ledger-appending or
  publishing call with nothing in the written value naming the loss, so every
  reader downstream holds a short value indistinguishable from a whole one;
  an identifier prefix, a bound under four items and a `print` are not cuts;
  `tests/`, `docs/` and `journal/` are exempt by path identity; the escape is
  `# noqa: SILENT_CAP — <reason>` on the slicing line, reason required — it
  reports and never refuses, and its banner carries the measured rate behind
  that choice: classified by hand over the whole committed `helm/` population,
  13 of 21 findings were true unmarked losses, 6 rendered an identity or a
  label short inside a human line, and 2 cut nothing at all. That rate is a
  property of the classification pass, not a running count: all thirteen true
  losses now mark their cuts, so the census reports the display-only and
  not-a-cut rows alone, and each cured writer is a production negative
  control in `tests/test_silent_cap.py`. The per-hit table behind those
  counts lives in the rung's own source beside the constants, so the rate and
  its evidence have one owner),
  then the REFUSING conflict-marker rung (`helm/conflict_marker.py`: staged
  merge-conflict marker lines — column-0 `<`/`>`/diff3-`|` runs at the
  per-path `conflict-marker-size` attribute length, default seven, on sight;
  the lonely equals separator only when bracketed by an opener above and a
  closer below, so RST/markdown heading underlines pass; diff-aware and
  rename-aware, so a marker ANY PARENT already carries is named but never
  blocked, a pure rename included; staged BYTES are read by index OID with literal
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
  in the blobs the push adds: those not reachable from a branch or tag that
  `git ls-remote` on the push URL advertises, never from a tracking ref, and
  only when the push provably goes there (no custom receive-pack, pushurl or
  pushInsteadOf, and a readable `git push` argv); every reachable blob
  otherwise, or when that advertisement cannot be read or is empty. Its
  diagnostics name a configured remote or "a URL remote" and never print a
  URL, a path or git's output; one-push skip `HELM_HOSTPATH_SKIP=1`). It
  reads git's
  outgoing ref lines from stdin ONCE and feeds the same bytes to a composed
  `pre-push.helm-user` (git-lfs uploads by them) and then to the scanner, so a
  user hook that consumes stdin cannot leave the scan an empty,
  nothing-to-push input; a stdin the hook cannot read refuses the push. An
  installed pre-push older than v4 reads STALE until reinstalled. The advisory parses each whole
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
  between `git add` and the commit becoming history (the 2026-07-29 leak
  shipped with a green suite exactly there). The needle half reads the
  commit's DIFF, never the whole file: a needle this commit ADDS refuses, a
  needle already in a PARENT'S copy prints a NOTE that names the file and says
  it needs its own scrub commit. Blocking that second case refused honest work
  for content the commit could neither write nor remove — measured
  2026-07-30, one needle in `helm/web_ui.html`'s history refusing every commit
  to a 3,000-line file until the seat reached for the bypass — and a guard
  that refuses honest work teaches its own bypass flag and then guards
  nothing. A binary staged change has no line structure to read, so the WHOLE
  blob is scanned there: the guard over-blocks rather than under-sees. The
  never-track path prefixes are unconditional — those files must not be
  tracked at all, whatever any diff says. They are the list shipped in
  `helm/nevertrack.py` plus any this machine lists, one prefix per line, in
  `$HELM_NEVER_TRACK_LOCAL` (default `~/.helm/_global/never-track.txt`): the
  place for a path whose name must stay unpublished too, with its ignore
  entry in `.git/info/exclude` rather than the tracked `.gitignore`.
  ADDED BY THIS COMMIT MEANS ABSENT FROM EVERY PARENT — for needles, markers
  and addresses; a blob's size and bulk shape are judged against `HEAD`, as
  above — which is what a merge needs: for a commit with N parents the added
  set is the paths whose result blob differs from that path's blob in EVERY parent, read from `HEAD` plus
  `MERGE_HEAD` (one oid per line, so an octopus needs nothing further). Against
  `HEAD` alone every blob a merge took from its other side read as this
  commit's — measured 2026-09-14 on a real lane, where merging the trunk was
  refused naming 14 files as carrying needles plus 30 addresses across 17
  files, all 25 byte-identical to the trunk's blobs, and the lane used
  `HELM_NEVER_TRACK_SKIP=1` over a pre-existing true positive. The same law
  and the same `commit_parents` reading now serve the conflict-marker rung,
  the split-budget rung and the citation rung; a single-parent commit is
  measured exactly as before. Install snapshots every scanner
  file beside the shared hook and bakes those stable paths into the scripts;
  installing from a lane never makes that editable/disposable lane the live
  detector. A LINKED WORKTREE IS NOT A BLIND SPOT for any of this: git answers
  `--git-path MERGE_HEAD` with an absolute path into the linked gitdir and
  sets `GIT_DIR` there for hooks, so a hand-resolved merge made in a
  `git worktree add` room reads its own two parents. A missing installed
  snapshot fails OPEN but LOUD, and a snapshot whose SOURCE has moved on since
  install warns at the commit, naming both files and the refresh: the scanner
  is frozen at install time, so a cure that lands never reaches a repo armed
  before it, and until 2026-09-14 nothing said so where the refusal happened —
  one project repo refused a merge for six files its second parent carried,
  three weeks after that was fixed, and the refusal was filed as a fresh defect
  in a producer that measures correct. `helm doctor` reads the same compare
  (`stale_guard_hooks`) across the registry; the hook asks it where the commit
  is. It never refuses over its own housekeeping, and it says nothing when the
  installing checkout is gone — `helm doctor` owns that case. `HELM_NEVER_TRACK_SKIP=1` skips the scan for one commit — an owner
  decision for false positives, not a lane convenience. Lane rooms may
  branch normally; commits on each room's own branch
  remain legal. Existing executable hooks run first and are preserved
  byte-for-byte (including symlinks and executable mode) as `.helm-user`
  companions; non-executable hooks stay inactive. That contract covers a
  USER'S hook. A companion carrying a helm marker is a superseded copy of
  this project's own guard, and since the managed hook invokes the companion
  first, its body would answer ahead of the installed rules — so `--apply`
  retires it to `<hook>.helm-superseded`, non-executable and byte-identical,
  and refuses if that destination already holds different bytes. Installation is serialized,
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

### `helm asks add <text> --needs "<what only the owner can supply>" [--kind owner-input|usage-reset] [--source S] | done <id> <evidence> | report <id> <chat-post-id> | list [--open] [--json]`
The OWNER-ASK LEDGER — the durable fix for dropped owner asks (root cause:
asks lived in memory-only panes + siloed scratch, and report-back went to
AGENTS, not the owner). One append-only jsonl at
`~/.helm/_global/owner-asks.jsonl`: every mutation appends a full snapshot
row `{id, ts, ask, source, hold_kind, status, done_ref, report_ref, last_updated}`
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

### `helm task add <title...> [--owner SEAT | --mine] [--note N] [--ref R]... [--id NNN] [--owner-asked] [--posture-na REASON] [--priority P0|P1|P2|P3] [--project NAME] | list [--all] [--all-projects] [--project NAME] [--owner SEAT] [--json] | triage [--apply] [--limit N] [--legacy] [--project NAME] | show <id> [--json] | resolve <token> [--json] | claim <id> [--owner SEAT] | takeover <id> --from-lane L --transfer-id ID [--superseding] | update <id> [--title T] [--note N] [--owner S] [--status S] [--origin owner|agent] [--ref R]... [--priority V] | close <id> <reason...> | comment <id> <text...>`
The FLEET TASK LEDGER — shared work items with real ids every seat can
resolve. One append-only jsonl at `~/.helm/_global/tasks.jsonl`, same
event-sourced shape as the owner ledgers: every mutation appends a full
snapshot `{id, ts, last_updated, title, status, owner, note, refs, source,
origin, closed_reason, comments, takeover?}` and the last line per id wins.
`takeover` is present only on an evidence-bound BUILD continuation and lands in
the same snapshot as its owner change.

**SCOPE: `--project NAME` WINS OVER cwd (task/2446).** `add`, `list` and
`triage` are the verbs where cwd would otherwise decide, and each of them
accepts `--project <registered name>`; the flag beats cwd, an unregistered
name is REFUSED naming the registry, and every scoped door prints which scope
it used AND how it decided (`from --project, which wins over cwd` vs
`from cwd; pass --project=NAME to override`) on one line. THE VALUE IS THE ONE
TOKEN BESIDE THE FLAG: `--project --apply NAME`, `--project --limit 1 NAME
--apply` and `--owner --project NAME seat-a` are REFUSED as a missing value
(nothing read, selected or written) rather than resolved by reaching across the
intervening flag, and `--project=NAME` is the spelling that carries a name
beginning with a dash — which is also the form `triage`'s dry preview
advertises, shell-quoted, so the command it tells you to copy parses.
Measured from a
client project's checkout: `helm task add` printed "scoping to project
'<that project>' (from cwd)" for a task filed ABOUT HELM, so the row landed
where the making team's `helm task list` does not look. A team USING helm must never need to stand in
helm's own checkout to report something about helm. `update`, `close` and
`show` resolve a FLEET-WIDE id and read no scope at all — they REFUSE the flag
with that sentence rather than accept it as a no-op. Moving an already-filed
row between projects is not any of these doors.

**THE POSTURE GUARD (`helm/posture.py`, task/1346) stands at this door and
at `helm dispatch send`.** A title-plus-note or a dispatch body that applies
a STRATEGY VERB (patch, fork, upstream, PR, rebuild, pin, vendor, wrap, shim,
subtree) to a NAMED DEPENDENCY (orca, herdr, dregg, cv, cliproxy, claude
code, codex cli — and the generic words upstream/fork under a seam-changing
verb) — the two adjacent, at most one connector word between, "an orca
rebuild", "patch dregg", "upstream PR to orca" — is REFUSED at exit 2 with
three questions: HORIZON (what does the next upgrade / reboot / crash /
release do to this?), OWNERSHIP (who keeps this seam aligned, at what
cadence, does that happen today?), WHO-CARES (does the other party want
this from us?). Two escapes, both recorded: a `posture:` clause in the body
naming all three labels (free text, case-insensitive), or `--posture-na
REASON`, written to the row as `posture_na` and shown by `task show`.
`--owner-asked` rows are the owner's own words and are exempt, and so is a
born-closed tombstone (history, not work). The guard lives in the
module-level `tasks.add`, `dispatches.send` and `dispatches.add` — every
caller passes it, the CLI only forwards the escapes. Measured
read-only over the live ledgers when it was built: 9 of 1,308 task ids and 0
dispatch notes trip it; task/1345 (the specimen fork-plus-PR row) fires,
task/1342-1344 do not.

**IDS KEEP THE NUMBER THEY WERE BORN WITH.** A row is `task/<n>`, so the
week of chat rows citing `#263` resolves BY CONSTRUCTION — no lookup table
and no alias verb. `resolve` takes any spelling a seat would type (`263`,
`#263`, `task/263`, `task-263`) and answers UNPARSEABLE (rc 2) separately
from ABSENT (rc 1), because a typo and a number nobody filed are different
questions. New rows CONTINUE that sequence rather than forking a second one,
minted under the ledger lock so two seats filing in the same instant cannot
put two different works behind one citation.

**A DASH VALUE NEEDS THE `=` SPELLING, AND THAT IS THE ONLY ESCAPE.** The
space form refuses a flag-shaped value — `--note --owner cj` once set the note
to `"--owner"` AND ate the owner, one typo producing two wrong fields at rc 0 —
so `--note -5` and `--note --force` are refused as probable typos. The `=`
form is authoritative and its value is always literal: `--note=-5`,
`--note=--force`, `--owner=cj`, `--id=904`, `--ref=#218`. `--` is a DIFFERENT
escape covering the TITLE only (`task add -- --a dash title`); it never reaches
a flag value, and the earlier claim that it did was measured false — before the
`=` form existed there was no spelling at all for a note reading `-5`, and
`--note=--force` silently landed the whole token in the row's NAME at rc 0.
Both spellings run through `tasks._take`, so `update` behaves identically.

**RANK IS A FIELD, A GLYPH, AN ORDER AND A SWEEP — and it was only ever the
first of those.** `--priority P0|P1|P2|P3` has existed since the free-text
census (40 of 204 open rows once carried a rank in their TITLE, in seven
incompatible spellings), and nothing rendered it, nothing swept it, and the
owner's board sorted by id. Measured when this was cured: 403 of the helm
project's 440 open rows carried no rank. Now the listing prints the rank in
its own column, `/api/tasks` sends it and orders the owner's card P0-first
through `tasks.board_key`, and `helm task triage` is the sweep.

**AND INSIDE A RANK THE ORDER IS OLDEST FIRST** (`tasks.board_key`, task/2622).
`rank_key` falls through to THE NUMBERING, which is filing order only for rows
this ledger minted — a migrated row keeps the number it was born with — so a P1
filed this morning could sit above a P1 that has been waiting since the private
list. The owner asked how he is supposed to cross-check the ordering of a
477-row backlog; AGE is the part of that order he can check without reading a
single row of prose. A row whose stamp will not read sorts AFTER the dated rows
in its rank rather than claiming to be the oldest: `tasks.stamp_epoch` answers
None, never 0, so an unreadable stamp cannot take the top of the board.
`rank_key` and `sort_key` are unchanged and every reader of them keeps what they
had; this is a third question, asked for by name.

**AND THE ORDERING IS PUBLISHED AS AN ORDERING** (`tasks.board_order`), not only
as a key. `helm task list` and `/api/tasks` both call it, so ONE snapshot comes
out of the terminal and out of the browser as ONE list — a published key alone
is an invitation each caller answers its own way, and that is how the two doors
came to sort the same rows two ways while the owner was cross-checking one
against the other.

**`add` RESOLVES THE TITLE AGAINST THE OPEN BACKLOG BEFORE IT FILES.** The
owner's other question on the same morning: "how do we prevent me continually
asking for things that have been placed on the list and ignored." 477 open rows
cannot be scanned by whoever is filing row 478, so the door does it —
`near_duplicates` scores the new title's words against every OPEN row in scope
and `duplicate_verdict` is the ONE decision: `distinct`, `duplicate`, or
`unknown`. A `duplicate` past `DUP_OVERLAP` is REFUSED and the refusal NAMES the
closest three with their id, owner and score, because "go comment on task/NNN"
is the move that actually stops a request being re-asked.

**`unknown` IS ITS OWN ANSWER AND IT REFUSES TOO.** A title made only of
grammar, of punctuation, or of no string at all has no comparable words, so
whether it is already on the list cannot be resolved — and a guard that opens
whenever it is confused guards nothing. Measured over the live ledger, that
refuses exactly ONE row of 2644, whose title is three dots. A malformed title
refuses with a sentence rather than raising.

**ONE TOKENIZER AND ONE THRESHOLD, IMPORTED.** Both are
`helm/store/index.py`'s — `dup_tokens` and `DUP_OVERLAP`, the repo's canonical
duplicate index — so retuning the store moves this door with it rather than
leaving a twin behind. The split is unicode-aware: the ASCII class it used to
use dropped every non-Latin character, so two byte-identical CJK titles both
tokenized to the empty set and were called distinct. The stoplist is this
field's own (titles are sentences) and it keeps POLARITY WORDS — with "not" in
it, a title and its negation tokenized identically and the door refused the
correction to a row already filed.

**THE GUARD IS IN `add()`, WHICH IS EVERY PRODUCER'S DOOR**, and under the
ledger lock on the same snapshot the write extends — so two concurrent adds of
one title cannot both be filed. A producer that legitimately files over a
near-duplicate passes `force_new=True` explicitly with its reason: `helm task
mirror` carries its own dedup key per row, and the resume-turn recovery row is
keyed by its id rather than by its generated title. `--force-new` is the
command-line spelling of that same bit — the flag `helm drain` already uses —
and forcing is never silent: the override says it fired. CLOSED rows are never
matched (a thing finished in June is history), and the scope is this project's
rows plus the UNSCOPED legacy bucket, the same population the board renders.

**AN UNRANKED ROW IS NOT A P3 AND NOTHING DEFAULTS IT.** Unset means NOBODY
HAS JUDGED THIS, which is a different answer from judged-lowest — the same
absence-versus-value law this ledger keeps for `origin`. So the listing leaves
the column BLANK rather than printing a filler, the card draws no chip, the
order puts UNRANKED after every rank, and `add` neither defaults a value nor
refuses: it FILES the row and says the word, naming `update --priority` and
`triage` as the two cures.

**`triage` IS DRY BY DEFAULT AND REFUSES THE CLAUSE IT CANNOT DERIVE.** It
counts the whole debt (never the `--limit` slice), applies the owner's rule
through `update()` one row at a time so each rank is its own audited event —
owner-asked rows P1, the rest P2 — and NEVER writes a P0, because "this is a
fleet blocker" is a judgement about what a row BLOCKS and no field records it.
The UNSCOPED legacy bucket is opt-in behind `--legacy`: those rows predate the
project axis, and ranking them by a stamp they never carried would retro-stamp
history nobody witnessed. They are COUNTED either way.

**THE RULE TRAVELS TO THE WRITER; THE VALUE NEVER DOES.** A sweep that picks
rows from a snapshot and writes them later is deciding about a row that may
have changed in between — measured: a deliberate P0 written after the preview
was overwritten by the sweep's derived P2, with every event kept and the final
rank the stale one. So `triage --apply` hands `update()` a RULE, and the rule
is resolved under the lock that already protects the row: a row that is now
ranked, closed, or out of scope is SKIPPED BY NAME and nothing is written for
it. The pass reports skips separately from refusals, because "42 skipped" and
"42 failed" ask the operator for opposite things.

**A RANK IS AN ACT WITH AN ACTOR, THROUGH BOTH DOORS.** `update --priority`
and `triage --apply` each resolve an admitted actor through
`helm.actors.resolve_actor` before writing, and the rank event records who
ranked it, whether the rank came from the RULE or by HAND, and what the rank
was before. The row's `source` is untouched: it records who FILED the work,
and relabelling it as who ranked the work is the erasure that field exists to
refuse. An identity that cannot be admitted REFUSES the rank rather than
borrowing the filer's name.

**EVERY SELECTION INPUT FAILS CLOSED BEFORE THE LEDGER IS READ.** An
unresolved project scope is UNKNOWN, not "everything" and not "the unscoped
bucket" — with no scope the scoped and legacy buckets select the same rows, so
one row was proposed and simultaneously reported as not covered, and `--legacy`
listed it twice. `--limit` must be present WITH a positive whole number: a
trailing bare `--limit` parsed as absent and silently selected the whole
backlog, a negative value reached Python's negative slice, and zero reported
the debt as PAID. The debt is read STRICTLY: a corrupt ledger line makes the
debt UNKNOWN, where the tolerant projection skipped it and reported a clean
backlog. And the dry run's advertised write command carries the options the
preview was computed with, so the sentence and the screen describe one
selection.

**FILING IS NOT OWNING.** A row is UNOWNED unless `--owner` or `--mine` says
otherwise, and unowned-and-open is the POOL `offer_rows()` hands an idle
seat — pre-assigning every row would empty that queue by construction. An
OWNED row joins that offer only when its owner is measured absent from the
live roster (the stranded-work rescue, marked `[assigned: seat]`); a live
owner's row is never offered, and an UNREADABLE roster offers the pool alone
rather than guessing who is gone. What
is refused is `in_progress` with no owner: work in progress by nobody is
incoherent, while work nobody has started yet is the normal case. `claim` and
`update --owner` cannot transfer or clear an incumbent, and raw `--force` has no
owner authority.

**TAKEOVER TRANSFERS BUILD CONTINUATION ONLY.** `task takeover` requires a stable
caller-owned `transfer_id`, the source lane, and a distinct current successor
lane. It appends a durable prepare row, sends the incumbent both an idempotent DM
and an idempotent room `@mention` under event ids derived from that transfer id,
then remeasures and compare-and-swaps the task. Any failure before the task
snapshot stays visible in `~/.helm/_global/task-takeovers.jsonl` and transfers
nothing; retries reuse the same chat rows and recover a commit whose final
receipt was interrupted.

Authorization is one fresh same-incumbent bundle: the exact `spawn.json`
occurrence and source worktree, proxywatch's typed turn state plus socket/pending
census, and the complete source-lane claim identity/session/lease/fence/expiry.
Missing, unreadable, malformed, stale, or mismatched input is **UNKNOWN and
REFUSES**. This is deliberately stricter than a diagnostic absence: a dashboard
may omit an unavailable optional signal to remain readable, while this answer
mutates ownership, so cannot-look is never consent and UNKNOWN is not death. No
reply and claim age are not inputs. The admitted measured contradictions are
`hung`, `starved`, `context-full` (proxywatch `compact-needed`), or `idle` only
with pending exactly zero, in-flight exactly zero, and that bound claim expired.
A healthy recent completed turn remains REFUSE however quiet the incumbent is.

WIP stays on the incumbent branch and worktree, untouched; takeover refuses if
the deterministic source room is absent, not a Git worktree, detached, or no
longer attached to its lane branch. The successor must carry the source tip as a
strict descendant commit, or the caller must explicitly
record `--superseding` to link a distinct superseding lane. Takeover never resets,
rebases, deletes, releases, or force-updates the source. Its opaque capability is
accepted only by the task owner boundary for `{owner: successor,
status: in_progress}` plus the durable transfer proof. It cannot satisfy review,
fold, release, land, or any unrelated task-field mutation.

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

**`helm task mirror [--apply] [--all-statuses] [--json] [--ensure-timer]`
carries every agent's harness task list into this ledger, one way.** Without
`--apply` it is a dry run that writes nothing. Each sweep also re-reads the
source item of every row it ever imported (task/1738): a source that is
`completed` (or explicitly `deleted`/`cancelled`) closes an UNTOUCHED row
through the normal close path, with a reason that names the source. A row a
seat claimed, owned, commented, edited or ranked stays open and gets ONE note
from `task-mirror`. A source that is pending, in progress, gone or unreadable
changes nothing, and the dry run prints each count. At most 200 writes happen
per sweep; the rest are counted as deferred and settle on later sweeps. A
closed row is not reopened by `update`; file new work that cites it.

Deliberately carries **NO board projection and NO owner-gate queue**: 120
engineering tasks filed where 3 owner rulings live is the burial
`ownerasks.py` already measured once at 19 rows. A missing ledger is
known-empty; an unreadable one is **UNAVAILABLE — the backlog is UNKNOWN,
not zero** on both `list` (nonzero) and any surface reading it.

### `helm decide file <title> [--asker SEAT] [--ref R]... | list [--open] [--json] | show <id> [--json] | verdict <id> <choice> [--comment T] | deliver <id> | comment <id> <text> | board-sync`
The OWNER DECISION QUEUE — the ask ledger GENERALIZED (owner steer
2026-08-04: the store review queue proved the primitives; decisions should
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
THE PUSH AND THE PUSH NAMES NO CONSOLE.** Owner, 2026-08-06: *"The only
reliable way that we have set up to reach my phone so far is the ntfy
notifications ... other than that on my phone, I just use orca's mobile
app."* helm web binds 127.0.0.1, which on a phone is the phone, so sending
him there was always a dead end — and a standing owner premise
already forbids building remote/mobile plumbing for this UI. The option KEYS
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
as sibling sections (#218) — with a count on the home deck that links
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
too). **Every row names its author** (task/2997), and the owner is never
inferred: every agent runs as his uid, so helm prevents ACCIDENTAL owner
attribution and makes a deliberate one an explicit act. The CLI is never
his door. A seat's `comment` records the acting seat (what `helm chat dm`
admits) and the DM reads `<seat> comment on`, sent as that seat; an actor
that does not resolve is UNKNOWN and writes nothing; `verdict` from the
CLI always refuses and names his doors, the web queue and his phone.
"owner" is written only through an `OwnerDoor` that the web queue's
handlers mint, with the door recorded beside it; the string `owner` is
refused. `deliver` relays a verdict as its recorded author, and `file`
refuses the owner's own name as `--asker`. The argv-guard refuses an
agent command that sends to the local decision endpoints; a same-uid
agent that deliberately bypasses it (a script file) is outside the
threat model. The board's `owner_gated_queue` key is a PRESERVED-PASSTHROUGH
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

### `helm telegram status | send <text> [--reply-key SEAT] | poll [--apply] [--room R]`

The owner's phone as a **two-way** surface — the INBOUND half of the rule
`helm/notify.py` was written to serve.

Fleet alarms delivered only into rooms read by seats can fail when those
readers share an unreachable pane-serving path. Orca panes may span daemon
generations, so no single daemon is assumed
to serve every pane. **An alarm about the fleet being unreachable must not
depend on a fleet member being reachable.** notify.py provides the OUTBOUND
half — the owner can be told. He still needs a way to reach IN without relying
on chat, beacons, panes, or a web console bound to `127.0.0.1` (which on a phone
is the phone). `decide` has the same shape from the other side — it pushes a
card's OPTIONS to his phone and then expects the answer on the web queue he
cannot reach from it. Telegram holds the queue, so his reply survives the fleet
being gone.

**NOT a second push path.** Callers keep using `notify.owner_push`, which fans
out to both transports behind one door; a test pins the set of modules importing
the transport to `notify.py` alone. Transports may multiply behind the door;
paths to the door may not. Only CONFIGURED legs vote on delivery — an opt-out
and a success both look like `True` at a leg, so summing them naively reports a
failed push as delivered whenever the other transport merely happens to be
absent.

**Config is a key name, never a value.** `HELM_TELEGRAM_TOKEN` and
`HELM_TELEGRAM_CHAT_ID`; `configured()` requires BOTH and is the whole public
surface. The token is a capability — anyone holding it can speak AS the fleet to
the owner and READ every message the bot sees — so nothing returns it, prints
it, or puts it in an error string. Unset is a deliberate opt-out that makes no
network call at all. A half-configured bridge answers False rather than pretend:
a token with no chat id can neither deliver nor route, and a caller believing the
outbound half alone raises an alarm nobody receives.

**`poll` is PULL, never a webhook** (this box has no public endpoint and should
not grow one) and it is **two-phase**: read without acknowledging, route, then
acknowledge only what was actually routed, so a crash mid-loop re-delivers
rather than swallows. At-least-once is the right side to err on — a duplicated
reply is visible and annoying, a lost one is invisible, and it is invisible to
the one person who cannot see that it failed. Without `--apply` nothing is
posted AND nothing is acknowledged, so a dry run stays repeatable. Note the
cursor is a single offset and cannot express a hole: an acknowledge clears
everything up to the highest id it is given.

**Routing needs no command syntax.** An outbound message carrying `--reply-key`
gets a footer the owner's own Telegram reply quotes back, so the answer returns
to the seat that asked. Free text with no key posts as the owner — the common
case, because he mostly just types, and a message dropped for lacking a key is
one he watched himself send into nothing.

Every inbound row is refused unless its chat id is HIS. That check is what
authenticates the `origin=telegram` owner rail (`seats.OWNER_RAILS`), which is
otherwise ADVISORY exactly as `chat.post` says — the room dir is same-uid tmpfs,
so any local process could forge the field; what the rail defends against is an
agent, or a prompt-injected one, acquiring the owner's delivery rule through
legit tooling.

```
$ helm telegram status
token:   bound
chat id: bound
bridge:  live
cursor:  at 4471

$ helm telegram poll
-> dm builder-1  yes, archive it — but keep the last 50
-> helm          how is the gate going

DRY RUN: printed, not posted, NOT acknowledged — these rows are still
queued, so --apply will see them again.
```

### `helm compose` — plan/mutate, verify, and activate

```
helm compose --room LANE --base REF --car ROWID:LANE [--car ...] [--apply] [--repo P]
helm compose --verify TIP [--repo P]
helm compose --activate TRUNK [--repo P]
```

The fold that produces its own evidence. READ-ONLY without `--apply`.

**Why a verb and not a discipline.** task/987 measured that a lane can fold
with ZERO review rows and nothing refuses it, and the two lanes it happened to
were the two handed over in chat with a green gate and the word LANDABLE. An
author who skips review skips an author-invoked guard too, so the evidence has
to be produced by the thing that does the work.

**Why replay and not patch identity.** `git patch-id` hashes CONTEXT, so a car
cherry-picked underneath other cars that touched its files re-keys — measured
on a live composed train: four unmatched cars, all four content-identical to
their reviewed lane commits, differing only in index lines and hunk headers. A
patch-id MISS therefore proves only that no commit carries that EXACT diff, and
converting such a miss into a refusal once refused a train's own top, a lane
its author had concurred on twenty-five minutes earlier. The manifest records
only immutable addresses. Fold time derives the actual result parent and tree
from Git and asks whether re-running the source pick reproduces that tree.

**The manifest records ADDRESSES ONLY** — the source ROW id, the source
COMMIT, the result COMMIT, the base and the sequence order. No trees, no
status words, no polarity. Every value the proof needs is derivable from those
addresses plus the repository and the ledger, so the record holds nothing a
reader must believe: recording a tree and then comparing a replay against it
is the same trust one layer down, and a hand-written manifest would simply
agree with itself. A car whose pick CONFLICTED is not recorded at all — an
address to a commit that was never made is not an address, and the verb stops
and says so.

**Where the manifest lives is not a preference.** OUTSIDE the worktree, under
the project home, keyed by the RESULT TIP: a gate binds the WORKTREE, so an
untracked file in the room turns `foldcheck` red and a tracked one would be a
car nobody reviewed. Keying by result tip is what lets a reader holding only a
composed sha find the evidence for it.

**The controls, after the picks:** `git cherry <room> <lane>` and
`git cherry <room> <base>` both all-minus; a clean porcelain; the deletion
rung; and the SEQUENTIAL REPLAY.

**The replay is the control, and a per-lane merge-tree cannot be.** Comparing
the final room tree against `merge-tree(base, lane)` once per lane is only
meaningful for a ONE-CAR room: with cars A and B the room holds base+A+B and
neither base+A nor base+B can equal it, so both rungs refuse every real
multi-car compose — measured on a live two-car probe. Instead each car is
re-run onto the parent it actually landed on, using
`git merge-tree --write-tree --merge-base=<source>^ <parent> <source>`, which
IS the cherry-pick performed read-only and prints the resulting tree without
touching a worktree. Three things are asserted and each is re-derived: every
car replays to its recorded tree, the parents CHAIN, and the chain ENDS at the
tip being asked about — the last clause is what stops a valid manifest for one
compose from answering for another.

**The deletion rung** requires every path the ROOM deletes to be deleted by
some CAR. That direction is deliberate: room deletions are a SUBSET, not an
equality, because one car may delete a file a later car recreates. What it
refuses is the case with no innocent reading, a composed tip missing a file no
reviewed car asked to lose.

**A failed control writes no manifest.** Evidence for a tip nobody should land
is worse than none — a later reader would find a record and stop asking.

**`--apply` requires `HELM_WORK_INTEGRATOR=1`**, the same door `helm work`
already uses. Without it the verb is read-only and always safe, which is the
same line `helm landgate` draws: asking is safe, the actuator is a separate bar.

**The consumer is a SEPARATE STAGE, not a sixth foldcheck rung.**
`foldcheck.check()` remains exactly five rungs. After those five pass, the real
`helm lr foldcheck` entry resolves the project and invokes composition proof
only when activation scopes the tip; an active proof must pass before the
entry returns success. `helm compose --verify TIP` exposes the same reader, and
the composer runs it on its own output after a successful mutation.

**It RE-DERIVES rather than reads.** Every car is replayed onto the parent it
actually landed on, the parents must chain, the chain must END at the queried
tip, and each recorded ROW is resolved through one canonical dispatch snapshot.
The row must be a real accepted REVIEW verdict with polarity `APPROVE`; `CONCUR`
authorizes nothing. Fold time replays the owner-layer approval predicate —
record-time approval tier, verdict index, frozen gate epoch and writer
capability — and, when a receipt is required, the ordinary whole-suite
receipt/tree rung. Only then may the reviewed tip's ancestry cover the source
commit. UNKNOWN is never consent: unreadable ledger, tier, epoch, receipt,
reviewed tip, Git ancestry, or root source all remain UNKNOWN rather than a
measured negative.

**OUT OF SCOPE IS THE ABSENCE OF A STAGE, not a verdict.** Before activation,
and for any tip already reachable from the activation trunk, the proof is NOT
RUN — it returns nothing rather than a synthetic PASS or UNKNOWN, because
manufacturing a verdict for a question nobody asked is the same laundering in
a politer costume.

**The cutoff is a COMMIT, not a date.** `helm compose --activate TRUNK`
requires `HELM_WORK_INTEGRATOR=1` and creates a write-once record plus an
independent latch under the project home with fail-closed interruption. Each
file creation is atomic and the latch lands first: an
interrupted activation, a malformed half, a mismatch, or deletion of either
half reads UNKNOWN and refuses; only two genuine absences mean never activated.
Repeating the same trunk is idempotent and replacing it with another trunk is
refused. A date cannot answer scope because a pick onto the source commit's own
parent reproduces the source object, dates included. The author controls dates;
they do not control ancestry.

**Both empty-commit classes are CARRIED.** `--allow-empty` preserves commits
born empty; `--empty=keep` preserves a nonempty lane commit made empty because
an earlier car already introduced its content. In both cases the car keeps its
row, message and sequence position, and replay confirms that its result tree
did not move.

**The CLI is three closed, exclusive modes:** plan/mutate, `--verify`, and
`--activate`. Help is valid only as the leading sole argument. `--car` alone is
repeatable; duplicate singular values, trailing help, unknown tokens, and mixed
modes refuse before any room or activation write is touched.

### `helm landgate --lane L --tip SHA --tree SHA --gate ID [--base REF] [--repo P]`

READ-ONLY. Answers "may THIS seat land THIS lane right now" and lands nothing.

The board's single-writer contract and the integrator's sole-lander role were
the same question, asked one layer apart, and the owner asked it first: is one
agent an unnecessary single point of failure? The meld (2026-08-01) answered
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

**WHICH COMMAND IS THE PROJECT'S OWN, not helm's.** helm was always meant to
help agent teams build ANY project, and a team USING helm must never need to
know how helm is made — so `run` asks who declares the command instead of
assuming its own. A registered project declares it ONCE, as the authored `gate`
field in `~/.helm/_global/registry-authored.json`:

```json
{"projects": {"clientproj": {"gate": {"command": ["pnpm", "-r", "test"],
                                      "protocol": "exit"}}}}
```

`command` is an ARGV LIST and never a shell string; helm spawns it from the repo
root, so a suite that needs a pipeline declares the project's own script as
argv[0]. `protocol` is `exit` (the default — the command's own exit status IS
the verdict, and the receipt records NO count, because a runner that prints
`Ran 9 tests ... OK` must not be able to claim a count helm never counted) or
`unittest` (helm reads python unittest's summary off stderr, as it always has).
Under `exit` the command's whole stdout+stderr is kept: written to
`gate-command-output.txt` beside the receipt ledger in `~/.helm/_global/`
(the same directory a node's artifact pull already fetches the ledger from),
echoed to helm's own stderr so the console and the node log carry it, and a
bounded tail (the same cap as unittest's `stderr_tail`) rides in the receipt's
`stderr_tail` at every status — red or green, because a declared receipt has
no failure list to diagnose from — with `stderr_tail_meta.source` set to
`declared-command-output` and `truncated` saying whether the tail is a window
of a larger stream; `detail` names the exit code and the sidecar path.
The receipt carries that command in a `suite_command` block bound into its
content id, beside the same identity fields every other receipt carries — repo,
head, tree, dirty, host, status, exit code, bounded failure text — so
`foldcheck`, the land gate and the verdict binding accept it with nothing in
them changed.

**THE AUTHORED FILE IS THE ONLY PLACE A DECLARATION COUNTS.** `registry.json`
beside it is the PROJECTION — rebuilt by any re-scan, written by every discovery
pass — so a `gate` block found there is reported as a projection and never run,
and neither a load nor a save promotes it into authority. A receipt's own
`suite_command` block is likewise the receipt's claim about itself: a whole-suite
receipt is admitted as a DECLARED one only where an authored declaration says
that command was declared under that protocol **for the repository that is about
to SPEND the receipt**, and a receipt whose origin cannot be established falls
back to the historical frozen-argv rule (which a pnpm or cargo command cannot
satisfy) rather than admitting itself. The project name, the argv, the protocol
and `repo_id` all live INSIDE the row, so none of them can choose the
declaration that judges it: a declaration authored for project A licensed a
whole-suite row that wore an unrelated repository B's head and tree and kept A's
path in `repo_id`. The subject is therefore the CONSUMING repository — the
standing repo a land or a verdict is spending the receipt in — and the
declaration must cover that repository (its recorded location, or one shared
`--git-common-dir` away).

**AND IT MUST BE THE DECLARATION THAT WOULD RUN THERE, not merely one that
matches.** What may be SPENT at a location and what would be RUN there are one
answer, resolved once: `gate run --repo P --plan` prints it, and the receipt
admission asks the same resolver. Looking for ANY authored declaration whose argv
and protocol matched the row and whose location folded into the consuming
repository is a weaker question than the spawn asks — a root declaring `["true"]`
and one of its own `git worktree`s declaring a real suite are ONE repository, so
the root's honest green `true` receipt was admitted in the worktree as a
whole-suite claim about the worktree. Nothing was forged in that: the row, its
content id and its `repo_id` were all honest. So the consuming location's
EFFECTIVE declaration — exact-path precedence first, then the repository fold — is
compared to the row's argv and protocol, and a location whose census is ambiguous,
whose exact declaration is malformed, or whose registry could not be read refuses
the receipt for the same reason it refuses to spawn. A caller that names its
repository by the shared admin dir resolves the consuming working tree first,
because a common dir has no declaration of its own: the dispatch row records the
checkout it was sent from (`repo_root`) beside the repository, and a verdict, a
composition capture, its replay and a scoped close all hand that coordinate to
the same bind. **The checkout is validated against the filesystem at the moment
of every spending act** — a recorded checkout that has been removed answers
UNKNOWN and admits nothing, one that belongs to another repository is refused,
and the path-to-repository memo is re-read and re-stamped by the act rather than
trusted for it, so a memo warmed while the tree stood cannot admit a receipt
after it is gone.

**AN UNREADABLE REGISTRY IS ANSWERED BEFORE ANY COMMAND IS CHOSEN.** A failed
strict read of `registry.json` is a third state beside registered and
unregistered, and the authored layer can still be read on its own — so a
repository whose merged registry was unreadable had its authored entry matched
and that command spawned. The unknown answer now comes first and refuses toward
no spawn; a registry helm CAN read that simply does not hold this repository is
still the ordinary unregistered sentence, and root inheritance, the documented
pathless declaration and helm's own default are all unaffected.

A receipt from another box enters through `gate import`, which does **not** rewrite
`repo_id`: the import records the origin separately on its binding row and keeps
the receipt's content, id included, byte for byte. So an imported receipt's
recorded origin is a fab node's path this box has never had — provenance for a
reader, never authority — and what admits it is the importing repository's own
declaration of the same command.

**A LINKED WORKTREE RESOLVES ITS REPOSITORY'S DECLARATION.** An adopter declares
once at its root and then works in `git worktree`s of it, which is where a lane's
gate actually runs; the repository is identified by `git rev-parse
--git-common-dir` and the command still runs with the worktree as its cwd. The
block above carries no `path`, which is the ordinary shape — a pathless
declaration applies to the project's REGISTERED location and folds across every
worktree of it. A worktree with its own authored entry keeps it; two declarations
inside one repository REFUSE as ambiguous rather than being resolved by sort
order; and a declaration whose location this helm cannot READ counts in that
census as UNKNOWN, so it refuses too — an unreadable location is not a different
repository. That holds when the unreadable entry is the ONLY candidate: the
answer is UNKNOWN rather than "this repository declares nothing", because
"declares nothing" is what sends the caller to helm's own default, and a tree
that ships helm would then run `python3 -m unittest discover` in place of the
command its project declared. An override feature must not answer with the thing
it overrides on a census it knows is incomplete. A candidate whose location READS
and belongs to another repository is not unknown, and still costs this repository
nothing.

**A DECLARATION FOLLOWS MEMBERSHIP.** The `projects` membership verbs decide where
a project lives, and a declaration for a location the project no longer holds is
INACTIVE — a path that is forgotten, or departed by a repoint, gets no command
until a restore or a repoint back makes it current again. Otherwise whatever
reappeared at that path, possibly a different repository entirely, would be gated
with the old project's command. A project NAME and the path stamp helm computes
for a second-location entry are disjoint spellings: the stamp separator is one a
project name may never contain, so a name is never mistaken for a stamp and the
withdrawal records, the enumerated declarations and every lookup all use one
spelling of each entry.

A key written under the HISTORICAL stamp spelling (`name@<8 hex>`, which a project
name CAN legally be) is decoded only where the authored file's own records prove
the stamping producer wrote it — a departed-location archive under
`forgotten_projects`, or a repoint archive under `project_bindings` naming that
location. Recomputing the hash is not the proof, because an owner may legally name
a project `alpha@<the 8 hex digits sha1 of alpha's own path>`; a full spelling the
file has no record for is a whole NAME and stays whole, and one that nothing
decodes declares only where membership holds it under that exact whole spelling.
`projects repoint` on a helm that predates the disjoint separator therefore keeps
working: its binding applies, the current location's declaration is selected, and
the next `save` re-spells the proven key in the computed grammar. The migration is
a write door only — a read never rewrites the authored layer.

**`projects restore` REPUBLISHES A DECLARATION ONLY WHERE THE ARCHIVE RECORDS
ONE.** `projects forget` archives the project's record and notes which
never-migrated (executable) fields in it were AUTHORED. An archive written before
that law — by a helm for which `gate` was not an authored field at all — holds
the merged record's block with no such note, and its `gate` therefore restores
into the PROJECTION, exactly where the helm that wrote it left it. Absence of a
mark is not a yes, and an authored declaration standing in the file is never
overwritten by archive bytes.

`helm gate run --repo P --plan [--json]` answers WHICH COMMAND WOULD RUN and
runs nothing — the seam anything outside helm asks before spending a box, so a
remote gate runner never has to keep a second copy of this policy. (`--plan`
beside `--focus` is still the focused-selection challenge surface.)

**helm's own source tree keeps its own suite with no registry edit**, decided by
the tree's own NARROW is-this-helm predicate (`selfrepo.is_helm_source_tree` —
the package root `helm/__init__.py`, never a `bin/helm` entry script, which an
adopter ships too; and a checkout git cannot reach is read as NOT helm, because
a false yes here SPAWNS unittest discovery on a tree that has no suite),
and its receipts do not move: no `suite_command` block, same version, same id.
A repo that declares nothing and ships no helm is now REFUSED, naming the field
to set — before it buys a FIFO position. That refusal is the whole cure: the
constant-read could not refuse, so gating an adopter's project spawned
`python3 -m unittest discover -s tests -t .` in a tree with no `tests/`
directory and minted an UNKNOWN receipt about a command the project never asked
for. An unbindable receipt, not a refusal.

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
never order. Dead, recycled-PID, and zombie waiters lose their place only after
the queue has atomically copied their complete row into its durable terminal
outbox. Each terminal carries structured `status`, `stage`, and `reason` fields;
the status is `UNKNOWN` whenever the queue cannot prove an outcome. Helm then
posts an ambient `GATE ORPHAN` room line and acknowledges the terminal only
after chat confirms delivery. A failed post stays pending for a later queue
operation or `helm gate list`; a crash after send but before acknowledgement can
duplicate a line, but cannot erase the only diagnosis. The same persist-before-
publish rule covers a FINISH line, including a receipt-mint/import failure, and
a cancellation before admission (`GATE TERMINAL`). These lines state the
CONSEQUENCE of the death rather than merely the event, because the reader cannot
derive one from the other: a waiter that never left the queue or died at the
start barrier ran no suite process, so NO RECEIPT EXISTS for that attempt and
none can arrive later; one that died while running or at finalization may or may
not have minted one, which the queue cannot decide, so the line says UNKNOWN and
points at `helm gate list`.
Every such line describes THE ATTEMPT, never the lane — and an earlier run's
receipt does not settle this one either. A RECEIPT BINDS AN EXACT TREE AND RUN,
NOT A MUTABLE LANE: it carries head/tree/host/status and NO queue-attempt
identity, so nothing in the ledger can be joined to a particular attempt. A
receipt for this tree tells you what SOME run found; which run, and whether it
was this one, the record cannot say. (A RECEIPT IS NOT A VERDICT: a receipt is
what the gate mints and `helm gate list` prints, binding a tree and naming the
box it ran on; a verdict is a reviewer's approve/fix/supersede in the dispatch
ledger, and only an APPROVE authorizes landing. This page and the notices
themselves say RECEIPT, so a reader who has one never reads it as a settled
review.) A running child is bound by exact PID
generation and periodically
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
receipt append. It persists the terminal result while moving the row to
`finishing`, posts ambient `GATE FINISH ... NEXT @seat`, acknowledges successful
delivery, and only then releases, so the next `GATE START` cannot appear first.
Chat remains observation only: posting failure never changes admission, while
the unacknowledged terminal remains observable and retryable. `helm gate list`
safely backfills already-dead exact process generations into the same outbox
without signalling a process; `--json` keeps its receipt-array stdout contract
and writes terminal diagnostics to stderr.

Queue wait does not consume `--timeout`; the clock starts at child creation.
Timeout and interruption kill the supervised child tree before release,
including descendants that created a new session or whose group leader exited.
Custom `-- <argv>`
commands remain outside this expensive whole-suite FIFO and remain non-binding.
Unreadable queue state or an unavailable queue lock refuses a new start rather
than treating the queue as empty.

**The node's tmp is measured before the queue.** A whole-suite, focused or
suite-shaped custom run mints scratch on the mount `tempfile` resolves, and a
receipt minted on a mount that cannot take a `mkdtemp` is a claim about the
node that every reader takes as a claim about the tree (measured 2026-09-11:
a build node at 100% of its tmpfs `nr_inodes` cap minted three FAILED
receipts whose failures were `Errno 28`). So `run` reads that mount on both
axes with the same reader as `helm scratch status` and, at or over the
doctor's warn threshold (`scratch.WARN_PCT`), refuses BEFORE buying a FIFO
position, starting a child or opening the in-flight marker — no receipt
exists for the attempt, and the refusal names the mount, both percentages and
the cure. Below the threshold the child runs with a `TMPDIR` of the gate's
own making, which the gate removes when the run ends however the child ended:
the suite's own per-process routing (`tests/__init__.py`) reaps on a normal
exit, and this is the layer that reaps after a timeout kill.

**A run this node refuses exits 3 — NOT RUN, never red.** `run` exits 0 when
its receipt binds VERIFIED, 1 for a red suite or any other refusal, 2 for a
usage error, and **3** (`EXIT_NOT_RUN_CAPACITY`) when THIS NODE refused to
start the suite on its own capacity: the whole-suite cap, the memory-stall
floor, or a pressured tmp. Until task/1740 the node's refusal also exited 1, so
a caller reading the exit code could not tell "the tree failed" from "the box
was full" — only the wording differed. Exit 3 mints no receipt, writes no
in-flight marker and says nothing about the tree: wait for the named runs to
finish, reap the tmp, or run it on another node. The stderr line starts
`helm gate: NOT RUN (capacity, exit 3) —` before the refusal itself, and
`--json` answers `{"minted": false, "reason": ..., "not_run": "capacity"}`. The
other refusals of the admission door (an unreadable process table, lock or
ledger) are UNKNOWN, not a full node, and keep exit 1. 3 was chosen because it
is the gate family's not-run code already (`gate window launch` exits 3 when a
running suite holds the window) and no consumer of `gate run` or `fab gate`
gives it another meaning: unittest exits 0/1/2/5, and fab's own exits (read from
the deployed fab-* scripts on 2026-09-24) are 1, 2, 75, 92-99, 124, 125, 127
and 143 — none of them 3. Every helm consumer maps it the same way:
`gate run --box` exits 3 when the remote gate's `--json` says
`not_run: capacity` AND its exit is 3 (a remote helm that predates the code
keeps exit 1); `gate fab reconcile` exits 3 for a Fab job that finished with
exit 3 and no artifact; and `gate window show` prints such a job as NOT RUN
with its relaunch, never as STRANDED, and only when the authority SAYS the job
holds no receipt: a receipt id it cannot read is UNKNOWN, not absent, and that
job stays STRANDED. `fab gate` passes the exit through unchanged, but its spill
to another node fires on only the two admission refusals, whose wording its
spoke matches (`whole-suite cap is`, `already stalling on memory`): a
pressured-tmp NOT RUN is reported with exit 3 and not spilled, until fab keys
its admission bit on the exit instead of the words.

**The suite never reads this node's admission.** `_admit_suite` reads the host
on purpose — its real `/proc` and the node-wide admission ledger under the RAM
root — and no environment variable redirects it, so a test that ran `gate.run`
used to be red or green by how full the node was (measured: 25 `test_gate` arms
refused by a build node's live whole-suite cap while two suites ran there). The
gate raises the audit event `helm.gate.host_admission` just before an admission
touches the host, and `tests/__init__.py` refuses it in any test that did not
ask. Fixtures call `tests._tmphome.pin_admission(case)`, which runs the real
door against a fixture box; an arm about the host's own reading opens
`with tests.HostAdmissionAsked():`. The scratch preflight is the third door and
raises the same event before it reads the host's tmp mount; `pin_admission`
hands it a fixture box's healthy tmp. A child python a test spawns never loads
the tripwire, so it prepends `tests._tmphome.pin_admission_code(...)` to its
own source. There is deliberately no switch that answers the event instead: the
cap has no override.

A receipt records the interpreter, `HEAD`, `HEAD^{tree}`, **whether the
worktree was dirty**, the argv, the runner's own `Ran N` / `OK` / `FAILED`
summary, and the wall time. `run` prints one canonical evidence line:

```
gate:0c7f3a91b2e45d68 | CPython-3.14.4 | tree=1c4eb37ada10 | whole-suite | Ran 5115 (skipped=8) OK
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
* **Receipt v7 is permanently withdrawn; v8 is the current bounded failure
  record.** A v7 row never mints, imports, or binds, and its burned integer is
  not available for reuse. A FAILED v8 receipt keeps the complete failure
  identity record without turning diagnostics into an unbounded ledger event.
  Parsing retains every canonical
  test identity for integrity validation and transport. The main receipt keeps
  at most 20 human diagnostics and records the exact total and omitted count;
  content-addressed identity chunks keep every ledger event at or below 64 KiB.
  Those full chunks are not a request to re-run an uncapped test population:
  base-check consumes only the bounded diagnostic/identity projection and says
  UNKNOWN/SKIPPED when failures were omitted. Its `failing_files` projection is
  bounded the same way and carries exact total/omitted counts. A missing or
  malformed referenced chunk makes the physically stored receipt object
  INCOMPLETE and UNJUDGEABLE, never absent and never binding; importing the
  valid complete artifact repairs such an object by installing only its missing
  validated chunks. Chunk and receipt hashes prove internal consistency and
  transport integrity, not origin or unforgeability.
* **An ordinary whole-suite run records bounded module timings beside its
  receipt.** The existing unittest outcome recorder measures each started test
  through the runner's own callbacks, aggregates wall and process-CPU time by
  owning module, and writes one content-addressed sibling event before the gate
  row. `gate show` displays the slowest 20 modules, total measured module wall,
  process CPU, runner elapsed, and the remainder not attributed to started
  tests (discovery/import, fixtures outside test callbacks, and runner work).
  The ranking is wall-descending with module-name tie-breaking. A `COMPLETE`
  sibling must reconcile its module and test counts, bounded top rows, raw
  wall/CPU measurements summed once before aggregate rounding (with only the
  correlated per-row display-rounding allowance, which is zero for one row),
  finite non-negative wall/CPU display totals in every state, numeric COMPLETE
  runner scalars, and module wall within runner elapsed
  (allowing only the half-millisecond unittest-footer rounding bound plus a
  one-microsecond safety margin), derived unattributed wall, and the receipt's
  unittest footer.
  A test whose class `setUpClass` (or module `setUpModule`) raised `SkipTest`
  never starts, and unittest records one skip for the whole class. The census
  counts such tests as skipped by class, not as never started, and a module
  none of whose tests could start owes no timing. `gate show` names each such
  class, its test count and the skip reason on the timing line. A setup that
  raised an error, or a class cleanup that failed after the skip, is not a
  skip: its tests still make the timing `UNKNOWN`, as does any planned test
  that never started with no setup skip recorded.
  An `UNKNOWN` timing names each census input that failed, in order, within
  the census's own 2,000-character bound. The stored event and `gate show`
  keep that reason whole; the 500-character cap applies to failure text only.
  Receipt reconciliation considers only rows that pass the authoritative receipt
  integrity filter; malformed or future gate-looking debris cannot contradict an
  honest sibling by reusing its id. Malformed, truncated, ambiguous, or
  contradictory timing is ignored during import or shown as `UNKNOWN` when
  stored, never as a confident top-20. If no
  timing sibling is available, `gate show`
  reports only that absence; it does not guess whether the receipt predates the
  recorder or an advisory setup, append, or import failed. Timing is diagnostic
  only: it changes neither the receipt id nor binding/landing authority,
  interpreter, serial discovery, nor test order.
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
  strict seam. Receipt and referenced chunks become durable first; the
  canonical binding for this importing repository is the authority that makes
  the import usable here. The later import-audit pointer is non-authoritative:
  append failure is returned as a warning and cannot undo a durable receipt plus
  binding. A FAILED suite imports honestly and the exit stays bind-derived,
  exactly as local. `--box` with a `--` command refuses: an
  interpreter helm did not choose cannot bind, so routing it would spend a
  box on an unbindable claim.

* **`helm gate fab contract|reconcile|detached` is the explicit Helm/Fab v2
  composition seam.** `contract` emits one canonical request only after Fab has
  measured the snapshot tree, remote interpreter, and exact installed runner
  bundle. The SHA-256 key binds canonical JSON identity format
  `helm-fab-gate-job-v2`: importing Git common-dir, 40-hex tree, frozen whole
  argv (`-m unittest discover -s tests -t .`) or the entire focus plan, exact
  interpreter, and runner format/argv/wrapper hash/cgroup. Queue and execution
  ceilings remain named values outside the key, so follower patience does not
  change suite identity. The sink nevertheless echoes the generation's admitted
  launch budgets (not a later joiner's preferences): `LAUNCHED` must echo the
  submitted budgets exactly, while only `JOINED` may return the generation's
  earlier admitted values. Every normal observation plus durable completion must
  match them exactly; direct integer budgets normalize to canonical floats before
  completion identity/replay. Focus identity is representable, but Fab has no live
  challenge custody; both contract execution and submit fail closed and name
  `helm gate run --focus --box HOST` as the only focused authority.

  Fab alone owns atomic submit-or-join and durable existence. A v2 handle is
  exactly `{v,key,job_id,host,generation}`; generation is the remote run id, not
  local follower identity. Every observe, wait, fetch, join, import, and kill is
  generation-bound. An abandoned pre-launch recovery mints a successor
  generation; an old handle receives terminal `SUPERSEDED`/93 and can neither
  attach to, import, nor kill the successor. Exact `gate-job` events echo key,
  job, tree, snapshot head, identity, budgets, node, and generation. The exact
  v2 snapshot is ten fields: state, exit/class, artifact path/SHA-256, receipt,
  queue/execution elapsed, liveness, and reason. `COMPLETED` and `CANCELED` are
  canonical terminal states; `SUPERSEDED` remains the stale-generation terminal.
  Typed `UNKNOWN` carries all ten fields with exact null authority, `live=false`,
  and an optional reason rather than being mistaken for absence. Exact
  `gate-fetch` additionally binds the remote source artifact, fetched local
  artifact and SHA-256, completion exit, and receipt id. A returned wait event is
  consumed directly rather than discarded for a stale re-observe; a non-live
  nonterminal snapshot refuses instead of waiting forever.

  **Optional `node_id` on `gate-job` (the node's machine-id hash).** A
  `gate-job` event is an exact-set object: its keys are the thirteen above, or
  those thirteen plus `node_id`, and nothing else. An unknown extra key still
  refuses. `node_id` is a JSON string of exactly 16 lowercase hex characters:
  `sha256(machine_id)[:16]` over the UTF-8 bytes of the node's
  `/etc/machine-id` (fallback `/var/lib/dbus/machine-id`) with surrounding
  whitespace stripped. This is the recipe `gate.host()` uses for a receipt's
  `host.id`, and Fab must compute it on the node that runs the job, never on
  the hub and never from a host table. The field is optional so that a Fab
  writer that does not emit it is not refused. When it is absent, the receipt
  rung is unchanged: `host.node` must equal the handle's `host`. When it is
  present, `node` stays in the event for display, the reconciled completion
  authority carries the same `node_id`, and the receipt's `host.id` must equal
  it. A different or empty `host.id` refuses and names both hashes, and the
  hostname does not decide in either direction: one machine can answer to a
  Fab alias and to its own uname. A present `node_id` that is not a 16-hex
  string (including `null` or an empty string) refuses the event; it is never
  ignored. A node without a readable machine id mints receipts with an empty
  `host.id`, so no `node_id` can match it and it cannot pass this rung.

  Local timeout, interrupt, TERM, malformed observation, or receipt-recovery
  exception detaches only the disposable follower. This includes submit-time
  `RECEIPT` recovery: it enters the same protected follower boundary rather than
  calling reconcile outside it. `detached` prints `CLIENT DETACHED`, exact remote
  state, and generation-bound join/status/kill/import commands; it never invokes
  kill. `reconcile --repo PATH --stdin` accepts one canonical object bounded by
  UTF-8 bytes (not Unicode code points) with exact keys
  `{request,handle,observed,fetched}` and no stripped transport whitespace.
  Malformed transport exits 2; semantic mismatch exits 1 with no authority write;
  a completion with exit 3, no artifact and its receipt ABSENT on both readings
  is a node that refused to run the suite and exits 3, NOT RUN, with no receipt
  owed; one that names a receipt ran a suite and stays unretrievable.
  Typed `UNKNOWN` and `SUPERSEDED` JSON is emitted before the exit-1 warning so
  the wrapper cannot lose the terminal state. Helm revalidates the
  immutable request and handle, exact event/snapshot schemas, key, generation,
  tree, head, identity, budgets, host, source artifact, artifact SHA-256, exits,
  and receipt ids before selecting the one whole-suite row from a possibly
  multi-row artifact. Delayed terminal publication may advance null artifact
  identity to an exact fetch identity, but a present observed path cannot change,
  and artifact plus error refuses. The importer opens the artifact once; receipt
  validation, hashing, completion, placement, and binding all consume that one
  bounded snapshot. Any valid generically importable receipt version may bind;
  version is not an authorization axis. The receipt must independently match
  admitted head/tree/interpreter/suite/argv, clean pre/post
  bracket, coherent status/exit, and generation host (machine id when the
  event carried `node_id`, hostname otherwise), then resolve in
  the importing repository. `UNKNOWN` is equivalent in both directions: its
  disposition can carry only the exact null snapshot, never normal terminal
  authority. The ordinary canonical import binding remains v1; a separate strict
  Fab-completion ledger preserves key, generation, admitted budgets,
  runner-bearing identity, source artifact, exact exit, and artifact byte
  identity without making historical binding readers reject a new schema. Its
  public importer runs the same exact nested whole-identity validator as request
  admission, so a self-hashed non-v2 runner, repository, or scope refuses.
  Completion is appended **before** the generic receipt and repository binding;
  a completion-ledger failure therefore reports `completion-pending` while no
  generic authority exists to spend. Retry continues idempotently through receipt
  placement and binding.
  Focused v6 still refuses at the plain artifact door. Completion can
  therefore reconcile after every local follower dies, and exact retries are
  idempotent.

  Task/1527 owns the matching Fab sink and operator wrapper. The second lander
  owes one resolved-tree gate of the composed Helm/Fab contract.

  Import also resolves the importing repository identity **before any receipt
  write**. `--repo` naming a worktree that was already reaped refuses as
  unreadable instead of leaving a globally stored receipt with no repository
  authority. The identical artifact may be retried from any surviving worktree
  of that repository; its common-dir binding then survives later lane cleanup.

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

### `helm gate audits [--repo P] [--json] [-- <test module>...]`

**Prints one pasteable `fab test` command naming every tree-wide audit.** It
starts nothing itself. A tree-wide audit is a test module that enumerates the
package or the tree and asserts a property of every module it finds; it imports
nothing it judges, so no consumer sweep over a change's symbols ever selects it,
and a lane that adds a module, a verb or a docstring reference meets it for the
first time at the whole-suite gate. The list is the one
`docs/MODULE_REGISTRIES.md` enumerates plus the arms on the non-test rungs, and a
test holds the two in step. Name the lane's own test modules after `--`
(`tests.test_x`, `test_x` or the file's path) and they are appended once each.
**Paste the printed command on the COMPOSED tree before every whole-suite gate.**
A listed audit the tree does not carry prints nothing and exits 1, because a
command naming it would fail on import instead of auditing anything.

### `helm gate window launch [--repo P] [--label T] [--trunk REF] [--supersede] | show [--recover]`

**ONE whole-suite gate per project LANDING WINDOW, enforced by a door that
refuses.** A window is one trunk head. Every compose room an integrator stands
for a window is merged onto the same trunk head, so the last room built on that
head CONTAINS every room built before it on that head, and a green receipt on
the top car of a stacked train lands every car beneath it. Twelve whole-suite
runs were launched for four landed trains before this door existed; six were
killed by hand as redundant. The rule was already in the typed store twice and
was injected into the integrator's own turns while it did the opposite — which
is the point: advice does not bind a habit, and the only thing that has ever
held in this tree is a verb that refuses.

**Why the existing admission refusal does not cover it.** `helm gate run`'s
ladder counts suites on the box it runs on. Those redundant runs were on
DIFFERENT boxes, each admitted honestly by its own host, and each still
redundant with the others. The window is a PROJECT fact; nothing measured it.

* **`launch` records before it dispatches, and it dispatches a KEYED job.** The
  project (the shared git common directory, so every worktree of one repo
  answers one identity), the trunk head, the room and its head go into
  `_global/.state/gate-window/runs.json` BEFORE the dispatch. A node's in-flight
  records carry an id, a phase and a pgid — never a repo, a room or a project —
  so which rooms a run covers is knowable only here.
* **The dispatch goes through the typed gate-job boundary, and the identity is
  bought before any suite runs.** `fab gate measure` names the node, its
  interpreter and the installed runner; the same request builder
  `helm gate fab contract` exposes turns those into the exact
  `helm-fab-gate-job-v2` request; `fab gate submit` admits or
  joins it. A plain `fab gate` dispatches with no request key, so the node
  writes no `~/fab/gate-jobs/by-key` record — and that record IS
  `fab gate reconcile`'s CANDIDATE tier, the only tier with an import door. A
  run dispatched the plain way can therefore only ever be discovered as
  UNVERIFIED, which is discovery without authority and no recovery at all. The
  job id is `gate-<key>` and the key is a hash of the request, so a door that
  cannot build a request **refuses with nothing dispatched** — the one moment a
  missing identity costs nothing. A submit whose answer could not be read is
  UNKNOWN, not a failure: Fab decides same-key existence at its own sink, so the
  record is kept and the window HELD.
* **The client outlives the caller.** The fetch and import legs are local, and
  they used to run inside a child of the launching process — so a backgrounded
  launch hitting a harness time limit, a TaskStop or earlyoom took the only
  local path to a minted receipt with it: node green, receipt on the node,
  nothing on the hub aware it existed, measured once at 48 minutes. `launch`
  now returns as soon as the record and the dispatch are written and hands
  `--join` plus `--import` to a process in its OWN SESSION, stdin from
  /dev/null, both streams in `_global/.state/gate-window/logs/<job>.log`. It
  prints the job id, the generation and that log path. The two legs are joined
  by a newline and never by `&&`: `--join` exits with the suite's own status and
  a red suite mints a receipt too. A client that cannot be detached is said out
  loud — the suite is running and recorded, so `show --recover` is what owes it.
* **Liveness is measured, never remembered.** A keyed job has no id in
  `fab status` (submit captures the dispatcher's stdout and echoes only the
  handle), so a typed record's liveness is asked of the AUTHORITY that owns it —
  `fab gate observe`, generation-bound — and a record is retired only on that
  authority's own exit or terminal state. Records written by the earlier
  plain-gate door still ask the node whether their fab id is in flight.
* **Every unknown in that ladder KEEPS the record.** `fab status <host>` prints
  `<host>: unreachable` on stdout and still exits 0, so a down node and an idle
  node leave fab in the same shape; the read keys on the `IN FLIGHT` banner
  instead, which is the node speaking. Banner with no rows is a measured idle
  node; banner absent is UNKNOWN, and UNKNOWN keeps. So does an authority that
  could not be read. A dispatch with no generation keeps too: it may have
  happened and only its name was lost, so the window is HELD for a bounded grace
  and then retires itself. Neither WAIT nor `--supersede` is reachable for a run
  nobody can name, and the refusal says that rather than printing a cure that
  cannot be run.
* **A DIRTY room is refused before any of that.** Fab snapshots tracked
  and untracked bytes into a dangling commit, so it would gate a tree that is
  on no branch while the record claimed the room's head — and that head is what
  every containment answer downstream is computed from. Commit the room first.
* **Two ways a request is already covered.** SAME-WINDOW — a run is live for
  this project on the same trunk head. CONTAINED — this room's head is an
  ancestor of a live run's head, so that run's receipt binds this tree when it
  lands, even if trunk moved. A launch on a trunk head no live run holds is
  admitted; that is the control case, not an exception.
* **The refusal names both open doors.** It prints the running id, host, label,
  elapsed time and room, then WAIT (with the command that follows it to its
  receipt — `fab gate --join` for a keyed job, `fab tail` for a record the
  earlier door wrote) or SUPERSEDE.
* **`--supersede` is one act, and it may not drop a car.** It kills the running
  run and gates this room in its place, and ONLY when this room CONTAINS the
  killed run's head — otherwise a car that run was carrying would silently stop
  being gated, and the refusal names that car instead. An ancestry question git
  could not answer refuses too: supersede must PROVE nothing is dropped. A kill
  that does not report success refuses before launching, because a launch on top
  of a run that did not die is the exact thing the door exists to prevent. A
  keyed job is cancelled through `fab gate kill --host --job --generation`; a
  record with no generation cannot be killed at all, so `--supersede` refuses
  rather than starting a second suite over it.
* **`show` reports what the node finished and this hub never bound.** It prints
  the in-flight window per project, then joins every retired record against the
  local receipt ledger. A retired record the ledger has no receipt for is
  **STRANDED** — printed with the authority's own state and the exact
  `fab gate --import HOST JOB --generation GEN --repo ROOM` that binds it — and
  `--recover` runs that door, then RE-READS the ledger, because the import's
  exit status is a claim and the ledger is the authority. Exit is nonzero while
  anything is owed. Four states are never accused: a CANCELED job minted no
  receipt and owes none; a job its node refused on capacity (exit 3, its
  receipt ABSENT — an unreadable one is UNKNOWN and stays STRANDED) ran nothing
  and is printed as NOT RUN with the
  `helm gate window launch` that relaunches it, and `show` exits 0 on it; an
  unreadable ledger calls nothing stranded, because
  unreadable and empty are different facts; and a record with no gate-job
  identity has no import door at all, so its render names
  `fab gate reconcile --host H --repo ROOM` and the UNVERIFIED tier that sweep
  will report, rather than a cure that cannot be run.

### `helm owed [--seat S] [--rows] [--json]`

The lanes sitting **cured-but-unreviewed** — oldest first.

A FIX verdict says a cure is owed by the lane — by the lane owner, or by the
reviewer who already wrote it and named the tip under `--patch-tip`, in which
case what the lane owes is the rebase. **Nothing tells the lane that curing
creates a NEW obligation** — a review dispatch on the cured tip — and no
surface showed a lane in that state, so lanes cured and then re-gated, because
re-gating is the action the tooling makes obvious. Lanes sat for weeks. This is
the screen that was missing; `--seat <you>` answers *what do I owe*.

Derived from the dispatch ledger alone — no git for the debt itself. A FIX
verdict that no LIVE row supersedes, where **live means a live row anywhere
BELOW it**, not merely a live direct child: a cancelled child is what `rebind`
leaves behind and its chain continues one level down, so asking about direct
children alone bills work somebody already did (measured: 112 rows against 16
genuine dead ends).

Counted in **lanes**, not rows — a lane whose chain was orphaned appears once
per orphaned root, and a burn-down that triple-counts its worst-maintained
chains is not believed twice. `--rows` shows every row.

**A FIX has three answers and the screen names all three.** Cure it; or
re-dispatch the cure you already made with `--supersedes`; or — the answer the
surface used to leave out — *you are right, this should not exist*. An author
who accepts a finding in full and deletes the artifact has no successor tip and
never can have one, so `dispatch cancel` refuses (a reviewed row is not
cancelled) and `--supersedes` would mint a fresh review over nothing: the
terminal is `helm lr close <id> --reason withdrawn --evidence "..."`. Naming
only the first two branches made this screen — re-delivered hourly by
`owed-push` — the instrument pressing an author to re-dispatch work a reviewer
had just correctly told them not to build. A withdrawn close leaves this
population in the same pass, so the nagging stops with it.

Three things it reports that nothing else does:

* **UNDECLARED verdicts** — a verdict that recorded no polarity is neither owed
  nor clear. Reading the absent field as a decided non-fix hid 26 of them;
  reading it as a fix would invent debts. It gets its own list.
* **FORKED chains** — one row with two live successors, where one branch can
  never be discharged by anyone. Printed even when nothing is owed, because a
  fork stays invisible until somebody asks why a lane never closes.
* **PARKED tips** — a HEAD that is an actuator rescue or a `wip:` park rather
  than an author's cure. It wants *resume this*, not a review dispatch, and the
  matching signal is named because the two can disagree and only the author can
  tell a park from a rescue of finished work.

An unreadable ledger **exits 1** and says UNKNOWN: an empty burn-down and an
unreadable one look identical on a screen, and this screen exists to be
believed.

### `helm owed-push [--dry-run] [--quiet] [--json] [--ensure-timer]`

**Delivers** what `owed` computes — one DM per owing seat.

`owed` is the screen. This is the leg that carries it. Both of the existing
surfaces are **pull**: a verb somebody must type, beside a web burn-down
somebody must open, so the debt reached nobody who was not already looking.
Measured on the live estate: **130 debts at a median age of 12.7 days**, and one
cure committed **fifteen minutes** after its FIX verdict whose row then sat
**22 hours** — because a cure is a commit, and only a dispatch moves a row.

It computes nothing. Every item, every addressee and every word of remedy text
comes from `owed`; the DM carries that sentence verbatim, including the
`--supersedes` it names — the flag whose absence built 124 unclosable land
loops. Delivery rides the ordinary DM rail, so a debt wakes its author at the
next tool boundary like any direct address.

**It cannot fire on noise, by construction.** The obvious design reads each
lane's worktree HEAD and fires when it moves past the reviewed tip — which
fires on a `helm work` bot's mid-edit snapshot commit, on a docs typo, and on
an in-flight subagent's half-written tree, so it needs a clean-tree heuristic
to suppress its own false positives. This predicate is pure ledger shape and
**never reads a worktree**, so a wip commit and a dirty tree are invisible to
it; only a real ledger event clears a debt. The cost is stated rather than
hidden: it cannot tell *cured and not re-dispatched* from *never cured*, which
`owed` ruled deliberately — both want the same next action from the same
person.

* **Deduped by chain root**, never the lane label. A lane is free text and the
  live board carries seven independent `--new-work` chains under one string;
  collapsing on the label would hide a real debt behind a stranger's.
* **Capped per digest, remainder counted.** One live seat owes 71 lanes. A
  71-line DM is not read, and a silently truncated one is the forgetting this
  loop exists to end.
* **Latched** on (kind, reviewed tip, owed-since), so a retip or a
  newly-declared polarity is NEWS while the same debt is not re-raised for
  three days — and **re-armed** when a row leaves the owed set.
* **An undelivered digest never latches.** A watcher that fails silent while
  its cursor marches on has dropped the window, and here the window is
  somebody's unanswered debt.
* **It never blocks.** The whole surface is a chat post: a false notification
  costs one line, a false block costs a seat its turn.

`--ensure-timer` installs an hourly cadence. Hourly rather than daily because
18 of the 130 live debts were younger than a day, and the incident behind the
whole mechanism would have been delivered a day late by a daily sweep. An
unreadable ledger **exits 1** and delivers nothing, rather than publishing a
clean burn-down from the one moment it cannot see.

### `helm dispatch send <recipient> <lane> <message...|stdin> --ref TIP --kind build|review --new-work|--supersedes ID [--key K] [--force] [--posture-na REASON] [--read-only-because REASON] | add <recipient> <lane> --ref TIP --kind build|review --new-work|--supersedes ID [--force] [--posture-na REASON] [--read-only-because REASON] | verdict <id-or-unique-prefix> <full-reviewed-tip> --approve|--fix|--supersede|--concur [--patch-tip FULL_SHA|--no-patch-because REASON] [--imperfect] <evidence> | cancel <id-or-unique-prefix> <reason...> | rebind <id-or-unique-prefix> --to <seat> [--force] [--reason R] [--repo PATH] [--json] | retip <id-or-unique-prefix> --ref NEW_TIP --reason R [--repo PATH] [--json] | list [--open|--overdue|--held] [--mine] [--issued] [--to SEAT] [--all-projects] [--json] | triage [ID...] [--all-projects] | mix [--hours N] [--sender SEAT] [--json] | briefs [--cut] | collisions [--json]`

**The project's light is checked at this door.** A NEW build row whose repository belongs to a project with an authored `red` light is refused as described under `helm projects state`; a `yellow` one is admitted with its reason printed as an admission note. `--force` does not change this, because it is a statement about the recipient.

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

**A send to a codex recipient consults the POOL BUDGET, and refuses only at a full wall.** `dispatch send` reads the pooled codex accounts' budget snapshot (written by `helm proxywatch`'s timed pass — the send itself never touches the network) and compares each account's LONGEST window against the fleet-wide soft ceiling `HELM_CODEX_WEEKLY_CEILING_PCT` (default 90). When EVERY pooled account was read AND every one of them is at or past that ceiling, the send REFUSES with the same shape as the UNUSABLE refusal and `--force` files it anyway. When some accounts are past it and some are not — or any account could not be read — the send PROCEEDS and warns, naming the headroom that is left. A recipient's family is its VERIFIED RUNTIME family, never its display spelling — a `pi-codex` seat whose roster row proves a codex runtime is measured against the pool, and a seat merely SPELLED like codex whose verified runtime is another family is not. An unreadable pool FILE stays in the census as an explicit UNKNOWN and is named in the warning: a file nobody could parse can never complete a census, so a capped account beside an unreadable one warns instead of refusing. No snapshot, a stale one, a pool that is KNOWN EMPTY, or a non-codex recipient admits silently: absence is not a measured wall. Unpooling the last account retires the refusal at the next `helm proxywatch --post` pass, which publishes the empty census rather than waiting for the old snapshot to age out. This exists because on 2026-09-14 all five pooled codex accounts hit their WEEKLY caps at 02:59Z while the 5h window every seat was paced on still read healthy, and the fleet kept filing rows into a budget that was gone.

**Verdict authority is evaluated at record time.** The author-bound verdict
writer emits event v4 and retains the exact-session runtime envelope plus a
versioned tier decision bound to the verdict identity, recipient, repository,
reviewed commit, gate, polarity, basis, reference, timestamp and sequence. The
policy observation comes from the validated typed-store population, not from
policy members supplied by the verdict. Readers resolve its retained version and
check the decision against those inputs; they do not consult today's roster or
approval policy. A rename, seat retirement, same-id policy edit, or policy
retirement therefore cannot revoke or grant an already-recorded tier. The send
advisory above deliberately still checks current policy.

Retained observations live in `_global/premises/policy-versions.jsonl` under
`HELM_HOME`. This append-only source ledger is durable, shipped with premises,
and must be backed up and restored with dispatch history; it is not a disposable
`.state` cache. There is no garbage collection or historical authority backfill.
The store retains an explicit no-policy observation only after a successful
empty-population read; later declaration does not alter that recorded `none`
decision. Authority capture uses a strict, read-only registry snapshot for both
project selection and adopted-root enumeration, then strict source reads under
the usual prior/legacy and cross-root shadow rules. Denied directories/files,
invalid UTF-8, malformed or partial typed-policy metadata, and damaged root
registries refuse capture instead of falling through the permissive inventory
loader to an apparently empty population. Truly absent optional roots remain
valid absence; configured readable symlink roots retain their normal semantics. Failure to retain the observation refuses the verdict append. A
retained observation orphaned by a later verdict-write failure grants nothing.

Old verdicts without tier evidence are **PRE-TIER: readable, nonauthorizing
history**, including old records that have runtime evidence alone. Re-review
through the current writer to obtain new authority; do not infer it from today's
roster or retrofit old rows. Partial, malformed, unsupported, contradictory, or
missing required v4 proof is damaged evidence, never pre-tier. A missing or
unreadable retained policy denies authorization; it cannot fall back to current
policy or pretend no policy existed. Content anchors bind inputs but are not
signatures: rewriting both trusted source history and verdict coherently remains
outside their authentication guarantee. Event v4 is also a downgrade barrier:
strict v3 readers reject it rather than ignore the new authority fields.

The pipeline keeps authorization separate from read health. PRE-TIER approves
and advisory CONCURs remain held/nonbillable; CONCUR explicitly says it does not
authorize landing. The existing `unmeasurable` API population means nonbillable
dwell, not failed projection reads, and carries a `kind` for display. Actual
unavailable projections retain their UNKNOWN/read-failure branches.

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

**`list` answers WHOSE rows these are, in BOTH directions.** `--mine` keeps only the rows naming THIS seat; `--issued` keeps only the rows THIS seat SENT; `--to SEAT` asks the recipient question about a named one. All compose with `--open`/`--overdue`/`--held`, and both identity flags derive that identity through the SAME door that stamps a dispatch author — declared name and roster binding must AGREE, and a derived family floor (no declared name and no roster binding) is refused. **When identity cannot be resolved, they REFUSE and print no rows.** Neither degrades to the unfiltered list: a listing the reader believes is "mine" while it names every seat is the exact failure these flags exist to close. The resume-turn hook has told every compacting seat since it was written that its live obligations are ONLY the OPEN rows NAMING IT — measured from a seat holding zero obligations, `list --open` returned two rows naming two OTHER seats and that seat adopted one of them (task/1007). The instruction was unfollowable, at the moment its reader had least context to notice; the hook then named `helm dispatch list --open --mine`.

**...and that command was still FALSE for an AUTHOR** (measured on one seat, 2026-08-12): `--mine` is RECIPIENT-scoped, so it printed "no matching rows" while the seat held an OPEN row it had SENT, two hours past deadline, that only it was positioned to chase. What a sender owes is the **DELIVERY LEG** — that the recipient knows the row exists and what it needs — so a row sent but never delivered, or delivered to a seat that went quiet, is the sender's to chase. `--issued` is that half, and **`--mine --issued` is the UNION ON THE DIRECTION AXIS**: every OPEN row in either direction. It is SILENT ON THE STATE AXIS — `--open` selects status `open`, so a row in state HELD is in neither view whichever way it points, and an empty union means "nothing OPEN owed", never "nothing owed". `--held` asks the state question those rows answer to. The hook names that one command rather than two, because two rebuild the incident at execution time — the reader runs the first, reads a reassuring nothing, and stops. Both halves bind to the SAME resolved identity, so the union can widen to no other seat's rows; rows in it that are addressed to someone else are marked `YOURS TO CHASE`, because a row you sent, read as a row you must DO, is duplicate work — the mirror of adoption. `--issued --to SEAT` intersects ("what did I send to that seat"). A row whose sender names nobody (the pre-`3e0fe8e` cohort: 417 of 2257 live rows carry a bare-family or absent sender, none of them open) matches NO seat here — the stop-guard nag deliberately keeps such rows, but a listing that answers "what do I owe" must not hand them to a reader as its own authorship.

**The listing is scoped to the PROJECT of the directory you run it in, and a row this registry cannot place is NOT counted in that project — which is a failed LOOKUP, never proof that the row is in no project.** This ledger holds rows for every registered project, each keyed by the repository its ref lives in, so `list` and `triage` narrow to the project the cwd resolves to and `--all-projects` widens them again; the clause that narrows is printed with the count it set aside, and `--mine`/`--issued`/`--to SEAT` are never narrowed at all because a row that names you is yours wherever its code lives. Two states are **UNPLACEABLE** rather than local: a row naming NO repository (written before the repository stamp — 71% of ledger lines are lifecycle events that never carry one) and a row whose repository lookup came back EMPTY — nobody registered it, its path would not resolve, or the registry could not be read, which the ordinary resolver reports as one and the same empty answer. So every sentence about this bucket states the lookup rather than a verdict on membership: the registry read failed or came back empty, the row is not counted in this project, and nothing there proves the row is in no project. The registry is re-read per lookup rather than snapshotted, so a listing can resolve its own project and then fail to place a registered sibling one read later. Those go to an **UNKNOWN PROVENANCE** bucket printed under its own heading with a count and its two sub-counts, below the project's rows and never inside them. Filing them under the reader's project is what the bucket exists to stop: measured 2026-09-13 from a client project's checkout, `helm dispatch list` reported 10 rows in that project and 9 were July/August helm rows the registry could not place — the same nine obligations simultaneously belonged to all 236 registered projects, because absence of a repository was being read as evidence of the reader's. Hiding them is the other half of the same defect and is worse: they are the oldest obligations in the ledger, and a row nobody can see is a finding nobody can carry. `--json` therefore keeps them in the array and stamps every row with `scope_class` (`local` / `origin_unknown` / `project_unresolved`) whenever the project axis ran, because the machine surface has no heading to read. The row's ref is deliberately NOT re-probed against every registered repository to guess a home: that is up to 236 `git cat-file` spawns on a surface the resume-turn hook runs on every compaction, and a clone shares objects with its parent, so a hit would prove reachability and never authorship.

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
* **`--read-only-because REASON` — a REVIEW brief may not silently forbid the
  cure.** A brief whose body or note carries one of a CLOSED set of
  DIRECTIVES to the reader — `read-only review`, `read-only read`,
  `read-only audit`, `read-only refute`, `read-only re-refute`, `review is
  read-only`, `source read only`, `do not edit source`, `do not edit
  anything`, `do not edit or commit`, `do not commit`, `do not patch`, `do not
  fix`, `no edits`, `report only`, `findings only` — removes the one move the
  review procedure is built on: the
  reader commits its cure off the exact tip it read. `send` and `add` REFUSE
  such a review brief unless this flag records why, and the row keeps the
  reason as `read_only_because` so a later reader can tell "asked and
  answered" from "never asked". The match is on WORD TOKENS, not raw
  substrings, so "thread only" is silent and the two spellings of `read-only`
  count as one hit. The bare words `read-only` and `do not edit` are NOT in
  the set, because in real briefs they mostly describe how something was
  measured ("against the live ledger, read-only") or whose worktree to leave
  alone ("do not edit my worktree, make your own checkout"), and a door that
  refuses a description teaches its senders to pass the flag unread. Only
  `--kind review` is asked:
  a build brief saying "report only" is describing its own deliverable. Every
  `send --kind review` also prints the procedure in one line, whatever else it
  prints: the reader commits its cure on a branch off the exact tip it read
  and returns FIX with `--patch-tip`; the author reviews that patch; agreement
  on the patch is what lands the chain.
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
* **`--patch-tip <full-sha>` — the reviewer's own cure, recorded as co-author
  work.** A reviewer of EITHER family who finds a MECHANICAL defect patches it:
  commit it in your own worktree on a branch off the exact reviewed tip, do
  not push, and name that tip here. The row then records
  `patch_tip` and `patch_author` — the reviewer this row was dispatched to,
  taken from the row rather than from the caller, because a name the writer
  accepts is a name the writer cannot check — and `helm dispatch triage <id>`
  and `helm lr show <id>` print it. The lane owner or the integrator rebases
  the lane onto that tip or cherry-picks it; `helm lr close --reason landed`
  credits every author the chain records. The tip must resolve to a commit in
  the row's repository whose ancestry CONTAINS the reviewed tip, else the
  verdict refuses naming it: a cure written against a different tree would make
  the rebase carry work nobody reviewed, which is the lane-discipline class
  arriving through a co-author. It rides `--fix` alone — APPROVE ends the loop,
  SUPERSEDE says the work is replaced, CONCUR blocks nothing — and a DESIGN
  finding is never patched under review: that takes a meld, because a design
  disagreement settled by one side's patch is the disagreement unrecorded. A
  FIX without the flag is unchanged. **What keeps the families independent is
  the RE-READ** — the composed tip is read once before the land gate by a
  reader who wrote none of it — never a rule that one family may only report
  while the other types. That re-read is MECHANICAL, not a promise: naming a
  cure here records you as an author of this CHAIN, so your own later APPROVE
  on the composed tip no longer carries the lane to READY. The row is not
  ready until a seat that wrote none of the chain approves the final tip, and
  the refusal names you (see the readiness rungs under `helm lr`).
* **`--no-patch-because REASON` — a FIX with no cure states why.** A reviewer
  who finds a defect is standing in the tree that carries it, so the default
  answer to "did you cure it?" is yes. A `--fix` verdict carrying no
  `--patch-tip` is therefore REFUSED unless this flag records which of the two
  reasons applied, and the refusal names the patch path in one sentence. The
  reason is ONE argv token (quote it) of at most 256 printable characters; it
  is stored as `no_patch_because`, said back by the verb, printed on
  `helm lr show <id>`, and included in verdict retry identity. A DESIGN
  finding bound for a meld is a valid reason and is recorded like any other,
  because a design disagreement settled by one side's patch is the
  disagreement unrecorded. The flag and `--patch-tip` are refused together —
  a verdict names the cure or says why there is none — and no polarity but
  FIX is asked the question.
* **`--imperfect --patch-tip <full-sha>` — a clean read that carries a cure.**
  `--imperfect` on its own is still refused as not-a-block: findings that
  remain true on a tip no worse than main are APPROVE plus separately filed
  remainder. But a reader can find the tip no worse than main on every path it
  touches AND commit a real improvement to it, and until this pair existed
  that patch had no door — `--approve` refuses `--patch-tip`, so the cure
  travelled by chat and nothing recorded that the author owed it an answer.
  The pair records polarity `fix`, exit answer `imperfect`, and the patch. It
  blocks NOTHING about the reviewed tip: `helm lr` prints AUTHOR AGREEMENT
  OWED ON THE PATCH on both the list line and the detail page, and what
  closes it is the lane's author recording an agreeing verdict on the patch
  tip. `--imperfect` with `--worse-than-main` is refused as two answers to one
  question, and a stored `imperfect` answer with no well-formed patch tip
  reads UNMARKED, the same fail-closed shape a forged blocking answer gets.
* **Optional finding observations:** `--finding-count N` records the total
  findings in this exact review (integer 0..999999999, not a confidence or
  line number); `--prior-relation regression-of-cure|uncured|new` records the
  relation of **all** those findings to the previous cure. The relation requires
  a count. Mixed or uncertain causality must omit the relation. Both flags
  precede evidence, occur at most once, and are included in verdict retry
  identity. The library arguments are `finding_count` and `prior_relation`;
  bools, floats, negative counts, and invalid relations refuse. Absent fields
  stay absent on replay, and malformed stored observations remain UNKNOWN
  without dropping the underlying verdict. Pre-observation readers still
  accept the verdict and ignore these additive advisory fields.
  The spiral guard reads these fields through the existing dispatch fold,
  never through prose extraction — the separate FINDING KEY above does read
  path tokens out of evidence prose, and it never manufactures a count from
  them. A pending review is not a zero count;
  disagreement among reviewers is not a total. FINISH is reachable for a
  still-unsettled FIX round with fewer, explicitly cure-caused findings;
  it allows stopping, not landing. A prior relationship that cannot be proved
  through the existing `supersedes` chain remains UNKNOWN/MELD. These fields
  do not retrofit any historical verdict or change receipt/approval authority.
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
  moot, superseded). It binds no reviewed tip, refuses a dispatch whose verdict
  DECLARED a polarity (that one is already honestly closed), and drops the row
  from every open / overdue / land read — so a stranded dispatch stops nagging
  the idle-watchdog without laundering a review that never happened.

  **It also closes the ADVISORY row, and that is a second admission rather
  than a widening of the first.** A verdict that declared NO polarity
  authorized no land and demanded no cure, so it is advisory by construction
  and an ADVISORY CLOSE is its one honest terminal. Every other door refuses
  such a row by correct reasoning about a different shape — this verb because
  a REVIEWED row is not cancelled, `lr close` because a row carrying no exact
  tip is not a land request at all (`project_raw` needs `tip`) — which left
  the population doorless while `helm owed` re-delivered its nag hourly. The
  close is recorded as an ordinary `cancel` event carrying `advisory: true`,
  its reason PREFIXED with `advisory-closed (verdict declared no polarity): `
  so the terminal names itself in the field every listing prints, and the CLI
  answers `ADVISORY-CLOSED` rather than `CANCELLED`. The operator's reason is
  budgeted against the composed string, because the reducer budgets the
  composed string. A row the land-request ladder ALREADY retired is refused:
  a cancel appended over a `close --reason landed` would be a second,
  contradictory claim about the same work.
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
  **A LIVE READER IS A SEPARATE, EARLIER REFUSAL.** The evidence gate asks
  whether the recipient CAN act and answers "not measurably unable" for every
  healthy seat — true, and it names nobody. So a row whose recipient is
  measurably live AND measurably mid-read is refused first, by its own rung:
  live means a live pane, an armed inbox beacon, or a tool boundary inside the
  freshness window; mid-read means the row is OPEN with its delivery OBSERVED.
  The refusal names the reader, the evidence that they are live, the row's
  state, and the two doors — WAIT for the verdict, or `--force --reason
  '<why the live reader must lose it>'`. Under the override the cancel reason
  records that it WAS an override and names the seat the row was taken from,
  so the reader who loses the row mid-read can see who took it and why. A
  recipient none of the three liveness surfaces can see is the stranded case,
  and the evidence gate above keeps whatever answer it had for it.
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
  A build retip whose base was REPLACED rather than fast-forwarded persists
  `base_replaced` on the event and the hop, and renders it at the CLI and in
  the recipient's notification: the tree under identical patches changed, so
  work in progress may no longer apply.
  so this is never a history rewrite and never a successor mint — the
  supersedes chain is reserved for real ROUNDS of work, and a rebase is not a
  round. REFUSED on any non-open row (a verdict BINDS the tip it was written
  against), on a same-tip no-op, without a `--reason`, on a `--repo` naming a
  different repository, on a ref that does not resolve, on DIFFERENT WORK
  (review rows: the per-commit ordered patch-id sequence must match — a rebase
  changes every sha and no patch — and when both tips already sit on trunk the
  reviewed patches themselves must match, because two empty ranges say nothing
  about the work; build rows ask a DIFFERENT question, because what a build
  recipient inherits is the FINAL CONTENT ON THE NEW BASE while a review
  reader read the HISTORY. CONTENT IS TRUNK-FREE: the old tip is REPLAYED
  onto the new one over their own fork point and the emitted tree must equal
  the new tip's exactly — no authority, no config, no remote and no HEAD. A
  patch hash cannot do this job, because it ignores hunk LOCATION: the same
  block with the same surrounding context at two places yields ONE id for an
  edit to either copy under `--stable` and `--verbatim` alike, while the
  trees differ. A history-only REORDER with a byte-identical result is
  ACCEPTED for a build and refused for a review; a MERGE that resolved a
  conflict by hand cannot be lost, because a replay carries whatever the
  merge produced. DIRECTION IS THE ONE AUTHORITY QUESTION, and content
  cannot answer it: when trunk's movement is TREE-NEUTRAL both spellings
  have identical content and only the BASE is older. So the authority is
  DECLARED (`helm.trunkRef` naming a canonical SOURCE ref, optional
  `helm.trunkRemote`), OBSERVED once at dispatch with a freshness receipt
  from the VCS seam, and PERSISTED on the row with the tip's base under it;
  a retip CONSUMES those and never discovers a trunk. The binding MOVES with
  the row on every hop, or a second retip would prove its direction against
  the original dispatch's base. On a build row an unreadable content replay,
  NO BINDING (every row predating the schema, which keeps FF retip and the
  cancel-plus-fresh-dispatch escape), a DECLARED authority that cannot be
  observed, an authority CHANGE, or a base not provably forward all REFUSE —
  each naming which proof was missing, because the recipient's tree is
  equally stale whichever unknown it was. On a review row an
  UNANSWERABLE patch-sequence identity also REFUSES, naming the missing proof
  rather than claiming the work differs. Restore the proof and retry, or send
  a fresh dispatch with `--supersedes` to state the new obligation), on a row
  whose OPEN successor already supersedes it (the
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
  inert at fold. New retips require `verified`; historical `unverified` hops
  still replay as recorded, not as new authorization. The stamp is the
  writer's recorded TESTIMONY, never
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


**A brief is stored WHOLE, by reference, and the row keeps a bounded copy.**
Every list read parses every ledger line, so the body on the row stays under
`MESSAGE_BODY_CAP`; the brief itself is written to one content-addressed file
under the helm home **before** the row that names it, so a row can never point
at a file that does not exist and a kill between the two leaves an orphan file
rather than a dangling reference. The row carries the file's byte length and
its blake2b-128 digest, and every reader that renders a brief — `triage`, the
rebind note, the compose contract — recomputes that digest over the bytes it
read before showing the text. A missing file, an unreadable one, or one whose
digest does not bind falls back to the row's bounded copy **with a loud line
saying so**, because a bounded copy presented as the whole brief is the defect
this store exists to end: four consecutive sends measured 10606, 7714, 7440 and
6906 bytes and stored under 4000, each cut before or inside the coverage list
the brief existed to carry.

**`send` refuses a brief over 32768 UTF-8 bytes rather than cutting it.**
Measured briefs run 5-11 KB and the largest ever recorded was 10606 bytes, so
the ceiling is roughly three times the biggest brief that has existed. It is a
BYTE bound because the existing 16000-CHARACTER bound is 16000 bytes of ASCII
and up to 64000 bytes of multibyte text — the character bound stops meaning
anything exactly where this one starts binding. The sender is present and can
split the work; the recipient of a cut brief cannot tell that anything is
missing.

**`briefs [--cut]` is the census of what the cap already ate.** Read-only: it
lists every OPEN row whose brief survives only as a truncated copy and carries
no reference — rows sent before this store existed, whose tails went out in the
original DM and are not in this ledger. Bare gives the denominator (how many
open briefs are whole by reference, whole on their row, and cut); `--cut` is
the table alone, for piping. The cure is not retroactive, so the census is how
the integrator finds what to re-send.

**`collisions [--json]` lists every event the fold dropped for a reused seq.**
Read-only. The fold takes one event per seq on a row, so an event that reuses
a seq an applied event already holds is on the ledger and no reader honours it.
A helm older than an event kind on the row writes exactly that. Each line is
LIVE or HISTORY. A LIVE line is on a row that is open or held and that no
successor carries. `helm doctor` WARNs on each LIVE event and names it, because
someone must decide whether to record it again with the trunk helm. A HISTORY
line is on a row that has ended: closed, retired or superseded. The doctor only
counts those, and its count is the number of HISTORY lines here. `--json` is
the whole list, and each entry carries its row's `ended` word and, for a
superseded row, the successor that carries it.

**EVERY REVIEW ROW GETS A qwen27 FINDINGS PASS, AND IT IS A NOTE (task/2960).**
When `send` or `add` files a review row, or `retip` re-points one, helm starts
`python3 -m helm.findingspass <id>` in its own session and returns at once:
filing never waits for the pass and never fails because of it. The worker
queues on one flock under the helm home, so one pass runs at a time on the
host, and then runs the configured reader script (`HELM_LOCAL_REVIEW_SCRIPT`,
else the `local-review-script` [local name](ENVIRONMENT.md#local-names)) in
place with `--judge`, the row's tip and checkout, this repository's `docs/preread-checklist.md`, and the
endpoint seat_catalog's `qwen27` entry names (its `pool_default` provider's
`base_url` plus `/chat/completions`; no host is written in helm). The whole
markdown output is stored by reference beside the ledger, the way a brief is,
and a `findings-note` event puts one line on the row, plus the kept
`[JUDGE: REAL n/3]` blocks verbatim (bounded, with the cut marked) and the
script's status line whole:

    qwen27: no findings (complete, 5 reads). Not a review, not an approval.
    qwen27: 2 finding(s) for the approving reviewer to adjudicate (complete, 2 reads). ...
    qwen27: PARTIAL read, not clean — errors=1; READER ERRORS, so these were NOT read: ...
    qwen27: ABSENT review (reader down or erroring), not clean — READER ERROR: ...
    qwen27: NOT RUN — the local-review script is missing at ...
    qwen27: NOT FINISHED — the pass ran past its 3600 s bound and was stopped. ...
    qwen27: FAILED — exit 0 says complete, but the status line is missing ...

The note is **never an approval, never a gate and never the different-model
read**: it changes no status and no obligation, nothing that reads a verdict,
an advisory read or a family reads it, and `verdict --reviewer-model qwen27`
is refused. "No findings" is written only for an exit 0 whose status line says
`complete` with both counts; any other answer, or any disagreement between the
exit code and the status line, is a line that says it is not clean. A note
lands only on an OPEN or HELD row. `helm dispatch triage <id>` prints it under
a named row, and both verdict paths say it back. To read a row again once the
reader is back, run `python3 -m helm.findingspass <id>` from a helm checkout; a
row that already has a complete or partial read of its tip is skipped.
`HELM_QWEN27_FINDINGS=0` switches the pass off (see `docs/ENVIRONMENT.md`).

### `helm lr list [--all] [--json] | show <id> [--json] | stalls [--json] | foldcheck <tip> [--gate gate:TOKEN] [--repo PATH] [--remote R] [--branch B] [--no-fetch] | legacy-completion-hints [--json] | land <id> [--json] | compose <id> [<id>...] [--trunk REF] [--repo PATH] [--bounded-concur] [--dry-run] [--json] | close <id> --reason landed|superseded|withdrawn|out-of-scope|stranded|subsumed|delivered-report|discharged|resolved|carried|chain-proof|expired|endorsement-moot [--evidence LINE] [--artifact-ref REF] [--report-ref CHAT_REF] [--tip FULL_SHA] [--repo PATH] [--trunk REF] [--live | --needs-restart WHAT] [--compose-manifest PATH --compose-gate gate:ID] [--dry-run] [--json] | annotate-delivered-report <id> --artifact-ref REF --report-ref CHAT_REF --evidence LINE [--json] | discharge <id> <full-superseding-tip> <evidence...> [--json] | withdraw <id> <evidence...> [--json] | abandon <id> --reason TEXT [--repo PATH] [--json] | close-landed <id> --trunk REF [--repo PATH] [--json]`

**`--reason endorsement-moot` — the door for an endorsement over work that
LANDED.** It is `expired` read in the other direction. A CONCUR is the one
verdict that authorizes nothing, so only two doors ever admitted it, and both
of them require Git to prove the reviewed work ABSENT from trunk. A concur
whose work is ON trunk therefore had no terminal at all — and that is the
common case, not the edge: every door refuses it, each for a sound reason of
its own, and the composition leaves no exit.

**It is a missing cell, not a new exception.** One git fact — this row's own
reviewed tip is an ancestor of trunk — is asked by four doors, one per verdict
shape: an authorizing APPROVE closes `landed`, a contrary FIX or SUPERSEDE
closes `resolved`, a row with no verdict closes `discharged`, and a CONCUR had
nothing. The other two non-approve cells were each given their own reason
rather than folded into `landed`, with the same argument stated both times:
LANDED is a claim about this row's work at HEAD under THIS row's authority.

**It grants nothing, and the law is what admits it.** "Concur authorizes
nothing" has a permissive consequence beside its prohibitive one. If the work
is on trunk and this row authorized nothing, then this row did not put it
there — the landing necessarily holds some other authority. Retiring the row
against that measurement records where the authority was NOT, and transfers
none, which is the same sentence `withdrawn` and `expired` carry over a
measured absence. Every LANDING and SETTLEMENT door still refuses a concur.

**The proof is two facts and the boundaries are both directions.** The verdict
must classify `advisory` — stricter than `expired`, which also takes a
pre-tier APPROVE, because an approve over landed work already has `landed`.
The tip must be an ANCESTOR of the pinned trunk: a patch-identity match is
refused, because an identical delta proves somebody wrote the same lines and
never that this endorsed work landed. An ABSENT tip is refused by name and
sent back to `withdrawn` or `expired`. The terminal renders SUPERSEDED, never
LANDED — the obligation ended because the work reached trunk under an
authority that is not this row's, and a row that rendered LANDED would let
every board and burn-down read an endorsement as an approved landing.

**`helm lr expired` — the door for a review nobody can act on.** A CONCUR, or
an APPROVE carrying no record-time authorization-tier evidence, recorded
against a tip that never reached trunk, is closable by no other reason:
`landed` needs it on trunk, `superseded` needs a successor, `withdrawn` needs
a proof of absence the row does not carry, `resolved` needs the row's own tip
landed. Nothing is wrong with these rows and nobody is late — the reviewer did
their job and the author moved on — but they are the bulk of what keeps the
board from reaching zero. `review_hold_kind`'s existing `advisory` and
`pre-tier` are exactly the verdicts that authorize nothing; `authorization-
held` is deliberately EXCLUDED, because that approve can still carry a land.

**The verb IS the census, and it is dry.** `helm lr expired` reports three
buckets and closes nothing; `--apply` is the act, and `--json` the shape.
There is no `--evidence`: the predicate yields the disposition AND the line,
so every row records its own measured reason rather than one operator sentence
stamped across a batch whose rows differ. There is no `--repo` or `--trunk`
either — they are the row's own, derived as every sibling close derives them.

**Three answers, never two.** ADMIT, REFUSE and **UNMEASURED** are separate
buckets, and any UNMEASURED row makes the run exit non-zero — the census AND
`--apply` alike: "could not tell" must be countable apart from "measured and
no", because a door that closes work may not let an unreadable input decay
into an absent one. A row whose land state is UNKNOWN, whose reviewed tip is
missing, or whose lane refs could not be walked, answers UNMEASURED and stays
open. **A row can become UNMEASURED during `--apply`**, after the census
admitted it — the lane refs were readable during the walk and are not now —
and it is reported UNMEASURED there too, never folded into the refusals.

**The census previews; the DOOR re-measures.** `land_state` on the projection
is a cached reading that can predate a merge or a fetch with no ledger write
of its own, so `--apply` asks the landing question again, per row, against the
exact trunk object it just proved the repository can read. A row the census
listed ADMIT whose tip reached trunk in between therefore REFUSES at the
write, and an unreadable answer stays UNMEASURED rather than counting as an
absence. The empty-lane guard cannot cover for this and makes it worse: a lane
whose work has landed has no commits of its own any more, so it stops
protecting the tip at exactly the moment the row must not close.

**A live lane's tip is never expired, and an EMPTY lane protects nothing.**
The guard asks whether a lane carries commits of its own (`rev-list --count
trunk..tip`), not whether the tip is some lane's tip: claiming a lane cuts a
branch at trunk and writes nothing, so the naive form would let every fresh
claim shield a row reviewed at that commit and would weaken each time somebody
claimed a lane. The batch also runs the mass-termination guard first — a
repository that cannot read its own trunk object closes nothing at all.

**It reads the projection `lr list` reads** — the warm body when one is
available, the cold replay otherwise — and it SAYS WHICH, because a cold read
from a seat that does not own git observation reports UNKNOWN where the board
reports ABSENT, and a census that hid that would answer a board nobody has.

**A ROW OUTLIVES THE SEAT IT NAMES, and `list` now says so.** `author` and
`reviewer` hold a seat name as a plain string and every consumer compares
holders by exact canonical-token equality, so a row addressed to a name
nobody answers to used to render identically to a row somebody owes work on
today. The owed leg is resolved through `seats_lineage.seat_lineage`, which
answers one of three states from one validated roster snapshot: a live row
answers to the name, the rename lineage `seat rename` writes names a
successor (**RENAMED** — the pair simply reports the successor and the row
needed no rewrite), or nothing answers at all (**ORPHANED**). An unreadable
roster, malformed lineage on any row, and a key claimed by two rows all mean
the question was NOT ANSWERED and mark nothing — ORPHANED is the loudest
claim available and it may not rest on the weakest evidence.

**ORPHANED IS A RENDERING AND IT EXPIRES NOTHING.** `owed_by`, `stalled`, the
deadline and every enforcement reader are untouched, and no row is closed,
re-bound or aged out — roster absence is TRANSIENT (four names moved
absent-to-present in thirteen minutes during the census that motivated this),
so a board that expired rows on this predicate would eventually expire a live
seat's work while it was between sessions. The cure is `helm seat reassign
<old-name> --to <seat> --apply`, which moves the BYTES; the mark names it.
Both states ride `/api/lr` as `owed_seat_standing`, absent rather than null
when there is nothing to say, and `holder_seat` reports the successor on a
RENAMED row. The mark asks the **owed** leg only, the one that actually holds
the ball; a dead seat on the OTHER leg is silent until the ball moves
(task/2343).

**`seat rename` CARRIES THE HOLDINGS NOW.** Renaming a seat used to move the
roster row and every keyed state file while leaving each dispatch row, task
row and worktree lease spelling the old name — `helm seat reassign` has been
the one door for those since it was built and nothing invoked it. `rename_seat`
now invokes it, after the roster lock releases and after the row has moved, so
the source is a name nothing answers to and the target is the same agent under
its new key. It never passes `--force`: if reassign's live-source refusal
fires, it STANDS and is reported in the rename's own message. A source proven
to hold nothing skips the verb; an unreadable census does not. Nothing there
can fail the rename, because by then the row has already moved.
The message counts what the verb moved, read from its own result, and not
what the rename's census listed under the old name. The census matches that
name canonically, so it also lists rows the new name already holds: a lease
stored exactly as the new name, or a dispatch row already addressed to it. The
verb moves neither, so a rename can print `Carried 0` beside the census count.
A dispatch row already addressed to the new name, or a sent row whose delivery
leg already belongs to it, is named as such and not counted as carried. An
exit 1 prints no count, a result with no moved rows to count prints no count
either, and a census that could not read every surface makes only the count
before the rename UNKNOWN.

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
**After the five PASS it names the leases the trunk now carries**, because a
land releases none of them and every board kept drawing those lanes as
building. `LEASES ON LANDED WORK` lists each held, live, clean lane room whose
committed work is on the trunk (`work.landed_leases`, over the same verdict
`helm work list` prints) with the exact `helm work release` line its holder
runs; `KEPT` lists the rooms it must not release yet — a landed tip under a
DIRTY room, and a lane the trunk carries only part of (its merge-base with the
trunk is a commit its own reflog records authoring), with the commits beyond
it. It releases nothing: a lease is bound to its holder's token and no verb
holds an authority over another seat's. Advisory, after the verdict, silent
when empty, and it never changes the exit code.
### `helm stale sweep [--dry-run] [--quiet] [--json] [--ensure-timer]`

**The standing HOUSEKEEPING loop** over aged open work (task/445). Detection
and terminals both existed already — `helm lr stalls` renders the per-stage
STALLED thresholds, `helm dispatch triage` re-measures a row's claims on
demand, `helm dispatch cancel` and `helm lr close --reason superseded` are the
attested doors — but every one of them is **pull-only**, and pull-only
staleness cost the fleet a 2-day orphaned build row and 3-day pending reviews
in one week. `stale sweep` is the **push**: it walks lr loops past their stage
threshold, open dispatch rows past their deadline with no visible progress,
and task-ledger rows untouched past 3d, re-measures each with the SAME
machinery those surfaces use (`clearspan` claim re-measurement; `landreq`'s
ancestry→patch-id landing predicate; the lr projection's own `base_behind`),
and **posts one digest per row-owner** proposing a disposition per row —
`supersede-candidate` naming the carrier sha when the reviewed content is on
trunk under a new object, `cancel-with-reason` when a dispatch's claims rotted
**and** its content is proven absent, `retip-candidate` when the base drifted
past the measured bar, `reanchor-needed` when every task citation rotted but
the work's truth remains unknown, or `still-live-keep` with the evidence line.
**It PROPOSES and NEVER EXECUTES**: the reply form is CONCUR/OVERRULE per line
(task/431's async convergence), and every terminal or citation-edit verb stays
the owner's — the bot only points at the measured next action. Attention
budget: one post per owner per sweep, a row re-proposes only after 3d **or**
when its proposed disposition CHANGES, unowned rows ride one
integrator digest, and an undelivered digest never latches its rows (so the
next sweep re-covers the window rather than marching past a dropped ask).
`--ensure-timer` installs the daily systemd user timer with a clean identity
(`HELM_CHAT_NAME=stale-bot`, session vars unset — repo-watch's measured
identity-hygiene lesson); the `helm doctor` line reports rows swept /
proposals filed / oldest-unproposed age plus the loop's own last-run age, so a
dead timer is visible — a watcher whose death is silent is the defect it hunts.

### `helm mcpd serve [--port N]`

**The stateless MCP endpoint** (spec rev 2026-07-28, stdlib-only, owner-ruled hybrid — the two owner decision cards of 2026-08-05). One localhost POST route serving `server/discover` | `tools/list` | `tools/call` over the **same verb layer the CLI fronts**: the CLI remains the universal floor and the diff oracle (`test_mcpd` pins both front-ends equal on one input — the owner's own reasoning: two implementations of one spec are a differential test neither can fake). Compliance shape: mirrored-header validation (`-32020`), version gate (`-32022` with the supported list), unknown method/tool (`-32601`), Origin validation (DNS-rebinding defence), `resultType` on every result, `ttlMs`/`cacheScope` + deterministic ordering on lists, two-tier errors (protocol vs `isError` execution text a model can self-correct from). **What MCP is NOT here**: the attention transport — beacons/wake stay `Monitor` + `helm chat wait`; an MCP notification informs a client process and never wakes an idle model. Slice 0 = shell + read-only `store_resolve`; slice 1 adds `chat_read`/`chat_post` + one work verb behind per-seat bearer auth, with server-side signing (the owner's signing verdict) behind a flippable seam so the proxied-provenance fallback stays one flip away.

### `helm derive [--repo PATH] [--trunk REF] [--families] [--json]`

**The state the ARTIFACTS support, beside the one the ledger remembers.**

`helm lr` projects a row's state by folding the dispatch **event ledger**, and
consults Git only for rows an explicit verb has *already* closed. So a row
reads `AWAITING_BUILD` because somebody once ran `dispatch mark-delivered` and
nobody has run a closing verb since — and an OPEN row is structurally blind to
its own work reaching trunk. Measured on the live estate 2026-08-05: **17 of 31
`AWAITING_BUILD` rows had their work already folded to trunk.**

This verb asks the artifacts instead. Branches, commits, trees, patch-ids, gate
receipts and verdict polarity are written by machines and cannot be forgotten.
It prints every row whose derived state **disagrees** with the ledger's, because
agreement is not news.

| state | what the artifacts had to show |
|---|---|
| `UNSTARTED` | the lane branch exists and carries nothing past the merge-base |
| `BUILDING` | commits exist; no receipt binds the head commit's tree |
| `BUILT` | a structured `delivered-report` close: the artifact WAS delivery, nothing was owed to trunk |
| `GATED` | a receipt binds the head commit's **exact** tree, `dirty=false`, `head==head_after`, status OK |
| `APPROVED` | the above **plus** a cross-family `approve` bound to that exact tip |
| `REVIEWING` | an open review row — its obligation is a verdict, and the lane branch belongs to the build row |
| `REVIEWED` | the verdict is recorded (`fix`/`supersede`/`concur`/undeclared, or an `approve` that has not landed); it authorized no landing |
| `LANDED` | trunk carries the work — by ancestry, by patch-id content identity, by a trunk commit naming the row id, or by a structured `landed` close |
| `SUPERSEDED` | a successor row carries the obligation |
| `CANCELLED` | the ledger withdrew the row: nobody owes anything, whatever its branch did |
| `UNKNOWN` | the artifacts do not answer — never a claim |

**The ledger's own terminals outrank inferred progress.** A cancel event, a
recorded verdict polarity and a structured close are machine-written artifacts
with the same standing as a commit; measured live before this rule, 139
change-demanding FIX/SUPERSEDE verdicts and 100 cancelled rows derived `LANDED`
off the ancestry of tips that were somebody else's landing. What stays
untrusted is the fold's reading of *absent* verbs, never the recorded events.

**Landing is not ancestry.** The integrator rebases, so the commit on trunk is
patch-identical and object-different and `merge-base --is-ancestor` truthfully
answers "no". Content identity by `git patch-id` is what sees it, and a single
commit in a stack that trunk does not carry is decisive *against* landing. A
merge commit has no patch-id, so its silence is refused rather than skipped.

**A lane NAME is not a binding.** `fold: <lane> at <sha>` subjects match every
sibling round of a chain, so only the row's own dispatch id counts.

**`--families` is opt-in.** Cross-family evidence resolves from the model or
provider — never the seat label, so `codex` and `codex-2` are ONE family while
`codex` and `ds4pro` are two — and a proxied seat costs a live canary of
1.5–2.0s. Without the flag an otherwise-approvable row honestly reads `GATED`.

**Cost**: the artifacts are set-shaped, so they are read once per repository
(~3.3s, dominated by two streamed `git patch-id` passes) and every row is then
answered from dicts at ~14µs. All 1473 rows on the live estate derive in 20ms.
`helm.rowstate.derive(row, world)` is a pure function — artifacts in, state out,
no I/O — and returns the evidence beside the state so a reader can check the
derivation instead of trusting it.

**An absent artifact is never a positive claim.** A reaped lane branch and one
that never existed are the same absence, and both read `UNKNOWN`.

### `helm lr list [--all] [--json] | show <id> [--json] | stalls [--json] | legacy-completion-hints [--json] | land <id> [--json] | compose <id> [<id>...] [--trunk REF] [--repo PATH] [--bounded-concur] [--dry-run] [--json] | close <id> --reason landed|superseded|withdrawn|out-of-scope|stranded|subsumed|delivered-report|discharged|resolved|carried|chain-proof|expired|endorsement-moot [--evidence LINE] [--artifact-ref REF] [--report-ref CHAT_REF] [--tip FULL_SHA] [--repo PATH] [--trunk REF] [--live | --needs-restart WHAT] [--compose-manifest PATH --compose-gate gate:ID] [--dry-run] [--json] | annotate-delivered-report <id> --artifact-ref REF --report-ref CHAT_REF --evidence LINE [--json] | discharge <id> <full-superseding-tip> <evidence...> [--json] | withdraw <id> <evidence...> [--json] | abandon <id> --reason TEXT [--repo PATH] [--json] | close-landed <id> --trunk REF [--repo PATH] [--json]`

**`compose` stands N READY lanes on ONE measured tip** in a detached `<repo>-wt/compose/` room: members compose in caller-given order, each member's patch-id is re-measured across the cherry-pick with one instrument for both sides, and every refusal NAMES the member (conflict, drift, already-landed by patch-identity, partial stack, capped-screen UNPROVABLE). It runs NO suite — the one gate the caller runs on the composed tip is the only per-tree evidence there is (an APPROVE carries on CONTENT; a gate rebinds the TREE). On a red composed gate, localize by PREFIX: re-compose the first K members; the longest green prefix lands and the first red member evicts to its own re-gate. `--dry-run` composes, measures, reports, and removes the room.

#### Prospective bounded CONCUR exception

`compose --bounded-concur` is a separate, default-disabled admission path. It
never changes a historical `REVIEWED` row to `READY`, never makes bare CONCUR
land-authorizing, and gives no privilege to a particular integrator name. The
original immutable **review dispatch brief** must start with this exact line,
followed by nonempty scope text (substitute full lowercase commit IDs):

```text
helm-compose-land/1 base=<FULL_SHA> tip=<FULL_SHA> bounded=1 effects=reversible
<bounded review scope>
```

The base and tip must match the ledger's immutable work pair: the chain-recorded
base and this review's exact tip. A missing, truncated, multiply marked or
hash-mismatched brief refuses. Its existing CONCUR/MEASURED verdict evidence
must be exactly the following statement, with the original full brief's native
BLAKE2b-128 UTF-8 message hash substituted (32 lowercase hex characters):

```text
helm-compose-land/1 brief=<BRIEF_HASH> pass=complete findings=0 effects=confirmed
```

Absence of findings is not a zero-findings statement. Partial passes, findings,
unconfirmed effects, inferred/unverified basis, missing record-time reviewer
identity/tier evidence and unknown source reads all refuse. Reviewer authority
is resolved from the recorded verdict runtime proof and immutable policy
history, not today's roster or policy; later rename or retirement does not
revoke the recorded review. The existing cross-family review obligation still
applies; the hash authenticates neither review quality nor code authorship.

**Effects have two independent inputs.** The reviewer confirms the brief's
comma-separated declaration, and Git independently measures every commit in
the bounded range against the exact canonical owner filenames in
`helm/compose_contract.py` (`PROTECTED_OWNERS`, with ownership comments).
Only the declaration `reversible` can qualify. Recognized protected declarations
are `schema`, `ledger-version`, `hooks`, `cross-seat`,
`administrative-terminal` and `ledger-mutation`; `unknown` refuses as UNKNOWN.
Either a protected declaration **or** a protected-owner diff vetoes the
exception. Rename/copy endpoints, deletions and type changes participate;
changing an owner and then reverting it still vetoes despite a clean net diff.
Malformed statuses, unreadable paths, empty commits, noncontiguous ranges and
merge commits refuse conservatively. There are no filename-pattern heuristics.
This guard plus attributed reviewer judgment is **not** a proof of semantic
completeness, and Git's ability to revert bytes does not imply reversible
effects. CLI/process delivery class is a separate declaration.

Composition still proves each member's content survived the carry. Bounded
compose requires a registered project with readable, active composition-proof
activation; inactive or UNKNOWN activation refuses this new writer rather than
silently omitting the fold stage. Ordinary activation rules are unchanged.
`--repo` may name the main root, a linked worktree or an existing subdirectory:
registration, activation, room placement and the canonical manifest home use the
resolved main project root, while Git operates from the supplied working path.
An unreadable registry refuses compose rather than treating UNKNOWN as an
unregistered project. A genuinely unregistered ordinary compose retains its
existing unscoped behavior; bounded compose still requires registration.

The sole standing artifact is the canonical
`<project-home>/compose-manifests/<result_tip>.json`, written through
`foldcompose.write_manifest`. It records every actual `row`, `source_commit`
and `result_commit` address, including ordinary cars in a mixed batch.
`compose_land_version=1` and `bounded_rows` explicitly select bounded row groups.
Member bounds in output are presentation only: consumers derive them from those
addresses and the immutable brief. Bounded compose writes **no room-adjacent
sidecar**, and dry-run writes no canonical artifact. A saved output copy cannot
replace a missing or changed project-home record.

The canonical consumer replays every cherry-pick tree and the entire sequential
result chain. Each bounded group's cars must equal its **complete** immutable
source range in order, with no omissions, duplicates or interleaving, and retain
rich content identity. Effects and recorded reviewer authority are independently
re-derived from the same raw dispatch snapshot used for ordinary cars. Ordinary
cars still require the unchanged APPROVE authority check; bare CONCUR does not.
Fold and scoped live close share the **complete** composition validator, including
ordinary-car review kind, APPROVE polarity, source coverage and required review
receipt authority. The locked writer supplies both canonical rows and accepted
verdict history from its existing locked snapshot; a bounded member cannot
launder an unreviewed ordinary car through the close door.

Compose reports `fold_authority` as pending: it produces prospective structural
evidence, **not final fold authority**. Gate the whole composed tree using the
normal authorized gate workflow. `lr foldcheck <TIP> --gate gate:<RECEIPT_ID>`
retains all five ordinary rungs, then passes that explicit candidate receipt to
the separate canonical composition stage, which independently requires a
whole-suite receipt bound to the complete composed tree. A focused receipt,
missing gate, or another tree refuses. No landed-close event is required to
validate this stage. After the exact tree has reached the chosen trunk, close
with the paired options (the manifest path is the canonical path above):

```text
helm lr close <ID> --reason landed --trunk <TRUNK> --live \
  --compose-manifest <MANIFEST_PATH> --compose-gate gate:<RECEIPT_ID>
```

Use `--needs-restart <WHAT>` instead of `--live` for process-held code. The
locked close writer independently remeasures source effects, carried content,
repository, pinned tree and canonical whole-suite receipt; a focused receipt
cannot satisfy this gate. Writer and replay share the proof validator. The
existing close event gains landed proof version **3**, distinct from ordinary
review proof v1 and BUILD-via-APPROVE proof v2. Replay checks captured Git
observations plus immutable verdict/tier/gate sources. Historical event replay
is unchanged: it does not acquire today's canonical composition manifest or
ordinary-car review availability, and does not depend on the writer switch.
Proof anchors identify
bytes, not signatures defending against replacement of the trusted ledger and
its source observations.

Same-tip peers share only candidate content location, not authorization: every
peer needs its own prospective brief and qualifying verdict. Open BUILD siblings
retain the ordinary APPROVE/discharged rules, in preview as well as live close;
a CONCUR-only landing does not make them dischargeable. Close `--dry-run`
appends nothing and reports both eligible and refused peers. Retry can finish a
partial sweep without another terminal event on the already-closed row, even
after trunk advances: each scoped peer revalidates the original composition and
historical pin through the canonical primary close, then proves its own admission.
An arbitrary caller-supplied historical pin is not accepted. A compose dry-run's
manifest cannot authorize close. `lr land` remains a diagnostic witness, not a
merge actuator or a substitute for these checks.

**Reader-before-writer rollout:** first deploy readers supporting landed proof
v3 everywhere the ledger is consumed, plus canonical bounded-manifest/fold
consumers, and confirm that compatibility externally. Project activation is a
separate existing control, not proof that compatible readers were deployed.
Only then may the operator explicitly set `HELM_COMPOSE_LAND_V1=1` for the new
compose/close writers. The unset default is disabled, including dry-run entry
through this path; readers never depend on the switch. The switch is an
operator assertion, **not** deployment evidence or compatibility proof. Older
readers reject this proof version, so writer-first rollout is unsafe. Turning
the writer off stops new scoped writes; it does not undo existing close events.

The **land-request lifecycle**, projected from the dispatch ledger plus Git's
observation of trunk. One dispatch already binds author, lane, the exact
review commit (`--ref` tip), and the reviewer's verdict, so a land loop is
those facts read together rather than a second writable workflow record.
`list`, `show`, and `stalls` are read-only; `land` writes diagnostic evidence,
while `close` (and the still-separate `abandon`) append narrow proof-specific
terminal annotations without rewriting the standing dispatch facts.

`close <id> --reason R` is **the one terminal verb**. Thirteen reasons (the
register is `landreq.CLOSE_CLI_REASONS`, test-pinned), one

proof ladder each, every rung tri-state (unreadable is UNKNOWN and UNKNOWN
refuses), the trunk pinned to one sha at ladder entry and recorded:
**landed** proves the reviewed change on the pinned trunk (ancestry or exact
patch identity — directly, or through the mechanical rewrite-translation
sidecar `lr migrate --apply` records, rendered `CLOSED (LANDED_REWRITTEN)`);
its ordinary proof-v1 path admits approve and UNDECLARED verdicts and refuses FIX/SUPERSEDE (a
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
(`close_contradicted`), owed by the integrator. **It is also the terminal for
the third answer to a FIX**: not *I will cure*, not *I already cured*, but
*you are right, this should not exist*. An author who accepts a finding in
full and deletes the artifact has no successor tip and never can have one —
`dispatch cancel` refuses (a reviewed row is not cancelled) and `--supersedes`
would mint a fresh review obligation over nothing — so this reason is the one
that ends the chain without asserting a cure. It admits a FIX-verdicted row,
and the `--evidence` sentence is the half git cannot carry: either the
resolution was carried out, or the verdict is accepted and the artifact should
not exist, with the refutation's location named. The door binds NO author on
purpose (the integrator carrying out a verdict is a legitimate closer, and
older verdicts predate stable author binding), so the closing hand is RECORDED
(`withdrawing_seat`) and `lr show` names it — *by the AUTHOR X — the verdict
was ACCEPTED, not ignored*, or the disclosure that the closer is not the
author — because to the reviewer who wrote the finding, a row that ended with
no cure and no successor otherwise reads as a verdict ignored.
**out-of-scope** retires a
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
domain, #177: a build row whose successor was minted first has no verdict of
its own and sat doorless while its work ran on trunk) whose work is on
trunk under a successor's landed, gate-verified APPROVE — the authority is
the discharging row's, re-derived from the ledger at write AND replay, never
taken from the event. A HELD never-verdicted row takes the same door only
under a SOURCE-CLEAN hold, the reader's structured claim of zero findings. A
verdict is where findings live, and a close reason is not a verdict, so an
ordinary hold (its findings, if any, live only in its prose), an owner-gated
hold, and a row whose advisory read names a finding or an exit answer are
refused. The refusal names the verbs that pay what is owed: release the row
and record the read as a FIX, which then closes as `superseded`, or hold it
again `--source-clean` when the read found nothing. `helm dispatch list`
prints that same answer as a second line under the row once its chain has
ended: DISCHARGED IN FACT, and a VERDICT is owed. **resolved** is the polarity-wrong door: a
FIX/SUPERSEDE verdict row whose OWN reviewed tip reached the pinned trunk by
ancestry (patch identity routes to subsumed; absence is withdrawn's
question), confirmed by one later same-repository cross-family review
verdict — approve or supersede, never fix — whose evidence OPENS with
`Resolution verified on trunk: ...`; the overridden reviewer is recorded on
the close event, so the contest path stays auditable.
**endorsement-moot** is the fourth cell of that same table: a CONCUR verdict
row whose OWN reviewed tip reached the pinned trunk by ancestry. A concur is
the one verdict that authorizes nothing, so the only doors that ever admitted
it are the two that prove the work ABSENT — and a concur whose work is PRESENT
could open none of them, which is the common case rather than the edge. The
door claims exactly two measured facts, the verdict classifying `advisory` and
the tip being an ancestor, and together they say the landing holds an
authority that is NOT this row's: a row that authorizes nothing cannot have
authorized the land it is retired against. So the close grants nothing and
transfers nothing, which is why the terminal renders SUPERSEDED and never
LANDED. It is stricter than every sibling on polarity — concur alone, because
an approve over landed work already has `landed` — and it refuses patch
identity for `resolved`'s reason and an ABSENT tip by name, sending that row
back to `withdrawn` or `expired`.


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

**A LAND CREDITS EVERY AUTHOR ITS CHAIN RECORDS.** A lane may carry several
authors — the lane owner, plus any reviewer whose committed cure the chain
rebased onto or cherry-picked, recorded as `patch_tip` / `patch_author` on that
round's FIX verdict. `close --reason landed` therefore derives the credit list
from the SAME projection its polarity gates read (`landreq.chain_credits`: this
row first, then the rows sharing its `chain_root`, each name once, blanks
dropped), attaches it to the result as `credited_authors`, and prints an
`AUTHORS` line whenever there is more than one. One author prints nothing — the
ordinary case is not worth a line, and a line on every close trains the reader
to skip it. A close that named only the sender wrote the reviewer out of work
the reviewer wrote.

**ONE LAND CLOSES EVERY ROW BOUND TO THAT COMMIT.** The owner asked it
plainly: "even if new reviews create new rows, when something lands they
should all close, no?" They did not — the landed ladder recorded one id and
returned, so ONE unit of work sat on his pipeline view as several live rows at
several ages (measured 2026-08-12: 46 live rows over 34 distinct lanes, one
lane carrying three rows on one tip in three different chains). Both landed
doors now sweep every other live row in the SAME repository whose bound commit
is byte-identical, after the row itself is durably closed:

* **The discriminator is the commit, never the lane.** A lane is a LABEL —
  free text, renamable, reusable by unrelated work — and the same-lane
  discharge tier was ruled in once and withdrawn. Two rows naming the same
  40-hex commit are not similar work, they ARE the same content, so the land
  discharges all of them by identity rather than by inference. Parallel review
  legs have no chain edge between them by construction, which is why the chain
  walk alone could never see this shape.
* **A peer re-proves itself.** An approve-verdicted peer goes through its own
  landed ladder; a never-verdicted one through `discharged`, whose new TIP
  tier is strictly stronger than the chain tier (identical content landed, not
  merely a successor). Only the trunk pin and the delivery declaration are
  inherited — both are the operator's statement about the same content.
* **A FIX or SUPERSEDE peer on that tip STAYS OPEN.** Findings are debt, not
  duplication, and the landed door refuses that polarity at the front already.
* **The rung a re-tip leaves behind closes on the same land.** The sweep also
  names every chain PREDECESSOR of the landed row that never got a verdict and
  whose bound tip is an ANCESTOR of the landed tip, so the work it bound is in
  that head byte for byte. Each goes through `discharged` on the CHAIN tier. An
  OPEN rung closes. A HELD rung closes only under a source-clean hold, and any
  other hold rides back as a refusal that names the verdict it owes. A rebased
  rung is not contained, so it is not named here and keeps its own doors.
* **Fail-open and legible.** The land is real before the sweep starts and
  nothing in it can take that back; refusals ride back as data and the CLI
  prints which rows also closed and which stayed open. `--dry-run` names both
  lists, and an idempotent retry finishes a sweep left partial.

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

`retire <id> --reason <code> [--note TEXT] [--seat NAME] [--dry-run]` is the
ADMINISTRATIVE terminal, and the one that claims nothing about the work. It
says only that helm MEASURED this row's proof chain unreachable, so nobody can
act on it and no counter should keep charging for it — never that the work
landed, never that it did not. That is why it is its own event kind and not a
`close --reason`: every close reason is a claim about the change. Six measured
reason codes, each re-run under the ledger lock immediately before the append,
every UNKNOWN refusing: **repo-unreadable** (Git for the row's own repository
cannot be read), **succession-unreadable** (the declared succession carrier's
proof cannot be read), **reviewed-object-destroyed** (see below),
**tier-unevaluable-parked** (an APPROVE whose reviewer's approval tier is
measurably DARK and whose seat holds no session and no live pane),
**author-unresolvable** (the owing seat is measured absent), and
**author-absent-lane-idle** (see below). A shared
liveness belt refuses every one of them while ANY party on the row is
reachable, because a live party can take the normal path and the normal path
produces a claim about the work. `--sweep` is belted for every reason without
exception; the single-id door sets the belt aside for one reason and narrows
it for one more, both described below.

**reviewed-object-destroyed** is the exit for a row whose own reviewed proof
object no longer exists. Three independent measurements, none sufficient
alone: the tip is a commit id `lr refs` audits; it is MISSING from the row's
own repository (bare `cat-file -e`, rc 1) in a repository that first proved it
can read its own HEAD object; and the recorded rewrite sidecar does not
translate it to a live object. The refusal names which of the three failed. A
surviving lane-family ref does **not** veto it — that is the whole difference
from `close --reason stranded` and from `abandon`, which both claim the work
is gone and so must veto. The object is gone, therefore that ref is not the
reviewed work; the first three ref names are recorded in the measurement, with
a `(+N more)` count when there are others, so a reader is told both where to
look and how much the line left off. It is also the one reason the liveness
belt can be set aside — **only at the single-id door**, `retire <id> --reason
reviewed-object-destroyed`, one named row and one explicit integrator act. The
normal path there is adjudicating a commit that does not exist, so no living
seat can take it, and a proxy for "can anyone act" cannot outrank a direct
measurement of it; the measurement records that the belt was set aside and
why. `retire --sweep` gets no such exemption: an unattended pass runs this
reason under the full all-parties belt like every other, and refuses while any
custodian, sender or recipient is live.

**author-absent-lane-idle** is the exit for a FIX-verdicted row whose author
left the fleet while the unlanded work stays on its lane branch. Five
measurements, each refusing on its own contradiction or on UNKNOWN: the row's
owing role is the author (a recorded FIX verdict); the author reads ABSENT
through the same reachability instrument every reason uses (roster, rename
aliases and alias evidence, the presence projection, the pane census, and a
ledger silence of the 7-day author-silence window), its roster lineage
answers ORPHANED, and helm's spawn register holds no record for it. The
register is every `spawn.json` the `registered_seats` walk can see, beside a
seats entry or under its `instances`, whatever config sits beside it: a record
whose config.yaml is gone is still helm's own and refuses PRESENT naming the
record, without promising a resume. A regular file under the seats root or an
`instances` directory, such as the port allocator's `instance-ports.json`,
holds no register and is not read. A spawn record or directory that exists
and cannot be read makes the author's presence UNKNOWN and names the path, and
so does a dangling link at either. A name a
renamed row still carries is PRESENT, and the cure is the renamed seat's move;
a name with its own roster row is out of scope for this reason. No same-chain
successor is still on the board, and a row on the walk whose chain identity is
UNKNOWN, absent or names another chain, or a row other than the chain's root
that carries this chain and whose parent field replays UNKNOWN, names no row,
or is absent or null, refuses as unresolved topology naming that row; the lane branch the row bound when it was dispatched
(`ref_branch`, never guessed from the lane label) exists and has no commit
inside the same 7-day window; and neither the lane tip nor the reviewed tip
landed on the authoritative trunk, read by ancestry and then by patch-id, so a
cherry-picked land counts as landed. Either tip on trunk refuses, naming both
readings and no command: `close --reason landed` refuses a row whose own
verdict is FIX, and `close --reason superseded` needs a later same-chain
APPROVE this reason does not measure. Every other refusal names what it
measured, and a command when one moves it; unresolved topology is ledger
damage and names none. The measurement leads with the branch and the full tip it
**retains**: retirement touches no ref and no worktree, and `helm work gc`
keeps an unlanded branch for triage. At the single-id door the liveness belt
does not ask about the row's reviewer, whose recorded FIX verdict discharged
its move; it still asks about every party that owes one (the author and any
custodian), and the measurement names the reviewer it set aside. `retire
--sweep` runs this reason under the full all-parties belt.

`retire --off-frontier [--apply] [--all-projects] [--json]` is the census over
the rows that are not on the **live frontier** at all, and the header term the
owner reads. A non-terminal row is ON the frontier while ANY of four readings
says the work is still somewhere a person can take it: a ref in its own lane
family exists in its own repository, a worktree room of that family is checked
out, a live lease in the claims ledger names the lane, or the row's tip is held
OFF TRUNK by a live branch or a checked-out room. A row that fails all four is
OFF the frontier — the work either reached trunk under a later tip, or it was
abandoned and the lane reaped — and until this verb no door retired it, so it
sat in the `open` count and read as queued work. Three measured reasons:
**landed-by-ancestry** (the row's tip is an ancestor of the authoritative
trunk), **landed-by-patch-id** (the tip carries the same patch identity as a
commit on trunk, which is how a rebased or cherry-picked land looks), and
**abandoned-unreachable** (the tip still resolves, is not on trunk, and NO ref
in the repository reaches it).

**The fourth reading is the one that needs no name, and it is there because a
name is not an identity.** A row's `branch` field carries the dispatch ref —
measured, 286 of 286 rows in one retire plan held a bare sha there — so every
name-keyed probe runs on the free-text `lane` label alone, and a label written
as `review-derive-row-state` does not stem-match the branch
`lane/derive-row-state-from-artifacts` it is about. Two GUARDED, locked, leased
rooms were listed for retirement on exactly that miss. The commit-keyed rung
asks `rev-list <live refs and room HEADs> --not <trunk>` once per repository
(20ms for 899 refs here) and tests membership per row. `--not <trunk>` is its
correctness: a tip that already reached trunk is contained by every ref that
contains trunk, so the question is not "does anything reach this commit" but
"is it held where only a lane can be holding it". Archive namespaces
(`refs/helm-retired/`, `refs/lane-backup/`, rescue and backup refs) are NOT
live — `work/_gc.py` copies a lane there before it deletes the branch, so the
archived copy is the normal end state of every reaped lane on this board.

**The census IS the default and it is dry**, the shape `helm lr expired` and
`retire --sweep` already use; `--apply` is the act and `--json` the shape.
There is no `--dry-run`, because that names a mode already on, and no
`--reason`, `--repo` or `--trunk`: the verb measures every row itself and each
row's repository and trunk are its own. `--apply` routes each row to the
EXISTING reason registry rather than a vocabulary of its own, and **the ladder
decides, not this verb**. Each one re-proves the claim under the ledger lock,
so a refusal comes back in the ladder's own words, the row stays open, and
nothing is forced. A second `--apply` writes nothing the first did not: a row
that closed is terminal and leaves the population, and a row the ladder refused
appended nothing, so it meets the same ladder and gets the same answer.

**THE ROUTE IS TWO FACTS: the git measurement AND the verdict's polarity**
(read from the chain, so a row whose own field is silent still routes on the
contrary a chained round declared). The measurement says where the work is; the
polarity says whether anybody is still owed a cure.

| measurement | non-contrary (approve / undeclared) | contrary (FIX / SUPERSEDE) |
| --- | --- | --- |
| landed-by-ancestry | `landed` (with the CLI-class delivery declaration) | `carried` |
| landed-by-patch-id | `landed` | `carried` |
| abandoned-unreachable | `withdrawn` | `withdrawn` |

**The `landed` column is measurably empty today, and an arm says so.** A
non-contrary row whose reviewed tip reached trunk is TERMINAL before this verb
sees it — the projection observes the landing, reads the row LANDED, and it
leaves the open bucket. The entry stays because it is the right door for that
measurement should such a row ever arrive here; what does not stand is a route
nobody exercises, which is what `stranded` was. The non-contrary rows that DO
reach the census are the ones whose work never landed, and they go through
`withdrawn` with the rest of that bucket.

Reading only the measurement is what made the first cut close nothing at all:
it proposed `landed` for every placed row on a board that is 295 of 301 FIX,
and met the same correct refusal every time — `close --reason landed` refuses a
contrary whose reviewed tip is on trunk, because the tip arriving is a CONTRARY
and not a resolution. The doors that DO admit this population were already in
the registry. `carried` exists for exactly it ("a FIX-verdicted row whose work
landed anyway"), admits every polarity, and gates on a measured carriage
witness. `withdrawn` is the absence door and asks the question the
`abandoned-unreachable` bucket already measured — off trunk by ancestry AND
absent by patch id. `stranded` is no longer routed to at all: it demands a
PRUNED object while this predicate proves the object PRESENT before it will
answer `abandoned-unreachable`, so that route was refused by construction and
the plan promised a door that could not open.

**`superseded` is not routed to, and that is a measurement.** It is the door
`landed`'s own refusal lists first among the doors for contrary debt, but
its ladder needs a later APPROVE VERDICT ROW on the same work chain that clears
the approval-authority rung — not merely a later tip. Asked against the live
ledger with each chain-derived successor tip, it refused every sampled row with
the same sentence: no later approve binds the superseding tip. Routing there
would have been a second door that cannot open, which is the defect `stranded`
already was.

**The refusals remain the product for the rows no door admits.** MEASURED on
the live ledger: `carried`'s witness cannot be taken for an EXACT-ancestor tip
(the replay range is empty, and the reached-trunk witness has nothing to
match), so that bucket comes back refused in the instrument's own words. Those
rows are task/2334's doorless population, now enumerated per row with the
measurement that puts each one there — and the door that fits their shape is
one register over: `retire --reason author-absent-lane-idle` is the same row
with its lane branch still present.

**PLACED IS NOT CLOSABLE, and both numbers are the owner's.** The
classification proves a row is residue; the close ladder still demands a proof
of its own, and the two are different questions. MEASURED over the whole placed
population of a copy of the live ledger: of **318 placed rows, 136 are closable
now** (122 through `carried` on the patch-id bucket, 14 through `withdrawn` on
the abandoned one) **and 182 are refused** — every landed-by-ancestry row among
them, because `git cherry`'s range for a tip already reachable from trunk is
EMPTY and an empty range affirms every possible trunk. So the surfaces print
two numbers: `N closable now` and `M placed but NOT closable yet`, and the
per-row refusal — the ladder's own sentence — prints beside each row in the
census. Before this split both surfaces printed the placed count as the count
`--apply` acts on, which was wrong about 57% of the rows it asked the owner to
act on.

The split is the DOOR's answer, not a model of the door: `off_frontier_closable`
runs the `carried` and `withdrawn` ladders in DRY RUN — the same functions
`--apply` reaches through `close`, with the same evidence — on the same walk
that classified the row, so a rung added to either ladder moves this number
without anybody editing it. It can only UNDER-count: a route with no preflight
answers "not closable" with its own name rather than promising a close nobody
measured. `--apply` then acts on exactly the rows that walk admitted and
reports the rest as REFUSED in the sentence the preflight already read.

**That answer is not free and the cost is measured.** One board read on a copy
of the live ledger went from **3.1s to 96.3s** for 1147 rows, and essentially
all of it is `git cherry` inside the reached-trunk witness, once per placed row
(~300ms each), because that witness recomputes trunk's patch identities for
every tip it is asked about. The board absorbs it: `/api/lr` serves
stale-while-revalidating with a 600s truth cap sized for a ~250s rebuild, and
`helm lr list` reads that warm body in milliseconds — only a cold replay pays.
The instrument that would remove the cost is already in the tree
(`_stored_patch_index`, which is why the classification itself is cheap), and it
belongs inside `rowworld._reached_trunk` where that proof has ONE owner, never
in a second copy here.

**UNCLASSIFIED is a bucket, and `--apply` never touches it.** The predicate
fails closed on every reading that can fail to happen: the repository is
unreadable, the ref table is unreadable, the worktree registry is unreadable,
the claims ledger is unreadable, the off-trunk walk failed, the tip no longer
resolves because git gc dropped it (task/2383), the landing ladder ran out of
its derive budget, or the row's chain polarity cannot be derived. One further
case is measured and still refuses — the tip is not on trunk but some ref
OUTSIDE the lane family still reaches it, which is work retained under another
name and a row a human should read. Every one of them is counted, printed with
the rung that produced it, and left open. The lane-family match is the same
generous, refusal-only probe the `stranded` ladder's fourth rung uses:
over-matching costs one row left on the frontier, under-matching would cost a
false terminal.

**The classification's cost is measured and its rung order is the reason.**
One walk of the open board on a copy of the live ledger: **2.6s for 1184 rows,
205 spawns**, on top of the projection the surface already pays. It was 150s
and 576 spawns when the rungs ran in the obvious order — 111 rows reached
`_landing_proof`'s absence leg, which then ran `ls-remote origin` plus a full
reflog scan PER ROW to ask the remote about a tip this repository cannot
resolve, and every one of those rows ends UNCLASSIFIED whatever the remote
says. (The ladder no longer has that leg: an object this clone cannot resolve
answers `unknown`, never `absent`.) The order is now: the KEPT proof (no
spawn, and it can still place a row whose object is gone), then `cat-file -e`
(one local read; a tip that does not resolve here is task/2383's population
and no later reading can place it), then the derive. Rows this board
withholds are not classified at all.

**The header stops printing a bare `open` count, and its residue term is the
verb's own count.** `helm lr list` and the web board's filed strip both render
`N open on the live frontier` as the first number and the off-frontier residue
as its own term naming this verb, from one owner (`landreq.frontier_verdicts`,
read by `filed_split` and by the census on the same walk) so the strip and the
command it names cannot report different populations. The first cut had them
read two different predicates and the strip said `541 OFF-FRONTIER … not work
owed` while the verb reported 286 placed plus 255 it refused to place —
the owner was told 255 rows were debris that no verb would ever clear.
UNCLASSIFIED rows now count as ON the frontier — work owed, the only direction
that cannot under-report a backlog — and the line discloses them by name inside
that number (`incl. N unclassified — lane gone, tip not placeable`).

**One reading answers both surfaces, and the census takes the caller's
projection.** The two used to agree only by luck: measured in one process on
one copy of the ledger, `filed_split` read 304 placed rows and the census read
316 on the very next line, because each opened its own `project_raw()` and
`open_bucket_rows` membership moves with the wall-clock derive budget a
projection gets. `off_frontier_census(lrs=…, raw=…)` now accepts the projection
its caller already holds, so within one answer the header's residue, its
closable split and the verb's plan are one computation over one population.
Two CLI invocations are still two reads — nothing can make a later read equal
an earlier one — and that is why the strip stamps the instant it was computed.

**The claims ledger is read STRICTLY on this path.** The lease rung is the one
reading the frontier calls decisive on its own, and it used to answer "no live
lease names this lane" over a claims file nobody could open: `claims_list`
reads through the lenient `pk.read_json(path, {})`, whose catch-all answers the
DEFAULT, so a corrupt, truncated or mode-000 file arrived as an EMPTY TABLE and
an absence cleared the row. The walk now asks `seats_claims.claims_unavailable()`
— the same strict reader the claims MOVER takes — once per walk, and an
unreadable claims file makes every row in the census UNCLASSIFIED.

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

**A LANE IS READY ONLY WHEN A SEAT THAT WROTE NONE OF ITS CHAIN HAS APPROVED
THE FINAL TIP.** Independence is a property of the CHAIN, not of one row. A
chain contributor is any seat the ledger records as an AUTHOR — a round's
sender, or the `patch_author` of a round whose FIX carried a cure — on ANY
round of the same chain, scoped by the bound repository plus the chain root and
never by the lane label. The `SELF-REVIEW` rung therefore fires when a
contributor approved but no eligible outsider approved the final tip, and
`list`, `show` and the land nudge all name it the same way: *contributor approval
present (`<seat>`), outsider approval missing*. A contributor's APPROVE is still
recorded and still counts toward the both-agree half of the procedure; it
simply never supplies the independent authority. An approval on an EARLIER tip,
or on another chain in the same repository, is a read of different work and
does not travel.

A sibling APPROVE passes **the same row-local eligibility** as the displayed
row: recorded approval-tier evidence, the writer's gate capability at the
accepted verdict position and frozen epoch, any required token, and presence
of its own receipt in the same reader's receipt index. An outside-tier or
PRE-TIER verdict, or a missing required token, cannot supply outsider authority.
Unreadable tier/capability evidence or an absent receipt instead answers UNKNOWN
(`READY-UNVERIFIED`) if no other eligible outsider read supplies it. A supported
row with no explicit `kind` remains eligible. This is not a new kind filter or
a deeper receipt version/host/tree check; verdict-time binding remains its own
owner. No historical reviewer checkout path supplies verification context.
Contest, repository observability and stale-base diagnostics apply once to the
consuming row and retain their own rungs, rather than becoming self-review
accusations. None of this replaces the canonical whole-suite land gate.

Unreadable chain identity is likewise UNKNOWN, never an empty contributor set:
an empty set would say nobody wrote this chain and make every stranger
independent exactly where helm knows least. An absent legacy repository means
the home repository, as at the dispatch write door; a present damaged identity
or malformed root is not a new chain. Its uncertainty follows actual chain
links only, never unrelated rows sharing a tip, lane label or UNKNOWN sentinel.
Seat comparisons use canonical recipient identity without rewriting historical
display bytes. The recorded contributors are conservative
**submission provenance** — the seat a cure was submitted through, taken from
the row's own recipient — and never a proof of Git authorship: helm does not open the commit
to ask who authored it. Every recipient, tier, gate token and cross-family
check is unchanged and still required; this rung is one more rung, not a
replacement for any of them.
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

### `helm reviewers <row-id> [--all-seats] [--json]`

Who can review this row right now, and for every seat that cannot, the ONE
conjunct that excludes it. A list of who CAN is half an answer; the half that
closes the loop is why each of the others cannot, because that is what tells a
reader whether to WAIT, to REPAIR, or to ROUTE ELSEWHERE.

Eligibility is six conjuncts that lived on six surfaces nothing joined:

* **tier** — the approval-tier policy. A seat outside it is a valid reviewer
  whose findings are INPUT; its APPROVE cannot close the row. The policy's own
  sentence prints, because that is where the valid set is.
* **mint** — the immutable verdict-time runtime-family evidence without which
  no authorizing verdict can be written in a seat's name. It had no CLI at all,
  and a whole fleet once read healthy while not one seat could mint.
* **chain** — already recorded as an author of this chain, so its APPROVE is
  not the independent read the row needs. Until this verb that surfaced only in
  a refusal AFTER a reviewer turn had been spent.
* **awake** — the same `can_take_work` the dispatch write door refuses on,
  labelled by shape: DEAF is a seat with no live beacon, healthy on every other
  axis, that takes the obligation and never works it; WALL/`<state>` is its
  vendor path not answering, and a PROXY-COOLDOWN or PROXY-LOCAL-403 there is
  HELM'S OWN proxy and measures nothing about the provider.
* **budget** — the pooled family ceiling proxywatch persists. Only a fully
  measured cap refuses; a partly-over pool ADMITS and prints its headroom
  beside the seat, so nobody makes a cost decision without knowing it.
* **pane** / **idle** — the pane tail the owner reads by looking, asked of one
  seat at a time and only of seats nothing cheaper excluded. GONE needs a
  respawn; RUNNING needs nothing at all.

The ladder runs from the refusals nothing local can change to the one that
clears itself, so a seat is named under the FURTHEST-OUT reason it cannot
review — calling an out-of-tier seat BUSY would send a reader to wait for an
answer that could never arrive.

Nothing here is measured twice: every rung calls the authority that owns the
fact and prints that authority's own words. An unreadable input is never a
refusal — a conjunct that cannot be measured renders UNKNOWN, names the input
it could not read, and leaves the seat a candidate in its own printed block,
because "no eligible reviewer" and "I could not read the roster" are different
worlds with different repairs. Exit 0 is at least one fully measured eligible
seat with every shared input read; exit 1 covers both the measured empty set
and the unread one, since a caller gating on the code needs the same stop for
"no" and for "I cannot tell" — the text is what tells them apart.

Candidates are the seats whose register home room serves this row's project,
never a family's numbered seats as a pool; `--all-seats` widens and the count
of seats homed elsewhere always prints.

### `helm preread <dispatch-id> | --ref <sha> --repo <path> [--no-dm] [--json]`

A COUNCIL OF CHEAP READERS PRE-READS A REVIEW ROW, and the result is reading
material — never a verdict.

The row's tip is diffed against its merge base with trunk through the
`helm/vcs.py` seam and split one chunk per file, code and docs first and tests
last, each file capped at the config's `cap_bytes` (a longer file is sent
truncated and the pre-read says which). Every file goes to every configured
reader endpoint under every seed it declares, each carrying
`docs/preread-checklist.md` — the defect classes this repository has been
bitten by. A **different** model then judges each file's drafts, keeps only
concrete defects on a quoted line, and writes a `Dropped:` paragraph naming
what it refused; a final small call merges the per-file judgements. Judging per
file is load-bearing: one judge call over a whole change's drafts answers NONE
FOUND and discards true findings it never had room to read. Last, a citation
check drops any finding whose backticked quote does not appear in the diff's
added or context lines. Only a NUMBERED block counts as a finding: the judge is
asked for a `Dropped:` paragraph and answers NONE FOUND when it refuses
everything, and counting that prose as a dropped finding makes the number the
reader acts on move when nothing was found.

The merge is a RENDERING, never the evidence. If it carries no numbered finding
while the per-file judgements do, the per-file union is the answer and the file
says `merge_empty: yes` — the same capacity failure that forced judging per
file, one level up. The per-file answers are written down verbatim either way,
so nothing the judge said is lost to a summariser.

Why the scaffolding: a single call to a cheap model over a whole change
recovers none of a strong reader's targets and loops; the same model, per file,
with the checklist and several seeds, raises them in its drafts. The judge and
the citation check are what cut the false rate that buys.

`helm preread <dispatch-id>` resolves the ref, the repo and the recipient from
the dispatch ledger and DMs that recipient the path (`--no-dm` does not);
`--ref`/`--repo` reads a tip that has no row yet. The result is one file under
`<helm home>/_global/prereads/`, plus one line in that directory's
`ledger.jsonl` ({ts, row, ref, readers, judge, drafts, kept, dropped, cost,
wall_s}) so review quality per model and per rung stays measurable. When a row
has a pre-read, `helm dispatch triage <id>` prints one line pointing at it.

**A pre-read mints no verdict and never writes the dispatch ledger.** Weak
models read; they do not approve. A reader or judge failure is recorded against
the file it happened on and never aborts the run — the file lists every file
nobody read by name.

Every endpoint, model, host and key lives in `<helm home>/_global/preread.json`
and none of them in the source; see `docs/preread-config.example.json`. An
absent config refuses and names the path. The judge's bearer token is read from
the file the config's `key_from` names, handed to exactly one request, and
never printed, logged or stored.

## ops — health, evolution, the browser

### `helm brief [--hours N] [--json]`

The operator's morning brief, composed from what the estate already knows —
read-only everywhere, never probes the network. Sections are omitted when
empty: **SINCE YOU LEFT** (session activity in the window, bucketed by
project; `--hours`, default 12), **KNOWLEDGE DELTA** (store entries
added/updated/retired + drain receipts + inject-ledger turn stats),
**SEATS** (the freshest cached quota observation per account — no cache says
`quota: run helm creds`), **WAITING ON YOU** with two explicitly separate sets:
**ESTATE HEALTH** (mechanically inferred owner gates: the interview, queued
attestations, stale project pointers, prem/prior duplicates) and
**FLEET-FILED OWNER ASKS** (actionable `owner_gated_queue` rows from the local
integration board, oldest first, each with its recorded age). Informational
board rows are not owner debt and stay out. A malformed or missing timestamp
renders `age unknown`, never a fabricated ancient age. A configured board that
is unreadable or whose queue is not a list renders the fleet asks as `UNKNOWN`,
never empty; an absent optional board is proven empty. `--json` prints the raw
dict, including `waiting`, `owner_asks`, and `owner_asks_unavailable`
separately.

```console
$ helm brief
helm brief — 2026-07-19T07:27:15Z (last 12h)

SINCE YOU LEFT — 7 sessions, 4 projects
  helm                   3  Review Helm 0.1-alpha release verification
```

### `helm doctor [--ensure] [--probe-memory] [--quiet]`
Health report over the whole estate: home layout, registry,
adoption, the adopted store, know-your-user, the recall index, env overrides
in effect, cred token families (the shared-family/revocation-bomb audit —
content hashes only, byte-copies = FAIL), the resume-turn state store
(`_global/.state/resumeturn.json`: a store that exists and cannot be read —
malformed or duplicate-key JSON, a non-object, a dangling symlink — is a FAIL
naming its path, because every compaction resume, deaf-in-effect nudge and
episode wake DM refuses as unknown on it; absent or readable is one OK row),
the skills hub link (one FAIL per claude config dir — each real credhome, the
default `~/.claude`, every seat and seat-instance `CLAUDE_CONFIG_DIR` — whose
`skills` entry is MISSING, a link to somewhere else, or a REAL dir, naming the
path and `helm skills sync --apply` as the fix, because a session launched
there sees NO helm skills and nothing else says so; a fully linked estate is
one OK row, no hub configured is no row, a configured hub that is missing is
its own FAIL, and OK is said ONLY over a complete census — a subtree the
discovery could not list is a WARN naming the path and errno, `census
INCOMPLETE`, never an OK over the dirs that happened to be visible), and git
presence (absent = WARN
with the exact per-distro install command; jj/jujutsu noted as a
git-compatible alternative on the radar), and the keepalive cadence — whether
`helm-keepalive.timer` is installed, any hand crontab line that runs the verb
(reported as superseded, never edited), and the age of the last recorded grant,
which WARNs once it passes the ~8h access-token lifetime the loop exists to
stay ahead of, because an unrefreshed helm copy of a healthy account is what
makes the quota page ask for re-logins. Without `--ensure`, no credential
repair is attempted and the ordinary report is unchanged. Exit 1 on FAILs.

`--ensure` backs up discovered authenticated named/default Claude homes through
`cred.backup_all` and credential-bearing Helm-minted seat configurations from
`hooks.seat_homes()`, deduplicated by real path. The bounded seat inventory does
not depend on the invoking process's `CLAUDE_CONFIG_DIR`: nested instance
configurations are included even when cron has no current configuration.
A seat without a credentials file has no pre-image to capture. An unreadable
seat subtree fails the census before backup or healing; unreadable credential
files are backup failures, not absent credentials. Census errors are reported
without private paths or exception contents.

Backup coverage does not include arbitrary configuration directories outside
the named/default and minted-seat inventories. Healing still uses the credential
owner's named-home drift inventory; a seat directory's name is not authority to
choose an account to restore. After backup, `cred.heal` runs with unattended
guards enabled, then health is reported. Any backup failure prevents healing. The credential owner still captures the occupant before eviction,
re-probes live holders, refuses uncertain holder probes, stale/unknown-expiry
or torn snapshots and shared-family/revocation risks, and verifies the restore.
No fresh login is attempted and no hooks or timers are installed or removed.
Unrestored plans and actuator exceptions are FAILs, never a successful no-op;
actuator messages expose counts, not credential paths or exception contents.
Every registry project that is a git work tree is judged against the guard
profile it declares, and three classes fold to one WARN line each: `NO git
leak guard installed` (every planned hook absent), `NEVER SEES A MERGE COMMIT`
(the hooks dir has other guard hooks but no `pre-merge-commit` git will
run — absent, or present and not executable — so a
`git merge --no-ff` lands what `pre-commit` would refuse), and `a stale,
partial or unreadable git guard` (any other stale, missing or unreadable hook
or scanner snapshot). Each line names the projects and the install command.

`--probe-memory` proves that Claude Code still honours the auto-memory base
variable (see `helm hooks install`), with one `claude -p` call on haiku. The
probe reads a credential home whose `projects` is a link in place through
`CLAUDE_CONFIG_DIR`, sets the variable to a scratch base whose path has a
`.claude` segment, and asks for one memory write. It never borrows a home
listed in `<helm home>/_global/probe-reserved-homes.json` (label to reason)
unless that home is the caller's own; a file that does not read reserves
every home but the caller's. A note under the base is OK.
A refused write, or a note that went through the home's link, is a FAIL. No
`claude`, no credentialed home, a timeout or no write at all is a WARN that
says UNKNOWN. The scratch tree is deleted after every run. The result is
recorded per Claude Code version in `_global/.state/memory-probe.json`. Run
it after every Claude Code upgrade. Every ordinary doctor pass also runs the
static check: the program that `claude` runs must still contain the variable
name. Absent is a FAIL that names the variable and the version. A program that
cannot be found or read is UNKNOWN. That row also reports the live probe's
result for the installed version, and a recorded failure is a FAIL. With no
home whose `projects` is a link, neither check makes a row.

The chat-dir row counts the entries in the flat chat directory, the rooms,
the cursors, the per-cursor sibling locks and the meld rooms idle past the
retirement bound. Every delivery hook lists the whole directory, so the row
WARNs above 200 entries per room or 20,000 entries in total, and names the
command for each class it sees: `helm gc --apply` for the locks and the
cursors of sessions that are over, `helm chat retire-rooms --apply` for the
idle meld rooms. An unlistable directory is a WARN that says UNMEASURED.

`--quiet` suppresses ordinary output, but prints FAIL rows and the failure
summary to stderr and retains exit 1. Use `helm cred heal` without mutation
flags to inspect refusals. Unknown arguments exit 2 before any work.

A three-minute scheduler requests recovery **attempts** at that cadence, not a
promise of repair within three minutes. Live or unknown holders, missing or
stale backups, and revocation guards can defer or refuse restoration. Periodic
backup also has a capture gap between attempts; this command does not establish
that the scheduler is running or retire existing boundary hooks.

### `helm rogue [--dry-run] [--json] [--kill|--no-kill] [--quiet] [--min-age S] [--min-cpu PCT] [--grace S]`
The LOCAL rogue-compute watchdog (task/1039) — the box-layer backstop behind
the fab-suite PreToolUse guard and the PATH shims, which only protect
sessions whose config loaded after install and only for the shapes they
enumerate. One pass: scan `/proc` for heavy-compute processes of the
measured classes (python unittest/pytest, node vitest/jest/webpack/next/open-next/opennext
build, pnpm/npm/npx install+run, cargo build/test, go test/build) whose
identity is not a fab wrapper; classify Wrangler and OpenNext Cloudflare
commands as network-bound, alert once, and never signal them; exempt anything
under a fab wrapper or the human escape env
(`FAB_ALLOW_LOCAL_SUITE/BUILD=1`); attribute the rest to a seat by walking
`HELM_CHAT_NAME` up the parent chain (UNKNOWN when absent — never guessed);
require consecutive high-CPU passes, capture evidence (cmdline, seat, parent
chain, start time) BEFORE acting, and update that same record after a signal;
alert `#helm` loudly, addressed to the seat and to the integrator the roster
resolves (`HELM_INTEGRATOR_SEAT`, else `helm-integrator`; when none resolves
the alert still posts and says `[UNROSTERED: ...]`, and the silent-drop alert
is addressed the same way); then, under the owner's standing kill authority, SIGTERM
the whole subtree after a grace window and SIGKILL a pass later.
Classification is IDENTITY-FIRST — the exe decides what a process
is — so a local fab client whose argv contains a suite spelling verbatim is
structurally unflaggable. Every kill re-reads the live starttime first: a
recycled pid is a different process and is never killed on a stale finding.
Defaults kill-on (`HELM_ROGUE_KILL=0` for alert-only), grace 60s, floors
age 30s / 20% subtree cpu (children counted — pool runners burn cpu in
workers). Rides the silent-drop 90s cadence (`helm seat silent-drop`), no
new daemon.

### `helm proxywatch [--post] [--json] [--force] [--install-timer]`
Every fifteen minutes, compose local proxy/seat evidence for every minted seat with
fresh upstream evidence for every family represented by those seats. The local
rungs remain unchanged: CONFIG drift, the silent-DROP latch, stale-pane HANG
candidacy, the invalid-key PROBE, refusal LOG streaks, and the fused TURN ladder.
TURN still calls a seat HUNG only when the pane is live, the transcript is stale,
there are zero in-flight connections, context is below the compact bar, and the
process is not fresh. An open request is THINKING; any unreadable pane, socket,
context, or spawn input is HUNG-UNKNOWN, never HUNG.

The exact-session proxy proof binds the live agent process, listener birth,
loaded config digest, selected credential trace, declared route, and authenticated
canary. The response model must equal that route's upstream model unless the family
catalog declares one exact response projection for that exact full route — alias,
provider, upstream model, and endpoint when present. This is needed for
Antigravity's `gemini-3.6-flash-high` route, whose response envelope names the
underlying `gemini-3.6-flash` model. OAuth config carries no models block from which
to derive that response identity, so the catalog records the authenticated measured
value beside its complete route. No suffix inference, alias-wide projection, or
family-wide wildcard is accepted; an undeclared sibling response still withholds
the proof.

Each posting pass also pops every sidecar's usage queue into the proxy-usage
ledger (`helm proxy-usage`), the write that keeps the sidecar meter
continuous; its rows ride `--json` as `proxy_usage` and the watch state. A
`FAILED-PERSIST` row (the ledger refused popped records, or only the read
marker, in which case nothing was lost) is a finding: the pass exits 1, and
the same stdout line (the one prefixed `proxy-usage <seat> FAILED-PERSIST`)
goes through the chat outbox to the room, every such pass, because
every such pass left the ledger behind the sidecar (lost records, or a
missing read marker that makes the next pass unable to prove continuity).
UNREADABLE rows stay on stdout and never move the exit code.

The unauthenticated PROBE sends a deliberately invalid key, never a real
credential. A `401`/`403` proves the local listener and auth path answer;
connection refusal is DOWN, a client timeout is HANG, and a tiny HTTP 200 is the
EMPTY200 fault that makes an auth failure resemble a dropped completion.

UPSTREAM starts with one deterministic, locally healthy primary per represented
family. Every request is capped at eight tokens; a completed dark response gets
one confirmation, and a still-dark primary then triggers every locally healthy
sibling, whose completed failures can each receive the same confirmation. HTTP
200 counts as HEALTHY only when it contains a valid assistant message with one
of three completed answer shapes: text exactly `OK` (valid thinking carrier
blocks may accompany it), a non-empty thinking-only message stopped by the
eight-token `max_tokens` cap, or a thinking-only carrier with a recognized
terminal stop reason and non-empty string signature. Empty text does not make a
carrier substantive.
Empty bodies are EMPTY200; error-bearing envelopes, non-assistant content, HTML,
and other invalid success envelopes are MALFORMED200. Named provider 4xx/5xx
responses are completed failures. A client timeout or unreadable request never
retries because the first request may still complete.

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

`PROXY-COOLDOWN` is narrower than generic HTTP 429. Helm admits it only when
CLIProxyAPI's typed producer verdict says the refusal origin is exactly `local`
(`X-CPA-Refusal-Origin: local` on the canary response, or
`refusal_origin_v1=local` in the bounded request log). Response prose never
supplies origin. The log marker only classifies that refusal streak; it grants no
restart capability. A canary-written exact-seat record is required for that.
Missing, malformed, mixed, provider, and legacy family-only origin evidence
therefore cannot create local restart capability. The persisted falsification
clock belongs to one exact seat and its verified proxy birth; when
that same-seat local cooldown remains past the recorded bar, consumers may render
`restart=HELPFUL` and only this human prescription: `PRESCRIBES: restart this exact
proxy, then rerun helm proxywatch.` Fresh, malformed, cross-seat,
or otherwise non-exact evidence renders remediation `UNKNOWN`; UNKNOWN is not a
death verdict. Proxywatch never calls a CLIProxy management API, reuses a seat
bearer as management authority, kills/signals/resets/restarts/resumes/reseeds,
auto-heals, or clears the latch. Only a later measured HEALTHY pass clears it.

`PROXY-LOCAL-403` is the same origin rule on a 403: when the producer marks a
403 `local` and no selected trace denies it, the refusal is ours whatever its
body says, so quota wording in it is never `QUOTA-WALL`. `helm burn` reads it
ORANGE on reach (`reach:our-refusal`: our proxy refused this itself, no request
reached the vendor; read the proxy's stated reason, fix it, restart and probe),
never on money, with no reset. It carries no falsification clock, so remediation
stays `UNKNOWN` with that repair named. A proxy 403 without the mark stays origin
unknown and its body decides.

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

### `helm upstream-watch [--dry-run] [--bundle] [--json] [--install-timer]`
The daily upstream-change watcher. Claude Code ships almost every day, and the
fleet kept learning what changed by accident: a model that needs a minimum
Claude Code version, an effort setting that only reaches a model under
`modelSettings`, a renamed settings key. One pass reads each new release and
files the helm tweaks that survive an adversarial refute.

1. **Detect.** A release under `~/.local/share/claude/versions` newer than the
   last one read. With no state yet, the release before the newest counts as
   read, so the first pass reads the newest pair. A rollback reads nothing.
2. **Evidence.** One bounded bundle (at most 200,000 bytes, every cut named):
   a schema-aware diff of the two programs (`helm/upstream_surface.py`: the
   settings keys and their descriptions, every `.describe()` in the program,
   CLI flags and subcommands, `CLAUDE_*`/`ANTHROPIC_*` names, hook events,
   the model catalog, and the bundled skills and SDK type declarations), the
   CHANGELOG entries since the last release read, and the diff of any tier-2
   vendor page whose text changed. The program diff reads the Bun module graph
   inside the executable, not the executable's raw strings, which are mostly
   bytecode churn. A release whose CHANGELOG entry has not appeared waits up to
   two days, then is read without it.
3. **Propose.** One headless `claude -p` run reads the bundle and proposes at
   most six helm tweaks, each citing evidence copied verbatim. A citation the
   bundle does not contain is dropped, and a proposal left with no verified
   citation is dropped before any refute runs.
4. **Refute.** One more run per proposal tries to kill it. Only survivors go on.
5. **File.** Each survivor becomes a task owned by the integrator seat (resolved
   from the roster, never spelled), with its evidence, a proposed diff and the
   refute's reason. A survivor that needs the owner's decision says so in the
   task and the digest. One digest goes to #helm, mentioning the integrator; it
   is **silent** when nothing survived or every survivor is already an open
   task.

**The state advances only after a pass succeeds.** The last release read, the
vendor-page hashes and their text snapshots live in
`~/.helm/_global/.state/upstream-watch.json` and `.state/upstream-watch/`. A
pass that fails (evidence could not be gathered, a model run returned nothing
usable, the ledger could not be written, or the digest did not post) leaves them
where they were, so the next pass reads the same release again; the exit status
is 1. A refusal is not a failure: a survivor that duplicates an open task, or
that the posture guard refuses, is reported and the state advances. A task filed
by a pass whose digest then failed is carried in the state and named by the
next digest.

**Model runs use the `claude` CLI only**, on the login of the Claude Code home
they run on (Max OAuth): `claude -p --model opus --output-format json
--no-session-persistence --tools Read,Grep,Glob --permission-prompts none
--strict-mcp-config --setting-sources project --effort high`, the prompt on
stdin, the working directory the stable helm checkout. The child environment
carries no `ANTHROPIC_*` variable and no `CLAUDE*` session stamp, and `--bare` is
never passed, because bare mode authenticates only with an API key. The runs can
read helm's code and nothing else; the watcher proposes and never writes a
setting, a file or a seat.

`--dry-run` runs every stage, the model runs included, prints the digest and
each task it would file with its owner, and files nothing, posts nothing and
writes no state. `HELM_UPSTREAM_WATCH_DRY_RUN=1` makes every pass a dry run.
`--bundle` prints the evidence bundle the next pass would read and spends
nothing. `HELM_UPSTREAM_WATCH=0` is the off switch. `--json` prints the pass
result. The other knobs are in [ENVIRONMENT.md](ENVIRONMENT.md).

**Tier 2** is a seam in `upstream_watch.SOURCES`: vendor pages that change more
slowly than Claude Code (models, pricing, rate limits, the codex changelog and
rate card). An enabled source is fetched each pass, reduced to its visible text,
hashed, and diffed against its last snapshot; the first sight of a page is a
baseline and is never reported as a change. Only `anthropic-models` is enabled.

**Scheduling.** `--install-timer` writes a `Type=oneshot` user service running
`%h/.local/bin/helm upstream-watch` and a daily persistent timer under
`~/.config/systemd/user`, then daemon-reloads and enables it. The unit carries
the absolute path of `claude` and, when set, `HELM_UPSTREAM_WATCH_CLAUDE_HOME`.
The watcher runs from the timer, never from a seat's session. `helm doctor`
reports `NO ACTUATOR: upstream-watch` until the timer is installed.

Tests run every stage on fixtures: two tiny fake releases, a CHANGELOG file and a
fake `claude` that records its argv and environment. They never run a real
model, reach the network or read the real helm home.

### `helm eval arms [--seat S] [--json] | register [--seat S] [--out PATH]`
The cc-codex vs pi-codex eval's guard rails, which exist **before** its runner
on purpose.

`arms` asks the one question §E-5 says the whole eval lives or dies on: are the
two arms COMPARABLE — same endpoint, same model? Measured 2026-07-29, they were
not, and nothing said so: `cc-codex` reaches codex through helm's CLIProxyAPI on
a subscription OAuth while pi had one provider configured (openrouter). Run as
specified, the eval would have compared Claude-Code-over-subscription with
pi-over-OpenRouter and credited the whole difference to the **harness**. Exit 1
when NOT pinned, naming each mismatched field and what the difference would have
been mistaken for. The pi arm is read from the **installed extension**, not from
what the generator would emit — a generator asked about itself agrees every time.

`register` writes the §C.5 decision rule **before** any run, stamped with the
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

### `helm nouncensus [--noun N] [--ambiguous] [--tier kernel|candidate] [--json]`
The **per-site uses-closure census** for the seat-state nouns (task/1300 slice
2). Who READS and who WRITES each of `actor`, `seat`, `session`, `pane`,
`lane`, `lease`, `routing`, `obligation`, `verb` — one row per SITE, derived
from call sites, never hand-counted. Lease tokens (`lease`, `lease_id`,
`fence`) belong to Lease, not Lane; Verb includes the chat command registries
(`SEAT_VERBS`, `MELD_VERBS`, `_TEXT_VERBS`). Direction comes from `ast.Store`
vs `ast.Load`, so a name that merely looks like a setter is not taken at its
word.

**Why per-site and not per-file.** Many modules touch MORE THAN ONE noun, so
"assigned exactly once" is unsatisfiable at file grain — it would have to hand
`seats.py` to one noun and be wrong about the rest. The claim is structural
and does not depend on the count: one multi-noun module is enough to make the
file-level census a lie, and there are dozens.

**`--ambiguous` is the interesting view.** A site whose tokens span several
nouns is an "assigned exactly once" violation, and the dominant pairing is
`seat` + `session` — the hard rule *Actor ≠ Session ≠ Pane* failing in the
code it governs, measured rather than asserted.

**This entry names the command instead of a count, deliberately.** It carried
frozen figures for one hour and they were already wrong: the module count came
from a hand-rolled regex probe run before this verb existed, and re-deriving
it through the verb on a later trunk disagreed. Two variables had moved — the
instrument and the tree — so the delta was unattributable, which is the whole
argument for not writing a measurement into a document. `helm nouncensus`
answers it live and cannot go stale; a number here always can.

**`--tier` answers R2.** Every row carries its proof tier, so
`--tier candidate` is "attributions resting on evidence the subject could have
forged" (`argv`, `environ`) and `--tier kernel` is observer-side proof
(`/proc/<pid>/exe`, a symlink the described process cannot write).

A module that will not parse is REPORTED, never dropped — a hole in the
closure that disappears makes the census describe a smaller, cleaner world
than the one it measured. An unknown flag or unknown noun exits 2.

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

Owner, 2026-07-29: *"'i won't do X because y (where y is based on clock time,
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
is `headlines-click-to-detail`, an owner-surface law: tell the owner
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
- **The axis is a CHOICE, and the three select very differently.** A row
  declares exactly ONE of `size` / `age` / `count`
  (`tests/test_gc.py::test_every_policy_declares_one_budget_axis` pins it), so
  this is an either/or and not three knobs. **`count: N` keeps the newest N by
  birth and nominates only the surplus** — the live generation survives every
  sweep, which is what makes it the axis for a store you want to keep warm
  (`drift-snapshots` keeps 64 baselines). **`count: 0` hands the decision to
  the finder**: everything it returns is a victim, which is how a stream whose
  members have no retention value at all states that — `chat-cursors` (the
  session is provably dead), `chat-unpaired-cursors` (a live session its seat
  no longer runs, the seat's own baseline kept), `chat-cursor-locks` (a
  per-cursor sibling lock, which nothing has opened since the cursor stack
  took one lock per room; held ones are skipped) and `work-worktrees` (the
  lane has no lease). **`size: N` nominates the WHOLE
  STREAM the moment the total crosses the budget**, not the overflow, so a
  size budget is right for a single file that ROTATES and is a trap on a
  multi-file store you want to keep: one byte over throws the live generation
  away. `age: D` nominates per file by birth stamp and is the only axis that
  reads a clock.
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

### `helm scratch [small|big|durable [--name N]] | gc [--apply] | unattributable [--json] | status [--json]`

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
  routed `TMPDIR` into every seat (opt out with `HELM_SCRATCH_TMPDIR=0`, pin it
  with `HELM_SCRATCH_TMPDIR=<path>`) so `mktemp`/`tempfile`/git temp files land
  correctly with nobody thinking about it. An operator's own `TMPDIR` always
  wins. That routed `TMPDIR` is the one target that is **disk
  unconditionally**, and it is not a preference: the harness derives its own
  per-uid scratch root from `TMPDIR`, so the estate under it is both unbounded
  and unattributable — nothing can ever reap it — and it used to stay on the
  ambient tmp while that mount passed a 60% headroom test. Measured: `/tmp` (a
  24 GB tmpfs, so every byte of it is RAM) sat at 58%, the test passed, the
  routed target was `/tmp/helm-scratch/tmpdir`, and one seat's 9.9 GB
  measurement copy then took `/tmp` to its cap. `big` and `small` still take
  the fast RAM mounts, because those are tagged and therefore reapable. **This
  takes effect at a seat's NEXT launch** — a running seat keeps the env it
  started with — so the reaper scans every routed root it has ever picked, not
  just today's.
* **`gc`** reaps dead-session scratch — dry-run default. **Liveness before
  age**: a tree is reapable only when no live same-uid process references its
  session id and no live cwd sits inside it; age alone never reaps. Only a child
  named by a session id or `pid-<n>` is even a candidate, a tree holding a
  harness transcript subtree is structurally off-limits (that is training
  corpus), and probe trouble reaps nothing. The TTL escalates with real
  pressure — 7d at rest, 6h past 85%, 1h past 95% — and every pass is bounded
  (candidates, victims, stats, processes, handles, entries). The automatic pass
  rides the existing **Stop hook**, throttled to once an hour, a no-op at rest,
  and writes one auditable `pk.event` line that `helm doctor` surfaces. Kill:
  `HELM_SCRATCH_GC=0`.
* **pressure has two planes, and a RAM mount lives on both.** A tmpfs
  percentage answers *how full is this mount* and never *is the box out of
  memory*: a `/tmp` at 44% of its cap read `ok` while the host was 23 GB into
  swap with its compressed swap device 14 of 16 GB full — and every byte of that
  mount **is** that memory. So for a **volatile** mount the level also rises
  with the host's own scarcity, read from `/proc/meminfo` and, where the kernel
  has it, `/proc/pressure/memory`: **warn** at MemAvailable ≤ 25% of RAM or swap
  ≥ 25% in use, **critical** at MemAvailable ≤ 10%, swap ≥ 60%, or PSI `some
  avg60` ≥ 10% of wall time stalled. A tree on **disk** is untouched by this —
  it costs the host no memory. An unreadable `/proc/meminfo` is *unknown*, which
  escalates nothing and reaps nothing.
* **the second tier — a live session is bounded too.** Liveness protects a
  session *tree*, and an integrator session lives for days: one held 5.6 GB with
  879 of 920 top-level entries untouched for six hours. Inside a live tree the
  **unit is the direct child** (and, for the harness's own
  `<tmp>/claude-<uid>/<project>/<session>/`, the children of its `scratchpad/`).
  A unit is reapable *while its session runs* only when **nothing holds it
  open** (a bounded `/proc/*/fd` pass), **no live cwd** sits inside it, it is
  **not a harness-owned name** (`tasks/` — the harness's own subagent-output dir,
  written continuously — or any dot-name such as `.remember/`), it holds **no
  transcript subtree**, it is **not a locked git worktree**, and its newest write
  is older than a short pressure-escalating ttl: **24h** at rest, **4h** at warn,
  **1h** at critical. A registered git worktree is refused twice over — a
  **locked** one because a lock is do-not-disturb, an unlocked one unless `git
  status --porcelain` proves it **clean**, because removing a dirty checkout
  destroys the only copy of that work. A plain **file** under a live
  scratchpad is a unit too (receipts, logs, one-off scripts: 54 of 64 picks on
  a real box) and is removed by `unlink`; a **symlink** is never a unit, never
  followed and never priced through. A reaped tree that *was* a registered git
  worktree leaves a record naming `git -C <repo> worktree prune`, for **every**
  repo involved. Kill this leg alone with `HELM_SCRATCH_GC_TIER2=0`.
* **the third plane — a seat's OWN memory cgroup.** Every seat runs in its own
  `agents-<seat>.slice` with a `memory.high` and no swap, and a tmpfs page is
  shared memory charged to the cgroup that wrote it. So a seat can freeze in the
  kernel's over-high throttle while the box and every mount read calm: three
  full clones a verifier subagent left in its seat's scratchpad did exactly
  that, and the owner saw the frozen pane before helm did. Each pass reads every
  seat slice **now** (`seatceiling.fleet_pressure`): **THROTTLED** when a
  process of the slice sits in state D on the over-high wchan, or the slice's
  `memory.events` high count **rises** across a sampled quarter second — a
  large count that does not move is history and counts for nothing — and
  **NEAR** at 90% of `memory.high` when at least a quarter of that ceiling is
  `shmem` (memory.stat), the memory a swapless slice can neither reclaim nor
  swap. A slice that high in reclaimable page cache reads **HIGH**: the kernel
  frees cache on its own, so HIGH is a quiet badge only — it neither reaps nor
  wakes. An unreadable `memory.stat` reads **HIGH-UNREAD** (`MEM HIGH · shmem
  ?`): the same safe action, under its own name, because cache cannot be
  told from tmpfs — its shmem is published as null, never 0. A pressing
  (THROTTLED or NEAR) seat's live trees on a RAM mount
  are opened first, and their unheld second-tier units of at least 32M reap at
  **ttl 0, biggest first by bytes**, until the picks project the slice under 80%;
  every other predicate of the tier still holds (no open handle, no live cwd,
  no harness-owned name, no transcript subtree, no dirty worktree). Smaller
  units keep the ordinary ttl — a receipt relieves nothing. The throttled seat
  never reaches its own Stop hook, so **any** seat's stop does it: the seat-plane
  read runs at most once a minute fleet-wide and overrides the hourly throttle
  while a slice is pressing, and `helm scratch gc` always reads it. The opening
  of each **spell** (it ends under 80% and unthrottled) posts one wake to the
  seat's home room naming the slice's numbers and its biggest scratch, with
  `@` its lead and the integrator; an undelivered wake retries on the next read.
  The same reading puts `⚠ MEMORY THROTTLED` / `⚠ memory NEAR its ceiling` /
  the quiet `· memory HIGH … mostly reclaimable cache` on the seat's `helm chat
  seats` row and a `MEM` badge on the web roster. A read that does not answer
  (the read raises, its process walk does not complete, or a slice's
  `memory.current` or `memory.high` will not read) puts `? memory UNKNOWN
  (<why>)` on the row and `MEM UNKNOWN` on the badge, and publishes
  `mem_pressure: "UNKNOWN"`. It is never the empty cell, because the empty
  cell means the seat is calm. Kill
  this plane alone with `HELM_SCRATCH_GC_SEATS=0`.
* **a bound may stop a reading; it may not conclude one.** Every probe here is
  bounded and stays bounded — a GC tick must never be the expensive thing. What
  a bound is not allowed to do is *answer*. A handle table that hit its
  descriptor budget, a process list that hit its own, a `cmdline`/`environ`
  read longer than the buffer, a newest-write walk that hit its stat cap, an
  entry count that stopped before it could look for a transcript subtree, a
  worktree scan that stopped before it could say whether a checkout is
  registered, a `git status` the pass had no allowance left for, a parent walk
  that spent its hops before reaching `pid 1`: each of those is **UNKNOWN**,
  and unknown **keeps** the unit and says so — counted in the `gc` report, in
  the audit line, and in the CLI. Where the reading is pass-wide (the process
  table, and a blob read whose missing bytes could name any session at all)
  the whole pass is UNKNOWN and reaps nothing. A unit too big to age cheaply
  is named outright (`too large to age cheaply: N entries`) rather than being
  silently immortal. The gates are re-taken immediately before each deletion,
  so the window between the evidence and the `unlink` is one unit wide.
  **This law is a class, not a sentence.** Written as prose it was broken
  seven times, one call site at a time — the cure for a truncated handle table
  sat two lines under an uncured process list. Every cap, shared budget, wall
  deadline, hop limit and sized read now passes through one primitive
  (`scratch.Reading`) whose only exit is `(value, complete)` and whose refusal
  text is built from the bound itself; a test walks the module's AST and fails
  the build on every shape a bound can take outside that primitive — a bound
  constant or one of the primitive's own bound fields read anywhere else, a
  sized `read()`/`readline()`/`recv()`, a slice with a constant upper bound, a
  `range()`/`islice()` length, and a counter-guarded `break`/`return` or
  `while` head that stops a reading at a literal, at any module-level name, or
  at a local holding one. It tells a COUNT from a SENTINEL by deriving both
  from the module, so the walk that ends at `pid 1` is not refused, and it
  carries a planted violation of every one of those shapes as its own
  must-hit. The one argued exception stays and is now
  visible: tier one may still **age** a tree from a sampled walk, because its
  liveness gate has already proved the session dead, and the sample is marked
  on the CLI line and in the audit line rather than passed off as a full
  reading.
* **the dry run states its own proposal.** `helm scratch gc` ends with a
  per-tier total — units and, since the dry run prices them, bytes — before
  the `UNATTRIBUTABLE` block, which is the list of what the pass will *not*
  touch. With `HELM_SCRATCH_GC_TIER2=0` the report carries the off row, so an
  operator who took the documented mitigation can see that they did.
* **`unattributable`** lists what helm will **never** reap — every child of every
  scratch root whose name helm cannot tie to a session — with size and age,
  biggest first. The refusal is deliberate and stays: helm minted the names it
  reaps, so it knows those trees are scratch, while an unnamed child of a shared
  tmp root is a tree whose *purpose* helm cannot read (another family's working
  copy, a fixture mid-run, a hand-off artifact written for someone to read
  tomorrow), and no age or open-handle probe distinguishes *abandoned* from
  *kept*. What changed is that the cost is no longer silent: the listing appears
  in this verb, in the `gc` dry run, and in a `helm doctor` row once a RAM mount
  or the host is pressured, and it names the only two real cures — mint the tree
  attributably (`helm scratch big|small` tags it with its session, which makes it
  reapable) or remove it by hand once you have read what it is. Sizes are counted
  in blocks, on one filesystem only, under a bounded walk; a `>=` means that
  walk was capped. The doctor row walks under a much smaller budget than the
  verb, and a budget is spent in readdir order — so once any tree in it went
  unwalked the row says **`SAMPLED, NOT RANKED`** and points at this verb for
  the ranked reading, instead of leading with a `Biggest:` its budget could not
  deliver.

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
  workstation / Local disk      write   2737.6 MiB/s · read   4448.7 · random   294081/   24067 IOPS · fsync p95   0.395 ms
  nas-1 / Local disk            UNAVAILABLE — inventory reports this box unreachable

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

Each row also carries a **`vendor=`** cell — `UNAVAILABLE <family> <cause>
since <time>`, `AVAILABLE <family>`, or `UNKNOWN <family> — <why>` — from the
one availability predicate (`seat_usability.availability`) that
`helm chat seats`, `helm lr list` and the web roster all read. It is the ONE
column here that is NOT a live probe: it is the verdict proxywatch's 15-minute
timer persisted, read once for the whole table, so it never gates this verb's
exit code the way a failed census probe does. A seat with no proxy family
prints no cell — the question does not apply to it. **UNAVAILABLE and UNKNOWN
are never the same answer**: the first is a measured wall, the second is helm
saying it could not read the record, and only the first may steer routing.
**A wall needs no relaunch** — the seat's env points at its own long-lived
loopback sidecar with helm's own bearer, never at the vendor credential, so the
next green probe flips the cell back on its own inside one cycle.

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
records the PreCompact payload's `trigger` (`auto` = the harness's native
autocompaction, `manual` = a typed or injected `/compact`), its `agent_id`
(absent on the main thread) and the transcript's position at that instant
(the byte length, inode and device of the payload's `transcript_path`:
`transcript_len`, `transcript_ino`, `transcript_dev`) under the session's key
in the resume-turn state — the trigger is the one fact `seat resume-turn`
needs to stay silent on a native autocompaction and cannot read from its own
SessionStart payload, and the position is what binds the record to THIS
compaction; a bounded wait on the state lock (`resumeturn.PRECOMPACT_WAIT_S`,
2s of the hook's 5s), fail-open (a write refused past the bound writes
nothing, says so on stderr, and leaves the record standing before it; a
position that cannot be taken is recorded as none, said on stderr, and such
a record never vouches — and a path that is not a REGULAR file has no
position to take: a FIFO stats fine and reports a size, so recording one
would send the consumer to open it — the resume leg reads a record only while
unconsumed, inside its window, newer than the entry's last decision and
bound to the transcript it is handed), and SessionEnd writes none — then
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
`seat add|up|down|launch|spawn|where|resume|resume-turn|smoke|autocompact|cred-follow|list|status|doctor`, including `seat lifecycle show|record`) is
wired into the dispatcher; `helm seat --help` lists every subcommand, and the
sections below document them one by one.

**`helm seat reassign <seat-or-session> --to <seat> [--reason R] [--force]
[--apply] [--json]` moves EVERY holding of a dead or renamed seat in one verb
and one ledger event** (the dead-seat holdings mandate): open and held dispatch rows in BOTH
directions, task rows, and worktree leases. Dry-run by default; the manifest
prints either way.

*Why it is a new door rather than a widened old one.* Three verbs already move
work between seats and **all three refuse the dead-seat case**, each correctly
for its own question. `dispatch rebind` demands measured starvation or context
exhaustion — death is neither. `task takeover` demands the incumbent's pane be
measured **LIVE** — the exact inverse of this population. `release_stale`
demands the holder's session be provably dead — which a *rename* leaves reading
live. A dead or renamed seat falls through all three, so there was no gate to
widen and this verb brings its own evidence.

*Why the holdings MOVE rather than being aliased.* A rename writes a
`seat_keys` alias list onto the roster row, and exactly two sites read it, both
in `seats_receipts.py` (chat receipts). `dispatches`, `tasks` and
`seats_claims` all compare with exact canonical-token equality or raw `!=`, so
teaching every comparator about aliases means teaching every comparator,
forever.

*The source resolves by SESSION first.* The population is seats whose NAME
stopped being a reliable key, so a name-first resolver would be correct only in
the cases where the verb is not needed. A token with **no roster row at all**
is the ORPHAN case and is not an error — that is exactly what a rename leaves
behind.

*The target resolves through ONE function.* An exact seat name wins; otherwise
a FAMILY token naming exactly one member resolves to it, and two is an
ambiguity to report rather than a tie to break. That single door is where role
addressing plugs in later without four ledgers changing.

*It refuses only on a measured contradiction.* A source that is measurably
LIVE needs `--force --reason` (moving work out from under a running agent is
how two builders end up on one lane); a target nothing answers to is refused.
UNKNOWN liveness **proceeds with the unknown named**, because this verb exists
for the morning after a reboot, when absence is the normal state of the
evidence — refusing on absence would make it useless exactly then.

*An unreadable surface is never rendered as an empty one.* The manifest
carries an UNREAD channel beside the counts, never folded into the total, and
an unread surface makes even a dry run exit non-zero.

*A target a mover cannot address is refused before anything moves.* A roster
key can be any string, and the movers are stricter than the resolver: the lease
mover refuses a name that is not a seat token (1-64 of `[A-Za-z0-9._-]`), the
dispatch rebind refuses what `dispatch add` would refuse for that recipient,
the custody writer admits only a bare seat token, and a task owner cannot be a
word the ledger prints for absence (`UNOWNED`, `-`). Each mover asked only at
its own turn, so a 65-character target once moved the tasks and then kept the
leases. Now one check asks every rung of every mover that will run for this
manifest, after the census and before the first write, in both the dry run and
the apply; it prints each refusal and exits 1 with nothing moved.
The same check asks what the movers refuse about the reason and the caller:
the custody writer admits one printable line of 256 characters or fewer, the
dispatch rebind's cancel refuses a control character, and the rebind refuses a
process with no seat identity. An operator's `--reason` is refused, never
rewritten; the default reason is made one line every mover accepts. A source
label that differs from the target only in case holds dispatch rows already
stored under the target's name; those count as moved, so a re-run exits 0.
Leases are held by the stored holder, so the lease mover moves only leases
stored exactly as the source's spelling. A lease stored exactly as the
target's name does not count as left behind. A lease stored under another
spelling of the source does not move and the target cannot release it, so it
still counts and the verb exits 1. That includes another case spelling, which
moves when that spelling is reassigned, and the target's name with
surrounding whitespace or a format character, which the claims table shows as
the target's name but which counts until that lease is released or expires.

**`helm seat resume --all [--apply]` is the POST-REBOOT SWEEP, and the
payload of the `helm-seat-rebind.timer` unit.** Measured 2026-08-22, the
morning after a reboot: orca restores every pane by replaying
`claude --resume <sid>` with no environment, so native-Claude seats come back
and every cliproxy seat (its session lives under a per-seat
`CLAUDE_CONFIG_DIR` only its launch.sh sets) is left at a bare shell printing
`35;5;40M` on every mouse move, while `seat rebind --all --apply` refused all
nine seats every five minutes — a correct guard with no actuator. The sweep
prints one row per registered seat (spawn registers, base and instance, plus
orca-adopted roster seats) classified through rebind's own proof: `LIVE`
(re-stamped, never relaunched — on a healthy fleet this is every row and
nothing is spawned), `DEAD-PANE` (rebind refused with exactly its two
zero-count sentences, the claude census agrees, and the register's pane KEY
still resolves to a live writable pane: the terminal-disarm line and then the
launch line are typed INTO that pane, so the owner's layout survives),
`PANE-GONE` (same evidence, orca answers `terminal_not_found`: a fresh pane),
`UNKNOWN` (any other refusal, an unreadable register, a config dir holding no
transcript, a pane that is not writable, a probe error — rendered with its
reason, never folded into skip or dead, and rc 1 under `--apply` so "swept
clean" and "could not tell" exit differently). Dry-run by default. No new
session is ever minted: a relaunch always carries `--resume` for the newest
transcript in the seat's own config dir. Only a register that PREDATES the
current boot (`ts` vs `/proc/stat btime`) is acted on, so the every-interval
timer never relaunches a seat that died mid-day or that the owner closed; the
`<HELM_HOME>/_global/.state/fleet-hold` marker renders every dead row `HELD`
(never SKIPPED), acts on nothing, and is named in the footer.

**An aborted `seat spawn <seat> --replace` LAUNCHES NOTHING.** The abort and
the launch are one decision, not two. When the replace refuses — an unresolved
stale same-name seat, a pane whose terminal state cannot be proven, an
unregistered pane wearing the seat's title, a lock or surface-ownership proof
that failed — no pane is created, no launch asset is re-minted, and the spawn
register is left byte-identical with nothing added to its archive. That holds
because the decision comes first and from read-only evidence: the reap resolves
a reminted pane WITHOUT repairing the register, answers the title-only refusal
before it archives a terminal pane, and writes the register and its archive in
one place, only after the runtime is positively closed; and every refusal the
spawn can answer without writing — an unreadable launch script, a surface the
re-mint could never write — is answered before the reap rather than after it
has stopped the live pane. The refusal SAYS all three, names the register
file the stale evidence lives in, and names the door out
(`helm seat rebind <seat> --apply` to re-point the register at the pane the
seat's live process occupies now, or `helm seat where <seat>` to read what the
register still claims). An operator who is told a replacement aborted and then
finds a pane for that seat cannot tell which register to trust; saying "nothing
was launched" is what makes the next command decidable.

**`seat rebind`'s claimant census counts LIVE PROCESSES, not records.** "N
distinct live panes claim seat S" is an answer about two facts together: a pid
whose `/proc/<pid>/environ` carries `HELM_CHAT_NAME=S`, **and** a pane key orca
still resolves. A register row, a roster row, an orca title and a stopped
beacon's pid authorize nothing on their own. A pane's shells OUTLIVE it — when
orca closes a terminal, the `bash -c` snapshot loader and the
`helm chat wait --seat S --follow` beacon keep running with the closed pane's
`ORCA_PANE_KEY` stamped in their environ — so the /proc walk alone counted a
dead pane's leftovers as a second claimant and rebind refused a seat that had
exactly one live pane. A claimant orca ANSWERS `terminal_not_found` for is
rendered `STALE-RECORD` with its source and its pids named, and the rebind
proceeds against the one live pane. Two claimants whose pane keys BOTH resolve
are still a genuine ambiguity and still refuse. Anything short of those two
answers is `UNKNOWN` — a transport failure, a deadline, any other error reply,
and a successful reply that names no handle, which is orca declining to say and
not orca saying no — and refuses naming the lookup that failed, with a sentence
that is deliberately NOT the two zero-count death sentences, because
`seat resume --all` relaunches off those. It is the same reading of the orca
answer `seat resume --all` uses for `PANE-GONE`.

**`helm seat list` answers USABILITY, not only proxy health.** Every seat row
is followed by one verdict line — `USABLE | DEGRADED | UNUSABLE | UNKNOWN`,
then `turn=` `last=` `pane=` `upstream=` `holding=` `runtime=` and the named
reason. The owner's catch, 2026-08-11: the roster rendered `proxy UP … 239h06m
left` for a seat that had been 42 HOURS dark, and `proxy UP` for one whose PANE
WAS GONE. Both true; neither answered "can this seat take work". Three surfaces
measured that seat — the roster (proxy+cred), proxywatch (turn liveness,
semantic age, starved/hung, the provider wall) and the dispatch/lr ledger (what
it already holds) — and none joined. `helm/seat_usability.py` is that join, over
the readers that already own each fact: one `proxywatch.health` pass with **no
probe and no upstream canary**, the **cached** `upstream_snapshot`, one fold of
the single ledger holding both dispatches and land requests, one process
census, one register read. Four reads for the whole fleet, whatever its size
(`seat list` 0.26s -> ~1.15s measured).

Every unreadable input renders **UNKNOWN for its own field** and never a
healthy default — an unreadable ledger makes the count UNKNOWN, not 0. `n/a`
is different and means the field cannot exist: a NATIVE claude seat has no
proxy, so it has no turn age and no provider wall, while its pane verdict is
still real (the census keys on `HELM_CHAT_NAME`).

**The verdict is a typed answer, and routing consumes it.** `seat_usability.join()`
returns per-seat `verdict / reason / can_take_work / holding / measured_at`;
the rendered line is one consumer of it. `seat_verdict(<seat>)` scopes the
measurement to a single seat and folds NO ledger (the count is a display fact,
so a router never pays for it — the row then reads `holding=-`, which is
"not asked", neither `0` nor `UNKNOWN`).

`dispatches._validate_recipient_usable` is the consumer — the sibling of the
rostered-recipient rung, on BOTH `send` and `add`. Rostered asks whether the
NAME exists; this asks whether the seat can work TODAY. It refuses a dispatch
to a seat that is LIVE and cannot work — a provider wall, a starved or hung
turn loop — naming the cause and the repair, because such a seat takes the
obligation and sits on it until a human notices. A recipient with NO LIVE PANE
is admitted silently: the ledger is durable and its beacon replays open rows on
relaunch, so that is the ordinary between-panes case. UNKNOWN and DEGRADED are
admitted carrying a `RECIPIENT:` note on stderr — a guard that fires on ABSENCE
downgrades every case whose evidence merely aged out, so unknown speaks rather
than blocks. `force=True` skips the gate and costs nothing.

**`helm seat spawn <seat>` — the harness-agnostic SELF-ONBOARDING spawn.**
**THE OPERAND IS A NAME, AND A PROJECT'S SEAT IS NAMED AFTER THE PROJECT.**
`<seat>` is `<family>`, `<family>-N`, or the project-canonical
`<project>-<family>` (`clientproj-codex`, `clientproj-claude`). Teams are per TLA: each
actively worked project gets one claude seat and one codex seat named
`<project>-<family>`, and a team using helm never needs to know how helm is made
— so a row, premise or brief about that team's seat says the project out loud
instead of a fleet number. The FAMILY IS THE LAST SEGMENT, so a project whose
own name carries a family word cannot shadow one; the numbered form is resolved
FIRST and unchanged, and never reads the registry. Native `claude` is a family
here and nowhere in the proxy table: it has no proxy, port, cred translation or
`launch.sh`, so its spawn rides `helm launch --seat <seat>` — the one producer
that exports `HELM_CHAT_NAME` — and `--print` reads the name back out of that
producer. The project is recorded on the seat (`spawn.json` `project`), and a
project that already has a seat of that family gets a NOTICE naming it (the
canon is one of each; whether a duplicate is refused is task/2449). An
unregistered project or an unknown family is REFUSED, naming the registry that
decides projects and the admitted forms.

**EVERY FORM THIS SYNOPSIS LISTS IS ONE SOME DOOR BELOW ACTUALLY ADMITS.** The
project form takes `claude` or `codex` only — the families whose spawn gate
admits a non-family instance, since a per-instance proxy is an OAuth-pool
feature and `mode=proxy-key` families (kimi, gemini, grok, ds4pro) are refused
one rung later. Native `claude` has no bare and no numbered form at all: there
is no proxy to number and a bare native seat is the fleet-wide default identity,
not a seat helm mints. Both refusals name the FORM rather than printing a flat
family list the next rung would decline.

**THE REGISTRY BINDS THE WORKSPACE, NOT ONLY THE NAME.** A project-canonical
spawn provisions a home worktree and cuts a seat branch, so the repository it
writes into must be the project's: its registered checkout, a registered LINKED
WORKTREE of it (a `-wt` lane room shares no path prefix with the root and is
certainly the project's), or anything under its recorded `cv_scope.cwd_prefixes`.
Scope, never raw containment — and the check runs BEFORE any provisioning,
because a refusal that arrives after `git worktree add` has already created what
it refuses. An unregistered checkout is refused naming what helm asked about and
what the project's scope is. Both sides of that comparison are CANONICAL: the
nearest existing directory of the workspace is resolved through its symlinks,
and so is every registered path, because a link inside registered A pointing at
unregistered U is spelled like A's while every byte written through it lands in
U — the seat would enter U with a register claiming A. Resolving first is what
makes the attribution about the directory that receives the writes; the linked-
worktree tier still admits the project's own `-wt` rooms, which is the whole
reason the check is scope rather than containment.

**ONE LIFECYCLE, TWO ADAPTERS.** Native is not a second spawn path. The
per-seat lifecycle lock, the prior register, the stale reap, the `--replace`
refusal (a LIVE same-name seat is refused without it, and reaped with it), the
cross-seat surface-ownership proof (an instance dir symlinked at a sibling is
refused before any mutation, so a register write can never land on another
seat's `spawn.json`), the default-home PROVISION on first spawn, the onboarding
submit with its DELIVERED / NOT_DELIVERED / UNKNOWN tri-state, and the role
posture (`--role lead` reaches the launched command as `HELM_SEAT_ROLE=lead` and
the ultracode `--settings`, never only the register) are all shared. What native
actually differs in is what it does not have: no proxy, no port, no cred
translation, no `launch.sh`. `--model M` applies to native too: it rides the
pane command as `helm launch --model M`, is recorded in `spawn.json` `model`,
and a later spawn without `--model` (`--replace` included) carries the recorded
one, so a relaunch never falls back to the credhome's `settings.json` default.
When the pane will launch on a named credhome, the spawn (and its `--print`
plan, as `token:`) prints that home's FRESHNESS against Orca and what the pane's
launch will do with it — sync it, launch unsynced because a live claude holds
the home (a seat spawning onto its own credhome), or refuse — because the
launch's own sync line prints only inside the new pane.

**THE PRODUCER WRITES THE REGISTER BEFORE THE PANE CAN RUN.** A pane's first
SessionStart is the only authority for its session id, and the hook resolves the
seat's family through the seat's OWN register — so a register written after the
pane existed meant the first binding had no authority to bind into and was
dropped. EVERY leg registers first — native, project-proxy pane and headless
alike — publishing family, project and the claude home its launch selected as a
PENDING attempt, and finalizing that same attempt once the child is up. THE
SELECTED HOME IS PINNED INTO THE PANE COMMAND, not merely recorded, because a
pane adapter is handed a shell command and no environment: the home the record
names and the home the child launches in are one value from one accessor. It
matters because that is where the seat's session records land — a proxy seat's
`launch.sh` pins `CLAUDE_CONFIG_DIR` at `<instance>/claude`, a native seat has no
`launch.sh`, and an exact-session census pointed at the unused instance directory
reads an empty listing as zero live processes, proof enough to call a live seat
dead. A worker's pane command CLEARS an inherited `HELM_SEAT_ROLE` rather than
merely omitting it, for the same "a command is the only env channel" reason.

THE LOCK IS SHORTENED, NOT REMOVED. The seat's lifecycle lock is released while
the child starts — the pane create, the pane-boot grace and the onboarding submit
— because the child's own first SessionStart takes that lock to bind its session
and initialize its room cursors, under a five-second hook timeout: holding it
across the grace could get the child's own hook killed and leave the seat deaf.
Serialization is carried across that window by the PENDING attempt, which refuses
a concurrent spawn or resume by name, pid and start time. THE ATTEMPT IS A TOKEN,
minted once when the pending record is published and threaded unchanged into the
child's launch line (`HELM_SPAWN_ATTEMPT`), so every later stage asks the same
value the same question. A pending attempt whose process is PROVEN gone (no such
pid, or the pid reborn under another start time) is ABANDONED and never a wedge;
one whose process helm cannot verify — an unreadable birth identity — is refused
by name as UNVERIFIABLE, never called abandoned, and `--replace` does not override
it. Only a first SessionStart CARRYING the attempt's token may bind into a pending
register that has no handle yet: a manual `helm launch --seat` typed in another
pane carries none and is refused with the pending attempt named (`helm launch`
reads the token it was handed and pins it into claude's env; it never mints one).
Every mutating verb knocks on the in-flight door BEFORE its first write — the
no-adapter `resume`, which re-mints the launch assets for a manual paste, included.
When a register cannot be written the pane is closed and its state is reported as
the terminal proof states it — proven CLOSED, or UNKNOWN — never as UP; and only a
PROVEN absence hands the seat back. An UNKNOWN closure KEEPS a truthful INCOMPLETE
record carrying every fact the spawn already held — the returned pane handle, or
the launched pid and its birth identity — and no invented session, because
deleting a fresh project seat's only record while its pane may still be running
leaves a live pane no verb can name, and keeping it without its handle names a pane
nothing can point at. The failure message then says what `helm seat where` WILL
resolve, derived from the record the settlement kept or restored. A successful
launch on a harness whose SessionStart helm cannot bind says so, naming the
harnesses it can.

**PORTS.** Native gets none. A project-codex seat gets an ALLOCATED endpoint
from the project-instance block, recorded in `<seats>/instance-ports.json` —
injective by construction, distinct from the family base and from every numbered
and project sibling, and stable across calls. A numbered suffix whose derived
`base+N` would reach that block is refused rather than allowed to alias an
allocated project seat.

THE ALLOCATION IS A COMMIT, AND ONLY A MINT MAKES IT. The span is 100 wide and
entries never expire, so admission asks a PURE predicate and the slot is spent
only after every refusal has passed, immediately before the launch assets that
carry the port are written — a `--print` preview, a bad argument or a refused
workspace consumes nothing. `resume` allocates at the same point `spawn` does —
past its persisted role, model and session gates — so a refused resume spends no
slot either. THE LEDGER IS NEVER EMPTY BY DEFAULT, and one reader owns every way
it can fail to answer: bytes that will not parse, a file whose whole content is
`null`, an entry that is not `name -> port`, an entry OUTSIDE the block (which
names a port a family owns), and two seats mapped to ONE endpoint are each a
REFUSAL naming the file and the entry, before the lock and again after it, with
the original bytes left for whoever has to read them. Reading any of them as `{}`
handed the next seat a port the ledger already recorded and then overwrote the
record of it. And the ledger is not the only authority the allocation reconciles:
a port an existing instance config already names (a high-numbered seat minted
while `base+N` was unbounded) and a port something on this host is LISTENING on
are both reserved — and each of those censuses REFUSES when it cannot be
completed, because an unreadable instance directory, an unreadable instance config
and an unreadable `/proc` TCP table are UNKNOWN, never free capacity.

READERS ASK A DIFFERENT QUESTION FROM ADMISSION, and MAINTENANCE asks the
reader's. A seat whose config exists always renders an integer port, even when a
new seat by that name would now be refused — and every path that operates on an
instance which already exists (`seat up`, the supervise reconciler, the drift
reader, `pi`'s extension writer) resolves its endpoint there, so none of them can
reach the new-admission refusal. Asking admission on those paths meant a seat
whose 8500 endpoint had just been resolved could not regenerate its own config,
so it could not be restarted or reconciled at all. A seat with neither an
allocation nor a readable config port is UNRESOLVED: an UNKNOWN row and a named
refusal, never a family-base substitution and never a `%d` of a None.

**THE NAME ROUND-TRIPS.** `_seat_family` answers the numbered grammar first with
no I/O, then reads the seat's OWN spawn register — the recorded `family`, not the
shape of the name — so `helm seat where`, `up`, `down` and the `resume --all`
reboot sweep all consume a project-canonical seat without learning a naming
convention. `seat resume`, `seat up` and `seat down` are the verbs with nothing
to ACT on for a native seat — it has no `launch.sh` to replay and no proxy to
start or stop — and each says exactly that, naming
`helm seat spawn <seat> --replace`, rather than routing into a proxy leg that
calls the seat's own registered family unknown. The refusal is keyed on the
REGISTER, so a bare `claude`, which is not a seat at all, still gets the
unknown-family answer.

**`helm seat rehome <seat> --home H [--model M] [--apply]` — move a LIVE seat
onto a credhome in one verb.** This was a hand procedure: read the pane, exit
the session, relaunch with `helm launch --seat S --home H -- --model M --resume
SID` to spend the remaining quota of one account. It became a verb because the
hand procedure has a hole. **A home whose refresh chain has EXPIRED is not
synced from Orca**, and `helm launch` says so in one line and starts the session
anyway (`cred.launch_sync` prints "launching on the stale token"). The seat
comes back on a dead token, and the only symptom is `Not logged in` inside a
pane whose scrollback the operator has just replaced.

So the home is proven FIRST, before the live pane is touched at all, and a
refusal costs the seat nothing. `cred.sync` runs DRY — the same decision the
launch will take — and the verb admits only two shapes: Orca's live copy
reaches the home (`would-sync`: the launch syncs it before exec), or the home's
OWN login chain is alive. A `skip` (STALE and not syncable), a `refused`
(identity DISAGREE, or CHAIN-UNPROVEN), a home on its own chain whose
`refreshTokenExpiresAt` has passed, and a home on its own chain that carries
**no** `refreshTokenExpiresAt` at all are each REFUSED naming the check and the
remedy — a fresh login in that home, or leaving the seat on the Orca-synced
default. The missing lifetime is refused because cred's own measure calls it
UNPROVEN rather than live, and this verb exits a LIVE session before the target
is ever used: unproven is not dead, and it is also not the proof the verb owes.

**A `would-sync` is only safe if the launch will actually sync.** `cred.sync`
answers what a sync WOULD do; `cred.launch_sync`, the door `helm launch` passes,
performs no sync at all when `HELM_CREDHOME_ORCA_SYNC` is switched off and
starts claude on the home's own stale token instead. So a home that is
launchable ONLY because the launch would sync it is REFUSED while that switch is
off, naming the switch — otherwise the verb reaches its own defect the long way
round. helm reads the switch in its OWN environment, which is the only one this
process can measure, and the remedy says so rather than implying more than was
read. A home that is not a named credhome is refused too: the default
`~/.claude` is Orca's own to rewrite on an account switch, so a seat there
follows the whole fleet instead of pinning one account (the sledgehammer, not
the scalpel).

**The apply re-asks the same gate before it touches the pane.** A plan can sit in
a terminal for an hour, and Orca's copy, the home's own chain and the sync
switch can all move inside that hour, so `--apply` runs `check_home` again —
the SAME predicate, not a second implementation of the same rules — and a home
admitted at the plan and refused now REFUSES with nothing typed.

Every other leg is an existing door. The session id is the seat's **CURRENT**
roster session (`orcaadopt.roster_identity`'s addressing half — a history sid
found alive is whoever reopened that transcript, not this pane). The pane is
bound to a stamped live process through `orcaadopt.authorized_handle` **at the
act**, not carried from the plan. The exit is ONE pane-input wake — `/exit`
typed and submitted and read back through the adapter's turn verb — and **never
a signal**: a killed claude loses the transcript flush the relaunch resumes.

**One pane across the exit, the proof and the relaunch.** `send_to_pane` takes
its own binding at send time, which is the one re-proof that may never be
skipped, so the exit rides in as an `operation` that compares that binding to
the one this verb bound and types NOTHING when they differ: a pane-key remap
between two independent resolutions would otherwise exit one process and type
the launch line into another. The old process must then be PROVEN gone
(`beacons.pid_alive` against the same birth stamp; an unreadable pid is UNPROVEN
and waits, never "gone"). Only then is the launch line typed into **that same
handle**, the way `seat resume --all` reuses a pane on its DEAD-PANE leg.

Afterwards three surfaces are asked three separate questions, and each asks
about the REHOME THAT WAS REQUESTED rather than about the seat in general — a
surface that merely exists proves the seat exists, which it did before the verb
ran. The **pane** must be held by a process that is a different identity from
the one the exit removed (never the pid number: a recycled pid is the same miss
wearing the victim's number), born after it, running on the planned home read
from its own `CLAUDE_CONFIG_DIR`, resuming the planned session with the planned
model in its argv — **and carrying this attempt's marker**.

**Time ordering cannot name a launch, so the launch carries a name.** Another
claude can be started in this pane after the exit and before the launch line
runs — by a person, by a sibling verb, by a supervisor — on the planned home,
resuming the planned session, under the planned model, and younger than the
process the exit removed. It satisfies every ordering test there is, and our
launch line may have started nothing at all; a timestamp or a snapshot only
moves that counterexample. So `--apply` mints ONE `uuid4` per attempt and the
line it types is `cd <cwd> && HELM_REHOME_ATTEMPT=<uuid4> helm launch ...`: the
assignment binds to the launch invocation itself, after the `cd` succeeds, so
the child inherits it and a later sibling launch typed into the same shell does
not (an `export` would hand the next launch a marker saying this rehome started
it). The verification reads it back out of the candidate's own environ, on the
same candidate it already checked for identity, birth, home, session and model;
absent, unreadable or foreign reads UNPROVEN naming the marker it wanted and
what it found. It is **correlation, not authentication**: the value is not
secret, it grants nothing, and it claims nothing against anybody who copies it.
What it buys is the one thing time cannot — this-attempt attribution. The **beacon** must have been armed for that session
SINCE the exit; a beacon that was already standing is the previous incarnation.
The **register** is an agreement check and says so — a rehome resumes the
seat's current session id, so the roster row it lands on is the row that was
already there, and all it can prove is that the roster still names this seat on
the planned session. Each is reported PROVEN or UNPROVEN with the mismatch
named, never folded into one word.

One row per rehome lands in `seat-rehomes.jsonl`, carrying what was proven —
the attempt marker, the EXACT line that was typed (the `cd` and the assignment
included, never the launch fragment alone, so an operator re-running it by hand
gets the line that ran), the new process identity, and a run that left a surface
down. **A row that could not be written is a partial effect, not a success**:
the seat HAS been moved, so nothing is rolled back and nothing is retried (a
second actuation on a pane coming up is a worse answer than a missing row), and
the verb reports what happened, what is missing and what to do, and exits
non-zero. A caller that reads only the exit status must never be told a rehome
nobody recorded went cleanly.

**Dry-run by default, and the dry run IS the plan**: it prints the resolved
session, the bound pane, the home verdict, the four steps, and the exact launch
line the apply would type — plus the marker note, because the attempt marker
belongs to an attempt and a dry run makes none. If the process does not exit, the verb reads the pane
and REFUSES naming any dialog standing there, rather than typing a digit — helm
has no authorized door for an arbitrary confirm dialog (`harness.choose_in_modal`
derives its keystroke from the vendor-escape option family alone, and the
composer door refuses a pane with no composer), so a human answers one prompt and
re-runs.

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
room is recovered from launch.sh); `--cwd DIR` sets the worktree. A live prior
seat still requires explicit `--replace`. For Orca, replace may close a pane
omitted from terminal inventory only when the register's exact live session,
that process's pane/worktree keys, and `resolvePane` all identify one handle;
any partial or duplicate inventory identity still refuses without closing or
spawning anything.

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
config, never printed). `ds4pro` and `ds4flash` are proxy-key families whose
endpoint and upstream id come from a per-provider pool table rather than from
the family row.

**`qwen27` — the first LOCAL family.** Qwen3.8-27B (UD-Q4_K_XL, unsloth)
served by llama-server on a LAN box, OpenAI-compatible, under the
alias `qwen27`. The catalog holds no host for it: the endpoint is the value
of the key `qwen27` in `<helm home>/_global/endpoints.json` (see
`docs/endpoints-config.example.json`), read on every call. While that key is
missing, `helm seat add qwen27` refuses and names the file and the key, and
the family's route binds no proof. It is mode "proxy-key" like the others,
on port 8345, with three differences worth knowing before you use it:

* **It is keyless.** The endpoint takes no Authorization header, so the
  family declares `keyless` and the mint reads no env var, no `--key-from`
  file and no credential pool. The provider block still carries ONE
  api-key entry holding the declared placeholder
  (`seat_catalog.KEYLESS_API_KEY_PLACEHOLDER`), because the proxy creates no
  client for a provider with no entry at all — probed: with the key absent it
  logged `0 OpenAI-compat` and held every request open without answering,
  while `/v1/models` still listed the alias. Nothing rotates that string and
  nothing authenticates with it.
* **Its window is a reading, not a constant.** `probed_context_length` is
  read off the endpoint's own `/v1/models` (`meta.n_ctx`), and that number
  moves when whoever runs the unit re-serves it — it was measured at 32768
  and again at 131072 forty minutes later. `max_context` is the slot less the
  turn's 16000-token output budget, because input and output share one slot.
  Re-read `/v1/models` before trusting the pin.
* **Thinking is on, and the budget is what keeps it honest.** The model
  reasons by default and a budget its reasoning exhausts returns an empty
  answer with no error — the trap `helm/preread.py` already names on its own
  leg, where it can send `chat_template_kwargs: {enable_thinking: false}`
  because it authors the request body. A seat's body is Claude Code's, so the
  cure here is `max_output_tokens` (16000) instead.

The family is **findings-only**: `family:qwen27` is not among the
approval-tier policy's members, so an approve minted on it resolves
`outside` — measured, and by the same closed-set check that refuses every
other outsider, not by a second door.

**`opus46` and `gptoss` — two seats on ONE credential and ONE quota group.**
Both ride the same Antigravity OAuth file the `gemini` seat rides (mode
"proxy-oauth", channel `antigravity`), on ports 8346 and 8348. What makes them
separate seats rather than gemini fallbacks is that the account meters TWO
allowances: the Gemini models bill one weekly/5-hour group and the Claude and
GPT models bill another. On 2026-09-18 the Gemini group read 0 percent
remaining until about Sep 22 while the other read 100 percent, on the identical
credential — so the gemini seat was walled and these two answered.

* **`opus46` runs `claude-opus-4-6-thinking`; `gptoss` runs
  `gpt-oss-120b-medium`.** One model, one seat name: a fallback that changed
  the model under a name would break the only thing a review's authority is a
  claim about. Re-measured through the live proxy, one 8-token completion each:
  200 in 2.79s and 200 in 1.76s, non-empty text on both.
* **The Claude seat is an OWNER EXCEPTION scoped to this subscription.** Claude
  models otherwise run only through Claude Code on the owner's Max credential.
  The allotment here is Google's, bought with the Antigravity plan, and no
  Anthropic API key or SDK appears anywhere in the path. The exception is about
  the CREDENTIAL, not about the model name.
* **The credential is SHARED BY COPY.** Each family declares
  `shares_credential_with: gemini`, and `helm seat add` copies the sibling's
  antigravity auth file into this seat's own auth-dir at 0600 when the seat has
  none and no `--auth-from` was given. It is a copy and not a shared directory
  because the proxy rewrites its auth file at every token refresh, so two
  proxies on one directory would be two writers on one file. An explicit
  `--auth-from` still wins; a source credential that cannot authenticate is
  refused rather than copied.
* **Both bill the `antigravity-claude-gpt` group, and `helm seat where` says
  so.** The line names the group and who shares it, and reports the remaining
  percent as `not yet measured` until something reads the vendor's quota
  endpoint (task/2800). It never renders a default percent: a plausible number
  in that slot is one an owner acts on.
* **Both are findings-only.** Neither family key is among the approval-tier
  policy's members, and a verdict records the author's FAMILY KEY as its
  resolved family — so an approve minted on either resolves `outside` by the
  same closed-set check that refuses every other outsider. `gptoss` is a NEW
  family and therefore a true cross-family read for both claude- and
  codex-authored work once it passes the known-defect bar; `opus46` resolves to
  a Claude MODEL on a non-native family key, which is task/2802's subject and
  is not loosened here.

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

**A limited family can declare a context budget and a fresh session between
rows (task/2944).** Every request re-sends the whole context, so for a family
with a limited allowance the context size is the spend rate. kimi's weekly
allowance went in about 30 hours: one long-lived session carried many rows, so
a typical request carried 329k tokens. Two `helm/seat_catalog.py` fields fix
this:

- `context_budget` narrows the window a seat is taught. The launch line puts it
  on both CC knobs and this watchdog reads the same number
  (`seat_catalog.taught_window`). `max_context` keeps the model's measured
  window and its evidence. A budget can only narrow: one at or above
  `max_context`, or on a family with no `max_context`, refuses at import.
  kimi is taught 380000 and compacts at 304k. The number is sized from the
  p95 per-row peak (`KIMI_ROW_PEAK_P95`, 300,373 tokens over 174 rows replayed
  under the rung below), so about 95% of rows finish without compacting. A
  200k budget would compact about one row in five, which is the "compacted
  about 5x too often" complaint that set kimi's 1M window.
- `fresh_session_floor` starts a fresh session BETWEEN rows. A seat of that
  family is cleared when all of these are true: it carries at least the floor
  (kimi: 100000), its transcript is quiet for 5 minutes, its pane is IDLE with
  an empty composer, no owed dispatch row names it under any alias, and it
  holds no live claim. The rung uses the `/clear` and onboarding rungs above
  without change, so the new session re-arms its beacon and reads
  `helm dispatch list --mine --open` before its next row. It clears between
  rows, never when a row arrives, because the beacon wakes the seat on a new
  row in seconds. An unreadable ledger, a claim with no holder, an open turn
  or a draft refuses. One clear per seat per 15 minutes bounds the cost if the
  onboarding alone crosses the floor. The routine clear and onboarding post
  nothing; a seat that was cleared but not re-briefed posts a loud alert,
  because its beacon is not armed. `--dry-run` shows `would start a fresh
  session`. The budget reaches a seat at its next launch; the between-rows rung
  runs on the next watchdog pass.

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

**Two kinds of compaction arrive with that source, and only the deliberate
one needs the leg.** Claude Code's own native autocompaction continues the
interrupted turn by itself; a `/compact` typed by a human or injected by
`autocompact` ends the turn and parks the seat. The SessionStart payload
cannot tell them apart — the distinction is the `trigger` field of the
PreCompact payload one hook earlier (`auto` / `manual`), which `helm handoff
check --hook-json` records under the session's key in the resume-turn state.
This leg reads that record: a recent `auto` (inside
`HELM_RESUME_TURN_SPIRAL_S`, which covers the 100–190s a compaction takes and
sits far under the hours between two compactions of one session), written by
the same thread (the record's `agent` equals the payload's `agent_id`; a
subagent shares the seat's key and its record vouches only for a subagent)
and not yet consumed, means nothing is injected, no room row is posted and no
DM is sent; the decision leaves one stderr line, a `native-auto` entry in
`--status`, and a `seat.resume-turn` ledger event. One record vouches for
exactly one SessionStart: the decision marks it consumed in the same locked
write. A record with a present-invalid field — a null record, a non-finite or
non-numeric `at`, a missing or mistyped `trigger`, `session` or `agent` —
never vouches, and a record stamped at or before the entry's last recorded
resume-turn decision (`last_at`, which every decision stamps) is obsolete and
never vouches. And the record is BOUND TO A TRANSCRIPT POSITION: the
PreCompact recorded the transcript's byte length, inode and device, and this
leg vouches only when the file it OPENS at the payload's `transcript_path` is
the same inode and device, has not shrunk, has grown by at most
`resumeturn.TRANSCRIPT_GROWTH_CAP`
(4 MiB, a read bound sized over six times the largest same-compaction growth
measured), and nothing appended since the recorded position is a
`compact_boundary` record (the record's compaction already ended — its
boundary and summary are on disk, and a stale record read by a later
compaction's SessionStart is exactly this) or a `user`/`assistant` turn (it
never ended and the session moved on). ONE DESCRIPTOR IS MEASURED AND READ:
the path is opened once, nonblocking and regular-only (`pk.open_regular`),
`os.fstat` of THAT descriptor gives the identity and the size the interval is
computed from, and the read must return the WHOLE interval — a file replaced
or truncated between a pathname stat and a separate open handed the old shape
an empty region, and an empty region holds no boundary and no turn, so a stale
record vouched over it. The read also happens BEFORE the state lock is taken
and its result is an input to the locked decision, so a FIFO or a slow file at
the transcript path can never hold the store's lock against the fallback leg;
the cap bounds the BYTES, and the nonblocking regular-only open prevents a
FIFO-open hang, not slow regular-file I/O. Observation exceptions, including
malformed paths and close failures, decline native-auto inside the same fallback
boundary as consumption failures; observation still happens outside the lock.
Past a declined record the ACT has one boundary of its own: composing the
directive (`resume_text`, which opens the transcript for the compaction
floor), writing it and forking the child fail into the same alert — a wake
child carrying the error, the stderr line — never into the hook's last-resort
catch, which prints and returns 0 with nothing armed. A transcript path the
kernel refuses (an embedded NUL raises ValueError, not OSError) reached that
catch once under a matching register (helm-codex CL97): native-auto declined
loudly, the register matched, and the leg died silently between the decision
and the spawn. The PreCompact producer refuses the same path the same way
(`_transcript_position`: no position, one stderr line), so the record stays
written and the nag still runs.
Measured on 431 boundaries: the
harness creates the boundary AFTER this hook has finished on every build
from 2.1.204 on and writes the summary after the boundary, so neither is on
disk while the leg runs, and what does land in between is session metadata
and queued messages, never a turn. Every failure to PROVE the binding — no
`transcript_path` in either payload, a record without a position, a file
that cannot be read, a path that is not a regular file, a replaced inode, a
shrunk file, growth past the cap, a read shorter than the interval,
an unparseable record in the region — declines with one stderr line naming
which, and declining is the loud path. A `manual` record, a consumed,
obsolete or unbound one, a subagent's, an unrecognised trigger, or no record
at all is the resume leg described below, byte for byte — absence of the
record is not proof of a native compaction (the PreCompact hook may be
uninstalled, timed out, or given up on the state lock inside its bound), and
the loud path is the one whose cost is a spurious row rather than a parked
seat. BOUNDARY, what remains: a binding is unprovable when the transcript is
unreadable on both sides (the producer could not stat it, or this leg
cannot), and unprovable declines — one spurious resume alert over a native
compaction, never a silent parked seat. Measured before this: 240 "could NOT
be auto-resumed" rows (~85 KB) in one room, every one about a seat that had
already resumed on its own.

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
slow case. Those three answers are read from the resume-turn state store,
and a store that exists and cannot be read answers none of them: the hook
injects nothing, alerts the room with no DM, prints the `unknown` refusal and
its reason on its own SessionStart output (the one line the compacted seat
reads, as it does for spiral and capped), and the same refusal governs the
deaf-in-effect nudge and the episode wake DM, until the store is repaired or
moved aside (`helm doctor` FAILs naming it). `helm seat resume-turn --status`
prints what the leg last did per seat, or that the store is UNREADABLE;
`HELM_RESUME_TURN=0` disarms it.

**A proxy seat cannot fan out through a skill or a fork — the deny set, not
the brief.** Every seat launches without plan-mode ENTRY; a seat on a **proxy
family** (codex, kimi, gemini, grok, ds4pro — each request spends a metered
upstream quota) also launches without `Skill` and `Agent(fork)`. The set lives
in one place, `seat_catalog.denied_tools(family)`, and rides three surfaces:
the launch line's `--disallowedTools`, the seat config dir's
`permissions.deny` (what a resume or an Orca relaunch inherits), and the
`disallowedTools` frontmatter of every subagent definition `seat launch
--multi` / `seat smoke` mints. The reason is an incident class, not a
preference: one run emptied two codex quotas through the skill door
(task/2287, 437 subagents and 32 forks). A brief binds the model's own `Agent`
calls; it cannot bind the agents a skill launches for itself, so the bound
must be mechanical. A native claude seat keeps both tools, and a slash command
the OWNER types in a pane is a different path and still works.

**A proxy seat MAY run a workflow — capped, and on its own tier.** `Workflow`
was denied on proxy seats too after a briefed reader reached 14 concurrent
agents through a workflow script (task/2491); the owner's ruling reversed that
half (task/2559): "astra can run sol workflows just like fable can run opus
workflows ... both valid strategies for cross-model same-family delegation
within a cc process, so the policy needs to be more nuanced". So the tool
stays, and the bound is mechanical in two parts. **The cap**: every proxy
launch line exports `CLAUDE_CODE_WORKFLOW_MAX_CONCURRENT_AGENTS=4` (the one
workflow knob the harness reads, per workflow run; the seat's settings.json
`env` map carries the same key so a resume inherits it), where the harness
default is 16. It bounds one run — two workflows in flight may hold eight —
and the seat-wide subagent ceiling is unchanged. **The tier**: a workflow
agent's `model` is sent upstream as a claude id, exactly like a built-in agent
type's frontmatter, so the seat's `oauth-model-alias` block (below) decides
where it lands: on an astra seat `opus`/`fable` agents run gpt-6-astra and
`sonnet`/`haiku` agents run gpt-5.6-sol; on a sol-launched seat every id runs
sol, and an agent that names no model inherits the pane's. The admission is
the owner ruling recorded on helm task/2559 (origin: owner), in the owner's
words: "astra can run sol workflows just like fable can run opus workflows,
same diff, both valid strategies for cross-model same-family delegation within
a cc process, well then the policy needs to be more nuanced, report to helm
team for fixing"; its scope is every proxy-family seat run inside the Claude
Code harness (the `seat_catalog` proxy modes), native claude seats outside
it. The ruling admits the tool; it does not itself speak of pre-existing deny
entries, and the old seeder's documented contract preserved every entry it
found, so an upgraded seat's settings.json is handled by three explicit legs
(`seat_catalog.RETIRED_SPAWN_DENIES`), none of which reads a list's shape or
a file's location as proof of who wrote an entry. **Recorded by the
operator**: an entry under `helm.operator_denies` is a helm-honoured key the
refresh never edits and always re-applies to `permissions.deny` — an operator
who wants `Workflow` denied on a proxy seat records it there. **Recorded by
helm**: every seed records what helm itself wrote under `helm.seeded_denies`
(the seeder is the only writer; census and doctor may read it); a retired
entry that record names is removed with its record, and the record never
names an entry the list no longer carries. **Unrecorded**: left untouched,
`Workflow` included — no automatic path removes it, in or out of helm's own
tree — and the refresh prints one line naming the entry, the file, and the
verb below. A native claude seat is not a proxy family: nothing is removed
there.

### `helm seat retire-deny <tool> [--seat S|--all] [--apply]`

The deliberate door for the unrecorded case. Dry-run by default, and the dry
run IS the list: every proxy-seat settings.json (the family dir and each
instance dir) carrying a deny of `<tool>` that neither `helm.seeded_denies`
nor `helm.operator_denies` records, marked `WOULD-REMOVE`; an entry the
operator key names is listed `KEPT`, one helm's record names `HELM-OWNED`
(the next refresh retires it). Every candidate is judged before anything is
written, and three states block every write and exit 1: `REFUSED` — the
candidate's instance dir or its `claude` child resolves to another seat's
tree (the seeder's own identity and nesting guards, applied before any
read, so a linked directory can never carry a write into the seat it points
at); `UNREADABLE` — the strict reader failed (a dangling link, a FIFO, a
directory helm cannot enter), named with its errno and never silently
absent; `MALFORMED` — the file's root or a present `permissions` container
is not an object (a file of `null` is a value, never a missing file; a
`"permissions": "Workflow"` is never read as no deny), or `permissions.deny`,
`helm.seeded_denies` or `helm.operator_denies` is not a list of strings —
none of which is ever read as permission to remove, and none of which is
ever read as absent. `--apply` then removes exactly the named entry from
exactly the `WOULD-REMOVE` files, appends nothing to `helm.seeded_denies`
(the entry is gone), prints one `REMOVED` line per file, never touches an
entry `helm.operator_denies` names and never touches a non-proxy seat; a
second `--apply` lists nothing and writes nothing. It refuses a tool the
current table still denies (a refresh would seed it straight back). **Applying it fleet-wide is an owner
decision** — it is not authorized by a review or by the integrator, and
nothing in deploy or doctor runs it.
Not mechanical: a
script that names an upstream id verbatim (`gpt-6-astra`) is served as
written, because the alias rows keep the family's real ids routable on
purpose. A native claude seat carries no cap; its workflows are the owner's.

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

**A SUBAGENT'S MODEL IS ITS FRONTMATTER ID, AND THE FAMILY DECIDES WHERE THAT
ID GOES.** CC's built-in agent types carry a claude model in their own
frontmatter and `CLAUDE_CODE_SUBAGENT_MODEL` does not override it (measured
2026-09-09: an Explore child on an astra seat sent `claude-opus-5` upstream and
died HTTP 502), so every proxy seat's config carries an `oauth-model-alias`
block mapping each catalogued frontmatter id onto a model the family serves.
By default that is **the seat's own launch model** — one pane, one model,
nothing to choose. A family that declares a **`subagent_tiers`** table in
`helm/seat_catalog.py` names a model **per id** instead, and codex declares
one: `claude-sonnet-5`, `claude-haiku-4-5-20251001` and the undated
`claude-haiku-4-5` (what CC's own alias table resolves the word `haiku` to)
-> `gpt-5.6-sol`. So ONE
codex pane bursts into **sol workers with no second pane** — the burst is
chosen by which built-in agent type the parent spawns, never by an env var —
while `claude-opus-5`, `claude-opus-5-5` and `claude-fable-5-1` carry no row and therefore follow
the seat's launch model, so the checkers always run what the seat runs and a
sol seat never escalates itself back to astra. Every tier value must be a model
that family catalogues (its `model`, a `probe_models` entry, or a
`model_context` key) or the catalog refuses to import; a family with no table
emits its launch model on every row, byte-identical to before (kimi,
gemini, grok). `helm seat doctor --ensure` reconciles a live config to the
block the generator now emits, so declaring a tier respawns that family's
sidecars on the next sweep. One caveat with no knob behind it:
`CLAUDE_CODE_MAX_CONTEXT_TOKENS` is process-global, so every subagent believes
the PANE's window, and the invariant that keeps that safe is **the advertised
window must be no larger than the capacity of EVERY model this family routes a
child to**. On an astra seat (220000) a sol subagent (320000) is UNDERSTATED,
which compacts early rather than wedging — the safe direction, and the shipped
one. The dangerous direction is a NARROWER child than the pane: a sol pane told
320000 with an astra tier at 220000 overstates those children by 100k, the
unrecoverable direction, and a tier on a model narrower than the launch model
is what would force the launch line to advertise the MINIMUM over the launch
model and every tier model. No shipped tier is narrower than its pane.

**AN INSTANCE DECLARES ITS OWN LAUNCH MODEL, AND ONE FAMILY SPANS TWO.** Owner,
2026-09-14: *"we may want to consider using sol agents as the standard and only
1 astra for planning/landing"* — so `FAMILIES[...]["instance_models"]` names a
model **per instance** (`codex-4`, `codex-5`, `codex-8`, `codex-9` ->
`gpt-5.6-sol`; codex-7 declares nothing and keeps the family's `gpt-6-astra` as
the planning and landing seat). One family entry, one OAuth cred pool, two
models across panes. An instance with no row launches on the family model, so
every other family and every undeclared instance mints the config and launch
line it minted before. Each key must be an instance of that family (`codex` or
`codex-<N>`) and each value a model the family catalogues, or the catalog
refuses to import — the same `family_catalogued_models` reading the tier
validator uses. The declaration reaches the seat through the generator, not
through a file on disk: `helm seat doctor --ensure` regenerates every instance
config from it (a hand edit is reverted within the minute), the launch line
carries the instance model plus that model's `model_context` window (a sol seat
is told 320000, an astra seat 220000), and `helm seat list` prints TWO model
columns per seat: `declared` (the catalog default) beside `minted` (the model
that seat's own launch.sh on disk would spawn, `unminted` where no launch.sh
exists, `UNKNOWN` where helm could not answer from that path — the read's error
class is printed beside it). There is no `launched` column: launch.sh is
re-minted on every add, launch and resume, so it proves what the NEXT spawn
uses and not what the live pane started with, and the only record a pane
self-writes (the roster runtime row) carries harness, family and backend and
no model at all. An explicit `helm seat launch <seat> --model`
still outranks the declaration for that pane.
