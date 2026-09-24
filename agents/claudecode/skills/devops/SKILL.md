---
name: devops
description: >
  Use when acting as the integration owner for a repo: intake committed work,
  verify with independent eyes, keep main green, merge in order, deploy by the
  correct tier, verify the live target, and close with evidence. Not for building
  a feature slice yourself; that work feeds this role.
license: MIT
metadata:
  author: helm
  version: "1.0.0"
---

# /devops - own the path to live

The integration owner is the single path from committed teammate work to
live-on-main. Contributors produce branches or commits; devops reads those messages,
verifies them, lands them, deploys them, and proves the live target changed.

## Invariants

1. **Ordered intake.** Read ready-to-integrate and review messages from one
   ordered view. Do not let contributors self-merge around the integrator.
2. **Main stays green.** Integrate from current main, fast-forward or rebase as
   required by repo policy, and know the exact post-merge HEAD.
3. **Author != verifier.** Non-trivial work gets independent review before land.
   Bounded MECHANICAL findings are fixed in-pass by the reviewer of EITHER
   family, committed off the exact reviewed tip and named with `--patch-tip` on
   the FIX verdict; you rebase the lane onto that tip or cherry-pick it, and you
   credit both authors at close. Design findings go to a meld. A lane carrying
   several authors is normal; what it owes is ONE re-read of the composed tip by
   a reader who wrote none of it, before the land gate.
4. **Merged != live.** State deploy tier and carry it through.
5. **Live verification closes the loop.** Verify the target that users or seats
   actually run, not only the source tree.

## Deploy Tiers

- **T0 source/config read next turn**: rules, skills, hooks, docs, or scripts
  consumed directly by agents. Validate syntax and non-empty behavior.
- **T1 binary/CLI**: requires build and binary swap before live behavior changes.
- **T2 daemon/server**: requires controlled restart or bounce after build.
- **T3 launch-loaded config**: requires new seats/sessions or operator-present
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
- **LIVE_VERIFY**: exercise the real CLI/API/UI/seat path or inspect active
  deployed content.
- **BACKFILL**: fix already-affected state when safe; surface destructive
  remediation as an owner call.
- **CLOSE**: record `DONE` with sha, deploy tier, live proof, and residual risk.

## Helm Intake And Close

Use helm chat for intake and receipts, and the integration board for ordered
intake state:

```bash
helm chat read --since 50   # ordered intake: ready-to-integrate and review posts
helm chat post "DONE|BLOCKED: <sha>, verify, deploy tier, live proof" --reply-to <id>
```

## Repository Hygiene — the leak rail

History is forever and a remote is a distribution. The integration owner is
responsible for what a repo's history CARRIES, not only for what its tree
compiles, because the one git mistake that costs more than every other kind
is data in history: the cure is a rewrite of a shared remote and a fresh
clone for every collaborator, and a remote that was ever public may already
be cloned or indexed.

**What never enters history, in any repo:**

- Data that is not the project's source: a customer's content export, a
  database dump, a browser-session snapshot, an archive, a subscriber or
  customer list, a media library. It lives OUTSIDE the tree (a gitignored
  directory, `/srv`, `/scratch`), never beside the code that reads it.
- Secrets, tokens, private keys. Owner identifiers and machine paths.
- Blobs larger than source ever is. The scanner's ceiling is 1 MiB; a
  legitimately large tracked file (a golden fixture, a vendored corpus) is
  declared `<path> helm-bulk=ok` in a STAGED `.gitattributes` — a decision a
  reviewer sees in the diff; an unstaged edit, `.git/info/attributes` or a
  global attributes file cannot exempt anything. The address threshold
  (20 distinct) is per COMMIT across every staged file. A source file that
  QUOTES an export header or a dump banner (a parser test, a fixture
  builder) fires the self-declaration shape exactly like an export wearing
  that suffix would, because quote characters occur inside the raw formats
  themselves (an XML attribute, an SQL comment) and no byte scan can tell
  the two apart — four review rounds of witnesses settled that. The ONE
  door is the declaration: `<path> helm-bulk=quotes` in the STAGED
  `.gitattributes` waives only the two self-declaration shapes for that
  path; the archive magic, the size ceiling and the address count still
  judge it. `helm-bulk=ok` waives everything and is for the golden export
  or vendored corpus that IS bulk on purpose.

**The guard is installed at bring-up, before the first commit, in EVERY
repo — not only rail-managed ones.** `helm work install-guard --apply
--profile leak --repo <path>` arms the two legs every repo owes: the
pre-commit never-track staged-set scan (`helm/nevertrack.py`: bulk-data
shapes, address crowds, oversized blobs, private needles) and the pre-push
host-path scan. Rail-managed repos take `--profile rail` (the composed
shared-checkout rail, which includes both). `helm doctor` counts every
registry project with no guard or a stale one, folded to one line naming
them — a guard that is built but not installed is inert, and the census is
what makes that visible before the leak instead of after.

**Untracking is not a scrub.** `git rm --cached` plus a `.gitignore` line
stops a file entering NEW trees; every clone and the remote still carry the
bytes, fetchable by blob hash without a checkout. When the scanner reports a
pre-existing leak ("ALREADY IN HEAD"), or one is found any other way, the
scrub ladder is:

1. **Stop the bleeding**: untrack + gitignore, so no later commit re-adds it.
2. **Rewrite from a mirror**: `git clone --mirror`, then `git filter-repo
   --invert-paths --path <dir>` for every offending path; verify the blob is
   gone (`git rev-list --objects --all | grep <hash>` prints nothing) and
   scan the WHOLE rewritten history for secrets (`gitleaks git <mirror>`)
   before anything is pushed — the one blob you know about is rarely alone.
3. **Prefer a fresh repo over a force-push.** A new remote never held the
   object, so nothing has to be garbage-collected by the host: rename the old
   repo aside, push the rewritten history to a new repo under the old name,
   transfer open issues, re-point local checkouts and worktrees, and have
   every collaborator re-clone. Re-using the name kills the host's redirect,
   so every durable reference to `<owner>/<repo>#N` is rewritten in the same
   pass (helm rows, docs, memory).
4. **Only when the repo identity must survive** (forks, stars, external
   links): force-push the rewritten history and ask the host's support to run
   garbage collection and clear caches; the leak is gone only when fetching
   the old blob by hash returns 404.
5. **Retire the old copy.** The renamed repo still holds the bytes: archive
   it owner-only, remove other collaborators, and delete it once the fresh
   repo has proven itself. Until deletion the liability is contained, not
   gone.

Time it for a quiet point (no lane mid-round), because every open branch is
rebased onto the rewritten history.

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

- `helm route` for who should take a review, build or verify.
- `/fix` and `/build` for contributor work before integration.
- `reviewer-implements-own-findings` — the review procedure behind invariant 3:
  reviewer patches, lane carries both authors, close credits each.
- `helm route review` for who should take an independent review.
