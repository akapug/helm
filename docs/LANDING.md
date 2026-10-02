# How a change lands

This page is for anyone who builds, reviews or lands work in a helm fleet. It
covers the review rule, how a review is booked and answered, how a change is
tested, where its change note goes, how a train lands it, and what the
land-request view tracks.
[NEW_AGENT_GUIDE §3](NEW_AGENT_GUIDE.md#3-work) is the same flow from the
seat's side, and [CONTRIBUTING](../CONTRIBUTING.md#how-a-change-is-tested)
is the testing detail.

## The path, in one picture

```text
claim a lane ──► build ──► focused test rounds ──► book a review
                                                       │
                   a reader who wrote none of it reads the exact commit
                                                       │
            approve, or hold it source-clean ◄─────────┤──► fix (the reader
                     │                                 │    may commit the cure)
                     ▼                                 │
       train: merge the reviewed tips, run ONE whole suite
                     │
                     ▼
        trunk  ──►  helm lr foldcheck proves what landed
```

## The review rule

**A review is independent when the reading context is independent of the
writing.** The reader is a different seat on a fresh session: a context
window that holds none of the premises the author adopted and stopped
testing.

- **Model and family are preferences.** A different resolved model and a
  different vendor family are printed on the row and rank candidates
  (`helm route` sorts the author's own family last). Routing aliases prove
  nothing about which weights ran, so helm reads the model id the runtime
  resolved. Neither is required: a fresh-context Opus read of Opus-authored
  work counts.
- **Subagent and Workflow runs.** A fresh-context subagent or Workflow run of
  the exact tip counts as its own reader, the author's own included, when it
  began after that tip was committed, wrote none of the lane, and its
  transcript names the tip. A run that built the lane never counts: it was
  alive while the lane was written. The author records the read with one
  verb, `helm dispatch verdict <row> <tip> --concur --measured
  --reviewer-model opus --reviewer-run <run> <evidence>`, and that CONCUR
  also holds the row source-clean, so auto-land can take it. helm refuses
  the record when it cannot recognise the model, and when the model is the
  author's own model or family, unless it is a fresh-context Opus run or
  Fable reading another Claude model's work.
- **A reviewer who patches is an author.** Its patch lands on PAIR
  AGREEMENT at exactly the patched tip: the lane's other author holds it, or
  another seat records CONCUR or APPROVE there and the patcher then holds
  it. Without one of those, the patcher's hold is refused. Pair agreement
  counts only for a patch recorded with no design finding, and only on a
  reversible lane: on a door lane, the patched tip also needs one read by a
  fresh-context reader that wrote none of it.

`helm/review_independence.py` states the predicate; the approval tier and the
recorded-run checks in `helm/dispatches.py` enforce it. Each refusal names
the input it found shared or could not read.

**The approval tier** is a separate question: whether a seat's APPROVE can
close a row at all. The approval tier is a policy you store (see
[VERBS](VERBS.md), approval-tier). The maintainers' fleet admits a
fresh-context Claude Opus, Codex, DeepSeek V4 Pro, Kimi and Grok; Gemini and
local models read as input only. `helm reviewers <row>` names who can take a row now.

**The landing bar** is one approval-tier read, bound to a whole-suite gate
receipt on a tree that carries the reviewed tip. The maintainers ask more of
a change that touches a door (production, a migration, a deletion, money,
credentials, a process kill, a public push, or a safety door such as land,
review, a guard or a hook's refusal): its read waits for an approval-tier
reader, with Gemini reading meanwhile as input only, and they prefer a reader
on a different model there. A fresh-context read on the author's model still
counts, on a door as on any other change.

## Booking and answering a review

```console
$ helm dispatch send <reader> <lane> "<brief>" --ref <tip> --kind review --new-work
$ helm dispatch verdict <row> <tip> --approve --measured <evidence>
$ helm dispatch hold <row> --source-clean <tip> <reason>
```

- `helm dispatch send` books a review against an exact commit and says which
  WORK it belongs to: `--new-work` or `--supersedes <id>`. The lane is only a
  label, so a renamed continuation would otherwise read as round one.
- The verdict's polarity is required (`--approve`, `--fix`, `--supersede` or
  `--concur`), and so is its evidence class (`--measured`, `--inferred` or
  `--unverified`). A decision that does not say which way it went would be
  recorded as UNDECLARED for good.
- A reader whose source read is clean, with no whole-suite receipt to cite,
  **holds** the row source-clean. The row rides the next train at its held
  tip, and after that train lands,
  `helm lr foldcheck <head> --gate gate:<id> --apply` closes it.
- An APPROVE needs a verified `gate:<token>` from a whole suite. A CONCUR
  endorses the work and authorizes nothing.

**Readers patch mechanical defects themselves.** The families are equal
counterparts, not a writing tier and a witnessing tier. A reader who finds a
MECHANICAL defect commits the cure off the exact reviewed tip — in a room it
holds, or, for a subagent with no room of its own, a `git clone --shared`
clone — and brings the commit into the repo with
`git -C <repo> fetch --no-write-fetch-head <clone> <sha>` (which moves no
ref), and names it on the verdict with `--patch-tip <sha>`. For an explicit
BUILD, a parent may instead assign its sole delegate a REGISTERED lane room
(`helm work claim`); the parent owns the lease through completion or accepted
handoff and then returns the room (`helm work release`), never unfinished work.
This narrower build allocation is never general reviewer authority. Never an
unregistered shared worktree, an unleased shared branch, a raw
`git worktree add`, a protected or shared ref write, a permission bypass,
config/canon edit, test-home leak or publication. The lane owner or integrator
takes that tip, the ledger records both authors, and
`helm lr close` with `--reason landed` credits each. Independence survives
because the composed tip is re-read once, before the land gate, by a reader
who wrote none of it.

**Design disputes go to a meld.** A DESIGN finding, or a review chain that
keeps going round, moves into a pair meld: a shared room where the two sides
agree the bar and record a typed outcome on the row.
[MELD_REVIEW_DOOR](MELD_REVIEW_DOOR.md) describes the door and the outcome
line.

## Testing a change

A change is tested by **focused rounds** while it is built and reviewed, and
by **one whole suite** when it lands.

- `helm gate run --focus --plan` prints the tests a change can reach and runs
  nothing; `helm gate run --focus` runs them and mints a focused receipt.
- `helm gate audits` prints one command that runs every tree-wide audit plus
  the modules you name.
- In helm's own tree, `helm gate run` refuses a whole suite in a lane room,
  refuses a second one on a tree that is already green, and runs a red tree
  again only with `--again`.

A **serial** receipt always authorizes a land. A **sliced** receipt (parallel
slices of one serial discovery) authorizes one only while the gate canary
stands: no DISABLE marker, a clean one-pass finder run (every module alone
in a fresh process), a leak-free report-mode sliced suite, three trees on two
hosts whose serial and sliced runs agreed test for test, a red tree whose
real failures the sliced run caught too, and evidence under 36 hours old.
Otherwise a sliced receipt binds a lane tip and a review's approve, never a
land. `helm gate window launch`
runs the land gate as slices while the canary stands; `--serial` forces
serial, and the canary's first divergence sends every land back to serial
with no person in the loop.
[CONTRIBUTING](../CONTRIBUTING.md#how-a-change-is-tested) has the table of
what each run can authorize, and where tests run on a host that refuses
local suites.

## A lane's change note

A lane writes the note for its change as `changes/<lane>.md`: one or more
markdown bullets, written like a `CHANGELOG.md` bullet. It never edits
`CHANGELOG.md`. When every lane added a bullet under `## Unreleased`, any two
lanes in one train conflicted on that hunk at compose, and the train dropped
one of them. No two lanes write the same file under `changes/`, so their
notes always merge. The tree-wide audit `tests/test_change_notes.py` is red
when a lane writes a line under `## Unreleased`, and it names the line. At a
cut, `scripts/release/release.py <version> --fold` moves every note into the
version's section of `CHANGELOG.md` in one commit
([CONTRIBUTING](../CONTRIBUTING.md#releasing)).

## Trains and the land gate

The land gate is **one whole suite per landing window** (one trunk head), on
the exact tree that lands: sliced while the canary stands, serial otherwise.

- `helm train` lists the approve-ready rows, their reviewed tips and the
  merge order. `helm train --apply` merges each reviewed tip by its exact sha
  into one detached compose room, refuses a conflicting row by name, and
  launches the gate. It merges and does not cherry-pick, so each reviewed sha
  becomes an ancestor of trunk.
- `helm gate window launch` is the durable road to that gate. It records the
  window before it dispatches, refuses a second whole suite on the same
  window, and leaves a detached client that fetches and imports the receipt.
  A green receipt on a stacked train's top car lands every car beneath it.
- **Land provenance** means a land takes only a whole-suite receipt whose
  origin helm can prove. `helm gate provenance` prints its state.
- `helm train auto` drives one train at a time by itself: it posts its
  intent with a veto window, then composes, gates and lands.
  `helm train auto --status` shows its state.

### Inside a whole-suite run

`helm gate run` binds the interpreter, the exact tree, before-and-after
cleanliness, the process exit and the unittest summary into one receipt.
Expensive whole-suite runs are kept honest by these mechanisms:

- **One ordered queue per repository.** A run first enters a process-owned
  repository FIFO. Every linked worktree shares one ordered slot, poll speed
  cannot barge the queue, dead positions are skipped visibly, and the slot
  stays held until the receipt is appended.
- **Compatibility with older runners.** During a mixed-version rollout, the
  FIFO head also holds the legacy `gatelock:<project>` mutex. It waits a
  bounded time through transient claims-lock contention, refreshes the mutex
  strictly, and validates its exact lease while it appends the receipt, so an
  older or manual runner cannot overlap it. Malformed strict claim state
  refuses rather than reading as empty.
- **Every descendant is accounted for.** A Linux child subreaper waits behind
  a one-byte launch barrier until its exact PID is bound into the FIFO row. A
  dedicated guard enters a delegated per-position cgroup before it spawns the
  inner supervisor, so every suite descendant inherits one kernel-owned
  membership without changing the PID namespace the tests validate.
  `cgroup.kill` removes the whole membership despite `setsid`, including when
  the guard and the supervisor are killed at the same time.
- **Dead launchers are cleaned up.** A pipe-free watchdog resumes a stopped,
  bound supervisor at once when the exact launcher generation dies, remains an
  unreaped zombie, or stops long enough that it cannot renew the
  compatibility lease. Its argv carries the immutable position token, so a
  later dead-launcher sweep can recover even a stopped supervisor that was
  never bound. A watchdog outside the cgroup kills and removes it after the
  launcher or the guard dies, and unrelated launcher children stay outside
  the cleanup.
- **Bounded shutdown.** The inner supervisor freezes a fork-capable detached
  tree before it kills it, a stopped supervisor is resumed into that cleanup
  path, and pipe collection stays bounded after shutdown.

Queue wait is outside the child timeout. Custom diagnostic commands stay
unqueued and cannot bind a verdict.

## Land requests

`helm lr` is the queue between "reviewed" and "on main": which tips are
waiting, which have receipts, which reader owes a verdict, and which recorded
proofs no longer resolve.

- `helm lr list` and `helm lr show <id>` are the views; `helm lr stalls`
  shows the rows past each stage's STALLED threshold.
- `helm lr foldcheck <tip>` runs the five-rung check on a landed head, and
  with `--apply` closes the source-clean holds that the land carried.
- Git-backed proofs are bound to commits, and a commit id is
  content-addressed over history. `helm lr refs` audits what a history
  rewrite broke, and `helm lr migrate` translates it into a sidecar that never
  rewrites an attested tip.
- When the exact reviewed commit is truly gone and no proof can be
  recovered, `helm lr abandon` writes that reviewed work off explicitly as
  **ABANDONED — LAND STATE UNKNOWN** instead of inventing a land.
- `helm lr close` can also close an open BUILD row for a delivered non-code
  report, when a typed artifact reference, a full chat row id and short
  handoff evidence are recorded. That close claims no review and no Git land.

[VERBS](VERBS.md) has the full grammar of every verb on this page.
