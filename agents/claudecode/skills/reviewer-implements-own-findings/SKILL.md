---
name: reviewer-implements-own-findings
description: >
  Use when a reviewer finds a concrete bounded issue and has enough context,
  tools, and verification access to patch it safely in the same pass. Keeps hot
  review context attached to the fix while preserving independent review for
  broad, subtle, or high-stakes changes.
license: MIT
metadata:
  author: mc
  version: "1.0.0"
---

# Reviewer Implements Own Findings

When a reviewer has just read the diff, found the bug, and can state the fix,
they usually own the first bounded patch attempt. Do not turn hot context into a
cold handoff unless an exception applies.

## Use When

All must be true:

- The finding has a concrete file, function, behavior, or command anchor.
- The fix is bounded and local to the finding.
- The reviewer can run or cite scoped verification.
- The change does not alter broad architecture, public contracts, or ownership.
- The reviewer is not fixing their own review/checking logic.

Route elsewhere when the patch is broad, security-sensitive beyond authority,
runtime-specific without access, or independence would be compromised.

## Protocol

1. Reviewer records severity, mechanism, affected files, and proposed fix on the
   review conv.
2. Reviewer checks exceptions.
3. If no exception applies, reviewer patches in the same context.
4. Reviewer runs changed-surface verification and reports evidence.
5. A second reviewer inspects the patch when the issue is subtle, meta-level, or
   high-stakes.

Use this handoff only when the reviewer should patch:

```text
Context is hot. Please implement your own finding.
Scope: <paths>
Verification: <commands/checks>
Report: files + tests + residual risk.
```

## MC Record Shape

```bash
mc comms send --from pane:<reviewer> --to pane:<owner> --conv <lane> \
  --priority action-required \
  --payload '<finding, patch status, verification, commit sha or blocker>'
```

If the reviewer commits, the final record must include the sha and verification.

## Pitfalls

- Writing a detailed fix recipe when the reviewer could safely patch it.
- Sending every finding back to the original author by habit.
- Letting hot context justify unrelated refactors.
- Accepting a reviewer patch without verification.

## Success Criteria

- Finding severity and mechanism are explicit.
- Patch scope matches the finding.
- Verification ran or an honest blocker is named.
- Regrade path is clear: self-check for small fixes, independent review for
  subtle or high-stakes fixes.

## Cross-Refs

- `/x` for review and refutation.
- `/lead` for routing reviewer patches through the team.
- `/debug-self` when review ping-pong becomes coordination stall.
