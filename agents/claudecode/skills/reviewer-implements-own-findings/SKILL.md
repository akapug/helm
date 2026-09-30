---
name: reviewer-implements-own-findings
description: >
  The cross-family review procedure. A bounded MECHANICAL finding follows the
  row's REVIEW FIX MODE: PATCH commits the cure off the exact reviewed tip and
  returns --patch-tip; MELD-DIFF posts the exact diff in the pair meld for the
  author to apply and records validated --diff-handoff ROOM/MSGID. Reason prose
  without that receipt does not prove a cure. Design findings go to a meld. Family independence follows
  the composed-tip re-read required by Protocol step 7, not a family ban.
license: MIT
metadata:
  author: helm
  version: "2.0.0"
---

# Reviewer Implements Own Findings

**This skill is the review procedure, not an exception to it.** A reviewer of
EITHER model family who has just read the diff, found the bug, and can state the
fix owns the first bounded cure. Follow the row's **REVIEW FIX MODE**: PATCH
commits that cure; MELD-DIFF supplies the exact diff in the pair meld so the
author applies it. The roles do not depend on which family holds the integrator
chair. Do not turn hot context into a vague, cold handoff.

The two modes compare who applies the same concrete cure. PATCH keeps the
finder's change as a commit; MELD-DIFF gives the author a directly applicable
unified diff with a hunk and changed line, not a prose or file:line fix
recipe. Neither mode makes a DESIGN finding mechanical.

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
calling consensus. Whether agreement alone lands the chain, or the composed
tip also owes a re-read, is decided by Protocol step 7. Changed-surface checks
and the canonical land gate still apply. A later patch
changes the artifact and requires renewed agreement and affected-surface review.
The integrator ships only after those obligations and the existing release
authority requirements are satisfied.

## The split that decides everything: MECHANICAL or DESIGN

- **MECHANICAL** — a wrong condition, an off-by-one, a missing guard, a stale
  name, an unhandled pole, a test that asserts nothing. The finder knows the
  cure. **Follow REVIEW FIX MODE**: commit it in PATCH, or post the exact diff
  for the author in MELD-DIFF.
- **DESIGN** — the shape is wrong, the contract is wrong, the abstraction is in
  the wrong place, two subsystems disagree about who owns a fact. **Take it to a
  meld.** A design disagreement settled by one side's patch is the disagreement
  unrecorded, and the other side finds out by reading the diff.

A FIX can carry both: cure the mechanical findings in the assigned mode and
name each design finding with `--design-finding "<finding>"`. In PATCH, name
the committed cure with `--patch-tip`; adopting it is never held back. In
MELD-DIFF, post the exact mechanical diff in the pair meld and give
`--diff-handoff ROOM/MSGID` plus `--no-patch-because` on the FIX; the author
applies the diff. Only a validated typed receipt plus send/add proof that
its first advancing direct child applies the diff count as cure confirmation
and suppress T1; an unrelated child is an ordinary round. Prose alone,
including historical rows without a receipt, is no cure and an ordinary
round/T1 design nudge. Explicit DESIGN findings still go to a meld.

## When findings keep arriving: agree the BAR

From round 3 the dispatch door opens a meld (or puts the BAR in the brief when
you cannot join). A review meld agrees the bar, not a patch:

1. the closed list of blocking harms (everything else is a note);
2. the closed set of falsifier classes the next read may use;
3. each open finding's disposition (cured-in-patch | inside-bar | note | refuted), or `none` when no finding is open;
4. the exact tip both sides will judge.

Research a candidate finding on the row only after the bar is agreed. Close
with one line in your last `[DONE]`:

```
MELD OUTCOME: AGREED|SPLIT|RESEARCH | BAR: <harms> | FALSIFIERS: <class>; ... | FINDINGS: <id>=<disposition>; ... | TIP: <full sha> | NEXT: <next action>
```

Then cite the outcome on the matching row with `--meld <room>` when the
citation binds: `verdict --approve`, `hold --source-clean <tip>`, PATCH
`verdict --fix --patch-tip`, or MELD-DIFF `verdict --fix
--diff-handoff ROOM/MSGID --no-patch-because`, with FIX findings inside the
bar only. `--meld` cites the outcome, not the diff; it never substitutes for
the validated message receipt. The pair round
must be the one opened for that row, and the parties must be the row's sender
and reader. A cure author who differs from the sender cannot currently cite
that pair round on an author-to-reader re-send; a valid source-clean hold need
not carry a typed meld outcome. Do not claim such an outcome without accepted
citation. Only an AGREED meld both sides spoke in switches the spiral rung
off. See docs/MELD_REVIEW_DOOR.md.

Everything below is about the mechanical half.

## Cure When

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
3. Read the row's **REVIEW FIX MODE** in its full brief or CLI receipt, not a
   default inferred from the reviewer's family. PATCH and MELD-DIFF are
   inherited by later reviews on that chain. A row without an enrolled mode
   retains the ordinary PATCH procedure unless its own instructions impose a
   read-only boundary; never infer MELD-DIFF without its recorded mode.
   **In PATCH, commit the cure off the EXACT reviewed tip, in a tree that
   shares the repo's objects; do not push.** A seat reviewing from a room
   claimed for the cure resets it to the reviewed tip and commits there. A
   reader with no room (a subagent, a fresh-context read) makes one in its
   scratch dir and brings the commit back into the repo:

   ```bash
   git clone --shared <repo> <scratch>/wt
   git -C <scratch>/wt checkout --detach <reviewed-tip>
   # edit, then commit in <scratch>/wt
   git -C <repo> fetch --no-write-fetch-head <scratch>/wt <your-commit-sha>
   ```

   A commit left only in the clone does not resolve in the repo, and
   `--patch-tip` refuses it as cross-repository proof. With no refspec the
   fetch writes objects only: no ref moves, so the rail guard stays whole,
   and `--no-write-fetch-head` leaves the shared checkout's FETCH_HEAD as it
   was. Name the last commit you made; its ancestors come with it. Never
   `git worktree add` or a new branch in the shared checkout (the rail guard
   refuses both for anyone but the integrator), never a commit in a
   `helm work peek` room (every reader of that sha shares it), and never an
   amend or rebase of the reviewed SHA.
5. **MELD-DIFF: do not commit the mechanical cure.** Post the exact unified
   diff with a hunk and changed line against the reviewed tip in the row's
   pair meld with one `[MELD e:N]` marker for this row's round, not a file:line
   suggestion. Obtain MSGID from that post, not from the room name or meld
   outcome. The author applies it in one step and verifies the resulting
   composed tip. Record the validated `--diff-handoff ROOM/MSGID` message
   receipt for your exact posted unified diff on the FIX; a room name or
   `--meld` outcome alone is not that receipt. Do not use `--patch-tip` for a cure
   you did not commit.
6. Run or cite changed-surface verification, then record the FIX in the
   row's mode:

   ```bash
   # PATCH
   helm dispatch verdict <id> <reviewed-tip> --fix --measured \
     --finding-count <n> --prior-relation <new|uncured|regression-of-cure> \
     --worse-than-main <path> --patch-tip <your-commit-sha> <evidence>
   # MELD-DIFF: --meld only when the pair round binds to this row
   helm dispatch verdict <id> <reviewed-tip> --fix --measured \
     --finding-count <n> --prior-relation <new|uncured|regression-of-cure> \
     --worse-than-main <path> --diff-handoff <room>/<message-id> \
     --no-patch-because "MELD-DIFF: author applies posted diff" <evidence>
   ```

   A FIX must state `--finding-count` and `--prior-relation`; either may
   be the literal `UNKNOWN`, which the row records as declared. The MELD-DIFF
   `--diff-handoff` requires a validated pair-message receipt for the exact
   diff, not merely the room name or reason text. `--meld` is independent:
   its outcome citation requires both row parties, a tip and exact round bound
   to the row; if those proofs are absent, record the FIX with the diff receipt
   but without `--meld`, not a fabricated outcome. PATCH records `patch_tip`
   and `patch_author`; `helm dispatch triage <id>` and `helm lr show <id>`
   print both. The PATCH tip must descend from the reviewed tip.
7. In PATCH, the lane owner or integrator rebases onto the reviewer's tip or
   cherry-picks it, and the ledger records each author. In MELD-DIFF, the
   author applies the reviewer's exact diff and commits the resulting cure;
   send its first advancing direct child of that FIX for confirmation, not a
   new round. With no validated receipt (including historical reason-only
   FIXes), this exemption does not apply and T1 still sees a no-cure nudge.
   **Agreement on the composed mechanical cure is enough on a REVERSIBLE
   lane; a SAFETY DOOR or non-mechanical cure owes one re-read** by a reader
   who wrote none of that tip. Safety doors include authority or repo
   selection, credentials, process-kill, delivery, and land/gate binding.
   This re-read preserves independence where a miss costs most, not a rule
   that one family may only look while the other types.

## When no reviewer seat can take it

No reviewer is NEVER a blocker. Take the cheapest reader that clears the bar
(store premise review-routing-is-cheapest-reader-that-clears-the-bar-measured,
owner 2026-09-25) — and **Sonnet and Haiku never review anything**. So "no
workable reviewer", a walled family, or a dispatch refused as UNUSABLE is a
routing fact, never a reason to park a REVERSIBLE lane. Take the ladder in
order:

1. a **fresh-context Opus read** — the default. From an Opus seat it is a
   subagent (**the Agent tool**), briefed like any reviewer with the diff and
   the question and none of the author's context; from another Claude seat it
   is a one-agent Workflow with opts.model `opus`. It is a full review leg on a
   **REVERSIBLE** lane;
2. **ONE approval-tier read by a reader that is NOT the author** when the
   lane touches prod, a migration, a deletion, money, credentials, a process
   kill, a public push or a safety door (land, review, a guard, a hook's
   refusal), chosen by `helm burn` and judged on the RESOLVED model (store
   prior approval-tier-2026-08-11-owner-revised): claude on Opus 5.5 as a
   fresh-context, non-author read (an Opus subagent or another Opus seat,
   never the author's own context), codex, ds4pro on V4 Pro, kimi or grok
   (the cursor route included). A different family is not required for that
   one read (the owner confirmed it, room row 2217). Among readers who clear
   the bar, prefer another lane over an Opus agent, especially a local
   seat once the owner admits it, without overusing codex, and never prefer
   Fable over Opus automatically; the exact order is weighed case by case,
   never a fixed list. Gemini, codex-spark and local seats read
   as INPUT only until the owner admits them on their record: for a local
   seat, 5 door reads with no miss is the evidence put to him, never an
   automatic admission. No seat is always free (qwen27 is prefill-bound), so
   ask who can take it now: `helm reviewers <row>`, then
   `helm dispatch send <seat> <lane> --ref <tip> --kind review --supersedes <row>`.
   When none can take it, the door read **PARKS until a tier reader can take
   it**, with gemini reading meanwhile as input only.

**Fable is for max QC only**, never a default and never automatic, and no
rung of this ladder (owner ruling, task/3202): the most important work (an
owner P0, a release, a public push, or a money or creds door with no other
reader), at about 3 Opus tokens per Fable token, and not while `helm burn`
reads anthropic ORANGE or worse. A max-QC read is a one-agent Workflow,
opts.model `fable` (the alias), never the Agent tool (it ignores its model
flag and runs your own model). On "You have reached your Fable limit": that
limit belongs to one credential. It is not a wall and never a reason to step
down a model — get **Fable through another credential or seat**.

A subagent or a Workflow run is not a seat, so the seat that ran it records
its read on the row as an ADVISORY read, naming the model and the run:

```bash
helm dispatch verdict <row> <reviewed-tip> --concur --measured \
  --reviewer-model <model> --reviewer-run <run id> \
  [--author-model <your model>] <evidence>
```

The reading model must be another family than the author's, or Fable for a
Claude author, and an Opus read is recorded as `fresh-context run <id>` on a
door lane as on a REVERSIBLE one, whatever model the author ran, when its run
record, which helm checks on disk, holds every bound, as recorded (unattested)
because a same-user process could plant one
(no fork, finished and not in flight, Opus models, no Write or Edit to the
lane's files in the shared checkout or the lane worktree (a cure committed in
your own clone is the procedure, not a write to the lane), begun after the
reviewed tip was committed (a builder that edited through the shell began
before its tip), a transcript that names the reviewed tip, and its own lines
with no fork mark and no
conversation it continues before a turn of its own; the session that spawned
it is no input, so a fresh subagent of the author's own counts). Act on its
findings and cite it in the re-dispatch. A model helm does not
recognise, a Sonnet or Haiku model, and the author's own model otherwise are
refused, and so is APPROVE (a model run CONCURs, or FIXes with its cure). Each
refusal names what to take instead: another approval-tier read, chosen by
`helm burn`, and the park when none can take it, with Fable only for max
QC. A fresh-context CONCUR at the row's tip also records the source-clean
hold it carries, so that one verb makes the row a `helm train` car for the
land gate; every other model run's read discharges NOTHING: the row stays
owed, and the integrator reads it for itself.

## Pitfalls

- Parking a REVERSIBLE lane on "no reviewer available" instead of taking the
  ladder above.
- Counting a gemini or local-seat read as a door's approval: it is input
  until the owner admits that reader on its record.
- Reaching for Fable when no approval-tier reader can take a door read: the
  door read parks until one can, with gemini as input meanwhile. Fable is for max QC on the most important work only, at about 3
  Opus tokens per Fable token, and never while `helm burn` reads anthropic
  ORANGE or worse.
- Stepping down to Sonnet or Haiku because Fable hit a limit on one credential.
- Sending a vague fix recipe instead of a committed PATCH cure or the exact
  MELD-DIFF diff in the pair meld with its validated `--diff-handoff` receipt.
- Mistaking `--no-patch-because` prose or `--meld` outcome for the typed diff
  receipt: neither proves a cure, including on historical FIXes.
- Committing a MELD-DIFF cure instead of letting the author apply the diff.
- Sending every finding back to the original author regardless of mode.
- Pushing a PATCH cure branch, or committing it on the shared checkout.
- Patching a DESIGN finding instead of melding it.
- Letting hot context justify unrelated refactors.
- Landing a composed tip that every one of its authors has already read.

## Success Criteria

- Finding severity and mechanism are explicit.
- Cure scope matches the finding and the row's REVIEW FIX MODE.
- PATCH commits off the exact reviewed tip and names `--patch-tip` on FIX;
  MELD-DIFF posts the exact diff in the pair meld, names validated
  `--diff-handoff ROOM/MSGID` plus `--no-patch-because` on FIX, and the author
  applies it.
- Verification ran or an honest blocker is named.
- The chain met Protocol step 7: agreement for a mechanical cure on a
  reversible lane, or the one re-read for a safety door or non-mechanical
  change.

## Cross-Refs

- `council-of-models` for a refutation by several model families in one turn.
- `/build` and `/fix` for the lane physics each mode's cure still obeys.
- `/devops` for integration: adopt PATCH or apply MELD-DIFF, then credit the
  actual authors.
