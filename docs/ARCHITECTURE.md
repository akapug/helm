# helm architecture

## The two-source model (the constitution)

helm inherits a hard-won information architecture. Its laws:

1. **Two primitives only.** Every fact is an **event** (something happened —
   immutable, append-only) or an **artifact** (authored content with
   identity). Events point at artifacts; artifacts never point back.
2. **The overlap-killer.** Every derived view (an index, a report, a
   registry) is *read-only as truth*. You change a view by changing its
   source; the view re-derives. This one rule kills "the same fact in three
   places with manual sync".
3. **Name your source.** Every store must say what it derives from — source,
   projection, replica, or trash. A store that cannot name its source is
   pretending to be canonical: migrate it or archive it.

   Laws 2+3 are *executable*: `registry.projections()` is the manifest — one
   row per on-disk store, classified (`authored | projection | events | state
   | backup`), every projection declaring its source and rebuild command.
   `helm projections` renders it; `helm doctor` enforces it (undeclared
   rebuild, source-free projections containing data, staleness, and
   unclassified-squatter files under `~/.helm` or `~/.cache/helm` — the
   standing guard against the store-rot that killed the ancestor's
   `~/.remember`). A manifest row may declare the exact empty JSON its producer
   writes at genesis. While no seat is registered, only that source-free shape
   is healthy; once a seat exists, missing sources fail again even if the bytes
   remain empty. Malformed, extended, or nonempty copies always fail closed.
4. **One recall index.** Transcript search/recall lives in one place; helm
   records *how to query it* per project and never builds a second index.
5. **Placement is policy.** Local-vs-shared, private-vs-published are
   replication policies on the same objects, not different stores.

## What helm owns vs references

| concern | owner | helm's relationship |
|---|---|---|
| code + shipped docs | the repo | authoritative; helm points at the toplevel |
| session transcripts | each harness's store | source; helm decodes, never copies |
| recall / search | the recall index (cv) | helm records the query scope |
| per-project agent memory | the harness memory dir | referenced via `memory_dir` |
| the typed knowledge store | **helm** (adopting the live store in place) | one resolver, many roots |
| the project registry + lineage edges | **helm** | projection of observations + authored edges |
| the authored chain (premises/heuristics/lexicon/prd/journal/evals) | **helm** (`~/.helm/<project>/`) | the durable per-project home |
| know-your-user | **helm** (`~/.helm/_global/know-your-user/`) | the one operator profile |

## Operating scale (why the machinery is sized the way it is)

Numbers from the founding deployment, so a reader can judge each subsystem
against its *actual* consumer rather than guessing (every one below shipped
with a live consumer on day one):

- the adopted store is **~880 entry files** — the parsed-entry cache and the
  substring prefilter exist because the per-prompt hook pays for that corpus
  on every turn (88ms → ~1ms measured);
- the session catalog spans **~15,000 sessions** across harnesses — the
  single-flight cache and stat-keyed rescans exist because `/api/sessions`
  and `deep_search` join against it;
- the quota **probe loop feeds the web burn view** (per-account per-hour
  drop of the binding window) — it is read, not speculative;
- `helm seat` shipped with a **codex seat live-proven the same day**
  (prompt, tool round-trip, subagent); other families are config rows on the
  proven path, not waiting machinery.

```
~/.helm/
  _global/
    registry.json        # the master project list (auto-map output + authored edges)
    know-your-user/      # profile.json + dated superseding notes
    priors/              # harvested prior art (shared corpus)
    premises/ heuristics/ lexicon/   # cross-project typed entries
    reflexes/            # (signal → steer) entries, harness-delivered
    archive/             # dated, write-once
  <project>/
    registry.json        # this project's record (mirror; travels with the chain)
    premises/ heuristics/ lexicon/   # project-scoped typed entries
    prd/ journal/ evals/ archive/    # the dev-cycle shelves
```

Each store under this root is classified by a row of the projection registry
(`helm projections`) — a file no row names is a **squatter**, and `helm doctor`
reports it as one.

A project that already has a knowledge home elsewhere is **adopted by
symlink**, never copied.

## The auto-map

Discovery is harness-broad and identity-correct:

- **Claude Code**: `~/.claude/projects/*/` — the real `cwd` is decoded from
  inside each newest session file; the directory slug is lossy and never
  trusted.
- **Codex**: `~/.codex/sessions/**/rollout-*.jsonl` — `session_meta.payload.cwd`.
- **OpenCode**: `storage/project/<hash>.json` — `worktree` is the real path.

Pipeline: filter scratch dirs → collapse worktrees into their main repo (via
git's common-dir) → group per root → promote (real git repo, or recurrence) →
rank by recency. Additive: re-sync never deletes a known project.

## The typed store

One resolver over several roots (the adopted live store, the global helm
chain, each project's chain), with scope precedence `project > helm-global >
adopted` (narrowest wins on a same-type same-slug collision). The adopted
root is the live Claude memory dir, taken in place —
`~/.claude/projects/<slug-of-$HOME>/memory` (`HELM_ADOPTED_DIR` overrides;
tests point it at a tmp dir). The adoption contract: helm's writers are
byte-shape-compatible with the incumbent writers, new writes never target the
adopted root unless explicitly pointed there, and lifecycle updates
(evidence/supersede/retire) write back in place wherever the entry lives —
adopted included. Entries are single markdown files with fenced `key: value`
frontmatter — human-editable, git-friendly, no database. Types and load
classes are documented in [CONCEPTS.md](CONCEPTS.md).

The active-fire surface is `helm inject`: one call a harness hook makes per
turn, returning the pinned lane (budget-capped) plus just-in-time matches for
the current prompt. One resolver, every harness — the same knowledge fires
everywhere.

## The actuators

- **drain** — the intake dir is transient; classification routes entries to
  typed homes; sources are archived with a reference back. Dry-run by
  default; the rollback net is verified non-empty before anything moves.
- **drift** — evidence accumulates on beliefs; the report surfaces *only*
  contradictions, tier-crossings, and decays. No drift, no output.
- **lineage** — the registry's edge layer; cleanup is archive-not-delete,
  gated on dry-run + confirm, and never touches external nodes.

## Local multiplayer

Helm's multiplayer boundary is local and adapter-first: an append-only blind
relay carries opaque client-owned CRDT updates, while a separate TTL channel
carries disposable presence. Neither interface names a terminal, browser,
metaharness, model, or CRDT library. The shipped implementation is tmpfs-local;
remote/web bridges belong to a separate hosted product. See
[MULTIPLAYER.md](MULTIPLAYER.md).

## Pluggability

Memory tooling evolves fast. helm's core is local markdown + JIT resolution,
but the store's `type`/`load_class` schema is deliberately orthogonal to
*placement*, and resolvers are behind one interface — so a backing store
(vector recall, attested records, a hosted memory service) can be plugged in
per-type without consumers changing.

### Attestation is native-primary

Premise attestation is a **helm-native, stdlib, append-only hash chain** — the
primary, offline, tamper-evident proof (`<helm-home>/_global/.state/attest-chain.jsonl`,
`rec_hash = blake2b256(canonical(core) + prev)`). It depends on no binary and
no service. A dregg node, when reachable, is an **OPTIONAL external checkpoint**
that anchors a record hash and is honestly labelled a *node* commitment, never
a user-cell signature (fail-open — the native record stands regardless). helm
rides dregg + cv only; it never depends on the meld binary. See
[ATTESTATION.md](ATTESTATION.md).

### The comparison-resolver seam

The interface is small: a backend has a `name`, a **mandatory `source`
declaration** (law 3 — a backend that cannot name its source is pretending to
be canonical), a `configured()` gate, and `resolve(text, project) -> ranked
entry-id list`. The **local keyword JIT resolver is the AUTHORITY**; a
registered COMPARISON backend runs in **parallel** and its results are
**logged/compared, never trusted as truth** (the overlay-not-store law: a
projection is compared, not injected).

Shipped comparison backend: **Cloudflare agentic-memory**, a thin
stdlib-`urllib` stub behind `HELM_CF_*`
([ENVIRONMENT.md](ENVIRONMENT.md)). Its laws, enforced in the `helm/inject` package:

- **OFF by default, zero cost.** No `HELM_CF_ENDPOINT` ⇒ the comparison is
  simply off; the turn pays one env read, no import / object build / I/O /
  store parse.
- **Fail-open.** A raising, slow, or failing comparison backend logs an
  `{error}` row and returns — it never touches the authoritative local lane and
  never blocks the turn (a short-lived hook process cannot host a background
  thread, so the query is synchronous + hard-timeboxed).
- **Byte-identical local lane.** The comparison step runs *after* the injected
  sections are assembled and only *reads* the computed local ids, so the local
  output is identical whether the comparison is on or off.
- **Measurement over vibes.** Each configured turn appends one divergence row
  (local-only vs compare-only vs agreed, ids never prompt text) to
  `_global/.state/compare-ledger.jsonl`; `helm inject --compare-report` renders
  the accumulated verdict so the owner judges a real backend on evidence.

A future write leg (mirroring typed entries up as an explicit, never-canonical
replica through a `_global/backends.json` registry) is the connector's outbound
half and remains unimplemented on its own lane — the seam above is the read/compare
half that needs no Cloudflare account.

### The version-control seam (git | jj)

`helm/vcs.py` holds a small `Vcs` interface — five call primitives
(`run`/`text`/`probe`/`capture`/`proc`, each one existing call site's exact
contract, including whether a missing binary fails open or loud) plus the
semantic ops a second backend must really reimplement (`worktrees`,
`add`/`remove`/`lock`/`unlock_worktree`, `dirty`, `head_sha`, `base_branch`,
`has_branch`, `is_ancestor`, `delete_branch`, `wip_commit`, `ahead_behind`) —
and today's only backend, `GitVcs`.

**Its reach, stated exactly.** The seam owns the version-control calls of
`helm work` (worktree + lane lifecycle), `helm ship`, `helm capsule`, the
handoff/now probes, the lane gc verdict reads, and the auto-map's root resolver.
Six private `_git` helpers existed; **five are migrated** (`capsule`, `handoff`,
`ship`, and `work/_lanes`' two) and `landreq.py`'s `--git-dir` helper is not —
it is a declared exception, so "six replaced" would be wrong. It is **not** yet
every git call in helm: **51 direct git spawns** remain across `cli`, `landreq`,
`seats`, `dispatches`, `record`, `lineage`, `rearm`, `seat`, `web`, `wiring`,
`cell`, `nevertrack`, `vacuous_assertion`, `hardcode`, `hostpath_guard`,
`conflict_marker`, `lane_discipline`, `clearspan`,
`world_prose_guard`, `retired_name_rung`, and `silent_cap` — each entry carrying its reason. `trailer_rung` left
this list when its rule stopped needing git's trailer parser. The ordinary call sites
are migration debt; the pre-commit/pre-push scanners are standing exceptions
because their canonical scripts must run with no importable helm package and
must not be editable by the lane whose staged set they judge.

That set is enumerated and count-pinned by `DirectSpawnAuditTest` in
`tests/test_vcs.py`. Its guarantee is bounded and worth stating precisely. The
pass has an explicit decided grammar: subprocess imports, simple aliases,
literal/string argv, and recursively-composed `functools.partial` calls using
Python's positional/keyword precedence. A separate reference ledger tracks direct
subprocess spawner attributes, imported or derived aliases, and any `getattr` on
a known subprocess module that names or might name a spawner. Each reference on
**that named surface** is either consumed by a decided binding/call or reported
**unresolved**; transport through a dict, class attribute, callback or conditional
therefore cannot silently clear its source reference. The detector's own decided
and unresolved spellings are a test battery.

This is not complete Python data-flow or introspection analysis. A capability
obtained through another module's re-export, `vars()`/`__dict__`, or otherwise
constructed without one of the tracked references is outside the audit's sight.
Runtime-assembled argv likewise cannot be decoded; those modules are pinned as a
named set, so known residue is declared rather than hidden. Silence means no
reference on the explicitly named accounting surface, not proof that arbitrary
Python cannot eventually reach a subprocess spawner.

**Selection is per-target, never per-cwd.** `backend(target)` takes the repo or
checkout the operation is about; a cwd-derived answer would pick git for a jj
repo the moment a verb ran from elsewhere. Order: `HELM_VCS=git|jj`, else the
nearest `.jj`/`.git` marker walking **up** from the target (git's own discovery
rule — the inner marker wins, so a `.jj` above an unrelated git checkout cannot
hijack it; a colocated repo carrying both resolves to jj), else **git** (the
default spelled out, never implied). Only `GitVcs` exists, so jj or an unknown
value resolves to git with one stderr line — a VCS preference can never take a
verb down. `helm ship` keeps the raw-process primitive on purpose (an operator
verb fails loudly). A jj backend would ride colocated (`jj git init --colocate`
keeps a real `.git`, so metaharness worktree adoption keeps working) and buy the
AX-safety win: jj records the working copy in its op-log on every change, so a
naive user's uncommitted work is recoverable rather than lost. See
[ENVIRONMENT.md](ENVIRONMENT.md) for the knob.
