---
name: refine
description: >
  Use early in a build, after a rough plan and before implementation, when the
  shape is non-trivial, fuzzy, or hard to reverse. /refine converges the idea
  through principle, amplification, and dialectic, then captures the resolved
  shape in the living artifact or a chat message before building.
license: MIT
metadata:
  author: helm
  version: "1.0.0"
---

# /refine - converge before building

Plan mode produces a plan. Refine checks whether it is the right shape before
code, config, or coordination makes it expensive. It is part of the `/build`
loop: Specify -> Plan -> Refine -> Implement -> Test -> Review -> Dogfood.

## Run These Frameworks Explicitly

- **Reflective equilibrium.** Hold the governing principle and the concrete
  choices against each other. Revise whichever is weaker until they cohere.
- **Iterated amplification.** Treat the input as a seed to improve, not a spec to
  transcribe. Surface implications and sharpen the target.
- **Dialectic.** Current shape -> counterpoint -> synthesis. Each round reduces
  ambiguity or adds a coherent layer.

## When To Use

- A design has open forks.
- A proposed implementation may be hard to reverse.
- The user has given a rough idea that needs a crisp v1 shape.
- A cross-family or teammate convergence would prevent building the wrong thing.

Skip it for purely mechanical work where the shape is already determined.

## Loop

1. **Extract the invariant.** Name the principle under the request.
2. **Amplify.** State one or two consequences the plan should satisfy.
3. **Resolve one fork.** Pick the highest-leverage open choice, recommend a
   direction, and settle it.
4. **Capture immediately.** Update the plan, PRD, task, or a chat message. Do not
   leave the convergence only in chat.
5. **Use plain names.** Let structure carry meaning. Avoid private shorthand.
6. **Detect convergence.** When the artifact is self-consistent and no live fork
   remains, stop refining and build.

## Coordination Form

When refinement involves another seat, use `helm chat` rather than TUI state:

```bash
helm chat post --room <lane> \
  "@<peer> REFINE request: subject, invariant, options, recommendation, open fork"
```

The reply should cite the request message or seq. Capture the resolved fork in the
artifact and, when useful, post a compact receipt in the same room.

Read `helm chat` before convening if ownership or claims matter.

## Success Criteria

- The highest-leverage ambiguity was resolved, not merely discussed.
- The living artifact or the chat message was updated with the convergence.
- The handoff to implementation is explicit: "converged; building now" plus the
  chosen shape.

## Cross-Refs

- `/build` for the full loop.
- `/grill-me` when a human-held context premise is the fork.
- `/x` or `decision-spirit` when the fork is technical composition risk.
