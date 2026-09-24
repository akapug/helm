---
name: learn
description: Capture a lesson durably in helm's typed store — premise/heuristic/lexicon with symptom-keyed retrieval, resolve-tested before it counts. Use on "let's /learn this", "from now on", "make this a rule", after any owner correction, or when an incident yields a rule that must fire before the next occurrence.
license: MIT
metadata:
  author: helm
  version: "2.3.0"
---

# /learn — durable capture into the helm store

A lesson is DURABLE when a future agent hits it **before** repeating the
mistake. That means: a typed store entry, symptom-vocabulary keywords, and a
resolve-test proving it fires. A lesson in prose, a chat message, or a memory
file is not captured — it is parked.

**This routes to HELM's store** (`helm store`), never to another project's
artifacts. That is a different project with its own physics; helm owns its own.
If you find yourself editing a different project's dirs or reaching for another
project's verbs from a helm seat, STOP — that is the cross-project bleed this
skill was rewritten to end (2026-07-22, the codex-orch scope-leak incident).

## The capture

```
helm store add premise "<kebab-id> | <STATEMENT> | <keywords,csv> | <domain>" \
  --source "<owner's VERBATIM words, or the incident ref>" \
  --rationale "<what happened, measured — not paraphrased>"
```

One pipe-delimited string, not separate args (a malformed capture swallows
the statement into the id — measured 2026-07-22). Types: `premise` (certain,
conf 1.0 — owner canon and proven facts), `prior` (belief + confidence),
`heuristic` (a move + trigger), `lexicon` (a term), `reference` (a pointer).

## The four laws of a capture that actually fires

1. **Verbatim source.** The owner's exact words go in `--source`. The
   statement is your compression of them — and compressions invert (measured
   3 inversions in 24h, 2026-07-22). Keep the original attached so an
   inversion stays catchable.
2. **Symptom keywords, not diagnosis keywords.** Write the words someone has
   BEFORE they know the answer ("pane died", "still waiting", "happened
   again"), not the words you have after solving it. Diagnosis-keyed
   premises sat unfindable through eight live hits of their own class.
3. **Resolve-test both ways, or it didn't happen.** After adding, run
   `helm store resolve "<a sentence someone would actually type>"` for 3+
   phrasings including one from the incident itself: each must HIT (or be
   re-routed under law 4). Then run
   2 must-MISS controls: an unrelated sentence that uses the entry's common
   words, and a routine notice or chat-wake line. Each must stay silent.
   0-for-4 on the first pass is the NORMAL result; a capture is not done at
   `stored:`. It is done when it FIRES where it should and stays quiet
   elsewhere.
4. **Few, short, rare keys; on a MISS, sharpen, don't pile on.**
   - First ask whether the moment is measurable (a tool call, a git or helm
     verb, a notice kind, a state reading, a work phase). If it is, the lesson
     needs a reflex (`helm reflex add <id> | <steer> --signal S` or
     `--pattern RE`), a hook, or a guard, and the store keeps only the class
     (see "What does NOT go in the store"). A heuristic's keyword list is not
     a trigger: it is keywords.
   - Otherwise write 3-6 probes, each a 2-3 word symptom phrase. Use a single
     word only when it is rare in real turns (a tool or product name).
   - On a MISS, swap a brittle probe for a shorter, rarer one, or add ONE
     probe for the missed phrasing, then rerun both tests.
   - If only common words would catch a phrasing, accept the miss: the
     lesson belongs to a reflex or the classifier lane, not to more keywords,
     which buy your recall with every other agent's precision. E2
     (2026-09-23) measured lone single-word matches at 61% of injections
     and only 8% relevant.
   - Read back `helm store get <id>` and drop any common single-word stem
     the store appended, with `helm store keywords <id> --remove <stem>`.
   - Full rule: heuristic `store-keywords-few-short-rare`.

## When the lesson is a conflict between two instructions

When two instruction sources GENUINELY conflict (a skill, a store entry, a
CLAUDE.md, a hook or reflex, the owner's words):

1. **Flag it first.** Name both sources, quote the clash in a line each, and
   say which you followed and why.
2. **Then reconcile.** Write one rule that keeps what each got right, and
   update BOTH sources in the same pass: the store entry directly, and the
   skill or doc through a lane in the repo that ships it. Resolve-test the
   result and tell the owner in one line what changed.
3. **Ask first only** when the reconciliation needs a vision call or would
   change the substance of owner canon; wording and scope are yours.

Not a conflict: a narrower rule overriding a broader default (that is the
design), a rule you merely find inconvenient, or one superseded by a newer
dated ruling. For those, follow the newest and note it in one line.

A flag without a reconciliation leaves the clash firing on every later turn
(premise `reconcile-conflicting-instructions-and-update-both`).

## Owner-canon echo

When the lesson comes from the owner's dictation, post its one-line
statement to the room the owner scans, tagged `owner-canon`, right after
capture. Silence changes nothing; an inverted capture gets caught the same
day instead of three incidents later.

## Resolve FIRST — update beats add (run BEFORE capturing)

Before `store add`, run `helm store resolve` with the lesson's own symptom
phrases (the same 3+ phrasings you will retest with). A HIT means the store
already holds this ground: UPDATE that entry (sharpen its statement and
its keywords, `--source` the new incident), `supersede` it if the new lesson
replaces it, or `confirm` it if the incident just re-proves it — never mint
a sibling id beside it. Duplicates are worse than misses: they split the
DF weight of shared keywords, so BOTH entries rank lower than either alone,
and future updates land on one while the other rots stale (owner-caught
2026-07-22, the gap live in an 845-entry store). Only a genuine 0-hit
resolve earns a new id.

## The am-I-being-stupid gate (run BEFORE capturing)

One question, every capture: **do we own the thing that misbehaved?** If the
misbehaving code is ours (helm, our scripts, our hooks), this is a FIX LANE,
not a rule — open it, and store at most a class pointer until the fix lands.
Rules are for truths we cannot change. A store entry teaching agents to route
around our own bug is self-bug-canonization (the --help incident, 2026-07-22:
a 3-line cli.py fix lived as a fleet-wide behavioral prior instead).

## What does NOT go in the store

Anything derivable from the repo (code structure, git history); anything only
this conversation cares about; and any lesson that should instead be a
**guard** — if it can be enforced deterministically (a refusing verb, a
census gate, a test), build the guard and store only the class.
