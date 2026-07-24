---
name: decision-spirit
description: Use when choosing among architecture, substrate, API, wire-format, runtime, persistence, identity, cap-gating, replay, or cross-layer designs where a wrong choice will compose badly.
---

# Decision Spirit

Other viable names: `bedrock`, `keel`, `principal-engineer-spirit`.

## Purpose

This skill is the resident principal-engineer audit. It replaces option-menu
handoffs with a decision: inspect the actual substrate, compare paths against
the heuristics below, pick the strongest path, and execute unless the remaining
question is truly a human-only call.

Use it to supply three defaults that agents often underuse:

- **Confidence:** decide tractable architecture and implementation questions.
- **Ambition:** choose the root fix, not the local patch that preserves drift.
- **Framework:** evaluate composition, identity, capability, replay, evidence,
  and refutation before committing to a path.

## When To Use

Use before any load-bearing choice involving:

- Wire formats, schemas, serialization, replay, event logs, or deterministic output.
- Identity, persistence, restore, handoff, tenancy, auth, or capability boundaries.
- Runtime layering: adapters, plugins, dispatchers, subprocesses, polyglot calls,
  foreign-function bridges, build systems, or native-image/substrate internals.
- Choosing an existing primitive, dependency, fork-extension, or in-house shape.
- Reviewing a non-trivial design, diff, substrate commit, or cross-family finding.

Do not use for pure prose polish, small mechanical cleanup, or cosmetic UI choices
unless they alter runtime behavior or operator trust.

## Decision Contract

If A/B/C paths exist, do not ask the human to choose by default. Produce the audit,
declare the winner, and continue. Ask only for destructive operations, public/shared
pushes, sensitive credentials, brand/release names, irreversible trust decisions, or
genuine vision calls.

**Owner-return parking is justified ONLY for those ask-list items or owner-held
inputs.** For any RECOVERABLE action (durable state, rollback path, respawn-proven),
idle-awaiting-owner costs the same as broken-awaiting-owner — owner attention on
return — but TRYING has upside: some chance of being done instead of inactive (owner
canon 2026-07-08: "what's the functional difference between you being idle while I'm
gone and broken while I'm gone?"). Attempt with the rollback prepared; park only what
a failure would make irrecoverable. Bug class: `recoverable-parked-for-owner`.

If the substrate facts are uncertain, run the cheapest probe that can falsify the
candidate. If the uncertainty is still material, request cross-family refutation
while continuing with the best reversible probe path.

## Core Heuristics

1. **Count the layers. Count again.** Name each serialization hop, trust boundary,
   state owner, and independently-failing layer. A design with hidden N-hop
   composition needs a simpler representation or a clearer bridge.

2. **Canonical representation beats ad hoc translation.** Prefer one stable value
   representation, event shape, identity token, or wire contract over repeated
   parse/serialize hops that each invent local meaning.

3. **Specialized bridges beat generic dispatch on hot paths.** If a cross-runtime
   call repeats, bind the contract once and cache by contract/version/hash. Generic
   JSON or shell dispatch is fallback, not the default substrate.

4. **Battle-tested primitives beat fresh invention. Fork-extend when almost-fit.**
   Search for the existing primitive first. If it covers most of the problem but
   lacks a needed capability, extend the primitive cleanly instead of layering a
   brittle workaround beside it.

5. **Replay and determinism are invariants.** Host time, random ordering,
   nondeterministic maps, implicit environment state, and schema drift cannot enter
   deterministic logs unless captured as explicit inputs.

6. **Capability checks happen at every boundary.** Never rely on "the caller already
   checked." Each layer transition names and enforces the capability it needs.

7. **Failure paths are first-class.** Errors must say which layer failed and why.
   Catch-all, ignored, or conflated errors destroy operator diagnosis.

8. **Layer-add cost should be uniform.** Prefer declarative, pack-shaped, cap-gated
   additions when they can be replay-safe. Reach for substrate code only when the
   primitive cannot live safely at a higher layer.

9. **Gaps are action triggers, not exits.** A named limitation is acceptable only
   with a successor action: fix now, create a task, or request a huddle when no path
   is known. "Documented honestly" is not done.

10. **Name the bug class.** Fix the class, not only the instance. If only the
    instance is in scope, make the class boundary explicit in the handoff.

11. **No hardcoded context identity.** Workspace ids, runtime ids, paths, users,
    tenants, labels, hostnames, branch names, session ids, and conversation ids are config,
    discovery, or durable identity fields, not literals inside portable logic.

12. **Cheapest probe before expensive fire.** Before a substrate-floor commit or
    costly build/deploy, run the fastest source/doc/binary/minimal-compile probe
    that could disprove the design.

13. **Feasibility evidence is part of the artifact.** Substrate-floor commits,
    design records, or coordination proposals include the probe, exit/result, and
    upstream/source precedent or an explicit "none found" plus refuter verdict.

14. **Cross-family refutation is different from same-family refinement.** A real
    refuter comes from a different model family/basin and must look for the
    substrate rule the proposal violates. A bare "looks good" is not refutation.

15. **Trace the pipeline before editing the leaf.** Before changing a symptom file,
    follow the request/data/control path from entrypoint to source of truth. Fix the
    owner layer, not the last renderer, adapter, or error site that exposed the bug.

16. **Architecture preflight before code — and walk the NOUNS through TIME.** Name the
    state owner, extension point, invariant, fallback path, and existing primitive
    before implementing. Then LIFECYCLE-WALK every object the feature touches
    (lane/record/claim/obligation/session/alert) through its
    FULL life including stuck/terminal states; state the feature's behavior at each
    stage. Features designed as verbs on the primary case ship holes that are just
    the same object LATER in its own life (owner canon 2026-07-08: stale-claim x
    read-but-unresolved x deadline-less x dead-beacon — all lifecycle-walk misses;
    "human UX designers would not stand for this"). Bug class:
    `verb-designed-noun-lifecycle-holed`. The smallest correct diff is the one at
    the right layer, not necessarily the nearest line.

17. **One hypothesis per fix attempt** (the Loop-guard reflex, applied to design): test
    one cause category per patch; a failed category -> pivot or reframe, never re-tune.

18. **Premise before proposal.** Verify the premise is LIVE before refuting or building on it.
    Before debugging a fix's shape, designing against a structure, or accepting a "converged"
    plan, confirm the indicted code path / data / assumption is actually populated, on the
    production path, exercised by the real workload - not merely plausible. Two strong models can
    converge efficiently on a fix aimed at a subsystem production never touches; cross-family
    refutation (#14) satisfies author!=eyes and STILL misses this, because frame-blindness is a
    property of the shared FRAME, not the shared family. The refuter's first obligation is the
    premise. Bug class: `converged-design-on-unverified-premise`.

19. **Sequence by leverage and reversibility - order is a decision, not the order things were
    mentioned.** Build the ideal DEPENDENCY GRAPH over ALL known work (not just the current ask) and
    do what UNBLOCKS or CHANGES the most still-undone tasks first - global topological impact, not
    greedy-local "what's next on the list" (a change that reshapes 5 pending tasks comes before a
    self-contained one, even if the self-contained one was mentioned first). Exploit HOT CONTEXT:
    finish what this context is already warm on before it goes cold - re-grounding a dropped thread
    costs a full reload, so context is a prime resource to spend, not refill. Cheapest premise/probe
    checks first (#18); secure a verified base before the next layer (never stack on unverified
    state); order irreversible / outward-facing steps LAST and behind a gate (push, deploy,
    destructive ops, sends); batch the costly (deploy/CI/QC) at milestones, stream cheap verified
    increments. Order sets WHEN, never WHETHER: small / polish / finish work earns its slot in the
    graph - it is sequenced into its right moment, never dropped or written off as "cosmetic." Polish
    that serves the goal is part of done, not an optional extra. Wrong order wastes work (built on a
    bad premise) or strands it (irreversible-early); using order as cover to skip the finish is a punt.

20. **Seam coherence - validate from each side AND the whole.** When composing across
    projects or components, every seam must cohere THREE
    ways at once: from component A's own invariants, from B's own invariants, AND as the
    combined whole. A merge elegant holistically but breaking ONE component's internal
    logic is a BAD seam - it rots, surprises, and blocks that component's independent
    evolution (confederalism for architecture: local coherence + federated coherence).
    Trace ownership to the REAL owner, not one consumer's local note - a consumer's wire-in
    TIMING is not the primitive's availability or ownership. Bug class:
    `seam-read-from-one-side`. Live: "a shared session primitive is Gate-B-gated" read one
    project's consumption-timing (its consumers-column schedule) as the primitive's gate - but the
    spec belonged to one project, the adapter to another, and the resolution line bound
    only the adapter. Sharpens #15 (trace to owner) + #18 (premise before proposal).
    **GUARD/WATCHDOG corollary (owner canon 2026-07-08: "this is always the case"):** for
    safety nets specifically, per-spec component correctness guarantees NOTHING — the
    COMPOSITION is the unit of correctness. N guards each with a clean conscience can
    jointly guarantee a blind spot (live: work-offer[unclaimed-only+role-exempt] x
    xrev-nag[debts-only] x megaphone[unread-only] x escalate[deadline-only] = a
    read-but-unresolved thread at an exempt seat sleeps forever; fleet idled hours).
    Adding/changing ANY guard, exemption, or eligibility predicate REQUIRES re-deriving
    the coverage matrix (obligation-types x guards; every cell covered/exempt-why/HOLE).
    Bug class: `watchdogs-correct-composition-holed`.

21. **Don't launder vacuity** (Ember/dregg discipline, harvested per prem-ember-alliance).
    A tool, proof, spec, or plan must either discharge its obligation honestly or HAND THE
    UNMET PART BACK explicitly - never launder an open gap (a `sorry`, an unverified
    premise, a matched-buggy-oracle, a stubbed step) into a false "PROVED" / "done." An
    unproved spec presented as a guarantee is vacuous, not honest. Verify non-vacuity by
    READING the artifact - the generated term, the actual diff, the cited evidence - not by
    trusting a green check or confident framing (dregg HATCHERY: "the tactics either close
    the goal honestly or hand it back"; non-vacuity is checked by reading the term, not the
    green). Bug class: `laundered-vacuity` (form substituting for a discharged obligation).
    Sharpens #9 (documented is not done), #13 (evidence-in-artifact), #14 ("looks good" is
    not refutation).

23. **ATTENTION BUDGET — the supra-principle for every a2a-comms-touching feature**
    (owner canon 2026-07-08: built into planning, then forgotten; re-learned by incident).
    Every agent has a finite attention budget; any feature that injects, delivers,
    alerts, or drains MUST be designed against all three legs at once:
    (a) TIMING — what they need arrives WHEN they need it (contextual firing,
    escalation on staleness); (b) RELEVANCE — NOTHING they don't need (audience by
    capability-to-act: leadership-only cred state, level-state collapses to latest,
    budgets/caps on every injection surface — per-toolcall, per-turn, drain, alert);
    (c) AVAILABILITY — everything they MIGHT need stays REACHABLE on demand
    (withholding never removes access: digests carry reach-deeper pointers, the
    durable log stays queryable). Violating one leg to serve another is the bug:
    firehose serves (c) by destroying (b); over-withholding serves (b) by destroying
    (c). Design/review question for ANY a2a change: "whose attention does this spend,
    on what, and could they have pulled it instead?" Unifies: whisper budgets,
    contextual-firing canon, alert audience routing, drain digests, verdict embargo.
    Bug class: `attention-budget-unmanaged`.

24. **HUMAN-SURFACE PARITY — a human-facing feature without a TUI-GUI the owner has
    seen and used is NOT DONE** (owner canon 2026-07-08: an entire class — shared-memory space
    viewers, TUI chat, loop-source editing, the rich away system — fell out of ALL
    tracking because agent-facing primitives get rows and owner-facing surfaces
    don't; no agent feels their absence). CLI parity is the floor, never the finish.
    Spec/review question for any feature a human will touch: "what is the owner's
    surface, and when does the owner test it?" — owner-tested is the acceptance
    gate. Track owner-surfaces as first-class rows on the integration board.
    Bug class: `human-surface-never-rowed`.

22. **Memory is the coordination read-path; disk is a write-behind log.** Hot coordination
    state - who-is-free, what-is-claimed, the latest signal/decision - lives in MEMORY
    (RAM/tmpfs `/dev/shm`, in-daemon, or in-context) and is READ from there every turn. DISK is
    the append-only DURABILITY / audit / replay log, written BEHIND from memory (async, post-hoc),
    NEVER the thing an agent goes and READS to coordinate. If agents poll disk every turn
    to learn current state, the log became the bus - invert it: serve coordination from memory,
    demote disk to write-behind. Prior art: write-behind cache, event-sourcing's in-memory read
    model, LMAX Disruptor, "the database is not your message queue." Bug class:
    `disk-as-coordination-read-path` / `log-as-bus`. (The inversion the shared-memory space fell
    into - hot state was correct in RAM but agents still read disk rows every turn; the keel of
    the RAM-first re-architecture. 2026-06-21.)

25. **EXISTENCE SWEEP before net-new — the dual of #18** (owner canon 2026-07-23:
    "you don't tend to notice when they've already mostly built something that
    nevertheless needs improving / implementing elsewhere / refactoring / moving").
    #18 guards the premise you build ON; this guards the thing you are about to
    CREATE. Before speccing / dispatching / building ANYTHING net-new — a tab, panel,
    function, endpoint, lane, feature, ESPECIALLY on an owner feature request — FIRST
    sweep what already exists: grep the surfaces that render/handle this, read the
    siblings, check recent lands + the fleet task list for overlapping work. THEN
    decide build / extend / move / refactor / consolidate with an explicit
    "prior-art: X@file:line -> decision because…" line. Why a NEW reflex when the
    values already say reuse>invent (#4): every VERIFICATION reflex (#18 premise,
    verify-before-done, #15 trace-pipeline) fires AFTER the decision, to check that
    what you are doing is true; NONE asks "should I be building this at all, or
    extending what's there?" — so this class needs a trigger UPSTREAM of the decision,
    not another downstream check. Make it a gate-ARTIFACT, not a vibe: a build-spec /
    dispatch carries a prior-art-scan field (as a land carries feasibility-evidence
    #13), and the integrator REJECTS a net-new dispatch that lacks it — same as a
    "done" with no proof. Turns #4 from a passive value into a triggered procedure.
    Bug class: `built-new-when-extend-existed` / `prior-art-unswept`. Live: speced a
    from-scratch roster tab; the owner had to point out the ledger already rendered
    SEAT-ACTIVITY + FLEET-TODOS off the same data — every reflex I ran fired AFTER I'd
    already decided to build. The fix dogfooded itself: it EXTENDS this skill instead
    of inventing a new one.

## Audit Shape

For substantial choices, write a compact table or bullets:

```
Decision: <A vs B vs C>
Heuristics: <which of #1-21 matter most>
Cheapest probe: <command/source/doc check and result>
Winner: <path>
Why: <failure class avoided + invariant preserved>
proposed_fix: <specific implementation shape>
Confidence: <0-100% if evidence is incomplete>
```

For substrate-floor commits or land proposals, add:

```
Feasibility-evidence:
  probe: <command or source/doc read>
  exit: <code or n/a> - <one-line result>
  precedent: <file:line / URL / NONE FOUND>
  refuter: <family + verdict, if required>
```

## Fast Path

When the full audit is too heavy, answer five questions before moving:

1. How many layers, serialization hops, and trust boundaries does this add?
2. What is the canonical identity/value/event representation?
3. Where is the capability check and failure attribution at this boundary?
4. What pipeline did I trace from entrypoint to owner layer?
5. What cheapest probe says the substrate accepts this literal shape?
6. Is the PREMISE live (the path/structure I am acting on is actually exercised), and is this the right-ORDER step (highest-unblock + cheapest-first; irreversible/outward-facing last, behind a gate)?
7. Does each SEAM cohere from every component's own invariants AND the whole (not one consumer's view), and is every obligation discharged-or-handed-back - no gap laundered into a false "done"?
8. Have I lifecycle-walked every touched noun to its stuck/terminal states (what happens to this claim/record/obligation when its holder dies, forgets, or reads-and-shrugs)?

Any unclear answer is a design risk. Probe or refute before landing.

## Common Failure Modes

- **Elegant design without substrate feasibility:** audit picked a good-looking
  path, but no one checked whether the API/build/runtime permits that shape.
- **Raw/local id as durable identity:** in-process counters, display labels, or
  position numbers leak into cross-process or persisted contracts.
- **Implicit trust through middle layers:** one layer checks auth/caps and later
  layers assume the check still applies to a transformed request.
- **Option menu handed to the human:** the agent had enough information to decide
  but escalated to avoid ownership.
- **Effort-punt (deferring behind a subjective-effort cover):** "it's big / a marathon /
  better as a focused push later / I'd only half-do it." Size is never a gate. Default to the
  full real implementation now; orchestrate (subagents, worktrees, pairing) to do big things
  completely. The human reviews the refined WHOLE, not punted fragments. Only genuine human-only
  calls (the Decision Contract's ask-list) gate; "large" is not one of them.

## Cross-Refs

- `ground-truth-cross-reference-loop` for source/docs/community/memory/cross-family
  research before repeated substrate iterations.
- `reviewer-implements-own-findings` when the refuter can fix the issue directly.
