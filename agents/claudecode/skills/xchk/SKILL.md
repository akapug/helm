---
name: xchk
description: >
  Use when a load-bearing claim must be verified before anyone builds on it, a
  "did this regress / is that stale?" question needs grounding, or sources
  conflict. Given a topic or claim, /xchk runs one disciplined helm-native sweep
  across web or primary sources, local docs, memory, implementation, git history,
  chat messages, and cross-family refutation. It returns a cited verdict
  plus red herrings. Aliases: xcheck, cross-check, ground-truth-this,
  red-herring-check.
license: MIT
metadata:
  author: helm
  version: "1.0.0"
---

# /xchk - ground a topic or claim

The job is to establish what is actually true before anyone builds on it. Output
a cited verdict, not a file dump. helm state is headless and API-first: use local
files, git, and `helm chat` messages as sources; do not depend on a
client view.

## When To Use

- A load-bearing premise: "this is already handled", "this is the canonical
  route", "the new mechanism proves the outcome".
- A stale/regression question about a doc, behavior, measurement, or claim.
- A nuanced issue where source signals disagree.

Skip it when one cheap deterministic check settles the fact. Run the check
directly and cite it.

## Source Sweep

Run every applicable source cheapest-first. A source counts only when it produces
a citation or an explicit no-hit token.

| # | source | success criterion |
|---|---|---|
| 1 | web / prior art | authoritative citation, or `NO_DOCS_APPLICABLE` with why |
| 2 | local docs / specs | relevant `.local/`, docs, or PRD read and cited, or `NO_LOCAL_HIT` |
| 3 | memory | relevant memory cited, or `NO_PRIOR_MATCH` |
| 4 | implementation | owner code path read at `file:line`, or `NOT_IN_CODE` |
| 5 | git history | introducing or last-touching commit cited by sha/date, or `NO_HISTORY` |
| 6 | chat | relevant `helm chat read/pending` message cited by seq/hash, or `NO_CHAT_MESSAGE` |
| 7 | cross-family refute | `/x` xverify verdict from a different model family, or `x DEGRADED: <reason>` |

Strong confidence needs three aligned sources, or one authoritative source plus a
direct falsifying probe. A single-source answer is reduced confidence.

## Red-Herring Pass

Actively test for these traps:

- **Mechanism != outcome.** The machinery running does not prove the claimed
  user or system outcome.
- **Agreement != verification.** Multiple models can share the same wrong
  premise; ground truth decides.
- **Self-report != fact.** Agent summaries, PR descriptions, and stale docs need
  code or fresh probes behind them.
- **Confound not isolated.** A measurement that did not isolate its variable does
  not prove the claim.

Name each red herring by class and cite the source that refuted it.

## Output Shape

```text
/xchk: <topic-or-claim>

VERDICT: <TRUE | PARTIALLY_TRUE | FALSE | UNRESOLVED> confidence=<high|normal|reduced>

TRUE:
- <claim> - <citation> [+ corroboration]

RED HERRINGS:
- <signal> - class=<mechanism!=outcome|agreement!=verification|self-report|confound>;
  refuted by <source+locator>; why: <one line>

SWEEP RECEIPT:
- web: <cite / NO_DOCS_APPLICABLE>
- local: <cite / NO_LOCAL_HIT>
- memory: <cite / NO_PRIOR_MATCH>
- code: <file:line / NOT_IN_CODE>
- git: <sha+date / NO_HISTORY>
- chat: <seq/hash / NO_CHAT_RECORD>
- cross-family: <verdict / x DEGRADED: ...>
- confidence basis: <why this confidence is justified>
```

Every substantive line needs a locator. If a source that would settle the claim
was unavailable, mark the verdict unresolved or reduced.

## Success Criteria

- Every applicable source has a citation or explicit no-hit token.
- The red-herring pass ran and names either findings or "none found".
- Confidence is bounded by the evidence, not by agent agreement.
- Coordination facts come from `helm chat` API reads, not TUI state.

## Cross-Refs

- `/x` for the cross-family refute.
- `decision-spirit` when the grounded verdict feeds a design or substrate call.
- `ground-truth-cross-reference-loop` when a diagnosis needs iterative
  escalation instead of one pass.
