---
name: build
description: >
  The central helm physics - the guidelines that keep a team of coding agents on track over long
  horizons. Use at the start of any non-trivial slice of work, and reload before a large/new task.
  /build is the resident operating skill: the work loop, the decide-don't-ask audit, the always-on
  reflexes, and the index of helm's reflexes + hooks. The per-turn reflexes (`helm reflex`, delivered
  by `helm inject`) are the compressed digest of THIS skill; /build is the full version. Early gate
  in the loop = /refine; evolve the helm physics itself = /learn; in the REPAIR basin = /fix
  (fixing/debugging, where ground-truth + verify erode under pressure).
license: MIT
metadata:
  author: helm
  version: "1.0.0"
---

# /build - keep the team on track

`/build` is the engine. It is the same physics at every scope, from a single seat to the whole
fleet. One source of truth; the per-turn injected reflexes are its digest; this is the full version
you reload before large work.

## The loop (every slice of work, in order)

**Specify -> Plan -> Refine -> Implement -> Test/verify -> Review -> DOGFOOD (log lessons, reassess, next).**
- **Dogfood** (the closing gate, `/dogfood`): use the shipped thing on REAL work before calling it
  proven - tests show the code does what you imagined; dogfood shows you imagined the right thing.
  Fold friction back immediately (bit-me -> fix+fixture; fought-me -> task; surprised-me -> premise).
- **Research is ordered + reflexive:** (1) web/prior-art FIRST for anything new, (2) then local
  primitives + memory + the codebase, (3) then a cross-family sibling for refutation.
- **Refine** (the early gate, `/refine`): converge the idea to its ideal shape before implementing -
  reflective equilibrium, one fork per round. Don't build the wrong thing well.
- The team runs many slices in parallel - specialized scalpels, never one prompt re-firing.

## Operating reflexes (always on - the digest lives in rules/dev-process.md)

- **Run every stop-point through /decision-spirit.** It surfaces the next real atom (from the live
  mandate + task list, measured against the FULL outcome) and you advance it this turn; or it proves none
  reachable by naming each blocked item + why, and that grounded proof warrants the pause. The pass is the
  sole arbiter; its output is the move. An idle teammate's next slice is your first atom.
- **Decide, don't ask, on tractable substrate.** Run the decision audit (below) and execute. Ask the
  human only for: public/shared pushes, brand/release names, destructive ops on shared state, sensitive
  creds, cross-team-trust commits, genuine vision calls.
- **No speed-bargaining + no effort-punting.** "partial build / skip the refute / hardcode-now" AND
  "it's big / a marathon / focused-push-later" are both punts behind a cover. Size is never a gate;
  do the full real implementation now and orchestrate (subagents, worktrees, pairing) to do big things
  completely. The human reviews the refined WHOLE, not fragments.
- **Author != reviewer.** A non-trivial change gets a cross-family refutation before it lands; the
  refuter looks for the substrate rule the change violates. A bare "looks good" is not review.
- **Receipt-verify coordination.** An addressed `helm chat` message has an observable delivery path
  (the delivery lane's tool-boundary nudge; an idle recipient wakes only through its armed beacon —
  `helm chat wait --follow` under a Monitor). Check delivery, not vibes; going quiet is not
  coordinating. Coordination FACTS (roster presence, advisory claims) are reads — `helm chat seats` /
  `helm chat claims` — never assumptions.
- **Minimal-diff + loop-guard.** Smallest correct change; a failed fix pivots to a new cause category,
  never a re-tuned retry.
- **Verify the verifier.** A tool's PASS/green/done is suspect until a second method confirms it saw the
  real, non-empty input and checked the right artifact.
- **No stale docs.** A doc is part of the code it describes - refresh the doc the slice touched in the
  done phase; planning/historical docs keep their record.

## +commands

Use `+task`, `+do`, `+fix`, `+clue`, and `+coach` as compact tactical prefixes at the start of a user
prompt. They are not separate storage or command machinery; the reflex recognizer maps them onto
existing helm primitives:

- `+task`: file the payload as a task.
- `+task+do` or `+do`: execute the payload this turn.
- `+fix` or `+task+fix`: treat the payload as a bug-task and enter `/fix`.
- `+clue` or `+task+clue`: attach the payload as evidence/context for the current work or task.
- `+coach` (inline `/coach`): the teaching front door - run the 4-step optimal-placement GATE (stop /
  optimal-place / no-cruft / simplify) and land the steer at the right layer (default lane = prior).
  `+canon` is its alias; `+premise` / `+prior` are its direct prior-ledger flavors.

Unknown `+verb` prefixes fail soft with a one-line explanation. Non-leading uses such as `a +task
later` are plain text.

## The decision audit (fast path - full version in the `decision-spirit` skill)

Before any load-bearing call, answer five:
1. How many layers, serialization hops, and trust boundaries does this add?
2. What is the canonical identity/value/event representation?
3. Where is the capability check and failure attribution at this boundary?
4. What pipeline did I trace from entrypoint to the owner layer?
5. What cheapest probe says the substrate accepts this shape?

Any unclear answer is a design risk - probe or request cross-family refutation before landing. For
the full 19-heuristic audit (composition, identity, replay, evidence, refutation), load `decision-spirit`.

## Reload triggers (automation beats hoping)

Reload `/build` before a large/new task:
- on plan-mode approval (ExitPlanMode) — the plan is about to become implementation;
- on a fresh seat the SessionStart join + per-turn inject already carry the digest — reload the full
  skill when the first non-trivial slice starts, not before.

## R/H index (manage the system from here)

- **Reflexes (R):** `helm reflex list` - the always-on (signal -> steer) layer, delivered per-turn
  by `helm inject` through the wired harness hooks; the every-turn pack is the compressed digest of
  this skill.
- **Hooks (H):** the deterministic seam, wired by `helm hooks install|status|sync` - the per-turn
  inject hook (reflex delivery + brief whisper), the tool-outcome recorder (`helm record`, whose
  counters drive the counter/latch reflexes - the loop-guard analog), and the chat delivery lane
  (SessionStart join, tool-boundary deliver nudge, stop-guard). The stop-guard REFUSES a turn that
  ends with no armed beacon: a launched seat whose `helm chat wait` Monitor is gone cannot be woken
  by anything, so arming it is enforced, not advised (`helm scratch` is the same move for scratch
  placement - the substrate routes small/big/durable and reaps dead-session scratch, so no agent
  reasons about RAM vs disk).
- **Skills (S):** `/refine` (converge pre-build), `/learn` (evolve the helm physics), `/xchk`
  (ground a claim/topic across all sources + flag red herrings); deep skills `decision-spirit`,
  `ground-truth-cross-reference-loop`, `reviewer-implements-own-findings`.
- **Chat surfaces (the interaction grammar):** multi-agent phases run over `helm chat` - the
  human-included fleet room (`helm chat post|read`) carries the exchange; `helm chat dm <seat>` is
  the true 1:1 lane; an addressed message reaches an idle seat through its armed beacon - handoffs
  never strand on a sleeping seat.

## Cross-refs

- `/refine` - the early gate of this loop.
- `/x` - cross-family analysis (xrev before landing); the diversity primitive behind "author != reviewer".
- `/learn` - when a learned behavior should become durable helm physics (store entry/reflex/skill/hook).
- `ground-truth-cross-reference-loop` - when debugging is not converging.
- `/xchk` - the user-invocable front door that runs one pass of the grounding loop across ALL sources
  (web/local/memory/code/git + a cross-family refute) to ground a claim/topic and flag red herrings.
- `reviewer-implements-own-findings` - when the refuter can fix the issue directly.
