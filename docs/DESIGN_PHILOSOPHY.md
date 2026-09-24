# helm design philosophy — why helm is shaped this way

helm is a coordination substrate for a fleet of coding agents. Most of its
surface area is not features; it is a small set of design commitments that keep
a fast, parallel, multi-family fleet producing correct work over a long horizon.
This document states those commitments generally, so a team standing up their own
fleet can adopt them on purpose rather than rediscover them under load.

Five principles carry the weight, ordered from *how we decide what to build* down
to *how we keep the built thing legible and honest*. Three concrete interface
floors follow from them — the non-negotiable rules for how any capability meets
the humans and agents that share the work.

---

## 1. UX → AX — reshape the human primitive, don't reinvent it

Human coordination primitives — kanban, incident command, standups,
double-entry bookkeeping, the postal system — are the *proven* solutions to the
underlying problems of coordination, attention, and trust. They survived
centuries of contact with real teams. A fleet of agents faces the same
underlying problems, so the first move on any agent-experience (AX) design
question is to **survey the proven human primitive before inventing anything.**

Agents differ from human teams on a few specific axes, and only those axes:

- **latent-native, not symbol-native** — they reason in a denser medium than the
  documents humans pass around;
- **parallel, not one-thread** — there is no single mouth, no single attention;
- **bodiless** — no mortality, no politeness cost;
- **bounded, not boundless** — the active context window is a *hard ceiling*, and
  each session starts cold. What persists across that bound is durable external
  memory plus session-resume; **handoff and compaction are the mechanisms built
  to live with the ceiling, not evidence that there isn't one.** An agent's
  working memory is finite and its continuity is engineered, not innate.

The synthesis: **keep every human primitive that solves a real coordination
problem; reshape only the ones that were artifacts of human *bodies*.** A
standup exists because humans cannot read each other's working memory — an agent
fleet can make status ambient instead. Turn-taking exists because humans share
one acoustic channel — agents do not. But the *ledger*, the *incident
commander*, the *work item with an owner* solve problems that do not go away when
the participants change; keep them.

The resident lens for finding the reshape: translate the agent system into its
human-UX equivalent — imagine human developers working a seemingly-broken
version of the same tools — and walk the interactions step by step. **The
missing affordances are the spec.**

---

## 2. Composition over accretion — recognize before you build

Advanced systems here are built by **small steps toward big ideas.** Primitives
get built for a local need, then composed into the larger capability — and
composed *wrong* many times before right. That is the normal shape of the work,
not a failure of it.

Two consequences follow, and both cut against the reflex to write new code:

**The big idea usually already mostly exists.** It "arrives" not when someone
builds it net-new, but when someone **recognizes that scattered primitives
already compose into it.** Most big-idea requests are already largely present in
pieces that shipped for other reasons. So the standing rule is to **run the
existence sweep first**: before writing a new verb, hook, lane, or store type,
sweep for what already composes into the ask. "This is already built" is the
*healthy* result of the pattern, not an embarrassment. Building net-new when the
capability is 80% present is how you add to the tangle.

**The best change removes code.** A change that re-composes and *deletes* code is
worth more than one that adds a feature — because every new primitive is future
surface that has to be understood, tested, and patrolled forever. When a third
same-shaped operation appears, reach for a data structure over more control flow.
Add a primitive only when it genuinely reduces the total.

Wrong compositions are **information, not defects to be ashamed of.** Each one —
a duplicate primitive, a tri-state split across two copies of a file, a resource
collision — teaches the correct seam. Read the map it draws; fix the *class* at
that seam, not the one instance.

The failure mode this guards against is the system **tangling**: orphaned
branches, duplicate primitives, conventions asserted with no mechanism to enforce
them. A tangled system is how an accreting codebase reaches ~90% of its ambition
and then collapses under its own weight — every new change fighting the residue
of the wrong compositions no one cleaned. Composition is the antidote; accretion
is the disease.

---

## 3. Review as an immune system — non-delegable, not polish

A fleet writes code fast and in parallel, which means it produces wrong
compositions fast and in parallel. Recognizing and cleaning them is not cleanup
you do if there is time left over. It is a **structural, non-delegable half of
the work** — the immune system that keeps §2's snowball from tangling. Skip it
and nothing catches the tangle until it is load-bearing.

Two duties live inside it, and the second is the one dropped first:

1. **Review and clean.** Councils, cross-family review, gap registers, the
   existence sweep. **Cross-family review is doing real work here:** a different
   model family catches what same-family refinement structurally cannot, because
   it does not share the frame that produced the mistake. Heterogeneous review
   outperforms homogeneous review on exactly the hard, subtle defects that a
   pre-land pass exists to catch.
2. **Triage.** Decide *what even needs cleaning.* A throwaway wrapper around an
   API does not deserve the scrutiny a core coordination primitive does. Spending
   the immune budget uniformly is its own failure; the judgment about where to
   spend it is part of the job, not a shortcut around it.

The immune pass is the first thing an agent is tempted to drop when tired or
rushed — and dropping it is precisely how accreting systems die. Treat it as a
hard gate: **if a slice ships code with no immune-system pass, the slice is not
done — it has only deferred the tangle.** Budget the pass *into* the work, not
after it.

A related discipline belongs here: **a convention with no enforcing mechanism is
a future tangle.** When review turns up a rule everyone "just knows," turn it
into a hook, a guard, or a check that fires — the substrate must enforce it, not
rely on every future contributor remembering.

---

## 4. Provenance discipline — an honest claim beats a confident one

Downstream decisions in a fleet — what to build next, which architecture to
commit to, whether a result can be relied on — are steered in part by how
confident, cross-referenced, and ground-truthed the fleet's own analysis
*appears* to be. That makes the epistemic quality of an analysis a first-class
product concern, not a matter of style.

The core rule:

> **A falsely-confident analysis is worse than an honestly-uncertain one** —
> because it feeds a downstream decision a bad reading, and the decision is
> steered off it.

So load-bearing claims carry their provenance explicitly:

- **mark each claim** *measured*, *traced*, or *inferred* — a benchmarked number,
  a value read out of the code, and an educated guess are three different kinds
  of thing and must not read alike;
- **calibrate confidence** and cross-reference to what is already established;
- **open with epistemic state** — a settled answer, a hypothesis to verify, or an
  honest "I don't know yet" — rather than defaulting every statement to the same
  assertive register.

Anything that *manufactures* confidence is a defect on the same footing as a
functional bug: a headline promising findings not yet in hand, a test that
asserts the absence of a complaint and therefore passes vacuously, a corrector's
next claim relayed as "verified" when it was only agreed with. Each one hands a
downstream decision a false reading.

The design obligation that follows: **build surfaces and tools so the grounded
answer is the easy one to give.** Make it cheaper to cite a measurement than to
assert one, cheaper to say "unverified" than to imply verification. The honest
path has to be the low-friction path, or provenance discipline erodes exactly
when the fleet is under the most pressure.

---

## 5. Information freshness is the design metric

The primary failure mode of a fast fleet is **not bad judgment — it is
decision-on-stale-state.** Worktree collisions, duplicate work, status-blindness,
a verdict dropped before anyone read it, a lane acting on state that changed a
minute ago: these are the recurring, fractal shape of fleet failure, and they all
reduce to a participant deciding on information that was already out of date.

This reframes what "good" means for the substrate. The goal is not more features;
it is **shared state that is ambient, fast, fresh, complete, and
relevance-curated** — so that good *collective* judgment emerges from many
participants each seeing an accurate picture. The metric a coordination
substrate is optimized against is therefore:

> **latency · freshness · coverage · relevance · determinism** — not feature
> count.

This principle also underwrites principle 4: a fleet deciding on stale state
tends to produce *falsely-confident* analysis — confident because the participant
genuinely believed its picture, false because the picture had moved. Keep the
shared picture fresh and a whole class of confident-but-wrong analysis simply
stops being generated. Freshness is not a nice-to-have on top of the coordination
layer; it is what the coordination layer is *for*.

---

## The interface floors

The five principles decide *what* to build and *how* to keep it honest. Three
floors govern *how any capability meets its users* — the humans who steer and the
agents who execute. They are non-negotiable, not because they are grand, but
because violating one quietly forces expensive work onto the other side of the
interface later.

### Floor 1 — Action symmetry

**Every state-changing action ships as a symmetric pair: a human-usable
affordance *and* an agent-callable tool.** Neither half is optional.

- An **agent-only** action forces humans to build the UI for it later — the
  capability exists but no person can drive or inspect it.
- A **human-only** action forces agents to scrape a surface built for eyes —
  brittle, and a standing invitation to error.

So the floor is a rule about coverage: no state change may exist that only one
kind of participant can invoke or observe. Objects that carry decisions —
reviews, verdicts, work items — get stable identity (IDs, URLs, transcripts) for
the same reason, so both halves can reference and operate on the same thing
rather than each keeping a private copy.

### Floor 2 — A human-visible surface is the acceptance bar

**A backend capability with no human-visible surface counts as nothing.**
"Compiles" is not "done." A slice is complete only when a person can *see* it
work on the real surface — because the surface is where human judgment and taste
actually engage with the system, and a capability no human can watch operate has
not been accepted, only asserted.

The practical corollary: a capability's status has to be written where the
surface actually reads it. A value emitted to a location no view renders is
invisible, and invisible is indistinguishable from absent. Wire the surface to
the state, or the state does not count.

### Floor 3 — Keep a symbolic, auditable representation at boundaries

**At every trust, durability, cross-family, or human boundary, the representation
stays symbolic and human-legible.** A denser, agent-native encoding is fine
*inside* a single trust boundary — where both ends are the same process or the
same family, no state is persisted, and no human or auditor has to read the hop.
But the moment a hop crosses a boundary — persisted to durable storage, handed to
a different model family, or surfaced for a person to read — **legibility wins
over density.**

A symbolic export format is what makes cross-family review possible (a second
family can only refute what it can read), what makes the record auditable after
the fact, and what lets a human read what the fleet actually did. The rule is a
gradient, not a toggle: go denser only as far inward as the trust boundary
extends, and no further.

---

## Scope — what this document leaves out

This document deliberately omits two ideas that belong to a specific
operator-partnership rather than to the general substrate: a thesis about the
complementary roles of a human owner and the fleet, and a judgment-consultation
safety model built around a single operator reading the fleet's epistemic
signals — both are partnership-specific and not fully built out, so they live in
project-internal notes and are called out here as a stated decision rather than a
silent gap.

---

## The through-line

The five principles are one commitment seen from five angles. **Reshape the
proven human primitive** rather than invent (1), and **compose the primitives you
already have** rather than accrete new ones (2), because a fleet's real risk is a
tangle of wrong compositions no one cleaned. **Run the immune system that cleans
them** (3) as non-negotiable work, not polish. **Keep every load-bearing claim
honest about its provenance** (4), because decisions are steered on apparent
confidence. And **keep the shared picture fresh** (5), because a stale picture is
the source of both bad decisions and false confidence.

The three interface floors are how those commitments meet the surface: **action
symmetry** keeps every capability operable by both halves, **a human-visible
acceptance bar** keeps "done" honest, and **symbolic representation at
boundaries** keeps the work legible and auditable across every trust line it
crosses.

The rule that keeps the whole thing self-correcting is the same one from
principle 4: **an honestly-uncertain claim beats a falsely-confident one, every
time** — including every claim in this document.
