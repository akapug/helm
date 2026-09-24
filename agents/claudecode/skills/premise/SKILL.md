---
name: premise
description: >
  Capture owner knowledge into the helm typed store from chat — no CLI needed
  from the human. Use when the user says "/premise", "premise:", "capture this
  as a premise/prior/lexicon", "add to the store", "remember this as canon",
  or states a durable fact/decision/term they want the fleet to know. The
  human speaks (keyboard or voice, typos expected); the agent runs the helm
  CLI on their behalf.
license: MIT
metadata:
  author: helm
  version: "1.0.0"
---

# /premise — chat-surface capture into the helm store

The owner uses chat (PC or mobile, keyboard or voice). Agents and senior devs
use CLIs; the product provides the CLI *for the agent*. This skill is the
bridge: the human states knowledge in plain words, the agent types it into
`helm store` correctly.

## Procedure

1. **Normalize.** Reconstruct the intended statement from the user's words
   (voice-input typos are expected — fix them, never quote them back). Strip
   conversational wrapper; keep the load-bearing content and any date.
2. **Type it.** Choose the store type:
   - `premise` — a certain fact or ruling the human is stating (conf 1.0 —
     the human-only certainty lane; only use when THEY stated it).
   - `prior` — a belief/heuristic with confidence < 1.0 (default 0.6–0.9).
   - `lexicon` — a coined term + definition.
   - `reference` — a pointer to an external thing (URL, repo, person-fact).
   - `heuristic` — a conditional move ("when X, do Y").
3. **Search first.** `helm store resolve <keywords>` — if an existing entry
   covers it, prefer `helm store evidence` (move belief) or `supersede`
   over a duplicate.
4. **Capture.** One quoted argument, pipes inside:
   `helm store add premise "<id> | <statement> | <keywords> | <domain>" --source "owner <date>, via /premise"`
   (id = short kebab-case you mint; keywords = the words that should trigger
   JIT recall).
5. **Confirm back in ONE line** — id, type, confidence, and the statement as
   stored — so the human can catch a mis-capture immediately. If the
   attestation substrate is live, `helm premise` (the CLI verb) also signs
   it; report "attested" or "store-only" honestly.

## Rules

- The human's words are the source of truth for CONTENT; you own SPELLING,
  id-minting, typing, and keyword choice.
- Never mint certainty (1.0) from your own inference — only from their
  statement.
- Scope: `--project <p>` when the fact is project-bound; global otherwise.
- This skill is also the answer to "how do I add a premise from my phone" —
  the user just says it; any agent with this skill lands it.
