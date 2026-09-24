---
name: tlz
description: >
  Run the trusted-list-zero task discipline for helm seats. Use when your task list
  has bloated, completed items lack evidence, delegated work is mis-owned,
  waiting items need chasing, or the user asks to clean up tasks. Alias: gtd.
license: MIT
metadata:
  author: helm
  version: "1.0.0"
---

# /tlz - trusted list zero

The task list is the external trusted system for open loops. TLZ keeps it small,
owned, evidenced, and actionable. It is the agent adaptation of capture,
clarify, organize, reflect, engage.

## 1. Capture

Every open loop goes somewhere: task list, owner handoff, memory/reference, or
deleted. Do not keep obligations only in chat.

Success: no known open loop is uncaptured.

## 2. Clarify

Before a raw item rests on the list, decide:

- **One-turn action?** Do it now.
- **Delegated?** Send the new owner an action-required chat post and keep only
  a waiting stub.
- **Multi-step?** Make a project plus one next action.
- **Not actionable?** Move to reference, someday, or drop.
- **Human-only?** Mark the exact owner-only gate.

Success: no unclarified pending item remains.

## 3. Organize

Use simple buckets:

| list | meaning |
|---|---|
| active | small set you are driving now |
| next | ready, yours, not active yet |
| waiting | delegated or awaiting an external reply |
| gated | blocked by a named dependency |
| someday | valid but not now |
| reference | facts, not tasks |
| done | completed with evidence |

Owner rule: a task lives on exactly one owner's list. Delegated work belongs to
the owner; your list keeps only the return-path stub.

## 4. Reflect

Sweep for:

- unclarified captures;
- projects without a next action;
- stale active or next items;
- completed items without evidence;
- waiting items whose owner has gone quiet;
- work owned by someone else but still sitting on your list.

Each finding gets a re-clarify, chase, drop, or dispatch decision.

## 5. Engage

Pick the next action by leverage, not recency. Keep the active set small. Return
to parked items as active work clears.

## Handoff Shape

```bash
helm chat post "@<owner> <delegated task, evidence, expected reply shape>"
```

Track the waiting stub by recipient, room, row id, and expected result.

## Success Criteria

- Every task has an owner and one next state.
- Done items cite live evidence.
- Delegated work has a return path through chat.
- The active set is small enough to scan.

## Cross-Refs

- `fleet-maintenance` when a waiting stub's owner has gone quiet.
- `/build` for choosing the next real atom.
