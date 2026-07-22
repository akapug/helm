---
name: manage-teams
description: >
  Coordinate many MC teams. Use when acting as Capcom, a Captain, or another
  meta-coordinator across leaders: produce a holistic rollup, start or
  redirect team work, form peer links between leaders, and escalate to the
  human only for true cross-team or owner-only calls. Aliases: manage,
  oversee, delegate, orch.
license: MIT
metadata:
  author: mc
  version: "1.0.0"
---

# /manage-teams - coordinate leaders

`/manage-teams` is `/build` at the many-team scope. The coordinator talks to
leaders; leaders talk to their teams. The output is either a rollup or a
directive.

## Role Naming

The buildr owner ruling recorded in `packs/foreman/ROLE-PAIRS.md`
(2026-06-09) ports into MC as naming canon, not as a new construction default:
the product surface uses clean coordination primitives, and skin labels stay
optional.

- The human owner is the Director: sets mission intent and makes owner-only
  calls.
- Capcom is the cross-project liaison to the Director.
- A Captain is the per-vessel crew lead and owns one team's `/lead` loop.
- `/manage-teams` is the many-team coordination verb. "Foreman" remains
  construction-skin terminology only; it is not MC's default title or a
  separate authority layer.

## Two Modes

- **Report.** Produce the cross-team view: leader, current slice, claims,
  status, blockers, next action, and human-only decisions.
- **Directive.** Start, stop, redirect, or rebalance work by sending a bounded
  action-required message to the responsible leader.

## Rollup Procedure

1. Read `mc cockpit get --ambient` for cells, claims, recent state, and pending
   work.
2. Poll the active coordination convs with `mc comms poll` and `mc comms
   pending` for action-required items.
3. Summarize by team, not by individual noise:
   - goal;
   - owner leader;
   - current committed sha or active claim;
   - status: green, moving, blocked, needs-review, needs-human;
   - next action and owner.
4. Surface only human-relevant gates. Everything else is dispatched or decided.

## Delegation Procedure

Send work to the leader, not to that team's workers:

```bash
mc comms send --from pane:<you> --to pane:<leader> --conv <lane> \
  --priority action-required \
  --payload '<goal, scope, constraints, expected receipt, review route>'
```

Then keep the return path explicit: the leader replies on the same conv with
`CODE_READY`, `BLOCKED`, `DONE`, or a specific ask. Verify by lifecycle and poll,
not by assuming a send was consumed.

## Cross-Team Peer Link

When a goal spans two or more teams, form a temporary leader-to-leader peer link:

- name the shared goal and owning seams;
- open or reuse one MC conv for the shared messages;
- each leader keeps driving their own team's slice;
- cross-team review uses independent eyes across the seam;
- dissolve the link when the shared goal is closed.

This is peer coordination, not a new supervisor layer.

## Councils

Use a council when independent leader judgment must stay separate. The convener
posts the mandate and a seed seam map; each leader posts a domain brief; leaders
cross-examine; the convener synthesizes a communique; each leader confirms or
refutes their section. Unconfirmed sections stay marked contested.

Use a mindmeld instead when the leaders need live co-design on one shared problem.

## Boundaries

MC instance cells coordinate work. If a team is blocked by external capacity,
message the blocker and route it to the owning surface instead of adding fleet
operations to this skill.

## Success Criteria

- The rollup is holistic, current, and leader-scoped.
- Every directive has a typed recipient, conv, output contract, and return path.
- Human escalation is limited to true owner-only calls.
- Cross-team seams have one owner and named consumers.

## Cross-Refs

- `/lead` for one-team operation.
- `/telepathy`, `/mindmeld`, and `/x` for the communication shape.
- `/dogfood` when management machinery itself changes.
