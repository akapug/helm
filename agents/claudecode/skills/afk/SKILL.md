---
name: afk
description: Use when the owner invokes /afk, declares they are away, asks for autonomous/AFK posture, or when away/auto posture is active and agents need the full operating contract behind the per-turn AFK nudge.
license: MIT
metadata:
  author: helm
  version: "1.2.0"
---

# /afk - Away Posture

## Purpose

AFK means the owner is not the live decision loop. Agents keep moving on
tractable work, coordinate loudly through helm surfaces, and keep user-facing
chat narration quiet unless a real blocker or human-only decision must surface.

The short per-turn nudge is intentionally tiny. This skill is the pull-depth
contract behind it.

## Helm Substrate

- `/afk` loads this skill — instructions for the CURRENT turn.
- Away posture IS mechanical, and it has one truth: the away flag
  (`helm/away.py`). The owner sets and lifts it from the helm web console's
  **away mode** card (his door: he does not type verbs); `helm away` /
  `helm back` write the same flag from a terminal, and `helm away status`
  reads it. Nothing infers it: no clock, no activity heuristic.
- The owner can also leave a **notice to every agent** on the same card
  (for example "Away: coordinate through a2a (helm chat / dispatch); do not
  narrate in local chat; save output tokens"). Only his web console writes
  or clears it (`helm/ownernotice.py`); a seat never does.
- **How you learn it:** at your next working turn, the per-turn inject leads
  with a `[helm posture]` line — once per change in your context, never
  every turn. Lifting shows a one-time `[helm posture] The owner is BACK`.
  Nothing is posted to chat and no seat is woken; a line that says AWAY was
  declared "not from an owner door" came from a terminal, so weigh it as
  that. An UNKNOWN line means the flag could not be read: do not assume he
  is here or away.
- The flag file is the only away state. Do not create a second sentinel, a
  reflex marker for away, or a posture chat row.
- `decision-spirit` is the primary guide while away. Use its fast path before
  every load-bearing call.
- Coordination uses `helm chat` and `helm dispatch` only. Do not broadcast
  pane text, and do not invent side-channel wake paths.

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

1. Take posture from the owner's declaration: the `[helm posture]` line from
   his away card, or his words ("/afk", "afk hard — A2A only", "autonomous
   overnight"). Restate the mode you heard in your ack. When he said it in
   words rather than on the card, and the run is long, record it durably in a
   `helm chat` row. `helm away status` reads the flag and his notice at any
   time; later turns and resumed seats recover posture from there.

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
   instead of degrading to monitor-ticks (the overnight-drift retro). Never
   downgrade an outcome into passive monitoring.

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
are present — their own message saying they are back, or the flag lifted (the
inject says `[helm posture] The owner is BACK` once; `helm away status` reads
"not marked away"). An incoming message by itself is not proof of return. His
notice can outlive the flag: it stands until he clears or replaces it.

After lifting, drop the AFK posture immediately: chat can become direct again,
but `helm chat` remains the durable path for team handoffs.
