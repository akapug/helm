---
name: drift
description: >
  Compare current execution against the last approved baseline or stated plan.
  Use when asked "did we drift?", after long agent-driven work, or when scope
  creep and accidental additions need review. Classify changes as on-intent,
  drifted, or slop, and recommend re-approve, revise, or revert.
license: MIT
metadata:
  author: mc
  version: "1.0.0"
---

# /drift - compare execution to approved intent

Drift analysis surfaces where agent execution diverged from approved intent. It
does not auto-revert. It gives the owner or leader a grounded re-approve-or-fix
decision.

## Inputs

- Optional baseline ref: tag, commit, branch, or MC message.
- Active plan: PRD, issue, task, user message, or MC message.

If no baseline exists, say so and offer a first-baseline procedure. Do not invent
an approval point.

## Steps

1. **Resolve baseline.** Identify the approved ref and the plan it represented.
   Cite commit, tag, file, or MC seq/hash.
2. **Read current intent.** Hydrate the current active plan or user request.
3. **Diff code and docs.**

```bash
git diff <baseline>..HEAD --stat
git log <baseline>..HEAD --oneline
```

Read the substantive diff for touched areas.

4. **Classify each changed area.**
   - **on-intent**: implements approved intent.
   - **drifted**: real work, but beyond or different from approval.
   - **slop**: unexplained addition, duplicate, dead code, speculative
     abstraction, stale doc, or leftover scratch.
5. **Compare plan to execution.** Flag abandoned intent, silent rescoping, and
   unapproved additions.
6. **Report.** Lead with drifted and slop findings, each with anchors and a
   recommendation.

## Output Shape

```text
DRIFT REPORT baseline=<ref> plan=<locator>

DRIFTED:
- <finding> anchor=<file:line/sha/seq> recommendation=<re-approve|revise|revert>

SLOP:
- <finding> anchor=<file:line/sha> recommendation=<revert|justify|archive>

ON-INTENT:
- <brief kept-on-intent item> anchor=<locator>

OPEN DECISIONS:
- <owner/leader call>
```

## Rules

- Skeptical by default toward unintended additions.
- Recommend, never auto-revert without explicit approval.
- Anchor every finding.
- Planning docs can preserve historical names; public docs must match current
  behavior.

## Success Criteria

- Baseline and active intent are cited.
- Every changed area is classified.
- Drift/slop findings have file, commit, or MC anchors.
- The report gives concrete re-approve, revise, or revert recommendations.

## Cross-Refs

- `/consolidate` for document cleanup after the drift report.
- `/grill-me` when unresolved intent needs a human-held answer.
- `/xchk` for disputed claims.
