---
name: mindmeld
description: >
  Use when two or more cells must deliberately converge on one hard problem in
  real time: co-design, thorny diagnosis, or contested spec. Mindmeld is a
  bounded synchronous MC comms session with an invite, ready/go start, turn
  markers, and a clean archived outcome. Use telepathy for quick async asks.
license: MIT
metadata:
  author: mc
  version: "1.1.0"
---

# /mindmeld - bounded live convergence

Mindmeld is the synchronous channel. It is expensive because participants stop to
converge together, so it must earn its cost. If both cells can keep progressing
independently, use async MC messages or telepathy.

## When To Use

- A design or diagnosis needs real back-and-forth.
- A telepathy answer has become deliberation.
- Two leaders need to converge on one shared seam.
- A disagreement would cause divergent implementation if left async.

## Start Gate

Before inviting, answer:

- What exact problem are we converging?
- Why is async insufficient?
- Who must be present?
- What is the round or time cap?
- What artifact will hold the outcome?

If these are unclear, refine the problem first.

## Protocol (the spec)

1. **Open a room.** A conv such as `meld-<epoch>-<slug>`.
2. **Invite.** A bounded action-required message whose protocol suffix (room +
   join instruction) is never truncated; only the topic preview shrinks.
3. **Ready.** The joiner appends a durable `READY:<epoch>` AND wakes the
   convener in the same send (a READY that lands silently strands GO forever).
4. **Go.** The convener's first real chunk is the GO.
5. **Chunk.** Speak in bounded chunks, content + floor marker in the one text
   field:
   - `[YIELD]` hands the floor to the peer.
   - `[HOLD]` means more is coming from the same speaker.
   - `[DONE]` means this participant is leaving the meld.
   - `[ABORT]:<epoch>` kills the meld, fail-loud.
6. **Bound.** Stop at the exchange cap or a no-progress timeout. Fall back to
   async by posting the current state and required next action.
7. **Separate.** When all required participants are done, write the decision,
   open questions, and owners to the durable task, PRD, or normal conv.

## Executor (2-party melds — use this, do not hand-roll sends)

`scripts/mc-meld.sh` (mission-control repo) runs the protocol over the existing
comms verbs, with the bounds enforced as behavior:

```bash
scripts/mc-meld.sh invite <peer-pane> <topic...>       # room + seeded [HOLD] problem + AR invite
scripts/mc-meld.sh join <room> --peer <convener-pane>  # durable READY:<epoch> + wake-back
scripts/mc-meld.sh recv <room> [--timeout <s>]         # marker-aware blocking read (default 90s)
scripts/mc-meld.sh say  <room> --marker YIELD|HOLD|DONE|ABORT <text...>
```

`recv` returns only peer rows carrying a real floor marker (READY/GO control
echoes and your own rows are skipped fail-closed) and drops stale-epoch rows.
At 5 exchanges (`MC_MELD_MAX_EXCHANGES`) or on timeout it prints the
fall-to-async instruction and exits 3; an `[ABORT]:<epoch>` row exits 4, loud.
Session state lives at `${MC_STATE_DIR:-~/.config/herdr}/melds/<room>.json`.
For 3+ participants or nonstandard flows, follow the spec above with raw
`mc comms send --conv <room>` + `mc comms wait --conv <room>`.

## MC Mechanics

`recv` wraps `mc comms wait --conv <room> pane:<you>` (server-side blocking
read) and acks what it examines. Use typed endpoints for every wakeable turn.
Do not depend on a client view or local screen state.

## Success Criteria

- The meld had a bounded problem, participants, cap, and artifact.
- Every chunk carried visible text and a floor marker.
- The session ended with a durable outcome or an explicit async continuation.
- No participant stayed blocked past the cap.

## Cross-Refs

- `/telepathy` for the quick async ask.
- `/refine` for shaping the problem before the meld.
- `/x` when the meld is cross-family design or diagnosis.
