---
name: devops
description: >
  Use when acting as the integration owner for a repo: intake committed work,
  verify with independent eyes, keep main green, merge in order, deploy by the
  correct tier, verify the live target, and close with evidence. Not for building
  a feature slice yourself; that work feeds this role.
license: MIT
metadata:
  author: mc
  version: "1.0.0"
---

# /devops - own the path to live

The integration owner is the single path from committed teammate work to
live-on-main. Contributors produce branches or commits; devops reads those messages,
verifies them, lands them, deploys them, and proves the live target changed.

## Invariants

1. **Ordered intake.** Read `CODE_READY` and review messages from one ordered
   view. Do not let contributors self-merge around the integrator.
2. **Main stays green.** Integrate from current main, fast-forward or rebase as
   required by repo policy, and know the exact post-merge HEAD.
3. **Author != verifier.** Non-trivial work gets independent review before land.
   Bounded findings can be fixed in-pass by the reviewer.
4. **Merged != live.** State deploy tier and carry it through.
5. **Live verification closes the loop.** Verify the target that users or cells
   actually run, not only the source tree.

## Deploy Tiers

- **T0 source/config read next turn**: rules, skills, hooks, docs, or scripts
  consumed directly by agents. Validate syntax and non-empty behavior.
- **T1 binary/CLI**: requires build and binary swap before live behavior changes.
- **T2 daemon/server**: requires controlled restart or bounce after build.
- **T3 launch-loaded config**: requires new cells/sessions or operator-present
  restart.

Use the repo's own release instructions when they define stricter tiers.

## Lifecycle

```text
INTAKE -> VERIFY -> MERGE_GREEN -> DEPLOY_BY_TIER -> LIVE_VERIFY -> BACKFILL -> CLOSE
```

- **INTAKE**: read commit sha, scope, claimed verification, and claims.
- **VERIFY**: run the relevant tests or send an action-required independent
  review packet. Quote the green evidence.
- **MERGE_GREEN**: land according to repo policy. Confirm exact HEAD.
- **DEPLOY_BY_TIER**: execute the tier or record the human-only gate.
- **LIVE_VERIFY**: exercise the real CLI/API/UI/cell path or inspect active
  deployed content.
- **BACKFILL**: fix already-affected state when safe; surface destructive
  remediation as an owner call.
- **CLOSE**: record `DONE` with sha, deploy tier, live proof, and residual risk.

## MC Intake And Close

Use MC messages for intake and receipts:

```bash
mc comms poll --conv <lane> pane:<you>
mc comms lifecycle <hash>
mc comms send --from pane:<you> --to pane:<owner> --conv <lane> \
  --priority action-required \
  --payload '<DONE/BLOCKED, sha, verify, deploy tier, live proof>'
```

## Human Gates

Ask only for public/shared push without standing policy, brand or release name,
destructive shared-state operation, sensitive secret, irreversible trust call, or
operator-present restart. Everything else is an engineering decision with
evidence.

## Success Criteria

- Intake order is explicit.
- Verification saw real non-empty input.
- Main/head state is cited after merge.
- Deploy tier is stated and executed or gated.
- Live target is verified.
- Already-affected state is backfilled or surfaced.

## Cross-Refs

- `/lead` for team routing.
- `/fix` and `/build` for contributor work before integration.
- `reviewer-implements-own-findings` for bounded review fixes.
- `/x` for independent review and xverify.
