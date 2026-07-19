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

## Layout

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

## Pluggability

Memory tooling evolves fast. helm's core is local markdown + JIT resolution,
but the store's `type`/`load_class` schema is deliberately orthogonal to
*placement*, and resolvers are behind one interface — so a backing store
(vector recall, attested records, a hosted memory service) can be plugged in
per-type without consumers changing.
