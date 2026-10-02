---
name: decision-spirit
description: Use when choosing among architecture, substrate, API, wire-format, runtime, persistence, identity, cap-gating, replay, or cross-layer designs where a wrong choice will compose badly.
---

# Decision Spirit

The resident principal-engineer audit: inspect the actual substrate, compare
paths against the heuristics below, declare the winner, and execute. It supplies
three underused defaults: **confidence** (decide tractable calls), **ambition**
(root fix, not local patch), **framework** (composition, identity, capability,
replay, evidence, refutation before committing). Open REFERENCE.md beside this
file for the incident stories and owner quotes behind any rule.

## When To Use

Before any load-bearing choice: wire formats, schemas, replay, event logs;
identity, persistence, restore, handoff, tenancy, auth, capability boundaries;
runtime layering (adapters, plugins, dispatchers, FFI, build systems); choosing
a primitive/dependency/fork; reviewing a non-trivial design or diff. Not for
pure prose polish, small mechanical cleanup, or cosmetic UI choices unless
they alter runtime behavior or operator trust.

## Decision Contract

If A/B/C paths exist, do NOT hand the human an option menu: produce the audit,
declare the winner, continue. Ask only for: destructive ops, public/shared
pushes, sensitive credentials, brand/release names, irreversible trust, genuine
vision calls. Owner-return parking is justified ONLY for that ask-list or
owner-held inputs: for any RECOVERABLE action (rollback path ready), idle and
broken cost the same owner attention, but trying has upside — attempt with the
rollback prepared; park only what failure makes irrecoverable. Bug class:
`recoverable-parked-for-owner`. If substrate facts are uncertain, run the
cheapest falsifying probe; if still material, request cross-family refutation
while continuing the best reversible path.

## Core Heuristics

1. **Count the layers. Count again.** Name every serialization hop, trust
   boundary, state owner, independently-failing layer; hidden N-hop composition
   needs a simpler representation or clearer bridge.
2. **Canonical representation beats ad hoc translation.** One stable value /
   event / identity / wire contract over repeated parse-serialize hops.
3. **Specialized bridges beat generic dispatch on hot paths.** Bind the
   contract once, cache by contract/version/hash; generic JSON/shell dispatch
   is fallback, not default.
4. **Battle-tested primitives beat fresh invention; fork-extend when
   almost-fit.** Search for the existing primitive first; extend it cleanly
   rather than layering a brittle workaround beside it.
5. **Replay and determinism are invariants.** Host time, random ordering,
   nondeterministic maps, implicit env state, schema drift enter deterministic
   logs only as explicit inputs.
6. **Capability checks happen at every boundary.** Never rely on "the caller
   already checked."
7. **Failure paths are first-class.** Errors name which layer failed and why;
   catch-all/ignored/conflated errors destroy diagnosis.
8. **Layer-add cost should be uniform.** Prefer declarative, pack-shaped,
   cap-gated, replay-safe additions; substrate code only when the primitive
   cannot live safely at a higher layer.
9. **Gaps are action triggers, not exits.** A named limitation needs a
   successor action (fix now, create a task, or request a huddle when no path
   is known); "documented honestly" is not done.
10. **Name the bug class.** Fix the class, not only the instance; if only the
    instance is in scope, make the class boundary explicit in the handoff.
11. **No hardcoded context identity.** Workspace/runtime ids, paths, users,
    tenants, labels, hostnames, branches, session/conversation ids are config,
    discovery, or durable identity fields, never literals in portable logic.
12. **Cheapest probe before expensive fire.** Before a substrate-floor commit
    or costly build/deploy, run the fastest probe that could disprove the
    design.
13. **Feasibility evidence is part of the artifact.** Substrate-floor commits,
    design records, or coordination proposals carry the probe, its
    exit/result, and precedent or an explicit "none found" plus refuter
    verdict.
14. **Cross-family refutation ≠ same-family refinement.** A real refuter is
    from a different model family and hunts the substrate rule the proposal
    violates; "looks good" is not refutation.
15. **Trace the pipeline before editing the leaf.** Follow the path from
    entrypoint to source of truth; fix the owner layer, not the last renderer
    that exposed the bug.
16. **Architecture preflight before code — walk the NOUNS through TIME.** Name
    state owner, extension point, invariant, fallback, existing primitive;
    then lifecycle-walk every touched object (lane/record/claim/obligation/
    session/alert) through its FULL life including stuck/terminal states, and
    state the feature's behavior at each stage. Verb-designed features ship
    holes that are the same object later in its life. Bug class:
    `verb-designed-noun-lifecycle-holed`.
17. **One hypothesis per fix attempt.** Test one cause category per patch; a
    failed category pivots or reframes, never re-tunes.
18. **Premise before proposal.** Confirm the indicted path/data/assumption is
    actually populated, on the production path, exercised by the real workload
    before refuting or building on it — two strong models can converge on a fix
    for a subsystem production never touches (frame-blindness is a property of
    the shared frame; #14 does not catch it). Bug class:
    `converged-design-on-unverified-premise`.
19. **Sequence by leverage and reversibility — order is a decision.** Build the
    dependency graph over ALL known work; do what unblocks/changes the most
    first. Friction cuts rank by payback days (build cost / tax per day); ~2
    days pays back goes ahead of features. Finish what this context is warm on
    (re-grounding costs a full reload). Cheapest premise/probe checks first;
    never stack on unverified state; irreversible/outward-facing steps LAST,
    behind a gate; batch costly deploy/CI at milestones. Order sets WHEN, never
    WHETHER — small/polish work is sequenced, never dropped as "cosmetic".
20. **Seam coherence — validate from each side AND the whole.** A composed seam
    must cohere from component A's invariants, B's invariants, AND the whole;
    elegance holistically but broken locally is a bad seam. Trace ownership to
    the REAL owner, not one consumer's timing. GUARD/WATCHDOG corollary: for
    safety nets the COMPOSITION is the unit of correctness — N individually
    correct guards can jointly guarantee a blind spot; adding/changing ANY
    guard, exemption, or eligibility predicate requires re-deriving the
    coverage matrix (obligation-types x guards). Bug classes:
    `seam-read-from-one-side`, `watchdogs-correct-composition-holed`.
21. **Don't launder vacuity.** A tool/proof/spec/plan discharges its
    obligation honestly or hands the unmet part back explicitly — never
    launder an open gap into a false "done". Verify non-vacuity by READING the
    artifact (the term, the diff, the evidence), not trusting a green check.
    Bug class: `laundered-vacuity`.
22. **Memory is the coordination read-path; disk is a write-behind log.** Hot
    coordination state lives in memory (RAM/tmpfs/in-daemon/in-context) and is
    read from there; disk is the append-only durability log, written behind,
    NEVER the thing an agent goes and READS to coordinate. If agents read disk rows every turn,
    the log became the bus — invert it. Bug class: `disk-as-coordination-read-path`.
23. **ATTENTION BUDGET — the supra-principle for every a2a-comms feature.**
    Design against all three legs at once: (a) TIMING — what they need arrives
    when they need it; (b) RELEVANCE — nothing they don't need (audience by
    capability-to-act, budgets on every injection surface); (c) AVAILABILITY —
    everything they might need stays reachable on demand (withholding never
    removes access). Violating one leg to serve another is the bug. Ask of any
    a2a change: whose attention does this spend, on what, could they have
    pulled it? Bug class: `attention-budget-unmanaged`.
24. **HUMAN-SURFACE PARITY.** A human-facing feature without a TUI/GUI the
    owner has seen and used is NOT DONE; CLI parity is the floor, never the
    finish. Spec/review question: "what is the owner's surface, and when does
    the owner test it?" Track owner surfaces as first-class rows. Bug class:
    `human-surface-never-rowed`.
25. **EXISTENCE SWEEP before net-new — the dual of #18.** Before speccing /
    dispatching / building ANYTHING net-new — ESPECIALLY on an owner feature
    request — sweep what exists (grep the
    surfaces, read the siblings, check recent lands + the task list), then
    decide build/extend/move/refactor/consolidate with an explicit "prior-art: X@file:line ->
    decision because…" line. Make it a gate-artifact: a net-new build-spec or
    dispatch carries a prior-art-scan field; the integrator rejects one that
    lacks it. Bug classes: `built-new-when-extend-existed`, `prior-art-unswept`.
26. **NEVER FIRST CONTACT unless the owner says it is.** In a long-running
    system an apparent first-encounter is almost certainly residue of a prior
    deliberate process: default posture is ARCHAEOLOGY — find the prior
    process, its scope, and what it skipped, BEFORE designing; the owner is
    the sole authority who can declare genuine first contact. A provenance
    stamp you did not chase is prior art you are about to re-derive.
27. **LAW ABOVE THE INSTANCE.** When an incident yields a lesson, capture the
    LAW one level above it, with the instance as evidence — an instance-capture
    fires only on its own recurrence; the law fires on the whole class. Test:
    would a future agent facing a DIFFERENT instance of the same law still be
    caught?
28. **SIMULATE THE HINT — the owner is not always there to supply the
    question.** Retrieval is keyed by the question being held; the facts are
    usually already in context in the wrong register. Before choosing, write
    the three questions the owner would ask and answer them from context:
    HORIZON (what does the next upgrade/reboot/crash/release do to this?),
    OWNERSHIP (who keeps this seam aligned, at what cadence, does it happen
    today — measure the drift), WHO-CARES (does the other party want this at
    all?). And when a STRONG DEFAULT fires, run its stated preconditions first —
    a rule's headline is not its scope.
29. **PRICE EVERY AGENT-LOOP CHANGE IN MODEL REQUESTS x CONTEXT.** One request
    re-sends the whole context since compaction. For any wake, notice,
    injection, guard, refusal, review route or delegation ask: how many
    requests does it add, at whose context size? Wake only for ACT; hold FYI
    for the next tool boundary; warn inside work already happening; run
    multi-step reads in a subagent's small context; lead every alarm with who
    it wakes and why. The fidelity contract is never traded: ACT wakes within
    ~1 min, owner/deadline/failure rows never suppressed, the unclassifiable is
    ACT, nothing dropped. Measure per seat-hour before and after. Bug class:
    `wake-spent-on-no-act`.
30. **ROUTING IS NOT PROGRESS: THE ROUTER OWNS THE FIRST COMMIT.** Before
    routing priority work, check the builder can FINISH it (credit/usage
    window vs build length, writer able to work the lane). Watch for the first
    commit; none in ~30 min means re-route or build with your own delegate. A
    build handed to land runs the tree-wide pre-gate audits, not only focused
    suites.

## Audit Shape

```
Decision: <A vs B vs C>
Heuristics: <which of #1-30 matter most>
Cheapest probe: <command/source/doc check and result>
Winner: <path>
Why: <failure class avoided + invariant preserved>
proposed_fix: <specific implementation shape>
Confidence: <0-100% if evidence is incomplete>
```

For substrate-floor commits or land proposals, add feasibility-evidence:
probe + exit + precedent (file:line / URL / NONE FOUND) + refuter.

## Fast Path

When the full audit is too heavy, answer before moving:

1. How many layers, serialization hops, trust boundaries does this add?
2. What is the canonical identity/value/event representation?
3. Where is the capability check and failure attribution at this boundary?
4. What pipeline did I trace from entrypoint to owner layer?
5. What cheapest probe says the substrate accepts this literal shape?
6. Is the PREMISE live, and is this the right-ORDER step (highest-unblock +
   cheapest-first; irreversible/outward-facing last, behind a gate)?
7. Does each SEAM cohere from every component's invariants AND the whole, and
   is every obligation discharged-or-handed-back — no laundered gap?
8. Have I lifecycle-walked every touched noun to its stuck/terminal states?
9. Have I SIMULATED THE HINT (horizon, ownership, who-cares from context), and
   did a strong default fire before I ran its gate?
10. If this touches the agent loop: how many requests does it add/remove, at
    whose context size, and does the fidelity contract hold? If it routes
    priority work: can the builder finish it, and who watches the first commit?

Any unclear answer is a design risk. Probe or refute before landing.

## Common Failure Modes

- **Elegant design without substrate feasibility** — no one checked the
  API/build/runtime permits the shape.
- **Raw/local id as durable identity** — in-process counters, display labels,
  position numbers leak into persisted contracts.
- **Implicit trust through middle layers** — later layers assume an earlier
  auth/cap check still applies to a transformed request.
- **Option menu handed to the human** — the agent had enough information to
  decide but escalated to avoid ownership.
- **Effort-punt** — "it's big / a marathon / later." Size is never a gate;
  orchestrate (subagents, worktrees, pairing) to do big things completely.
  Only the Decision Contract's ask-list gates; "large" is not on it.

## Cross-Refs

- `ground-truth-cross-reference-loop` — source/docs/community/memory/cross-family
  research before repeated substrate iterations.
- `reviewer-implements-own-findings` — when the refuter can fix the issue directly.
- REFERENCE.md — incident stories, owner quotes, and the long form of every
  heuristic above; open it when a rule's wording is ambiguous or you need the
  measured evidence.
