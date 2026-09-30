# The review door: when a review chain becomes a meld

Review in helm is organized around melds. A chain of review rows that keeps
going is a sign that the two sides have not agreed what counts as done. The
review door moves that chain into one live meld that agrees the **bar**, and
it uses the rows for everything else.

The code is in `helm/review_door.py`. The round count comes from the same
fold the stop rung reads (`dispatches.chain_rounds` over `_spiral_fold`), so
the door and the rung cannot disagree about how many rounds a chain has.

## The bar

A meld's output is the bar, not a patch. The meld agrees four things:

1. **Blocking harms.** The closed list of harms that block the lane.
   Everything else is a note, filed as remainder.
2. **Falsifiers.** The closed set of falsifier classes the next read may
   use. A new class goes back to the same meld room, never to a new round.
3. **Findings.** What happens to each open finding: `cured-in-patch`,
   `inside-bar`, `note` or `refuted`; `none` when no finding is open.
4. **Tip.** The exact tip both sides will judge.

Candidate findings are researched on the row only after the bar is agreed.

## Triggers

| Trigger | When | What the door does |
|---|---|---|
| T0 | The first build row of a chain, when the brief names an irreversible target (a prod write, a drop or migration, a deletion, a credential boundary; the closed phrase set is `IRREVERSIBLE_PHRASES`) | Refuses the row until it cites an AGREED design meld with `--meld ROOM`, or records `--async-because REASON`. |
| T1 | Round 2, when the read it continues carries a DESIGN finding (`--design-finding` on the FIX, or `--no-patch-because` without a validated MELD-DIFF `--diff-handoff ROOM/MSGID`), or the author `--disputes` a finding | Prints the meld invite for those findings only. It never opens a meld or holds back a mechanical cure. A validated typed diff receipt suppresses the no-cure nudge; prose alone, including historical no-receipt rows, does not. |
| T2 | Round 3 or later | Opens the bar meld itself when the reader has a live beacon and no open row in flight. Otherwise the BAR rides the row as its brief. The row is always written. `--async-because REASON` skips the meld and is recorded. A chain that reads FINISH (recorded counts strictly falling, newest findings regressions of the previous cure) is exempt: no meld, and the row carries a one-line note saying why. It never stops work. |
| T3 | The chain reads UNDER-ARMED (every read found a defect the previous read's arms could not see) | The meld is a bar meld. The stop rung blocks with the bar invite instead of an advisory. |

A review `send` at chain round 3 or later prints and stores one convergence
whisper **only when it opens a new eligible review round**. Rebinds and
`retract --reissue` sends at an existing tip (fan-out), and closing re-sends
of adopted cures, do not get a fresh whisper. The whisper names
`xfam-reviewer-fixes-its-own-findings`,
`per-case-handler-spiral-cure-is-whole-object-or-refuse`, and
`xfam-review-never-blocks-a-reversible-land`. Its pair round's seed carries the
same block and asks the reader for the FULL falsifier set, including when the
pair uses a standing room. The count is the chain's review-round count, not the
room's epoch. A Codex-family review
records a `review_mode`: PATCH (reader commits a cure from the exact tip) or
MELD-DIFF (reader posts a precise fix in the meld). The choice alternates over
new chains sent to that reader across repositories and is inherited by later
reviews on that chain; the first hand-set task/3698 chain counts as PATCH
before the automatic alternation starts. The full brief and CLI receipt name
it. The T2 BAR refers to that mode rather than always ordering a reviewer
patch. A concurrent change to a chain's measured reading refuses a stale
preflight rather than writing a different meld plan; a door that failed open
made no plan and does not block the row. Generated guidance is stored in the
full brief, so it spends the brief byte ceiling, not the sender's argv cap.

**The mixed case.** A FIX can carry several mechanical findings and one
design finding (`--design-finding`). PATCH names the committed mechanical
cure with `--patch-tip`; MELD-DIFF posts the exact fix in the pair meld and
records `--diff-handoff ROOM/MSGID` with `--no-patch-because` while the author
applies it. Adopting the PATCH cure or sending the first advancing direct
child of a validated MELD-DIFF FIX is cure confirmation, not a new round;
a FIX on that child restores a real round. The door offers a design meld for
an explicit design finding only. Without a validated receipt, even on a
historical MELD-DIFF row, reason prose is a no-cure design nudge and the child
counts as an ordinary round.

## One reading for the door and the rung

The finding trajectory (MELD, FINISH or UNDER-ARMED) is read over the
chain's **answered** rounds only (`dispatches._answered_reading`). The stop
rung runs after a send, with the newest round in flight; the door runs
before it. Both read the same answered rounds, so they give the same answer
about the same chain. The round count still includes the round in flight.
`tests/test_review_door.py` `DoorAndRungAgreeTest` runs one chain through
both surfaces for each reading and asserts they agree.

## Exemptions

These add no round, so no trigger fires on them:

- the closing re-send at the reviewer's own adopted PATCH tip (task/3072),
  or the first advancing direct child of a MELD-DIFF FIX with a validated
  `--diff-handoff ROOM/MSGID` whose send/add also proves that it applied the
  diff (task/3713 D2). A read AT either cure tip that answers FIX is a real
  round. Historical MELD-DIFF FIXes with only reason prose, and unrelated
  children of receipted FIXes, count normally. At three counted
  rounds the door treats a closing re-send as the T2 point, as the stop rung
  does;
- a dispatch nobody answered, once a newer tip replaced it (task/2682). A
  chain with enough dispatches for a block and too few reads for rounds reads
  **UNREAD**: an advisory, never a meld. It names the fix: the reader records
  each read on its row with `helm dispatch verdict`, and an answer given only
  in chat does not count. Once the reads are recorded, the same chain reads
  MELD or FINISH as usual;
- one tip sent to several readers (it is one round);
- a chain whose bar a meld already AGREED (a finding outside the bar goes
  back to that room).

A reader who does not join a door-opened meld within the **entry window**
(`ENTRY_WINDOW_S`, 10 minutes; `HELM_MELD_ENTRY_WINDOW_S` moves it) is not a
spiral. The row carries the bar, and the stop rung stops walling the seat
for the reader's absence (store entry `melds-reach-some-seats-rows-reach-all`).

## What the meld hands back (the outcome)

Each party's last `[DONE]` carries one line:

```
MELD OUTCOME: AGREED|SPLIT|RESEARCH | BAR: <harms> | FALSIFIERS: <class>; ... | FINDINGS: <id>=<disposition>; ... | TIP: <full sha> | NEXT: <next action>
```

An AGREED block must name its FALSIFIERS: the closed set of falsifier
classes the next read may bind. A block that agrees the shape and leaves
that set open has not converged, so the parser refuses it by name, and two
AGREED blocks that name different sets are a SPLIT.

`helm chat meld say --marker DONE` asks the door before the round seals
(`review_door.done_refusal`). It reads the block with `parse_outcome`, the
one validator the citation uses, so a block that seals is citable: any
block that parser refuses is refused in every room, with every missing
field named and the line above printed. A round the door reads (a pair
meld, or a problem statement with a `(chain <id12>)` marker) also owes the
block. A round the door binds to a row (the row that opened a pair round,
or the rows of the chain its statement names) closes only on a tip that
row's record is about: its dispatched tip, or a commit that descends from
it. In any room, it refuses a block that names a dispatched row's tip when
the problem statement does not carry that row's chain marker, and prints
the invite that opens a round that binds. A refused `[DONE]` posts
nothing, so the round stays open; a design meld closes with no block, as
before.

The row's reader records the result at that tip. When the pair round has a
citable outcome, name its room (and `@EPOCH` when citing a named round):

- **D8, findings inside the agreed bar:** PATCH uses
  `helm dispatch verdict <row> <tip> --fix ... --patch-tip <sha> --meld <room> <evidence>`.
  MELD-DIFF posts the exact mechanical fix in the pair meld for the author
  to apply, then uses
  `helm dispatch verdict <row> <tip> --fix ... --diff-handoff ROOM/MSGID --no-patch-because "MELD-DIFF: author applies" --meld <room> <evidence>`.
  `ROOM/MSGID` identifies the reader's own exact unified diff post (file
  headers, hunk and changed line), carrying one `[MELD e:N]` marker for the
  pair round opened for this row, chain, reader and reviewed tip. The verdict
  validates it there and stores a typed digest-bound receipt; an unreadable
  chat, file:line suggestion, room prose or `--meld` outcome is not proof.
  Post through `helm chat meld say ROOM --marker YIELD` (or the pair-meld
  equivalent): its chat row records the generated marker separately. A bare
  inline ` [DONE]` in an older or hand-framed post is indistinguishable from
  literal added diff content, so it cannot prove a child; repost through the
  meld verb rather than guessing which bytes to remove.
  Both FIX forms still require
  `--finding-count` and `--prior-relation`. `--no-patch-because` explains
  why no reviewer patch was committed, but alone proves no cure and remains
  a T1 design nudge even for historical MELD-DIFF rows. A finding outside
  the agreed bar goes back to the meld, not onto this FIX row.
- **D9, no findings on the exact reviewed tip:** close with
  `helm dispatch verdict <row> <tip> --approve ... --meld <room> <evidence>`
  or `helm dispatch hold <row> <reason> --source-clean <tip> --meld <room>`
  where the source-clean hold is the applicable close. Do not invent a FIX
  or a patch merely to cite the outcome.

A citation is optional, not a prerequisite for recording a valid source-clean
hold. `meld_citation` requires an outcome block from each party, the meld
parties to include this row's **sender and recipient**, the outcome tip to
bind to the record, and a pair round opened for **this row** (exact-round
authority). Only an accepted citation writes `meld_room` and `meld_outcome`.
**D10 remains unresolved:** when the cure author differs from the row sender,
the cure author's pair meld does not by itself satisfy that sender/recipient
party check on an author-to-reader re-send. Do not promise that `--meld` will
work there; the reader may record a valid source-clean hold without a typed
meld outcome. Proving a binding citation for that mismatch belongs to the
separate task/3710 lane.

AGREED means the parties' blocks MATCH on the bar, every finding's
disposition, the tip and the next action (case, spacing and finding order
aside). Blocks that differ read SPLIT, and a citation records exactly the
room's verdict.

A meld exempts only the chain it was about, and it needs two proofs. First,
every tip the parties' blocks name must be one of the chain's dispatch tips
or reviewer patches. Second, the problem statement must name the chain:
every invite the door and the rung print carries `(chain <id12>)`, and a
statement with a marker binds only to that exact chain, never to the lane.
A statement with no marker binds by the lane only for a caller with no chain
id: T0's design meld, sent before the build row's chain exists, and a
legacy lane-keyed chain. T0's `--meld` is also bound to the build row's
author, reader and tip.

Only a meld where both sides spoke, every party's block matches AGREED, and
the meld was about the chain switches the spiral rung off. A meld the reader
left to research, a split, one side's DONE alone, a meld with no outcome
block, and a meld about other work do not.

A source-clean hold ends a round only when the row's reader made it: the
hold door stamps the holding seat (`hold_actor`) and refuses a clean claim
from anyone but the row's recipient, and the fold binds the same hand, so a
clean claim held by anyone else, or written before the stamp, settles
nothing. Rounds are
ordered by the ledger's append order, so two sends in one second are two
ordered rounds at the door and at the rung.

## Other levers in this lane

- **Lever 2.** The stop rung's meld suppression requires `exchanges >= 1`
  and the peer in `spoke_peers` (`meld.converged_with`), plus the AGREED
  outcome above.
- **Lever 6.** `dispatch verdict --fix` requires `--finding-count` and
  `--prior-relation`. Either may be the literal `UNKNOWN`, which the row
  records in `declared_unknown`.

## What each row records

| Field | Written by | Meaning |
|---|---|---|
| `meld_door` | send / add | The trigger that fired and the action (`refuse`, `cited`, `async`, `nudge`, `auto-open`, `brief`, `exempt`), with the round count and the reason |
| `async_because` | send / add | The sender's reason for skipping the meld |
| `design_findings` | verdict | Design findings named beside a FIX |
| `diff_handoff` | verdict | Validated `ROOM/MSGID` message receipt for a MELD-DIFF mechanical FIX; absent or invalid history proves no cure |
| `diff_application` | child send / add | Proof bound to the parent's FIX and receipt that the candidate child actually applies its diff; absent proof means an ordinary round |
| `declared_unknown` | verdict | Observation fields a FIX declared UNKNOWN |
| `meld_room`, `meld_outcome` | verdict, hold | The meld whose outcome this record carries |
| `meld_bytes` | verdict, hold | The meld room's size when its outcome reached the row: the pair meld's cost, kept after the room is retired |
| `meld_epoch` | verdict, hold | The round the citation read: `--meld ROOM@EPOCH`, or the newest round for a bare room |
| `hold_actor` | hold | The seat that held the row, stamped by the hold door; a source-clean claim answers a round only when it is the row's reader |

## The pair meld: one room per task

Every task runs as one mixed-family pair in one persistent meld (store
premise `every-task-runs-as-one-mixed-family-pair-in-one-persistent-meld`).
The code is the pair-meld section of `helm/review_door.py`; the round
mechanics are `meld.invite(room=...)`.

A WORKING PAIR THAT KEEPS A STANDING ROOM (task/3560, `helm chat meld
standing <peer>`, `helm/meld_standing.py`) runs every task through that one
room instead: when the row's sender and reader are both members of their
pair's `meld-0-standing-<a>-<b>`, a dispatch's round is one
`[STANDING-ROUND]` row there, no per-chain room below is minted, and a FIX
verdict's hand-back names the standing room. The standing room has no cap
and never blocks (docs/VERBS.md, `helm chat meld`). Everything below is the
per-chain pair meld, for pairs that keep none.

- **The first dispatch of any kind opens it.** The room is between the
  row's sender and its reader, and its first round is THE PLAN: the
  problem, its invariants and the acceptance checks; the split into
  genuinely independent work, and who owns the combined result.
- **One room per chain, by construction.** The name is a pure function of
  the chain: `meld-0-pair-<project>-task-<n>` when the chain's FIRST row
  names exactly one task in its lane or note, else
  `meld-0-pair-<project>-chain-<id12>`. The project is the one that owns
  the chain's repository, so a reviewer from another project is invited
  into the owner's room. A later round with a renamed lane stays in the
  room its chain opened.
- **Every later dispatch is a ROUND of it.** A FIX, the cure, the re-read,
  a rebind to a new reader, and the door's own T0, T1 and T2 melds each
  open the next round of the same room. A round is one meld epoch: the
  fence retires the rounds before it, the exchange cap of 5 counts this
  round only, and `helm chat meld join` prints the earlier rounds as a
  digest sized to the joiner's context window (1% of it, between 1 KB and
  8 KB; 1 KB for a seat with no window stamp) with the pointer to the whole
  log, never the log itself.
- **The row stays the ledger.** Verdicts, patch tips and the gate live on
  rows. The room carries the conversation and the MELD OUTCOME a verdict
  records with `--meld ROOM` (the newest round) or `--meld ROOM@EPOCH` (a
  named round); the row records `meld_epoch`, the round it read.
- **Exact-round authority, falsifier (h).** A round's seed names the
  dispatch row that opened it, and its outcome closes that row only: round
  N's agreement cited on round N+1's row is refused by name, with the epoch
  to cite instead. At the door a pair round's AGREED, recorded or converged,
  never exempts the next send, and at the rung a state vouches only while
  its round is the room's newest. A meld in a room of its own keeps its
  exemption.
- **The spiral rung counts a pair round only as a conversation.** It stops
  walling only when the chain's pair room holds a turn (a YIELD, HOLD or
  DONE chunk) from both the author and the chain's current reader inside
  the rung's window, and that reader's beacon is live. A round the dispatch
  opened and nobody spoke in never silences the rung, and a pair room never
  lapses the way a door-opened meld does.
- **One wake.** The round's invite row carries no @mention: the dispatch's
  own DM (or `add`'s mention) carries the room and the join command.
- **Never a wall for the reader.** A reader who never joins gets the row as
  before, and a round that cannot open leaves the row as the conversation.
  The stop guard names a pair meld only when its floor is the stopping
  seat's (`HELM_STOP_GUARD_PAIR`).
- **A same-family pair says so.** The pairing is read from the families the
  approval tier proves for each seat: mixed, SAME FAMILY (the room says it
  is not a mixed-family pair), or UNKNOWN.
- **Archival.** A pair room is a `meld-` room, so `helm chat retire-rooms`
  archives it after its idle bound like any meld, or a day after every
  member of its newest round closed its side. A round after that is reborn
  under the same name, and its history digest starts from that round.

The pair meld's falsifiers, measured by `helm dispatch melds`:

- **(a)** Fewer than 60% of 20 real tasks whose pair meld opened reach a
  typed AGREED on a row (`meld_outcome` agreed with `meld_room` naming the
  task's room). Under 20 tasks it reads UNMEASURED. The census also counts
  the rooms keyed by the chain-root fallback, and whether their first row
  named no task or two, which is what the task/N parse missed.
- **(b)** A round that opens a second room for the same chain. The name is
  derived from the chain's first row, never minted per opening.
- **(c)** A reader without a live beacon blocked or waiting on a meld.
- **(d)** An AGREED outcome with no falsifier set counted as converged.
- **(e)** A reader that goes dark strands the chain. A rebind invites the
  new reader into the same room, and the row stays owed.
- **(f)** A joiner handed the whole log instead of a digest bounded to its
  window.
- **(g)** Cost not measurable. The AGREED row records `meld_bytes`, and the
  census compares bytes per chain converged through a pair meld with bytes
  per chain converged on rows alone.
- **(h)** An AGREED from round N closes or exempts round N+1.

## Falsifiers

Two results would show that the shape is wrong. `helm dispatch melds
[--hours N] [--json]` measures both, read-only, from the ledger and the meld
rooms. A falsifier with no rows reads UNMEASURED, never HOLDS.

- **(a) T2's shape.** If fewer than 50% of the readers of door-opened melds
  join within the entry window, T2 opens melds that seats do not enter, and
  its shape is wrong. The census also reports the base rate for every meld
  convened in the window.
- **(b) T1's scope.** If T1 fires after an all-mechanical PATCH FIX or a
  MELD-DIFF FIX with a validated `--diff-handoff ROOM/MSGID`, no explicit
  design finding and no recorded dispute, T1's scope is wrong. A FIX with
  only `--no-patch-because` prose (including old rows) is no-cure evidence,
  so its T1 nudge is expected.

### First measurement

MEASURED on the live ledger and chat estate at the lane's build, window 168h:

- (a) T2: UNMEASURED (no door-opened meld yet). Base rate: 26 of 31 melds
  convened in the window had the reader join within 10 minutes (84%).
- (b) T1: UNMEASURED (T1 has not fired yet). Replay over the 205 review
  sends that continued a chain: T1 stays silent after 33 all-mechanical
  patch FIX verdicts, would offer a meld after 16 design-class FIX
  verdicts, and 134 sends continued a round with no FIX on it. This historical
  replay predates typed MELD-DIFF receipts; missing receipts are not inferred
  from the prose.

Re-measure with `helm dispatch melds` after the door has run for a week.

### PATCH / MELD-DIFF write guidance

Every review writer (`dispatch add`, `dispatch send`, recipient rebind, and
`retract --reissue`) chooses the Codex PATCH/MELD-DIFF mode against the
dispatch ledger snapshot under the append lock. The round-three whisper is
chosen there only for a new eligible review round at round 3 or later, never
for fan-out or an adopted-cure closing send. The mode, and any eligible
whisper, are recorded on the row and in the whole stored brief; a moved review
inherits its original full brief and replaces only the generated guidance,
rather than stacking duplicate instructions. Retries return the recorded row
without recomputing guidance or re-delivering it. The send operation hash
still binds the sender's original text, not the appended guidance.

### PATCH / MELD-DIFF trial metrics

The same read-only census folds each chain root carrying a recorded
`review_mode`. It reports the mode, the distinct-tip round where the chain
entered the trial, active rounds and FIX cure cycles for the mode-enrolled
reader only, and wall time from that reader's first enrolled send to the
chain's first accepted source-clean hold. Fan-out at one tip is one round; a
PATCH reader's adopted cure confirmation and the validated MELD-DIFF FIX's
first advancing direct child proved to apply its diff are not another active
round; a later independent reader's fixture cure is not charged to the mode reader. A historical FIX with
no typed receipt is not a MELD-DIFF confirmation even if its reason describes
a diff. A
retracted FIX counts as neither an active round nor a cure, but a prior
accepted source-clean hold remains an answer. The enrolled reader is a seat:
from the chain's first mode row on, a row sent to a seat the mode was recorded
for is its work even without a mode, so the FIX that `retract --reissue`
mints to the same reader is its cure. A release/re-hold, or a later
cancel of the held row, does not rewrite the first hold that stopped the
clock, and a later cancel of a mode-enrolled send does not rewrite the send
that started it. Cancelled rows retain their recorded mode and accepted
answers when selecting the mode, enrolled reader, and readable event kinds;
a chain with only cancelled rows remains in the trial. A hold the fold takes
without its source-clean claim
(owner-gated, or a tip it cannot read) stops no clock and answers no round.
A chain whose rows record two modes has no enrolled reader, so its mode,
rounds and cure cycles read `UNKNOWN`, never 0. A row carrying an event kind
this helm cannot read is not read in full: on the mode reader's row, the rounds
and cure cycles read `UNKNOWN`; on any row, the first send->hold reads
`UNKNOWN`.

State and accepted events come from one coherent dispatch-ledger snapshot. A
source-clean hold records the stop even when a separate pair meld cannot be
cited on that row; the census never opens the room and never infers a typed
meld outcome from it. Reviewer and author token totals remain explicit
`UNKNOWN` until token evidence binds to a dispatch or chain. `brief_bytes` and
`meld_bytes` are cost measures, not token counts, and are never substituted.
