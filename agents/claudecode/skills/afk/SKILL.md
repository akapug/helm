---
name: afk
description: Use when the owner invokes /afk, declares they are away, asks for autonomous/AFK posture, or when MC away/auto posture is active and agents need the full operating contract behind the per-turn AFK nudge.
license: MIT
metadata:
  author: mc
  version: "1.0.0"
---

# /afk - MC Away Posture

## Purpose

AFK means the owner is not the live decision loop. Agents keep moving on
tractable work, coordinate loudly through MC surfaces, and keep user-facing chat
narration quiet unless a real blocker or human-only decision must surface.

The short per-turn nudge is intentionally tiny. This skill is the pull-depth
contract behind it.

## MC Substrate

- `mc afk soft|hard|off|status` is the only away-state front door. The `/afk`
  command wraps it and reads `mc cockpit get --ambient` before and after the
  state change.
- `agents/claudecode/hooks/lib/reflexes/afk.sh` detects away posture from
  `MC_AFK` or cockpit status and renders `rules/per-turn-stack.md` entries
  `auto` and `afk`.
- `decision-spirit` is the primary guide while away. Use its fast path before
  every load-bearing call.
- Coordination uses `mc comms` and `mc cockpit` only. Do not create a second
  away sentinel, do not broadcast pane text, and do not route via blackboard or
  awareness verbs.

## Modes

- `soft`: owner away; keep autonomous progress visible through compact cockpit
  turns and needed comms records.
- `hard`: owner away and chat-quiet; coordinate through MC records. A real
  blocker or human-only decision is still signal and may surface through the
  lead/captain lane.
- `auto`: long-running autonomous posture detected by the hook. Treat it as
  same-or-more autonomous than AFK: decide and execute tractable calls, batch
  durable updates, and widen escalation for milestones or blockers.
- `off` or `lift`: owner is present again. Lift only on an explicit return or
  explicit status change; an incoming message by itself is not proof of return.

## Activation Workflow

1. Set or inspect posture through the command front door:

```bash
/afk soft "owner away - autonomous run"
/afk hard "A2A only"
/afk status
```

2. Read the cockpit ambient state if you are acting manually:

```bash
mc cockpit get --ambient
```

3. Load `decision-spirit` and answer the fast path before each load-bearing
   call:

- How many layers, serialization hops, and trust boundaries does this add?
- What is the canonical identity, value, or event representation?
- Where is capability checked and where is failure attributed?
- What owner pipeline did I trace from entrypoint to source of truth?
- What cheapest probe says this shape works?

4. If the owner supplied a concrete outcome, LOCK it into an armed `/goal`
   NOW — reflexive, not optional. The pane-scoped goal sentinel (resolved by
   `hooks/lib/goal-sentinel.sh`; readers re-inject it every turn, TTL-guarded)
   is what keeps an overnight AFK driving the OUTCOME instead of degrading to
   monitor-ticks (the overnight-drift retro). Then write a compact active-spec
   or cockpit turn so later resumes know the north star. Never downgrade an
   outcome into passive monitoring.

## Away Operating Contract

- Decide and execute tractable technical calls. Ask only for public/shared
  pushes, destructive shared-state operations, sensitive credentials, brand or
  release names, irreversible trust calls, or genuine owner vision calls.
- Stay narration-free to user chat. Use `mc cockpit turn/status` for progress,
  and `mc comms send --priority action-required` for handoffs that require a
  recipient turn.
- Keep siblings asleep unless a real turn-taking handoff is needed. Fan-out is
  intentional, never default.
- Before waiting on another agent, create an observable reply path with an
  action-required MC message and verify the lifecycle when it matters.
- Check addressed inbox rows with `mc comms pending pane:<you>`, act on them,
  then `mc comms ack` only after the record is consumed.
- For code-ready work, include the commit SHA. No SHA means not ready.
- Verify as the user where the surface is user-facing; tests alone are not an
  AFK completion claim.
- Keep the no-surprises contract: the owner returns to completed work, active
  work with durable state, or a clearly surfaced blocker. Do not leave half
  states hidden behind green local checks.

## Self-Audit

Before ending an AFK turn, check:

- Did I run the next real atom, or prove each remaining item is blocked?
- Did I use MC comms/cockpit rather than chat narration or pane injection?
- Did every handoff that needs action carry a typed recipient and
  `action-required` priority?
- Did I verify the verifier saw non-empty, intended input?
- Did I avoid a second away-state mechanism?
- Did I leave a durable state breadcrumb for future resumes?
- Outcome-carrying AFK: is the `/goal` sentinel armed, not just noted?

## Lift Workflow

Lift only when the owner explicitly returns or a trusted state change says they
are present:

```bash
/afk off
# or
/afk lift
```

After lifting, drop the AFK posture immediately: chat can become direct again,
but MC comms remain the durable path for team handoffs.

## Port References

MC source of truth:

- `agents/claudecode/command-templates/afk.md`
- `agents/claudecode/hooks/lib/reflexes/afk.sh`
- `agents/claudecode/rules/per-turn-stack.md`
- `agents/claudecode/skills/decision-spirit/SKILL.md`

MC modifications: cockpit presence replaces buildr sentinels and overlays;
`mc comms` replaces blackboard wake/broadcast paths; the existing AFK hook reflex
is the shim, so no additional hook is needed for this slice.
