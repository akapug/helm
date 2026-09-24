---
name: reviewer-implements-own-findings
description: >
  The cross-family review procedure. Use whenever a reviewer of ANY model family
  finds a concrete bounded MECHANICAL defect: patch it in your own worktree off
  the exact reviewed tip and name that tip on the verdict (--patch-tip), so the
  lane carries both authors. Design findings go to a meld instead. Family
  independence is preserved by one re-read of the composed tip by a reader who
  wrote none of it, never by keeping a family read-only.
license: MIT
metadata:
  author: helm
  version: "2.0.0"
---

# Reviewer Implements Own Findings

**This skill is the review procedure, not an exception to it.** A reviewer of
EITHER model family who has just read the diff, found the bug, and can state the
fix owns the first bounded patch attempt. Every family is an equal counterpart —
a codex reviewer patches exactly as a claude reviewer does, including when a
claude seat holds the integrator chair. Do not turn hot context into a cold
handoff.

The whole system was built to find the shortest path to maximal-quality code.
Read-only review was a guess at that path and it was the wrong one: it spends a
round of latency and a second seat's context to retype a fix the finder already
had in hand.

## Plan together; own the combined result

The owner's direction is: "plan together, split work, cross-review and patch each
others' work, then when both agree no further patch is needed, ship" and "we need
to balance the agent rainbow coalition methinks".

Before splitting work, use one task-local meld to align on the premise,
invariants, acceptance checks, and ownership of the combined artifact. Split
independent work while naming who verifies the seams. Both families can plan,
implement, and refute; author and reviewer are responsibilities for a particular
patch, not permanent family roles. Reuse the task context across repair rounds.
Keep detailed exchanges in the meld; the fleet room gets decisions, blockers,
and artifact pointers rather than every review paragraph.

The exit requires both counterparts to agree that no further patch is needed
on the exact composed tip. Record unresolved disagreements instead of silently
calling consensus. Agreement does not replace the independent composed-tip
review below, changed-surface checks, or the canonical land gate. A later patch
changes the artifact and requires renewed agreement and affected-surface review.
The integrator ships only after those obligations and the existing release
authority requirements are satisfied.

## The split that decides everything: MECHANICAL or DESIGN

- **MECHANICAL** — a wrong condition, an off-by-one, a missing guard, a stale
  name, an unhandled pole, a test that asserts nothing. The finder knows the
  cure. **Patch it.**
- **DESIGN** — the shape is wrong, the contract is wrong, the abstraction is in
  the wrong place, two subsystems disagree about who owns a fact. **Take it to a
  meld.** A design disagreement settled by one side's patch is the disagreement
  unrecorded, and the other side finds out by reading the diff.

Everything below is about the mechanical half.

## Patch When

All must be true:

- The finding has a concrete file, function, behavior, or command anchor.
- The fix is bounded and local to the finding.
- You can run or cite scoped verification.
- The change does not alter broad architecture, public contracts, or ownership
  (that is the DESIGN half — meld it).
- You are not fixing your own review/checking logic.

## Protocol

1. Record severity, mechanism, affected files, and the cure.
2. Check the mechanical/design split above.
3. **Branch off the EXACT reviewed tip in your OWN worktree** and commit the
   cure there. Never the shared checkout; never an amend
   or rebase of the reviewed SHA; **do not push**.
4. Run changed-surface verification.
5. Post the tip on the verdict:

```bash
helm dispatch verdict <id> <reviewed-tip> --fix --measured \
  --worse-than-main <path> --patch-tip <your-commit-sha> <evidence>
```

   The row then records `patch_tip` and `patch_author`; `helm dispatch triage
   <id>` and `helm lr show <id>` print both. The tip must descend from the
   reviewed tip or the verdict refuses to name it.

6. The lane owner or the integrator rebases the lane onto that tip or
   cherry-picks it. The lane now has **several authors** and the ledger records
   each: `helm lr close --reason landed` prints an `AUTHORS` line crediting
   every one the chain names.
7. **One re-read before the land gate**, by a reader who wrote none of the
   composed tip. That re-read is what preserves family independence — not a rule
   that one family may only look while the other types.

## When no reviewer seat can take it

No reviewer is NEVER a blocker. A review is
**CROSS-FAMILY**, or **FABLE** when only a different model is needed, read in a
fresh context — and **Sonnet and Haiku never review anything** (store premise
review-is-cross-family-or-fable-never-sonnet). So "no workable reviewer", a
walled family, or a dispatch refused as UNUSABLE is a routing fact, never a
reason to park the lane. Take the ladder in order:

1. **any other-family seat** — and the **openrouter** seat is always one, the
   free lane that exists so this rung is never empty:
   `helm dispatch send openrouter <lane> --ref <tip> --kind review --supersedes <row>`;
2. a **Fable one-agent Workflow** — the Workflow tool, one agent, opts.model
   `fable` (the alias), briefed like any reviewer (never the Agent tool: it
   ignores its model flag and runs your own model);
3. on "You have reached your Fable limit": that limit belongs to one
   credential. It is not a wall and never a reason to step down a model — get
   **Fable through another credential or seat**, and failing that the openrouter
   seat.

A Workflow run is not a seat, so the seat that ran it records its read on the
row as an ADVISORY read, naming the model and the run:

```bash
helm dispatch verdict <row> <reviewed-tip> --concur --measured \
  --reviewer-model fable --reviewer-run <run id> \
  [--author-model <your model>] <evidence>
```

The reading model must be another family than the author's, or Fable for a
Claude author; a model helm does not recognise, a Sonnet or Haiku model, and
the author's own model are refused, and so is APPROVE (a model run CONCURs, or
FIXes with its cure). The read discharges NOTHING: the row stays owed until
helm can verify the run on disk, and the integrator reads it for itself.

## Pitfalls

- Parking a lane on "no reviewer available" instead of taking the ladder above.
- Stepping down to Sonnet or Haiku because Fable hit a limit on one credential.
- Writing a detailed fix recipe when you could have committed the fix.
- Sending every finding back to the original author by habit.
- Pushing your cure branch, or committing it on the shared checkout.
- Patching a DESIGN finding instead of melding it.
- Letting hot context justify unrelated refactors.
- Landing a composed tip that every one of its authors has already read.

## Success Criteria

- Finding severity and mechanism are explicit.
- Patch scope matches the finding.
- The cure is committed off the exact reviewed tip and named on the verdict.
- Verification ran or an honest blocker is named.
- Someone who wrote none of the composed tip read it once before the land gate.

## Cross-Refs

- `council-of-models` for a refutation by several model families in one turn.
- `/build` and `/fix` for the lane physics the cure commit still obeys.
- `/devops` for the integrator's side: rebase or cherry-pick, then credit both.
