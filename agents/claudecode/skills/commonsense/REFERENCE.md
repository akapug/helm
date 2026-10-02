# commonsense — reference: owner rulings and incident stories

Open this when a SKILL.md line needs its evidence or its measured history.
The rules live in SKILL.md; this file is their provenance.

## What this skill is

The user invokes this when you missed something a senior dev wouldn't need
told: "you're missing something obvious - use common sense." It's not a
scolding, it's a lens. The two failure families it catches first: the
*deferral* punt (defer/shrink/fake-finish and call it prudence) and the
*narrow-framing* punt (commit to the first viable path, or hand over a false
binary, without generating the full option space).

## Part 1 provenance

### #1 — no fatigue, no fresh-context deferral

Owner ruling 2026-06-09 (kept as history): helm sessions run days-to-weeks;
"late in a long session / fatigued context" is the SAME punt as "fresh
energy". When context fills, you MANAGE it with tools and proceed: archive
open work to the integration board, checkpoint to memory, dispatch subagents
for big-LoC work, prune + re-triage the tasklist, /compact deliberately.

### #6 — the false binary

Example (owner ruling 2026-06-09): proposing live-cluster deploy vs a
sub-slice when the real criterion was "test safely" — and a spare cheap box /
fly.io / local docker / home-lab nodes all satisfied it with zero live risk.
A false binary ("rush the risky live deploy OR punt") almost always hides a
safe third path.

### The only real gates

In the core/public build, "a real, OBSERVED resource limit" means a concrete
wall you can point at (the task literally can't proceed without an input you
lack). Never invent a "budget" you can't measure. (Cred/usage tooling lives
in the fleet layer above helm seats — not a seat concern.)

## Part 2 provenance

### Verify like you'll be quoted on it

"Never claim done from inference" was feedback the owner flagged 2×. Punting
a verify to the human when the auth is local-or-mintable IS the punt.

### Strategy & dependencies

Added 2026-08-22 after the Orca fork/upstream whiff — the corpus above it was
entirely tactical; the miss was strategic.

- SIMULATE THE HINT: the facts are almost always already in your context in
  the wrong register — citations, incidental observations; what is missing is
  the QUESTION. The owner is not always there to supply it, so generating it
  is the job.
- Non-Ember dependency posture: state release cadence, appetite for our
  patches (PR volume, our past PR outcomes), our fork's measured drift from
  upstream, and which of its surfaces are STABLE (CLI, env contracts, file
  formats) versus INTERNAL (RPC schemas, renderer code) BEFORE forking /
  upstreaming / patching. High-velocity upstream that does not know us →
  compose on its public surfaces from OUR side, make our behaviour idempotent
  against whatever it does, and make every seam assumption DETECTABLE (a
  doctor line, not a post-reboot surprise).
- EMBER REPOS BYPASS THIS GATE ENTIRELY: emberian/* (dregg, cv, DreggNet,
  mediateor, ...) are an unconditional upstream — always push the fix up, no
  posture assessment, no proving gate, and — the owner's standing ruling, the
  one named exception to the public-push ask in Part 1 #5 — no asking first
  (store heuristic ember-repos-always-upstream-no-gate; the alliance WANTS our
  signal, and withholding a fix until we deem it proven is doing Ember's
  evaluation for them, badly). The fork-is-PR-staging pattern is Ember's; it
  is not a universal default, and Orca in particular is ruled out of it (store
  premise orca-accommodations-live-in-helm-never-fork-or-upstream).
- A STRONG DEFAULT FIRES BEFORE ITS GATE. Live: three open PRs to Orca read as
  precedent for a fourth; the gate premise had been injected eight hours
  earlier and did not fire at the decision. A hard rule or a local precedent
  arrives as a conclusion; its preconditions live one layer down and are
  skipped under momentum.

## gbrain crossover (long form)

If the user chose the full install with gbrain curated, `/commonsense` can be
*enhanced*, not replaced: gbrain's retrieval + typed graph can surface this
agent's OWN past misses ("you deferred X for 'fresh energy', then it was fine
at 2am"; "you skipped the git-history check last time") as evidence, making
the check concrete rather than exhortative, and can rank which corpus items
you most often skip. Detection-gated (only if gbrain is present); the core
skill stands alone without it.

Note: **gbrain is the memory layer of [gstack](https://github.com/garrytan/gstack)**,
the parent methodology toolkit (helm's peer) — so "g" here means
gbrain-the-memory-engine specifically, not the whole gstack toolkit. helm
borrows gbrain's *retrieval* mechanics for this crossover; gstack itself is a
recommended companion, not a bundled backend.
