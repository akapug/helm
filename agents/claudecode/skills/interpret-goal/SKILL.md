---
name: interpret-goal
description: >
  Turn a large objective into driveable, bounded MC sub-goals with verifiable
  exit conditions. Use when the user gives a high-level objective, asks the
  agent to set the sub-goals, or an autonomous run needs a concrete finish line.
  Each sub-goal includes an as-the-user or real-surface verification gate.
license: MIT
metadata:
  author: mc
  version: "1.0.0"
---

# /interpret-goal - derive bounded sub-goals

The human can set a large objective without writing every exit condition. Your
job is to ground the objective, decompose it into a small graph, and drive each
slice to a proof the human can evaluate from the surfaced evidence.

## When Not To Self-Interpret

Escalate a sharp question when the goal's product shape is genuinely ambiguous,
would change a wider contract, needs a public/shared push, needs a brand or
release name, touches sensitive secrets, or creates irreversible trust. Otherwise
decompose and proceed.

## Steps

1. **Ground the objective.** Read the source: user message, PRD, issue, task, or
   MC record. Quote the actual requested outcome.
2. **Decompose into 2-6 sub-goals.** Each gets:
   - one-line exit condition;
   - owner;
   - dependencies;
   - touched surfaces;
   - verification gate;
   - stop condition for true blockers.
3. **Make each exit surfaceable.** The final report must show evidence the
   reader can evaluate: command summary, test output, file/line, screenshot, API
   response, comms lifecycle, or commit sha.
4. **Attach real-surface verification.**
   - UI or web: drive the real page with browser automation and capture the
     assertion.
   - CLI or API: run the real command or endpoint with non-empty input.
   - Data flow: trace entry to owner to effect on real input.
   - Coordination: prove send, lifecycle, pending/poll, ack, and recipient state
     as appropriate.
5. **Drive one authoritative plan.** Do not stack independent loops. Update the
   sub-goal graph as evidence changes.
6. **Report per sub-goal.** State shipped, evidence, blocked, and remaining work.

## Output Shape

```text
INTERPRETED_GOAL: <objective>

Sub-goals:
- <id>: <exit condition>; owner=<cell>; deps=<ids>; verify=<gate>; stop=<blocker>

Execution order:
- <wave 1>
- <wave 2>

Human-only gates:
- <none or exact question>
```

## Success Criteria

- The objective is grounded in a cited source.
- Sub-goals are bounded, ordered, and independently verifiable.
- Verification exercises the real surface, not just compilation.
- Completion reports cite evidence for every sub-goal.

## Cross-Refs

- `/refine` when the objective shape is unclear.
- `/lead` for dispatching the sub-goal graph.
- `/x` for claim verification before declaring done.
- `/dogfood` for proving new user-facing or coordination behavior.
