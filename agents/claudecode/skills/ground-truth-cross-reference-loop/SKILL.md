---
name: ground-truth-cross-reference-loop
description: >
  Use when debugging is not converging, a failure mode is novel, a substrate or
  architecture decision is expensive to reverse, or a claim depends on multiple
  uncertain sources. Cross-reference docs, implementation, history, MC messages,
  probes, and cross-family refutation before the next costly experiment.
license: MIT
metadata:
  author: mc
  version: "1.0.0"
---

# Ground-Truth Cross-Reference Loop

Do not keep iterating on a single-source hypothesis when another source can
falsify it cheaply. This loop anchors diagnosis and design before expensive
builds, restarts, commits, or public claims.

## When To Use

- A debugging loop has reached repeated attempts without convergence.
- The same error appears in consecutive experiments.
- A substrate, API, runtime, wire-format, persistence, identity, or replay change
  is proposed.
- The failure mode is novel or lacks a known class.
- The next experiment would consume meaningful build, deploy, or review cost.
- Agents agree, but no one checked an independent source.

Skip only for trivial syntax, typo, or import fixes where one source is genuinely
the full truth.

## Sources

Use the cheapest relevant subset first, but do not land load-bearing claims from
one weak source.

1. **Official docs/specs**: product docs, language specs, API references.
2. **Source**: owner implementation, not only comments or wrappers.
3. **History**: `git log`, `git blame`, prior commits, and release notes.
4. **MC messages**: `mc comms poll`, `mc comms lifecycle`, `mc cockpit get`, and
   relevant conv history.
5. **Project memory or plans**: prior class, PRD, task, or dogfood evidence.
6. **Cross-family refutation**: a different model family attacks the premise.
7. **Cheapest probe**: grep, unit repro, API call, or minimal live check that can
   falsify the next step.

Strong confidence usually needs three aligned sources, or one authoritative
source plus a direct falsifying probe.

## Required Questions

Before the next costly experiment, answer:

- What do the official docs or specs say?
- What does the implementation actually do?
- What changed in history?
- What MC message or live state confirms the coordination/runtime fact?
- What prior class or plan matches?
- What would a different model family attack?
- What cheapest probe could disprove the next step?

If an answer is unknown, say why and either run that check or mark the confidence
gap explicitly.

## Output Shape

```text
Ground-truth cross-reference:
- docs: <source / NO_DOCS_APPLICABLE>
- source: <file:line / NOT_IN_CODE>
- history: <sha/date / NO_HISTORY>
- mc: <seq/hash/lifecycle/cockpit / NO_MC_RECORD>
- memory_or_plan: <locator / NO_PRIOR_MATCH>
- cross_family: <verdict / DEGRADED: reason>
- cheapest_probe: <command/check + result>
- conclusion: <continue / revise / stop>
```

## Escalation

- After two failed attempts, add a new source or cross-family refute.
- After three failed attempts, search docs/issues/history before trying again.
- After four failed attempts, pivot hypothesis category or trace source-level
  ownership.
- After five failed attempts, stop the loop and convene a focused review or
  mindmeld with evidence.

## Success Criteria

- The next action is based on independent evidence, not repeated intuition.
- Every load-bearing claim has locators.
- The cheapest falsifying probe ran before an expensive cycle.
- The conclusion says whether to continue, revise, or stop.

## Cross-Refs

- `/xchk` for a one-pass user-facing grounded verdict.
- `decision-spirit` for design/substrate composition calls.
- `/learn` when the result should become a durable MC rule, skill, hook, or
  memory.
