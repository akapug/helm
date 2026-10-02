---
name: afk
description: Use when the owner invokes /afk, declares they are away, asks for autonomous/AFK posture, or when away/auto posture is active and agents need the full operating contract behind the per-turn AFK nudge.
license: MIT
metadata:
  author: helm
  version: "1.2.0"
---

# /afk - Away Posture

AFK means the owner is not the live decision loop. Agents keep moving on
tractable work, coordinate loudly through helm surfaces, and keep user-facing
chat narration quiet unless a real blocker or human-only decision must
surface. The per-turn nudge is intentionally tiny; this skill is the
pull-depth contract behind it.

## Helm Substrate

- `/afk` loads this skill — instructions for the CURRENT turn.
- Away posture IS mechanical, with one truth: the away flag (`helm/away.py`).
  The owner sets and lifts it from the helm web console's away mode card (his
  door: he does not type verbs); `helm away` / `helm back` write the same flag
  from a terminal, `helm away status` reads it. Nothing infers it: no clock,
  no activity heuristic.
- The owner can leave a notice to every agent on the same card; only his web
  console writes or clears it (`helm/ownernotice.py`), a seat never does.
- **How you learn it:** the per-turn inject leads with a `[helm posture]` line
  once per change in your context; lifting shows a one-time
  `[helm posture] The owner is BACK`. Nothing is posted to chat and no seat is woken. A line
  that says AWAY was declared "not from an owner door" came from a terminal —
  weigh it as that. An UNKNOWN line means the flag could not be read: do not
  assume here or away.
- The flag file is the only away state. Do not create a second sentinel, a
  reflex marker, or a posture chat row.
- `decision-spirit` is the primary guide while away; use its fast path before
  every load-bearing call.
- Coordination uses `helm chat` and `helm dispatch` only — no pane text, no
  side-channel wake paths.

## Modes

- `soft`: owner away; keep autonomous progress visible through compact
  `helm chat` turns and durable records.
- `hard`: owner away and chat-quiet; coordinate through helm chat records. A
  real blocker or human-only decision is still signal and may surface as one
  compact row addressed to the owner.
- `auto`: long-running autonomous posture — same-or-more autonomous than AFK:
  decide and execute tractable calls, batch durable updates, widen escalation
  for milestones or blockers.
- `off` / `lift`: owner present again. Lift only on an explicit return or
  explicit status change; an incoming message by itself is not proof.

## Activation Workflow

1. Take posture from the owner's declaration: the `[helm posture]` line from
   his away card, or his words. Restate the mode in your ack. When he said it
   in words and the run is long, record it durably in a `helm chat` row.
   `helm away status` reads the flag and his notice at any time.
2. Load `decision-spirit` and answer the fast path before each load-bearing
   call (layers/hops/boundaries; canonical representation; capability check +
   failure attribution; pipeline traced to source of truth; cheapest probe).
3. If the owner supplied a concrete outcome, LOCK it in NOW — reflexive, not
   optional. Post it as a durable `helm chat` row and, when it should survive
   the session, a
   `helm store` entry. An armed outcome keeps an overnight AFK driving the
   OUTCOME instead of degrading to monitor-ticks. Never downgrade an outcome
   into passive monitoring.

## Away Operating Contract

- Decide and execute tractable technical calls. Ask only for: public/shared
  pushes, destructive shared-state ops, sensitive credentials, brand/release
  names, irreversible trust calls, genuine owner vision calls.
- Stay narration-free to user chat. Use `helm chat post` for progress, and an
  `@seat`-addressed post for handoffs that require a recipient turn — the
  mention is what wakes a seat.
- Keep siblings asleep unless a real turn-taking handoff is needed; fan-out is
  intentional, never default.
- Before waiting on another agent, create an observable reply path with an
  @-addressed helm chat row; arm `helm chat wait` when you must block on it.
- Check addressed rows with `helm chat read` and act before stopping — a real
  read consumes the owner-unread marker and advances your cursor; the
  stop-guard blocks an idle stop on undelivered rows.
- For code-ready work, include the commit SHA. No SHA means not ready.
- Verify as the user where the surface is user-facing; tests alone are not an
  AFK completion claim.
- Keep the no-surprises contract: the owner returns to completed work, active
  work with durable state, or a clearly surfaced blocker — no half states
  hidden behind green local checks.

## Self-Audit (before ending an AFK turn)

- Did I run the next real atom, or prove each remaining item is blocked?
- Did I use `helm chat` rather than chat narration or pane injection?
- Did every handoff that needs action carry an explicit @recipient?
- Did I verify the verifier saw non-empty, intended input?
- Did I avoid a second away-state mechanism?
- Did I leave a durable state breadcrumb for future resumes?
- Outcome-carrying AFK: is the outcome posted durably, not just noted?

## Lift Workflow

Lift only when the owner explicitly returns or a trusted state change says
they are present — their own message saying they are back, or the flag lifted
(the inject says `[helm posture] The owner is BACK` once; `helm away status`
reads "not marked away"). An incoming message by itself is not proof. His
notice can outlive the flag: it stands until he clears or replaces it. After
lifting, drop the AFK posture immediately: chat can become direct again, but
`helm chat` remains the durable path for team handoffs.
