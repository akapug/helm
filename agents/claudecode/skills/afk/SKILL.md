---
name: afk
description: Use when the owner invokes /afk, declares they are away, asks for autonomous/AFK posture, or when away/auto posture is active and agents need the full operating contract behind the per-turn AFK nudge.
license: MIT
metadata:
  author: helm
  version: "1.1.0"
---

# /afk - Away Posture

## Purpose

AFK means the owner is not the live decision loop. Agents keep moving on
tractable work, coordinate loudly through helm surfaces, and keep user-facing
chat narration quiet unless a real blocker or human-only decision must surface.

The short per-turn nudge is intentionally tiny. This skill is the pull-depth
contract behind it.

## Helm Substrate

- `/afk soft|hard|off|status` is the away-state front door: the owner (or a
  trusted state change) declares the posture, and this skill is the contract
  behind it.
- Away posture, when wired mechanically, rides the reflex layer's marker-file
  signal (`helm reflex add <id> <steer> --signal marker --marker <path>` —
  a reflex fires while the flag path exists). One sentinel, one reader.
- `decision-spirit` is the primary guide while away. Use its fast path before
  every load-bearing call.
- Coordination uses `helm chat` only. Do not create a second away sentinel,
  do not broadcast pane text, and do not invent side-channel wake paths.

## Modes

- `soft`: owner away; keep autonomous progress visible through compact
  `helm chat` turns and needed durable records.
- `hard`: owner away and chat-quiet; coordinate through helm chat records. A
  real blocker or human-only decision is still signal and may surface as one
  compact row addressed to the owner.
- `auto`: long-running autonomous posture. Treat it as same-or-more autonomous
  than AFK: decide and execute tractable calls, batch durable updates, and
  widen escalation for milestones or blockers.
- `off` or `lift`: owner is present again. Lift only on an explicit return or
  explicit status change; an incoming message by itself is not proof of return.

## Activation Workflow

1. Declare or inspect posture through the front door:

```
/afk soft "owner away - autonomous run"
/afk hard "A2A only"
/afk status
```

2. Load `decision-spirit` and answer the fast path before each load-bearing
   call:

- How many layers, serialization hops, and trust boundaries does this add?
- What is the canonical identity, value, or event representation?
- Where is capability checked and where is failure attributed?
- What owner pipeline did I trace from entrypoint to source of truth?
- What cheapest probe says this shape works?

3. If the owner supplied a concrete outcome, LOCK it in NOW — reflexive, not
   optional. Post the outcome as a durable `helm chat` row and, when it should
   survive the session, a `helm store` entry, so later resumes know the north
   star. An armed outcome is what keeps an overnight AFK driving the OUTCOME
   instead of degrading to monitor-ticks (the overnight-drift retro — MC
   history). Never downgrade an outcome into passive monitoring.

## Away Operating Contract

- Decide and execute tractable technical calls. Ask only for public/shared
  pushes, destructive shared-state operations, sensitive credentials, brand or
  release names, irreversible trust calls, or genuine owner vision calls.
- Stay narration-free to user chat. Use `helm chat post` for progress, and an
  `@seat`-addressed post for handoffs that require a recipient turn — the
  mention is what wakes a seat.
- Keep siblings asleep unless a real turn-taking handoff is needed. Fan-out is
  intentional, never default.
- Before waiting on another agent, create an observable reply path with an
  @-addressed helm chat row, and arm `helm chat wait` when you must block on
  the reply.
- Check addressed rows with `helm chat read`, and act on them before
  stopping — a real read is what consumes the owner-unread marker and
  advances your cursor; the stop-guard blocks an idle stop on undelivered
  rows.
- For code-ready work, include the commit SHA. No SHA means not ready.
- Verify as the user where the surface is user-facing; tests alone are not an
  AFK completion claim.
- Keep the no-surprises contract: the owner returns to completed work, active
  work with durable state, or a clearly surfaced blocker. Do not leave half
  states hidden behind green local checks.

## Self-Audit

Before ending an AFK turn, check:

- Did I run the next real atom, or prove each remaining item is blocked?
- Did I use `helm chat` rather than chat narration or pane injection?
- Did every handoff that needs action carry an explicit @recipient?
- Did I verify the verifier saw non-empty, intended input?
- Did I avoid a second away-state mechanism?
- Did I leave a durable state breadcrumb for future resumes?
- Outcome-carrying AFK: is the outcome posted durably, not just noted?

## Lift Workflow

Lift only when the owner explicitly returns or a trusted state change says they
are present:

```
/afk off
# or
/afk lift
```

After lifting, drop the AFK posture immediately: chat can become direct again,
but `helm chat` remains the durable path for team handoffs.
