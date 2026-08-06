# Model-family failover — roles fall through model families, warmly

A fleet that runs several model families should treat any single family as
degradable, not indispensable. When the family carrying a role stops
producing good turns, the role falls through to the next family in its chain —
and it does so as a **wake**, not a cold launch. This is the standing operating
procedure for that fall-through.

The failover is deliberately *narrow*: it moves the affected role and nothing
else. An error streak is a trigger to investigate, not a verdict on the whole
family. Most of the SOP below is about keeping the response proportional — recover
the smallest thing that will work before you evict anything.

## The posture: warm standby

The economics of a fleet decide the posture. Three things are cheap: chat
transcripts on disk, saved-and-resumable sessions, and idle agent processes.
Two things are expensive: tokens spent inside a weekly or per-session window,
and human attention. A failover that rebuilds work from scratch, or that pulls
an operator into a manual scramble, spends the expensive resources to save the
cheap ones. That is backwards.

So the default posture is **warm standby**. Each critical role keeps a parked,
resumable session in at least one other family — briefed enough that promoting
it is a single wake. A failover is the wake of that standby, never a fresh
build. Idle standbys are cheap; their warm context is what makes the next
failover fast.

**Readiness is freshness, not existence.** A standby is not ready just because
its process is alive, its credential authenticates, and its proxy answers. Those
prove the standby *can* run; they say nothing about whether promoting it
resumes the role or restarts it. Readiness means the standby holds a
**current-enough checkpoint** of the role: the working context, the in-flight
lane, and the recent decisions, captured recently enough that one wake continues
the work instead of rebuilding it. Define this concretely per role — a staleness
bound (the standby's resumable session must reflect the role's state within the
last N minutes or the last checkpoint boundary) and a re-brief cadence that keeps
it inside that bound. A standby whose last checkpoint predates the role's current
work by too much is a cold start wearing a warm label; promoting it pays exactly
the token-and-latency cost the posture exists to avoid. Keep standbys fresh, or
the web is a diagram.

This posture is only real if the substrate can carry it. The preconditions are
below, and any hole in them outranks feature work — a warm-standby web that the
system cannot actually launch, resume, or authenticate is a diagram, not a
capability.

## Substrate preconditions

Three things must hold for every family in a role chain, not just the primary.
These are the load-bearing wall; treat a gap in any of them as a P0 lane.

- **Sessions.** The system can launch, resume, and address every family's
  session. "Resume" is the operative verb — a standby that can only be
  cold-started is not warm, and the failover pays token and latency cost the
  posture exists to avoid.
- **Credential homes.** Every family's credential home is system-managed and
  health-probed, and a family may hold **more than one** credential/account.
  A standby you cannot authenticate on demand is not a standby, and a family
  with a spare credential can often be rescued in place rather than evicted.
  The probe is what turns "we think that family works" into a measured fact
  before you need it.
- **Proxies from committed source.** Every non-primary family reaches the
  primary harness through a proxy adapter, and that proxy must be built from
  committed, reviewable source — not an offline patch or a hand-built binary.
  A fix that lives only in a compiled artifact is one rebuild away from silent
  regression, and you will discover the regression during the outage you built
  the standby for. The rule holds whatever the adapter is.

## Trigger states — measured, never vibes

A failover fires on a **measured** signal, not on a hunch that a family "feels
slow". A proxy health monitor supplies most of these states; one class it
cannot see yet is named honestly below. Note the **scope** column — it is the
whole point of the next section. A signal fires on one credential's traffic;
whether it means "this credential" or "this family" is something you establish,
not something the error code tells you.

| Signal | Source | Meaning | Default scope |
|---|---|---|---|
| proxy down / misconfigured | health-monitor tick | the family's proxy is dead or its config is bad | infrastructure — usually intra-family recoverable |
| refusal cluster (`log=blip|streak`; alert `BLIP|REFUSAL|STARVED`) | health-monitor tick | three trailing failures establish a cluster; only a cluster spanning at least 60 seconds **plus** `turn=starved` earns STARVED. Classify the evidence: 401/403 auth, 402 billing/quota-exhausted, 429 rate-limited, 5xx upstream/transient, mixed UNKNOWN | credential-local or UNKNOWN until independently corroborated; never family-wide from one cluster |
| hung turn | health-monitor tick | pane live but transcript stale past a threshold — a turn that never completes writes no row, so liveness looks fine while nothing lands | seat/pane — usually a wedged process |
| latency slow-drip | proxy latency column | a uniform per-request latency floor: every call takes minutes instead of seconds, for a sustained stretch | proxy or provider — corroborate before deciding |
| provider outage | status page / fleet room | the whole family is down at once, freezing every seat on it simultaneously | **family-wide by definition** |

**Detection gap, stated honestly.** The slow-drip class is the hardest to
catch, because a basic health probe still reports the family as *up* — requests
succeed, just slowly. Until a latency threshold is a first-class monitor state,
the trigger for slow-drip is an operator or a seat noticing minutes-per-turn
and checking the latency column directly. Do not pretend the monitor covers a
class it does not yet measure.

**Trust the monitor's structured fields; audit its sentence.** Transcript age,
pane, turn, probe, and log fields are the shared instrument and should not be
replaced with hand-rolled liveness probes. The prose attribution is a derived
claim, not an oracle: `probe=healthy` with `turn=ok` outweighs a short error
burst, 5xx means upstream rather than credentials, and mixed codes establish
only UNKNOWN cause. A refusal cluster and a starvation verdict are separate
facts; the alert may join them only when both are measured.

**Provider evidence does not establish deployment intent.** The incident that
motivated the log rung included Grok's measured two-day 402 wall, but the family
was deliberately parked by owner decision until CLI proxy cursor support worked;
it was not waiting on payment, and clearing billing would not authorize
re-entry. Report what the transport proves, then consult the fleet's declared
intent before proposing a remedy or restoring capacity.

## An error streak is not a family outage — recover intra-family first

The most common mistake is to read any refusal streak as "the family is down"
and evict the whole family from every role chain. That over-reacts. The
credential-facing classes — 401/403 auth, 402 billing/quota, and 429 rate limit —
are frequently scoped to **one credential, account, workspace, or model**: an
expired token, one account's exhausted quota, a per-key rate limit, or a
workspace policy block. A 5xx cluster instead establishes upstream failure, not
credential failure; mixed classes establish UNKNOWN cause. Same-family recovery
is still the proportional first response before any family-wide conclusion.

So before you declare a family-wide failure, do the cheap recovery in place:

- **Rotate the credential/account.** If the family holds a second credential
  home, repoint the seat's proxy at it and retry. An auth or quota refusal on
  one account routinely clears on another.
- **Restart or repoint the proxy.** A `proxy down / misconfigured` state is
  infrastructure, not a provider outage. Bounce the adapter or point it at a
  healthy instance before you touch the role chain.
- **Corroborate the scope with a second credential.** This is the deciding
  test: does an *independent* credential on the same family also fail the same
  way? If a second credential serves, the streak was per-credential — you have
  already fixed it, and no cross-family failover was ever warranted. If a
  second credential reproduces the failure, the scope is now **provably
  family-wide**, and only then do you escalate.

Two decisions fall out of this, and they must stay separate. **Failing the
affected role over** to its next family is warranted as soon as same-family
recovery for that role fails or is unavailable — a dead credential with no
independent same-family credential to fall back on. The role cannot serve, so it
moves *now*, even while the family-wide scope is still UNKNOWN; announce that
scope as credential-local or UNKNOWN, never as family-wide. **Excluding the
family from every chain** is the heavier action and takes the heavier proof: a
confirmed provider outage, or a refusal streak a second independent credential
reproduces. A single-credential refusal moves one role forward but does not evict
the family — it stays in every other chain until the scope is proven family-wide.
An UNKNOWN scope is never a reason to leave a role stuck: a role that cannot serve
fails over now, and the family is judged separately.

## Role chains

The chain is defined **per role, not per seat**. A role is a responsibility —
integrator, builder lane, gate reviewer, coordination beacon — and its chain
lists the families that can carry it, in preference order. When the family at
the head of a role's chain degrades, the role moves to the next entry; the
physical seat is incidental.

The chain is **deeper than two**. Two families is not a chain, it is a single
spare, and a single spare is worth nothing on the day two families degrade at
once — which does happen. Give each critical role at least a third fallback.

A worked example of the shape (family names are placeholders; substitute your
own roster):

| Role | Chain | Notes |
|---|---|---|
| Integrator | family A → standby → family C → family D | the standby lands mechanical, unambiguous work; judgment calls park for the surface owner rather than being auto-decided by a fallback |
| Builder lanes | family B → family C → family D → family E | the head of the chain is chosen for headroom, not merely availability |
| Gate reviewers | any live family ≠ the author's | independence is the existing rule; a re-gate is a fresh dispatch to the next live family, unchanged by failover |
| Coordination beacons | any live seat | peers relaunch each other's panes, so the beacon role survives on whatever is up |

A family is excluded from every chain only when it is **provably family-wide
bad** — a confirmed provider outage, or a refusal streak reproduced on a second,
independent credential (see the section above). A refusal seen on a single
credential is not a family exclusion; rotate the credential and keep the family
in its chains. Once a family *is* excluded, it re-enters only when the health
monitor shows it clean — and you never leave a provably-dead family at the head
of a chain "in case it comes back", because that just delays every turn that
hits it.

## Mechanics

A failover runs the same six steps every time. The first is a recovery attempt
that often ends the incident without any cross-family move at all; announce and
rebind matter as much as the promotion itself, because they keep the fleet's
ledger truthful.

1. **Detect.** A monitor state, or a measured slow-drip from the latency
   column. Never a vibe.
2. **Recover intra-family first, then decide the two actions separately.**
   Rotate the credential/account and restart or repoint the proxy; then test
   whether a second, independent credential on the same family also fails. If
   intra-family recovery restores the role, **stop here** — no failover, no
   eviction. If it does not — or there is no independent same-family credential
   left to try — **fail the affected role over now** (steps 3–4), with its scope
   announced as credential-local or UNKNOWN: a role that cannot serve does not
   wait on a family-wide verdict. **Family exclusion** (removing the family from
   every chain) is the separate, heavier call, and it proceeds only when the
   scope is corroborated family-wide.
3. **Announce.** Post to the fleet room, tagged as a failover: the role, the
   from-family, the to-family, one line of measured trigger evidence, and the
   scope actually established — credential-local, UNKNOWN, or family-wide (an
   UNKNOWN-scope role failover is announced as such, not dressed up as a family
   outage). The announcement is what lets other agents stop waiting on the
   degraded seat.
4. **Promote, resume-first.** Wake the parked standby for that role (`resume`
   on that family's session, launched under its credential-home profile).
   Because sessions are cheap and resumable — and the standby was kept fresh —
   this is a wake, not a rebuild. Kill-first reseed is reserved for a genuinely
   wedged pane; the default never rebuilds work that survives on disk.
5. **Rebind obligations, don't edit them.** Open work assigned to the degraded
   seat is **re-dispatched to the standby, not reassigned by editing the
   original record.** Verdicts and reviews bind to the dispatch, so editing a
   dispatch in place corrupts the ledger; issuing a fresh one keeps it
   truthful. In-flight lanes stay claimed, and the standby continues them in
   the same worktree.
6. **Recover upward, deterministically.** The role falls back **up** to the
   primary after the health monitor shows it healthy for **N consecutive
   healthy ticks** — pick N and hold to it (3 is a sound default); "it looks
   better now" is not a promotion criterion. The standby returns to **parked** —
   not killed — and is re-briefed to current state so it stays a warm, fresh
   standby. Idle is cheap, and its now-warmer context is the head start for the
   next failover.

## Why this is a web, not a script

The steps above describe one role. Run them across every critical role and the
fleet becomes self-healing: a family degrades, the affected roles fall through
to warm standbys, the work continues, and when the family recovers the roles
climb back and the standbys return to rest. No single family is a single point
of failure, because no role depends on one — and because a per-credential hiccup
is recovered in place instead of amputating a whole family, the web stays
proportional as well as resilient.

That property is bought entirely by the substrate preconditions. Sessions you
can resume, credential homes you can probe and rotate, and proxies you can
rebuild from source are the wall the whole web leans on. Keep the wall standing
before you build anything that stands on it.
