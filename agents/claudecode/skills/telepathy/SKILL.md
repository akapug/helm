---
name: telepathy
description: >
  Use when you need a fast bounded answer from the peer who already knows it and
  you can keep working while the reply arrives. Telepathy is async ask/reply over
  MC comms with a correlation id, typed endpoints, and no blocking. Escalate to
  mindmeld when the answer needs back-and-forth.
license: MIT
metadata:
  author: mc
  version: "1.0.0"
---

# /telepathy - async ask the knower

Telepathy is the quick two-way channel: one dense ask, one dense reply, no
blocking. It is useful when asking the cell that already knows will beat
re-deriving the fact. It is not for durable verdicts or broad deliberation.

## Use It When

- The answer is bounded and the peer is the cheapest source.
- You can continue useful work while waiting.
- The result does not need a full durable review message.

Do not use it when the work needs live back-and-forth; use `/mindmeld`. Do not
use it for a decision/verdict that must be reconstructable; send a normal MC
message.

## Ask

1. Generate a correlation id, for example
   `tp-<your-label>-<epoch>-<short-rand>`.
2. Write a task/list stub or note so the ask cannot be forgotten.
3. Send one bounded action-required message:

```bash
mc comms send --from pane:<you> --to pane:<peer> --conv telepathy \
  --priority action-required \
  --payload '[TP-ASK <cid> reply_to=pane:<you>] <specific bounded question>'
```

4. Keep working. Do not spin waiting for the reply.

## Reply

If you receive `[TP-ASK <cid>]`, answer with the payload, not a context dump:

```bash
mc comms send --from pane:<you> --to pane:<asker> --conv telepathy \
  --priority action-required \
  --payload '[TP-REPLY <cid>] <answer plus pointer if needed>'
```

Then return to your slice.

## Discipline

- Ask the specific thing you cannot cheaply derive.
- Keep asks short enough to answer in one reply.
- Include the resolved typed reply endpoint.
- Close your local waiting stub when a matching `TP-REPLY` arrives on any conv.
- If the reply is long, contested, or needs a second round, convene a mindmeld or
  move the issue to a normal durable conv.

## Cross-Family Use

For a quick refute on one bounded claim, telepathy can carry a pointer to the
claim. The final PASS/NOT_PASS or design decision still lands on the durable
review conv.

## Success Criteria

- The ask has a unique cid and typed reply endpoint.
- The asker keeps making progress instead of blocking.
- The reply is bounded and closes the waiting stub.
- Durable decisions are recorded outside the ephemeral ask/reply.

## Cross-Refs

- `/mindmeld` for live convergence.
- `/x` for durable cross-family review.
- `/debug-self` when an ask appears stranded.
