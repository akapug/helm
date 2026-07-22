---
name: debug-self
description: >
  Escape MC coordination stalls. Use when a pair or team is trading receipts with
  no forward progress, waiting on a message that may not have landed, both sides
  appear idle, or you are about to resend the same request. Alias: /unstick.
  Run four self-checks: forward progress, receipt truth, peer liveness, and
  waiting-on-nothing.
license: MIT
metadata:
  author: mc
  version: "1.0.0"
---

# /debug-self - escape coordination stalls

When a team stops building and spins on coordination, hold up the mirror before
sending another message. MC gives headless truth through `cockpit` and `comms`;
use those APIs, not TUI inspection or guesses.

## Tells

- The last several turns produced only status, ack, reread, or "waiting" text.
- You sent a request, got no reply, and are about to resend it.
- Both sides appear to think the other has the ball.
- Receipts are accumulating without an edit, probe, commit, or decision.

## Four Checks

Stop at the first check that fires.

1. **Forward progress.** Count real operations in your last few turns: edit,
   test, probe, commit, dispatch, or artifact. Mostly comms means you are idle
   behind coordination. Escape: advance your own slice now, then coordinate.
2. **Receipt truth.** A `comms_sent` hash proves the message was sent, not consumed.
   Check `mc comms lifecycle <record-hash>`, `mc comms pending <endpoint>`, or
   `mc comms poll --conv <lane> <endpoint>` for the real state. If delivery is
   unread, stranded, or addressed wrong, fix addressing instead of resending.
3. **Peer liveness.** Read `mc cockpit get --ambient` for presence, claims, and
   pending work. Working means do not resend. Idle with pending action-required
   work means route a concise re-task or pull the state yourself.
4. **Waiting-on-nothing.** State exactly what you are blocked on. If it is a
   reply that did not land, a peer with no active claim, or a fact you can read,
   you are not blocked. Pull the state, decide, and move.

## Escape

Ground truth the state -> make one forward operation on your slice -> coordinate
through the durable MC surface with the correct typed recipient -> escalate only
on a true blocker.

Use this shape for a needed re-task:

```bash
mc comms send --from pane:<you> --to pane:<owner> --conv <lane> \
  --priority action-required \
  --payload '<bounded ask, evidence, required next action>'
```

## Feedback

If the stall was caused by MC itself, send one compact improvement message on the
relevant conv: expected behavior, actual behavior, evidence locator, and proposed
fix. Do not hide substrate friction as a local workaround.

## Success Criteria

- You verified delivery/liveness through MC API state.
- You stopped resending blind messages.
- A real operation or a precise action-required re-task happened in the same
  pass.
- Any MC substrate friction was messaged with evidence.

## Cross-Refs

- `/x` xdiag when the stall is a technical diagnosis that is not converging.
- `/fix` when the stall traces to a reproducible MC bug.
- `/build` for returning to forward work.
