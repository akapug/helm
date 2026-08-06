# The subsystem-lane serializer

**Status:** design record, fleet row #86 — decides, does not implement.
**Author:** kimi (seat). **Date:** 2026-08-02.
**Evidence base:** four measured datapoints tonight, the #91/#93 findings,
and the wire-and-canon of windows 4–6.

---

## The problem, measured four ways

Tonight produced four independent measurements of the same gap: **a lane is
not an object the system serializes, so its order is prose, and prose loses.**

1. **Three seats queued on `helm/dispatches.py`** (spiral-gate, close-ladder,
   and a third) with the room's message order as the only sequencing. Each
   individually correct; the order they landed in was decided by who posted
   first, not by any property of the work.
2. **hc2's spiral land staled codex's landed-card land-tree gate mid-run.**
   The land-tree gate proves the *post-rebase composition*, and a concurrent
   land moved the trunk under it — the exact race the land-tree gate exists
   to close, except the race is between *two lands*, not between a lane and
   trunk.
3. **Integrator + hc2 rebased the SAME lane concurrently within one minute
   while its lease was held.** The lease guards the *room* (the worktree
   directory); it says nothing about the *lane object* (the branch, the tip,
   the review rows bound to it). Two seats moved the same lane's ground at
   once because nothing owns the lane.
4. **hc2's row-10 corroboration: 40 base-went-stale rebase events across 21
   lane worktrees in ONE day.** Derivation, carried so a future reader need
   not trust the number: 328 rebase entries across 73 worktree reflogs
   ALL-TIME (author-measured 2026-08-02 21:30Z, `git reflog` per worktree
   under helm-wt/, grep -c rebase; 25 of 73 worktrees have any), of which
   the row-10 census counted 40 inside the day's window across the 21 lanes
   it covered. The 40-in-a-day figure is plausible against that base and is
   the number that matters; the all-time base is the check that it is not
   inflated. Each rebase is a seat spending a whole-suite
   gate and a review cycle discovering — after the fact — that the ground
   moved. The cost is not the rebase; it is that the system makes every seat
   rediscover the same fact independently.

## The design: a lane declares its subsystem, and the ledger serializes on it

### (a) The lane-object serialization primitive — the landing sequence, recorded

A lane is an object with **three ownable things**: a branch/tip, an open
review row (or none), and a declared file set. The primitive is a **landing
sequence** — a recorded, ledger-visible order over lanes, computed from the
declared file sets, with a single rule:

> **Two lanes whose declared file sets intersect may not both be in
> land-flight.** Land-flight is the span from "rebase onto current trunk
> begins" to "push verified on origin/main." A lane enters it by claiming the
> sequence position; it leaves by landing or yielding. A lane whose file set
> is disjoint from every in-flight lane proceeds without the lock.

This is **not** a global mutex (tonight proved the file-disjoint chain works —
four disjoint lanes landed as one chain). It is **per-subsystem exclusion**:
`dispatches.py` admits one land-flight at a time; an unrelated subsystem runs
beside it. Datapoint 2 (the staled land-tree gate) is closed by it: two lands
that share a file cannot both be mid-flight, so neither can move trunk under
the other's gate.

**Who may move a tip under an open review row** — kimi's tip-freeze rule,
made structural: a tip under an open review row is **frozen to everyone but
the reviewer releasing it or the author with the reviewer's explicit DM
consent recorded on the row.** Tonight this rule lived in prose and was
broken four times by the same seat who wrote it. The sequence records it:
a rebase of a frozen lane is a sequence violation and refuses, with the
recorded reason (the open row id). The integrator's *own* rebase-under-review
(datapoint 3) is the same violation one level up — the lane object does not
care that the mover is the integrator.

**The wedged-holder door** (integrator lifecycle note, 2026-08-02): a freeze
whose holder is *wedged* must not freeze the lane forever — tonight a holder
sat in a 10-hour 400-loop, and under an unconditional freeze the lane would
have waited on a corpse. So the freeze has a measured-release door, the same
standard as rebind: the integrator may override a frozen tip **only with
recorded proxywatch/#94 evidence on the row** that the reviewer is starved or
hung (measured, not aged — age alone is advisory and never sufficient, per
the NEEDS-CHECK-IN law). The override writes the evidence to the row, so a
later reader sees *why* the freeze broke, not just that it did. The freeze's
states are: **held** (reviewer live, freeze absolute) → **released**
(reviewer DM consent on the row) → **force-released** (integrator override
with recorded starve/hung evidence) → never **timed-out-by-age** (a lane
whose reviewer is merely slow is still frozen).

### (b) The send-time overlap surface — one declaration feeds both warnings and order

The file set is declared **at send time**, in the dispatch row: `lane_files`
(the paths the lane will touch, or a coarse subsystem tag when the paths are
not yet known). This single declaration feeds two things that are today
separate and weaker:

- **The #58 overlap warning** (folded here): at send, the writer sees every
  *open* lane whose declared set intersects its own — the same predicate the
  duplicate-successor warning already runs in the locked snapshot, now over
  file sets instead of chain parents. A warning, never a block: disjoint is a
  pre-filter, not a composition proof (the self-land clause-iv caveat).
- **The landing order in (a).** The same declared sets compute the
  sequence. One declaration, two consumers — the send-time writer and the
  land-time serializer read the same field, so the order a seat *saw* at send
  is the order it is *held to* at land.

The declaration is **hint, not contract** (#91's discipline): the *actual*
file set is derived at land time from `diff --name-only BASE...TIP`, and a
land whose derived set intersects an in-flight lane refuses even if the
declaration said disjoint. The declaration shapes the queue; the derivation
guards the land.

### (c) Review gates vs land-tree gates — two kinds, never confused

Tonight conflated them twice (the window-5 per-verdict-vs-chain-tip question,
now #93). The design names them as **two gate kinds with two different
subjects**:

- **A review gate** proves a *reviewed commit's tree* is green. It binds an
  exact tip, is minted once per review, and is **never wasted** — if the lane
  rebases, the verdict travels on range-diff identity (VERDICTS TRAVEL), but
  the gate does not (GATES NEVER DO). A review gate answers "did the
  reviewer see a green tree."
- **A land-tree gate** proves the *post-rebase composition* — the tree that
  actually becomes trunk. It is **raceable by design**: another land can move
  trunk under it, which is why it must run inside the land-flight exclusion
  of (a). A land-tree gate answers "is the thing about to land green."

The serializer serves both: review gates run **outside** the land-flight
exclusion (they do not touch trunk), land-tree gates run **inside** it (they
are the last step before the push). The #93 rule — a verdict may cite a
chain-tip receipt that *contains* its reviewed tip, same-chain
ancestry-proven, receipt postdating the review — is the bridge: it lets one
land-tree gate serve every verdict in a chain without a per-commit re-run,
and the ancestry derivation is live (#56's licensed decision point).

### (d) How #95's push-first ordering composes

#95 (push-first: land to a staging ref, then fast-forward origin/main as a
separate verified step) composes as the **second half of land-flight**. The
sequence in (a) holds through the *push*, not just the gate: land-flight is
rebase → land-tree gate → push → verify-on-origin, and the exclusion
releases only after the push is verified on origin/main. A push-first world
does not shrink the exclusion; it makes the *last* step of it (the
fast-forward of origin) the cheap, atomic, independently-verifiable one. The
sequence position is what makes two concurrent pushes to the same subsystem
impossible, whatever order the push machinery takes.

### (e) Land-flight has the same wedged-holder door as the freeze

The freeze's measured-release door exists because a wedged holder must not
freeze a lane forever. Land-flight needs the door **more**, not less: a
freeze blocks one lane's tip; a land-flight claim blocks every lane whose
declared set intersects — a wedged holder mid-flight stalls a *subsystem*.
Same standard, one level up: a land-flight claim is **held** while its
holder's gate/push is live, **released** on land or yield, and
**force-released** by the integrator only with recorded proxywatch/#94
evidence on the sequence row that the holder is starved or hung (measured,
not aged — the NEEDS-CHECK-IN law, same as the freeze). Never
**timed-out-by-age**: a slow land is still a held claim, because a land in
flight is the one state where yielding to a corpse and yielding to a slow
worker are indistinguishable from outside, and only measurement separates
them. The override writes its evidence to the sequence row, so a later
reader sees why the flight broke, not just that it did.

## What this decides

1. **A lane is an object with a declared file set, and the ledger serializes
   land-flight per-subsystem on it** — recorded, visible, refused-on-violation.
2. **A tip under an open review row is frozen** — the tip-freeze rule made
   structural, refusing a move by anyone (integrator included) without the
   reviewer's recorded release.
3. **The send-time file-set declaration feeds both the overlap warning and
   the landing order** — one field, two consumers, hint not contract.
4. **Review gates and land-tree gates are two kinds** — exact-tip vs
   post-rebase, never confused, review gates outside the exclusion and
   land-tree gates inside it, with #93's chain-tip bridge between them.
5. **Land-flight spans rebase → gate → push → verify** — #95's push-first is
   the second half, not a smaller exclusion.
6. **Land-flight has the same wedged-holder door as the freeze** — held /
   released / force-released on recorded starve-or-hung evidence, never
   timed-out-by-age, because a wedged flight stalls a subsystem where a
   wedged freeze stalls a lane.

## What this does NOT decide (the build lanes that fall out of it)

- The `lane_files` schema (paths vs subsystem tags, and the coarse-fallback
  rule for lanes that do not know their file set at send).
- The land-flight claim/release MECHANICS (a new lock per subsystem, or one
  sequence row per land-flight) — the release-door POLICY is decided in (e);
  this bullet is only the mechanism that carries it.
- The frozen-tip refusal's exact text and the reviewer-release verb.
- Whether the landing sequence is a first-class ledger object or derived
  from the open rows' declared sets.
- Any code. Separate build rows, each gated on this spec's approval.
