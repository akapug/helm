---
name: dogfood
description: >
  Close the build loop by using what you just changed on real helm work and folding
  the friction back immediately. Use after landing a verb, rule, skill, hook,
  guard, script, or user-facing workflow; when the user asks whether it was
  actually used; and during autonomous runs after each shipped slice.
license: MIT
metadata:
  author: helm
  version: "1.0.0"
---

# /dogfood - prove the change on real work

Tests show the implementation did what the author expected. Dogfood shows the
expectation matched reality. A slice is not proven until a real task has leaned on
it and the result is recorded.

## Loop

1. **Use it on the next real task.** Prefer the new canonical path immediately:
   new command, new hook, new skill, new guard, or new workflow. If no natural use
   appears, create the smallest real use that exercises the actual surface.
2. **Watch three friction classes.**
   - **bit**: it misbehaved on real input. Fix now and add a guard/test.
   - **fought**: it worked but pushed you toward a workaround. Improve the
     ergonomics or file a task with evidence.
   - **surprised**: it revealed a premise. Capture it in the right layer.
3. **Fold back immediately.** Bugs become fixes plus regression guards. Ergonomic
   issues become tasks or refinements. Durable lessons go through `/learn`.
4. **Record the verdict.** Put a compact dogfood receipt where the work is being
   coordinated: task, commit note, PRD, or `helm chat post` in the relevant room.

## Receipt Shape

```text
DOGFOOD <capability>: <clean|bit|fought|surprised>
real task: <what used it>
evidence: <command/output/file:line/chat msg id>
follow-up: <none|fix sha|task id|learned artifact>
deploy tier: <source-only|next-session|hook-live|rebuild|daemon-restart|launch-loaded>
```

If another agent must act on the follow-up, send it directly:

```bash
helm chat post '<dogfood verdict + required action>' --dm <owner-seat>
```

Otherwise post it durably without waking anyone.

## Standing Posture

Helm agents are user zero for helm. Once a canonical path lands, use it
instead of the old habit. If you avoid the new path during real work, treat that
as a fought-me signal and improve it or record the task.

## Success Criteria

- The changed capability was exercised on a real task, not only a synthetic demo.
- Evidence proves the intended surface was used.
- Bit/fought/surprised friction was fixed or routed the same pass.
- The dogfood verdict is recorded with deploy friction.

## Cross-Refs

- `/build` for the full loop.
- `/learn` for durable lessons.
- `/x` for correctness review; dogfood covers real-use fitness.
