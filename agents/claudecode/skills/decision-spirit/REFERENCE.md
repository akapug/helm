# decision-spirit — reference: incidents, owner canon, long form

Open this when a SKILL.md rule's wording is ambiguous, when you need the
measured evidence behind a heuristic, or when writing a capture that cites one.
The rules live in SKILL.md; this file is their provenance.

Other viable names for the skill: `bedrock`, `keel`, `principal-engineer-spirit`.

## Decision Contract — the recoverable-action canon

Owner canon 2026-07-08: "what's the functional difference between you being
idle while I'm gone and broken while I'm gone?" For any RECOVERABLE action
(durable state, rollback path, respawn-proven), idle-awaiting-owner costs the
same as broken-awaiting-owner — owner attention on return — but TRYING has
upside: some chance of being done instead of inactive. Attempt with the
rollback prepared; park only what a failure would make irrecoverable.
Bug class: `recoverable-parked-for-owner`.

## Heuristic provenance

### #16 — walk the nouns through time

Owner canon 2026-07-08: stale-claim x read-but-unresolved x deadline-less x
dead-beacon — all lifecycle-walk misses; "human UX designers would not stand
for this". Features designed as verbs on the primary case ship holes that are
just the same object LATER in its own life. The smallest correct diff is the
one at the right layer, not necessarily the nearest line.
Bug class: `verb-designed-noun-lifecycle-holed`.

### #18 — premise before proposal

Two strong models can converge efficiently on a fix aimed at a subsystem
production never touches; cross-family refutation (#14) satisfies author!=eyes
and STILL misses this, because frame-blindness is a property of the shared
FRAME, not the shared family. The refuter's first obligation is the premise.
Bug class: `converged-design-on-unverified-premise`.

### #19 — sequence by leverage and reversibility

Build the ideal DEPENDENCY GRAPH over ALL known work (not just the current
ask) and do what UNBLOCKS or CHANGES the most still-undone tasks first —
global topological impact, not greedy-local "what's next on the list" (a
change that reshapes 5 pending tasks comes before a self-contained one, even
if the self-contained one was mentioned first). Friction is in the graph: a
tax cut (a fix that removes a step agents repeat by hand, a workaround or a
false refusal) ranks by payback days = build cost / tax removed per day, and
one that pays back within about 2 days goes ahead of new features. Exploit HOT
CONTEXT: finish what this context is already warm on before it goes cold —
re-grounding a dropped thread costs a full reload, so context is a prime
resource to spend, not refill. Cheapest premise/probe checks first (#18);
secure a verified base before the next layer (never stack on unverified
state); order irreversible / outward-facing steps LAST and behind a gate
(push, deploy, destructive ops, sends); batch the costly (deploy/CI/QC) at
milestones, stream cheap verified increments. Order sets WHEN, never WHETHER:
small / polish / finish work earns its slot in the graph — it is sequenced
into its right moment, never dropped or written off as "cosmetic." Polish that
serves the goal is part of done, not an optional extra. Wrong order wastes
work (built on a bad premise) or strands it (irreversible-early); using order
as cover to skip the finish is a punt.

### #20 — seam coherence

When composing across projects or components, every seam must cohere THREE
ways at once: from component A's own invariants, from B's own invariants, AND
as the combined whole. A merge elegant holistically but breaking ONE
component's internal logic is a BAD seam — it rots, surprises, and blocks that
component's independent evolution (confederalism for architecture: local
coherence + federated coherence). Trace ownership to the REAL owner, not one
consumer's local note — a consumer's wire-in TIMING is not the primitive's
availability or ownership. Live: "OpenSession is code-analysis-Gate-B-gated"
read code-analysis's consumption-timing (its CONSUMERS-column schedule) as the
primitive's gate — but the spec belonged to one project, the adapter to
another, and the resolution line bound only the adapter. Sharpens #15 (trace
to owner) + #18 (premise before proposal).

GUARD/WATCHDOG corollary (owner canon 2026-07-08: "this is always the case"):
for safety nets specifically, per-spec component correctness guarantees
NOTHING — the COMPOSITION is the unit of correctness. N guards each with a
clean conscience can jointly guarantee a blind spot (live:
work-offer[unclaimed-only+role-exempt] x xrev-nag[debts-only] x
megaphone[unread-only] x escalate[deadline-only] = a read-but-unresolved
thread at an exempt seat sleeps forever; fleet idled hours). Adding/changing
ANY guard, exemption, or eligibility predicate REQUIRES re-deriving the
coverage matrix (obligation-types x guards; every cell
covered/exempt-why/HOLE). Bug class: `watchdogs-correct-composition-holed`.

### #21 — don't launder vacuity

Ember/dregg discipline, harvested per prem-ember-alliance. A tool, proof,
spec, or plan must either discharge its obligation honestly or HAND THE UNMET
PART BACK explicitly — never launder an open gap (a `sorry`, an unverified
premise, a matched-buggy-oracle, a stubbed step) into a false "PROVED" /
"done." An unproved spec presented as a guarantee is vacuous, not honest.
Verify non-vacuity by READING the artifact — the generated term, the actual
diff, the cited evidence — not by trusting a green check or confident framing
(dregg HATCHERY: "the tactics either close the goal honestly or hand it back";
non-vacuity is checked by reading the term, not the green). Bug class:
`laundered-vacuity` (form substituting for a discharged obligation). Sharpens
#9 (documented is not done), #13 (evidence-in-artifact), #14 ("looks good" is
not refutation).

### #22 — memory is the coordination read-path

Hot coordination state — who-is-free, what-is-claimed, the latest
signal/decision — lives in MEMORY (RAM/tmpfs `/dev/shm`, in-daemon, or
in-context) and is READ from there every turn. DISK is the append-only
DURABILITY / audit / replay log, written BEHIND from memory (async, post-hoc),
NEVER the thing an agent goes and READS to coordinate. If agents poll disk
every turn to learn current state, the log became the bus — invert it: serve
coordination from memory, demote disk to write-behind. Prior art: write-behind
cache, event-sourcing's in-memory read model, LMAX Disruptor, "the database is
not your message queue." Bug class: `disk-as-coordination-read-path` /
`log-as-bus`. (The inversion the first shared-memory room fell into — hot
state was correct in RAM but agents still read disk rows every turn; the keel
of the RAM-first re-architecture. 2026-06-21.)

### #23 — attention budget

Owner canon 2026-07-08: built into planning, then forgotten; re-learned by
incident. Every agent has a finite attention budget; any feature that injects,
delivers, alerts, or drains MUST be designed against all three legs at once:
(a) TIMING — what they need arrives WHEN they need it (contextual firing,
escalation on staleness); (b) RELEVANCE — NOTHING they don't need (audience by
capability-to-act: leadership-only cred state, level-state collapses to
latest, budgets/caps on every injection surface — per-toolcall, per-turn,
drain, alert); (c) AVAILABILITY — everything they MIGHT need stays REACHABLE
on demand (withholding never removes access: digests carry reach-deeper
pointers, the durable log stays queryable). Violating one leg to serve another
is the bug: firehose serves (c) by destroying (b); over-withholding serves (b)
by destroying (c). Design/review question for ANY a2a change: "whose attention
does this spend, on what, and could they have pulled it instead?" Unifies:
whisper budgets, contextual-firing canon, alert audience routing, drain
digests, verdict embargo. Bug class: `attention-budget-unmanaged`.

### #24 — human-surface parity

Owner canon 2026-07-08: an entire class — shared-memory room viewers, TUI
chat, loop-source editing, the rich away system — fell out of ALL tracking
because agent-facing primitives get rows and owner-facing surfaces don't; no
agent feels their absence. CLI parity is the floor, never the finish. Track
owner-surfaces as first-class rows on the integration board. Bug class:
`human-surface-never-rowed`.

### #25 — existence sweep

Owner canon 2026-07-23: "you don't tend to notice when they've already mostly
built something that nevertheless needs improving / implementing elsewhere /
refactoring / moving". #18 guards the premise you build ON; this guards the
thing you are about to CREATE. Why a NEW reflex when the values already say
reuse>invent (#4): every VERIFICATION reflex (#18 premise, verify-before-done,
#15 trace-pipeline) fires AFTER the decision, to check that what you are doing
is true; NONE asks "should I be building this at all, or extending what's
there?" — so this class needs a trigger UPSTREAM of the decision, not another
downstream check. Live: speced a from-scratch roster tab; the owner had to
point out the ledger already rendered SEAT-ACTIVITY + FLEET-TODOS off the same
data — every reflex ran AFTER the decision to build. The fix dogfooded itself:
it EXTENDS this skill instead of inventing a new one. Bug classes:
`built-new-when-extend-existed` / `prior-art-unswept`.

### #26 — never first contact

Owner canon 2026-08-07, verbatim: "its never first contact unless I say it
is" — the LONG-RUNNING-SYSTEM sharpening of #25 and #18. Age flips the burden:
in a system with months of deliberate process-building, any apparent
first-encounter — a stray list, an unexplained provenance stamp, an unowned
surface, a hole that reads nobody-thought-of-this — is almost certainly the
RESIDUE of a prior deliberate process. Live 2026-08-07: rows stamped
source:task-corpus-migration were audited without chasing the stamp; a
one-time drain was re-done by hand while the owner's original ask was a
standing loop, and the prior process's exact skip (in_progress items) was
rediscovered rather than looked up.

### #27 — law above the instance

Owner-ratified phrase, 2026-08-07: "law above the instance is a really good
way to put that. please make sure that it becomes Canon" — the CAPTURE
discipline that governs every other capture. This is fix-propagates-to-class
applied to LEARNING itself: #26 exists because one stranded-tasks incident was
pushed up to its law.

### #28 — simulate the hint

Owner, 2026-08-22, after the Orca fork/upstream whiff: "it's weird how easily
you realize what the optimal strategy should be once I mention even a hint of
it, but until I do you seem completely unable to put the obvious pieces
together ... I am not always around to notice when you need a little hint, so
you really need to get good at looking for them yourself". MEASURED MECHANISM
of that whiff: every fact the strategy needed was already in context (PR
numbers in the 9000s, the fork forty versions behind the running binary, two
owner-installed updates that day, and the gate premise itself injected eight
hours earlier) — what was missing was not a fact but the QUESTION, because
retrieval is keyed by the question being held, and the question held was "how
do I fix this" rather than "who keeps this aligned next month". A hint works
by supplying the frame; once it exists every fact snaps to it. Composes with
#25 (existence sweep asks "should I build this at all"); this asks "should I
build it HERE, and who pays to keep it working".

### #29 — price every agent-loop change

Owner, 2026-10-01: the fleet's Max usage went mostly to cached re-reads on
wakes that needed no act. One model request re-sends the whole context since
compaction (a cache hit, 0.05x on Opus 5.5, plus the new tail and output).
Requests come from turn openers (owner messages and WAKES), tool-result rounds
(independent calls batch into one), and stop-hook blocks (pure overhead).
Measure before and after, per seat-hour and by request source. Store:
agent-loop-cost-unit-is-requests-times-context-with-zero-fidelity-regression.

### #30 — routing is not progress

Owner, 2026-10-01: a P0 sat 2.5 h behind one seat whose own guards blocked its
writer, and another that hit its 5-hour wall 30 min after dispatch. The finder
need not build it, but the finder owns that it gets built. Store:
owner-p0-is-built-now-by-the-finder-dispatch-is-not-progress.

## Feasibility-evidence block (long form)

```
Feasibility-evidence:
  probe: <command or source/doc read>
  exit: <code or n/a> - <one-line result>
  precedent: <file:line / URL / NONE FOUND>
  refuter: <family + verdict, if required>
```
