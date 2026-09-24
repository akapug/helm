---
name: grill-me
description: >
  Use before investing in a non-trivial, multi-step, or hard-to-reverse solution,
  or when the user invokes /grill-me. Surface the agent's load-bearing context
  assumptions as crisp confirm/refute questions. Ask only what cannot be
  discovered from code, git, docs, helm's live state (chat, claims, the
  roster), or another model's logic.
license: MIT
metadata:
  author: helm
  version: "1.0.0"
---

# /grill-me - surface context cruxes before building

The human may hold context the agent cannot derive from the repo or helm's
live state.
`/grill-me` extracts those cruxes before work is built on a false premise. It is
about information, not permission: decide and act on tractable substrate, but ask
for the facts only the human can know.

## When It Fires

- Before a non-trivial, multi-step, or hard-to-reverse plan.
- When a false assumption would change the implementation or coordination path.
- When the user invokes `/grill-me`.

Do not use it for discoverable facts. Find environment, path, version, wiring,
ownership, claim, or delivery facts through local files, git, `helm who`,
`helm work list`, `helm chat claims`, and `helm chat read`.

## Question Filter

Ask the question when all are true:

- The assumption is load-bearing.
- The human uniquely or most cheaply knows the answer.
- A wrong answer would change the plan.

Do not ask when:

- The fact is discoverable locally or from helm's live state.
- The issue is a logic/design premise a cross-family reviewer can refute.
- The detail is non-load-bearing; choose a conservative default and proceed.

## Loop

1. **Extract cruxes.** Name each assumption as a falsifiable claim and what
   changes if it is false.
2. **Ask crisply.** Use plain language, include the real consequence, and give a
   recommended default if useful.
3. **Fold immediately.** Update the plan, task, premise, or PRD before building.
4. **Build.** Return the clarified work to `/build` and continue.

## Output Shape

```text
/grill-me

I need to clear these load-bearing assumptions before building:
- <claim>. If false: <plan change>. Question: <confirm/refute?>
- <claim>. If false: <plan change>. Question: <confirm/refute?>

Discoverable facts checked:
- <file/git/chat/claim locator>

Recommended default if unanswered: <default and why>
```

Keep it short. The useful artifact is the answered crux, not a menu.

## Success Criteria

- Every human-held load-bearing crux was surfaced before implementation.
- No discoverable fact was asked.
- Refuted assumptions redirected the plan immediately.
- The resolved premise was captured in the active artifact or task.

## Cross-Refs

- `/refine` for broader convergence.
- `council-of-models` for logic or design refutation by other model families.
- `/build` for the clarified implementation loop.
