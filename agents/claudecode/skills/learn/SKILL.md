---
name: learn
description: Capture a lesson durably in helm's typed store — premise/heuristic/lexicon with symptom-keyed retrieval, resolve-tested before it counts. Use on "let's /learn this", "from now on", "make this a rule", after any owner correction, or when an incident yields a rule that must fire before the next occurrence.
license: MIT
metadata:
  author: helm
  version: "2.2.0"
---

# /learn — durable capture into the helm store

A lesson is DURABLE when a future agent hits it **before** repeating the
mistake. That means: a typed store entry, symptom-vocabulary keywords, and a
resolve-test proving it fires. A lesson in prose, a chat message, or a memory
file is not captured — it is parked.

**This routes to helm's typed store** (`helm store`) — a lesson in prose, a
chat message, or a memory file is parked, not captured.

## The capture

```
helm store add premise "<kebab-id> | <STATEMENT> | <keywords,csv> | <domain>" \
  --source "<owner's VERBATIM words, or the incident ref>" \
  --rationale "<what happened, measured — not paraphrased>"
```

One pipe-delimited string, not separate args (a malformed capture swallows
the statement into the id). Types: `premise` (certain,
conf 1.0 — owner canon and proven facts), `prior` (belief + confidence),
`heuristic` (a move + trigger), `lexicon` (a term), `reference` (a pointer).

## The three laws of a capture that actually fires

1. **Verbatim source.** The owner's exact words go in `--source`. The
   statement is your compression of them — and compressions invert (measured
   3 inversions in 24h, 2026-07-22). Keep the original attached so an
   inversion stays catchable.
2. **Symptom keywords, not diagnosis keywords.** Write the words someone has
   BEFORE they know the answer ("pane died", "still waiting", "happened
   again"), not the words you have after solving it. Diagnosis-keyed
   premises sat unfindable through eight live hits of their own class.
3. **Resolve-test or it didn't happen.** After adding, run
   `helm store resolve "<a sentence someone would actually type>"` for 3+
   phrasings including one from the incident itself. A MISS means widen the
   keywords and retest. 0-for-4 on the first pass is the NORMAL result; a
   capture is not done at `stored:` — it is done at FIRES.

## Owner-canon echo

When the lesson comes from the owner's dictation, post its one-line
statement to the room the owner scans, tagged `owner-canon`, right after
capture. Silence changes nothing; an inverted capture gets caught the same
day instead of three incidents later.

## Resolve FIRST — update beats add (run BEFORE capturing)

Before `store add`, run `helm store resolve` with the lesson's own symptom
phrases (the same 3+ phrasings you will retest with). A HIT means the store
already holds this ground: UPDATE that entry (sharpen its statement, widen
its keywords, `--source` the new incident), `supersede` it if the new lesson
replaces it, or `confirm` it if the incident just re-proves it — never mint
a sibling id beside it. Duplicates are worse than misses: they split the
DF weight of shared keywords, so BOTH entries rank lower than either alone,
and future updates land on one while the other rots stale (owner-caught,
the gap live in an 845-entry store). Only a genuine 0-hit
resolve earns a new id.

## The am-I-being-stupid gate (run BEFORE capturing)

One question, every capture: **do we own the thing that misbehaved?** If the
misbehaving code is ours (helm, our scripts, our hooks), this is a FIX LANE,
not a rule — open it, and store at most a class pointer until the fix lands.
Rules are for truths we cannot change. A store entry teaching agents to route
around our own bug is self-bug-canonization (the --help incident:
a 3-line cli.py fix lived as a fleet-wide behavioral prior instead).

## What does NOT go in the store

Anything derivable from the repo (code structure, git history); anything only
this conversation cares about; and any lesson that should instead be a
**guard** — if it can be enforced deterministically (a refusing verb, a
census gate, a test), build the guard and store only the class.
