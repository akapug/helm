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
mistake: a typed store entry, symptom-vocabulary keywords, and a resolve-test
proving it fires. A lesson in prose, chat, or a memory file is parked, not
captured. This routes to HELM's store (`helm store`), never to another
project's artifacts. REFERENCE.md holds the incident history; open it when a
law needs its evidence.

## The capture

```
helm store add premise "<kebab-id> | <STATEMENT> | <keywords,csv> | <domain>" \
  --source "<owner's VERBATIM words, or the incident ref>" \
  --rationale "<what happened, measured — not paraphrased>"
```

One pipe-delimited string, not separate args. Types: `premise` (certain, conf
1.0 — owner canon and proven facts), `prior` (belief + confidence),
`heuristic` (a move + trigger), `lexicon` (a term), `reference` (a pointer).

## The four laws of a capture that actually fires

1. **Verbatim source.** The owner's exact words go in `--source`; the
   statement is your compression, and compressions invert. Keep the original
   attached so an inversion stays catchable.
2. **Symptom keywords, not diagnosis keywords.** Write the words someone has
   BEFORE they know the answer ("pane died", "still waiting"), not the words
   you have after solving it.
3. **Resolve-test both ways, or it didn't happen.** Run
   `helm store resolve "<a sentence someone would actually type>"` for 3+
   phrasings including one from the incident itself: each must HIT (or be
   re-routed under law 4). Then 2
   must-MISS controls (an unrelated sentence using the entry's common words; a
   routine notice or chat-wake line): each must stay silent. 0-for-4 on the first pass is
   NORMAL; a capture is done when it FIRES where it should and stays quiet
   elsewhere, not at `stored:`.
4. **Few, short, rare keys; on a MISS, sharpen, don't pile on.** First ask
   whether the moment is measurable (a tool call, a git/helm verb, a notice
   kind, a state reading, a work phase): if it is, bind the lesson to that moment, not to
   words — a ROUTE cell (`helm store keywords <id> --add
   route:act.helm.<verb>[.<sub>|.<flag>]`, `route:act.spawn`,
   `route:arrival.<kind>`), an act reflex (`helm reflex add <id> | <steer>
   --signal act --verb "<verb> [--flag]"`, resolve-tested with
   `helm store resolve --act '<the command>'`), a counter/state reflex, or a
   hook/guard for a non-helm shell command (a `--pattern` reflex reads PROMPT
   text only). Otherwise write 3-6 probes, each a 2-3 word symptom phrase; a
   single word only when rare in real turns. On a MISS, swap a brittle probe
   for a shorter, rarer one or add ONE for the missed phrasing, then rerun
   both tests. If only common words would catch a phrasing, do not add them:
   bind the lesson to its act with a route cell (above) when there is one, and
   drop the words the route replaces in the same change. Read back
   `helm store get <id>` and drop common single-word stems the store appended
   (`helm store keywords <id> --remove <stem>`). Full rule: heuristic
   `store-keywords-few-short-rare`.

## Resolve FIRST — update beats add (run BEFORE capturing)

Before `store add`, resolve the lesson's own symptom phrases. A HIT means the
store already holds this ground: UPDATE that entry (sharpen statement and
keywords, `--source` the new incident), `supersede` it if replaced, or
`confirm` it if re-proven — never mint a sibling id. Duplicates split the DF
weight of shared keywords so BOTH rank lower, and updates land on one while
the other rots. Only a genuine 0-hit resolve earns a new id.

## The am-I-being-stupid gate (run BEFORE capturing)

One question, every capture: **do we own the thing that misbehaved?** If the
misbehaving code is ours, this is a FIX LANE, not a rule — open it, and store
at most a class pointer until the fix lands, with the friction tax noted so
the fix ranks by payback days. A store entry teaching agents to route around
our own bug is self-bug-canonization.

## When the lesson is a conflict between two instructions

1. **Flag it first.** Name both sources, quote the clash in a line each, say
   which you followed and why.
2. **Then reconcile.** Write one rule that keeps what each got right; update
   BOTH sources in the same pass (store entry directly; skill or doc through a
   lane in the repo that ships it). Resolve-test; tell the owner in one line.
3. **Ask first only** when reconciling needs a vision call or would change the
   substance of owner canon; wording and scope are yours.

Not a conflict: a narrower rule overriding a broader default (that is the
design), an inconvenient rule, or one superseded by a newer dated ruling —
follow the newest, note in one line. A flag without reconciliation leaves the
clash firing every later turn (premise
`reconcile-conflicting-instructions-and-update-both`).

## Owner-canon echo

When the lesson comes from the owner's dictation, post its one-line statement
to the room the owner scans, tagged `owner-canon`, right after capture — an
inverted capture gets caught the same day instead of three incidents later.

## What does NOT go in the store

Anything derivable from the repo (code structure, git history); anything only
this conversation cares about; and any lesson that should be a **guard** — if
it can be enforced deterministically (a refusing verb, a census gate, a test),
build the guard and store only the class.
