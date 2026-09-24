# Cross-family self-evaluation councils — SOP

## What this is

A **council** is a periodic, cross-family, evidence-anchored rating of a system
*by the team that builds it* — producing an owned, ranked gap register, and
CLOSED by a re-evaluation on the **identical prompt** that measures whether the
scores actually moved.

The re-evaluation is what makes this an **eval** — it measures a delta — and not
a retrospective, which only collects opinions. A council with no re-council is
half the instrument: it names gaps but never proves any of them closed.

The whole design leans on one bet: a fleet of coding agents drawn from
*different model families* will see different failures in the same system. Point
those distinct basins at your own work, anchor every score to the ledger, and
you get an honest read on how good the thing actually is — from the only
reviewers who use it every day.

## Tiers — match the weight to the stakes

A council is the heavyweight instrument. Spending one on a small question is
ceremony; using a lighter tier for a release is under-powered. Three convergence
weights, cheapest first:

- **Quick fork** (genus): two agents, one deciding question, hot context,
  minutes. The default for "which of these two fixes" or "is this premise still
  live." No sealed judgment — just a fast second basin on a bounded call.
- **Standup** (informal 2+): a handful of agents aligning on a plan or a design
  shape along the way. Convergent, but still no sealed judgment — it steers the
  next slice, it does not rate the system.
- **Council** (formal, quorate, sealed judgment): the rating instrument this SOP
  describes. Expensive — it pulls a full panel for one pass. Reserve it. What
  makes a panel *quorate* is defined below.

## When a council is required

- **REQUIRED** at every named or numbered release. A release takes a
  whole-system council as its gate: you do not ship a version without the fleet
  rating it and naming what "done" missed.
- **A milestone or large-arc "are we actually there?"** — council.
- **"How good is X / what's still broken?"** — council, or a subsystem council
  (below) if X is a single surface.
- **The steps along the way to a release are standups and quick forks, not
  councils.** An intermediate slice converges with a standup; only the release
  itself convenes the council.
- **Not a council:** a single lane (that is a gate); a quick check (that is a
  probe).

## Subsystem councils — keep the parts sharp between releases

A council need not be whole-system. Scope it to **one subsystem** for a targeted
tune-up, run between release councils — e.g. a council on just the stop/idle
hooks, just the wake-and-monitor layer, just the dispatch ledger, or just the
signing path.

A subsystem council uses the **same open-ended shape** ("rate this subsystem on
the scale and name its biggest frictions / tweaks") bounded to that one surface,
and its members are the seats that actually **use** that subsystem day to day —
they hold the live friction data a whole-system panel would average away. Same
quorum, same six-step loop, same re-council-to-measure discipline, smaller blast
radius.

The whole-system council is the release gate; subsystem councils are how the
parts stay sharp in between. They ride alongside release and gap work, never
displacing it — a subsystem council is a tune-up, not the mainline.

## Quorum — what makes a panel a council

A council's quorum is **4–5 members, each from a distinct model family**. The
distinctness is the whole point: the instrument's honesty comes from different
basins seeing different failures, so two seats from the same family count as one
basin, not two. The **floor is 3 distinct families** — below that the
cross-family mechanism is gone and what you have is a standup, not a council.

Substitution, when a member drops mid-pass or is unavailable:

- **Replace within the same family first.** The goal is to preserve the *set of
  basins*, not the specific seat — a different agent from the same family holds
  roughly the same failure modes.
- **If no same-family substitute exists, hold at the floor** (3 distinct
  families) rather than adding a second seat from an already-represented family.
  A doubled family buys no new basin and biases the aggregate toward it.
- **Never drop below 3 distinct families and still call it a council.** Downgrade
  the result to a standup and label it as one; a two-family panel that reports a
  council-grade number is the failure this rule exists to prevent.

Quorum matters twice: once when convening — a council that quietly ran with two
families was never a council — and again at re-evaluation, where the panel must
stay *comparable* for the delta to mean anything (see the re-eval step and the
panel-drift guard).

## The rating — scale, anchors, aggregation, and evidence cutoff

The score is a number on a **1–10 scale**, rating the system against what it
*claims to do*, as of a stated evidence cutoff. Anchor the number to what the
ledger shows, not to aspiration or effort:

- **≤5 — not yet real.** Core function is missing, unreliable, or proven only in
  a demo. The team cannot depend on it; claimed capabilities fail off the happy
  path.
- **6 — works, with known rough edges.** The core function is real and used, but
  reliability or coverage is partial: several claimed capabilities are proven
  only on the happy path, and the team routes around specific frictions daily.
  Evidence exists but is thin — a few gates run, a handful of real lands.
- **7 — solid core, uneven edges.** The mainline is dependable; newer or
  peripheral layers lag and carry most of the open gaps.
- **8 — relied on, gaps bounded.** The core is proven across sustained real use
  with a citable ledger — gates run, defects caught, lands audited, features
  dogfooded. Remaining gaps are known, bounded, and sit on newer layers or
  edges, not in the mainline.
- **9 — trusted as infrastructure.** Failures are rare and recoverable, and the
  ledger is deep and consistent. The gaps that remain are *deliberate* —
  judgment calls left to a human on purpose — rather than unbuilt capability.
- **10 — reserved.** Effectively no open gaps at the rated scope. Rarely honest
  to claim; a 10 sitting on top of a non-empty gap register is a tell that the
  panel converged too easily, not a result.

**Every member returns three things, not just a number:** the rating, the
**top-3 remaining gaps** (ranked), and the **ledger evidence** the rating rests
on. A bare number is not a council reply — the gaps are half the instrument, and
the evidence is what keeps the number from being a vibe.

**Aggregate by median, not mean.** The council's headline number is the *median*
of the member ratings. One outlier basin — a family that is unusually harsh or
unusually kind — should not swing the read; the median is the number that
survives cross-family disagreement.

**Split by layer when the system has distinct layers.** A single number hides
the shape. When members rate the parts separately — say a solid coordination
substrate carrying a thinner layer on top — aggregate *per layer* and report the
per-layer medians. A blended average is exactly what the split-the-score honesty
guard exists to prevent. When members split the rating **unprompted**, that
convergence is the signal, not noise.

**Fix the evidence cutoff before convening.** The panel rates against a named
ledger state — which gates have run, which defects were caught, which lands were
audited, what is dogfooded *as of now*. Members rate against that cited cutoff,
not against work still in flight and not against a general sense of momentum.
Record the cutoff with the register. The re-evaluation rates against a *later*
cutoff, so the delta means precisely "between these two ledger states" — the
only delta that is real.

## The loop — all six steps, or it is not the SOP

1. **Convene to quorum.** Meet quorum: 4–5 members from **distinct model
   families**, floor of 3 (see Quorum). The whole value is different basins
   seeing different failures — a same-family panel just runs one check N times
   and calls the agreement a result.

2. **Prompt open-ended, never leading.** "Rate X on the 1–10 scale and name the
   top-3 remaining gaps." Do **not** supply the answer or a checklist — a leading
   prompt manufactures agreement. Ask for the number, the top-3 gaps ranked, and
   the ledger evidence each rests on — nothing narrower.

3. **Converge fast, hot context, one pass.** Members reply from what they
   already know, marked done. A fork that needs fresh research is not a council
   reply — it closes with an async continuation and does not hold the pass. No
   three-round spiral: the value is the first honest read across families, not a
   negotiated consensus.

4. **Synthesize into an executable ranked register.** Turn the replies into a
   **ranked gap register** — first-class rows, each with an owner and a gate —
   not siloed prose. Output that cannot be executed is a failed council. Keep
   the full register, and the evidence cutoff it was rated against, as the
   durable record.

5. **Drive every gap to live and dogfooded.** A fix nobody runs is dead
   scaffolding — a gap is not closed until both the change and the thing that
   exercises it are wired and used on real work. Fixes route to seats or
   automated workflows, get cross-family reviewed, and land. Track burn-down on
   the register.

6. **Re-evaluate on the identical prompt AND a comparable panel.** Re-run the
   same words against the same members where possible — at minimum the same
   family composition, with a majority carried over. Compare the new **median**
   to the baseline. If the targeted layer's number moved, the fixes worked; if
   it did not, the register named the wrong gaps — which is itself the next
   council's finding. A changed panel does not produce a delta you can trust:
   see the panel-drift guard.

## Honesty guards — what keeps it from being self-congratulation

The fleet is rating **itself**. Cross-family diversity plus evidence-anchoring is
the only thing keeping that honest. These guards are load-bearing, not polish:

- **Rate against evidence, not effort or vibes.** Cite the ledger — gates run,
  defects caught, lands audited, features dogfooded — not "we worked hard."
  Effort is not a score, and the number is only as trustworthy as the cutoff it
  was rated against.

- **Report the split, not the average.** If the system has distinct layers,
  publish the per-layer medians. A single blended number buries the finding that
  matters most — that one layer is carrying another.

- **Cross-family diversity is the honesty mechanism.** If members converge too
  easily, or every reply is praise, suspect same-basin membership or a leading
  prompt — and re-check both before you trust the number. A panel that quietly
  lost a family is a panel that lost the mechanism.

- **Identical prompt AND comparable panel on the re-eval, or the delta is
  meaningless.** A drifted prompt measures a different question; a changed panel
  measures a different set of basins. Either one lets ordinary variance read as
  progress — a higher number from a new panel is *panel drift, not improvement*.
  Same words, comparable members; if you cannot hold both, report the re-eval as
  **unmeasured** rather than as a gain.

- **"Stays conventional, correctly" is a valid finding.** Not everything should
  be mechanized — some gates are judgment calls that belong to a human, and a
  register that leaves them manual **on purpose** is right to. Name why the gap
  stays open rather than pretending it is closed.

## A living practice

This SOP is a working discipline, not settled doctrine — tune it as the shape
reveals itself, and keep the reasoning for each change with the change. Two
directions worth holding onto: tie the council trigger precisely to release
state (a named or numbered release requires a council; the steps along the way
take the lighter tiers), and keep subsystem councils running between release
councils so no single surface drifts unmeasured.
