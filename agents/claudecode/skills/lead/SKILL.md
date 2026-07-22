---
name: lead
description: >
  Coordinate one MC team. Use when you are the leader for sibling cells in one
  workspace: claim scope, dispatch disjoint slices, keep your own slice moving,
  route author-versus-reviewer, integrate commits, and report through MC comms.
  This is /build at team scope, headless and cockpit-native.
license: MIT
metadata:
  author: mc
  version: "1.0.0"
---

# /lead - coordinate one team

`/lead` applies `/build` one level up. A leader is still a builder: dispatch
siblings on orthogonal work, then keep advancing the next atom instead of only
monitoring them.

## Operating Loop

1. **Read the cockpit.** Start from `mc cockpit get --ambient`: cells, claims,
   pending work, and recent state.
2. **Shape the graph.** Decompose into nodes with scope, output contract,
   dependencies, shared resources, and review route. Parallelize only
   file-disjoint, dependency-disjoint, resource-disjoint nodes.
3. **Claim before touch.** Use `mc cockpit claim --sender <you> --sha <sha>
   <path>` for your own paths, and require siblings to claim theirs.
4. **Dispatch through MC.** Send bounded, action-required messages to the owning
   cell:

```bash
mc comms send --from pane:<you> --to pane:<worker> --conv <lane> \
  --priority action-required \
  --payload '<scope, output contract, dont-touch paths, verify gate, handoff target>'
```

5. **Keep building.** While siblings run, work your own non-overlapping slice.
   Waiting is justified only by a named external gate.
6. **Collect by lifecycle.** A send hash is not completion. Check
   `mc comms lifecycle`, `mc comms pending`, `mc comms poll`, and commits.
7. **Review and integrate.** A non-trivial slice gets different-family review.
   A reviewer may fix bounded findings in-pass; the leader still reviews the
   final diff before landing.
8. **Record outcome.** Post `PASS`, `NOT_PASS`, `CODE_READY`, `BLOCKED`, or
   `DONE` on the same conv with commit sha and verification evidence.

## Working V1 Rule

For a human-facing request, the first meaningful deliverable is a working v1 with
dogfood evidence and ranked recommendations, not a plan to approve. Ask only for
human-owned calls: public/shared push, brand or release name, destructive shared
state, sensitive secret, irreversible trust, or genuine product shape ambiguity.

## Task Discipline

Treat the task list as the leader's private trusted system:

- Capture out-of-scope distractions instead of chasing them.
- Burn ready work by leverage.
- Prune done, other-owner, and stale entries every turn.
- Keep a small active set; delegated work belongs to the owner, with only a
  waiting stub on your list.

## Routing Rules

- Talk to the owning cell or leader, not around them.
- Author and reviewer must be different model families for non-trivial changes.
- Same-family review is refinement, not the independent gate.
- Use the cheapest decisive probe before expensive build, bake, restart, or
  merge operations.
- Never duplicate another cell's claimed path; route through the current owner.

## MC Messages

Use durable MC messages for decisions, handoffs, verdicts, blockers, and
what-shipped evidence. Use ephemeral steering only for low-value nudges that do
not need reconstruction. If unsure, use `mc comms send`.

## Done

A leader slice is done when all owned nodes are either committed and verified,
handed off action-required with a sha and evidence, or blocked by a named gate
that cannot be advanced now. Known do-able work does not get parked without an
owner and a message.

## Cross-Refs

- `/build` for the base loop.
- `/manage-teams` for many-team coordination.
- `/x` for cross-family review.
- `/dogfood` for proving new team machinery on real work.
