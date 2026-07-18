# helm ⎈

**The personal knowledge home for people who build with coding agents.**

Every coding-agent harness leaves knowledge scattered: hundreds of session
directories keyed by lossy path slugs, memory files nobody re-reads, glossaries
and beliefs and specs spread across repos, dotfiles, and half-remembered
conventions. helm is the layer that gathers it into one home — `~/.helm` — and
keeps it alive:

- **`helm projects`** — your real project list, discovered from what your
  agents actually did, across every harness (Claude Code, Codex, OpenCode —
  more welcome). Worktrees collapse into their repo; scratch dirs are filtered;
  a project you only ever touched in one harness still counts.
- **The knowledge chain, per project** — `premises / heuristics / lexicon /
  prd / journal / evals / archive` under `~/.helm/<project>/`, following the
  organic dev cycle: prior art feeds specs, the build happens in the repo,
  evals and journals record what happened, the archive keeps history without
  clutter.
- **One typed store, one resolver** — beliefs (priors, with confidence that
  updates on evidence), vocabulary (your lexicon — the terms you coined),
  heuristics (moves you apply), and reference material, unified behind a
  single just-in-time resolver. Relevant entries surface when a prompt touches
  them; nothing wallpapers every turn. *Salience is the scarce resource.*
- **The drain** — raw memory intake is a transient inbox, never a destination.
  `helm drain` classifies raw entries and routes them into their typed homes,
  archiving the source. Nothing is deleted; everything stays retrievable.
- **The drift report** — beliefs carry confidence and evidence logs. When the
  evidence starts contradicting something you hold as true, `helm drift`
  surfaces it — and only then. No news is silence.
- **know-your-user** — a first-class profile of how you like to work: voice,
  autonomy, standing corrections, goals. The interview takes five minutes and
  every agent that reads the store gets warmer.
- **`helm sessions`** — every local session across every harness, grouped by
  project, with the exact resume command one paste away (it's just the
  harness's own CLI — no wrapper).
- **The lineage map** — projects fork, compose, supersede, and launch. helm's
  registry carries those edges and renders the family tree, including
  read-only external nodes, plus a ranked (read-only) "safe to archive and
  why" report.

## Principles

1. **Overlay, not another store.** helm references authoritative homes — your
   repos, your harness session stores, your recall index — it never copies
   them. Every projection is read-only as truth; an edit lands at the source.
2. **The session store is data, not identity.** Working directories are an
   attribute; sessions are the key. helm decodes real paths from inside
   transcripts, never from directory names.
3. **Additive and idempotent.** `helm sync` never deletes a known project.
   Cleanup means *archive with a reference back* — like old photo albums you
   keep — never deletion.
4. **CLI-first, with a web equivalent.** Every curation verb works in a
   terminal and in the browser (`helm web`).
5. **Zero dependencies.** Python standard library only. One checkout, no
   install step: `./bin/helm sync`.

## Quickstart

```console
$ ./bin/helm sync        # discover your projects across all harnesses
$ ./bin/helm projects    # the list, newest activity first
$ ./bin/helm show meld   # one project's full record
$ ./bin/helm sessions    # every session, all harnesses; resume in one paste
$ ./bin/helm doctor      # health check (read-only)
$ ./bin/helm web         # the same, warm, in a browser
```

`HELM_HOME` overrides the default `~/.helm` (legacy `MELD_*` variables are
accepted as a fallback).

## Status

Young and moving fast. The registry/auto-map, typed store, drain, drift,
lineage, and web views are live; reflexes and the self-evolution loop are in
active development. Issues and harness-format reports welcome.
