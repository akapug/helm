# learn — reference: incident history behind the laws

Open this when a SKILL.md law needs its evidence. The laws live in SKILL.md;
this file is why they exist.

## Why helm's store only

This skill was rewritten 2026-07-22 after the codex-orch scope-leak incident:
a helm seat found itself editing a different project's dirs and reaching for
another project's verbs. Helm owns its own physics; if you are reaching across
projects from a helm seat, STOP.

## The capture — measured failures

- **One pipe-delimited string, not separate args:** a malformed capture
  swallows the statement into the id (measured 2026-07-22).
- **Law 1, verbatim source:** compressions invert — measured 3 inversions in
  24h, 2026-07-22.
- **Law 2, symptom keywords:** diagnosis-keyed premises sat unfindable through
  eight live hits of their own class.
- **Law 4, few/short/rare keys:** more keywords buy your recall with every
  other agent's precision. E2 (2026-09-23) measured lone single-word matches
  at 61% of injections and only 8% relevant.

## Resolve-first — the duplicate cost

Duplicates are worse than misses: they split the DF weight of shared keywords,
so BOTH entries rank lower than either alone, and future updates land on one
while the other rots stale (owner-caught 2026-07-22, the gap live in an
845-entry store).

## The am-I-being-stupid gate — self-bug-canonization

The --help incident, 2026-07-22: a 3-line cli.py fix lived as a fleet-wide
behavioral prior instead of being fixed. Rules are for truths we cannot
change; a store entry teaching agents to route around our own bug canonizes
the bug. Put the friction tax on the fix lane (the steps agents spend routing
around the bug x times per day) so the fix ranks by payback days.

## Route cells and reflexes (long form of law 4)

Measurable moments and how to bind to them:

- A helm verb or flag, or an Agent spawn: add a ROUTE cell,
  `helm store keywords <id> --add route:act.helm.<verb>[.<sub>|.<flag>]` (or
  `route:act.spawn`), or write an act reflex,
  `helm reflex add <id> | <steer> --signal act --verb "<verb> [--flag]"`.
  argv-guard then says the rule once per context when the agent runs that
  verb, and never refuses. Resolve-test with
  `helm store resolve --act '<the command>'`.
- A notice kind: `route:arrival.<kind>`.
- A counter or state: `helm reflex add <id> | <steer> --signal S`.
- A shell command that is not a helm verb: a hook or a guard — a `--pattern`
  reflex reads PROMPT text only, so it never sees a command the agent runs.

The store keeps only the class (see "What does NOT go in the store"). A
heuristic's keyword list is not a trigger: it is keywords.
