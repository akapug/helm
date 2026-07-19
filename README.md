# helm ⎈

**The steering station for you and your agent fleet — everything they know,
everything they're doing, everything they run on, one helm.**

Working with coding agents at any real scale scatters your operation across
harnesses: sessions in path-slug jails, accounts and quota windows in your
head, knowledge in memory files nobody re-reads, credentials in fragile
dotfile homes, coordination in tribal convention. helm gathers the whole
operation into one place — a CLI and a warm web app over one home
(`~/.helm`) — and serves it back to both audiences: **you** get the cockpit
(sessions, accounts, configs, projects, the family tree of everything you've
built), and **your agents** get the same truth injected into their context
every turn (beliefs, vocabulary, reflexes — budget-capped, salience-gated).

The pillars:

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
  harness's own CLI — no wrapper). Content search inside transcripts
  (`helm search`), windowed reads (`helm transcript`), re-homing, and
  resume-optimized copies (`helm prune`) included.
- **Accounts and quota** — `helm creds` is the live scorecard (headroom,
  reset windows, use-it-or-lose-it verdicts); `helm swap` rescues a seat that
  ran dry with exact resume-under-a-healthier-account commands; `helm homes`
  manages credential homes safely (helm prepares, you run every login).
- **Attested truths** — `helm premise` captures a certainty into the store
  *and* commits a signed digest to a verifiable ledger; `helm premise-check`
  re-verifies it and quotes the finality tier. Belief history supersedes as a
  provable chain, never deleted.
- **The web app** — `helm web` serves four views from one self-contained
  page: your knowledge home, the quota chart + accounts table + credential
  homes, the full sessions browser (search inside transcripts, role-colored
  drawer, one-click resume, team tray), and the config-cascade editor
  (backup → validate → atomic write, one-click restore).
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
$ ./bin/helm sync              # discover your projects across all harnesses
$ ./bin/helm projects          # the list, newest activity first
$ ./bin/helm show <project>    # one project's full record (any name from the list)
$ ./bin/helm sessions          # every session, all harnesses; resume in one paste
$ ./bin/helm doctor            # health check (read-only)
$ ./bin/helm web               # the same, warm, in a browser
```

Optional PATH install — the entry script resolves through symlinks, so one
line makes it `helm` everywhere:

```console
$ ln -s "$PWD/bin/helm" ~/.local/bin/helm
```

**What first run does.** `helm sync` scaffolds `~/.helm` (the global chain +
one dir per discovered project) and, if you already use Claude Code, **adopts
your live memory dir in place** — the resolver reads
`~/.claude/projects/<slug-of-home>/memory` as one more root, same files, no
copy, byte-shape-compatible writes (your existing hooks keep working
untouched; new helm entries land in `~/.helm`, never there unless you point
them there). On a fresh machine with no harness stores at all, everything
still works: sync scaffolds an empty home, the project list is empty until an
agent runs somewhere, and `helm doctor` tells you exactly what it is (and
isn't) seeing. Quota, recall, and the attestation substrate are all
optional — each degrades to one informative line.

- **Command reference** — every verb with syntax and examples:
  [docs/VERBS.md](docs/VERBS.md)
- **Environment** — every `HELM_*` variable (all optional):
  [docs/ENVIRONMENT.md](docs/ENVIRONMENT.md). `HELM_HOME` overrides the
  default `~/.helm`; legacy `MELD_*`/`SESH_*` spellings are accepted as
  fallbacks forever.

## Docs

[VERBS](docs/VERBS.md) — the command reference ·
[ENVIRONMENT](docs/ENVIRONMENT.md) — every knob ·
[ARCHITECTURE](docs/ARCHITECTURE.md) — the two-source model ·
[CONCEPTS](docs/CONCEPTS.md) — the axes and laws ·
[HOOKS](docs/HOOKS.md) — wiring `helm inject` into your harness ·
[WEB](docs/WEB.md) — the browser surface, API, service unit ·
[ATTESTATION](docs/ATTESTATION.md) — the ledger leg ·
[EVOLUTION](docs/EVOLUTION.md) — the self-evolution loop ·
[CONTRIBUTING](CONTRIBUTING.md) — setup, tests, the laws new code obeys

## Status

Young and moving fast. The registry/auto-map, typed store, drain, drift,
lineage, and web views are live; reflexes and the self-evolution loop are in
active development. Issues and harness-format reports welcome — see
[CONTRIBUTING.md](CONTRIBUTING.md) for the on-ramp.

License: TBD (see [docs/LICENSE-TODO.md](docs/LICENSE-TODO.md)).
