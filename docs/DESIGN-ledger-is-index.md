# The ledger is an index, not a store

**Status:** design record — decides, does not implement.
**Author:** kimi (seat). **Date:** 2026-08-02.
**Evidence base:** close-ladder, window-5 phantom, landed-card grouping,
the stored-went-stale finding, first measured on the 470-row ledger, re-measured at 802 rows on review (2026-08-02).

---

## The thesis, and its limit

Some ledger fields duplicate what git derives live. Storing them is not
free: a stored value is a claim that can go stale, and tonight produced three
independent instances of a stored form contradicting the derived truth —
window-5 (a tip moved under the verdict that named it), the landed-card
phantom (a diff against a moved trunk showed a file the lane never touched),
and #91 (a stored form stale where the derived-fresh form was correct). The
thesis is: **stop storing what git can derive, and derive it on read.**

The limit is equally measured, and it is the part the naive version of this
lane would get wrong: **the ledger's reason to exist is to record what git
cannot.** A verdict's polarity, the evidence prose, the stated reason, the
timestamp, the declared chain, the signature — none of these is in git, and
deriving them from git is the error in the other direction (close-ladder:
the human's decision lived in verdict *prose*, and a gate that only knew the
polarity *field* could not see it). So the design is not "derive everything."
It is a precise partition, and the partition line is: **git owns facts about
code; the ledger owns facts about decisions.**

## 1. The inventory

### Git-derivable — candidates to demote, with the staleness that proves it

| field | derived by | the staleness evidence |
|---|---|---|
| `tip`, `reviewed_tip` | `cat-file`/`rev-parse` | window-5: a rebase moved the tip under a bound verdict; the stored value pointed at the pre-rebase commit while the work had moved. |
| `tree` | `cat-file <tip>^{tree}` | same class — a stored tree goes stale on rebase. |
| `file_set` | `diff --name-only BASE...TIP` | the window-5 phantom: a stored/stale base made `web_ui.html` appear in the lane's diff when the lane never touched it. |
| `ancestry` | `merge-base --is-ancestor` | derives live; storing it freezes a relationship that moves. |
| `patch_id` | `git show` + diff | derivable but **expensive** (~5ms) — see cost. |
| `delivery_ref` | a sha | derivable. |
| `ref` | — | **special: polymorphic.** It is a build base on `--kind build` and a reviewed tip on `--kind review`. It is not one fact; it is two, and the row does not say which. hc2's landed-card finding: indexing it blindly let build rows dispatched *from* trunk claim trunk as their chain's tip. This field is not a candidate to derive — it is a candidate to **split or annotate**, because its ambiguity is the defect. |

### Irreducible record — must keep, and why git cannot hold them

| field | why git cannot derive it |
|---|---|
| `polarity` | the human's verdict. close-ladder's whole point: the decision is ALSO in the prose, and the chain can hold a declared polarity the row's own field never reached. The field is irreducible AND insufficient — but the answer there was to read the *chain*, not to derive from git. |
| `verdict_ref` | the evidence prose — the human's words. Not in git at all. |
| `reason`, `close_reason` | the stated reason. A decision, not a fact about code. |
| `ts` | the ledger's own event time. Git knows commit time; it does not know "when helm recorded this verdict." |
| `chain_root`, `supersedes` | the **declared** work identity. Git has no concept of a helm chain. This is the ledger's core job. |
| `gate` | a receipt id. The receipt is the record; the field is a pointer to it. |
| `sender`, `recipient`, `lane`, `kind` | helm-level semantics. |
| `message_hash`, `operation_key` | idempotency keys — the duplicate-suppression mechanism. |
| signatures | the attestation itself. |

## 2. The migration shape — derive-on-read, stored demoted to a hint

**Not** dropping columns, and **not** a schema-version bump on day one. The
shape is a *read path change* with the stored value kept as a **hint**:

- The reader derives the git-derivable fields **live** and treats the stored
  value as a hint that can be checked *against* the derivation. A stored
  value that disagrees with the derivation is not silently overridden — it is
  the `#91` signal made visible: **the row moved** (rebase, rewrite), and the
  disagreement is the evidence. This is the window-5 discipline generalized:
  the phantom was *invisible* until the FF check caught it; a derived read
  makes every such move a named UNKNOWN, not a silent wrong answer.
- `ref` is the exception that proves the design: do **not** derive it —
  split it. A build row records `base`; a review row records `reviewed_tip`.
  The polymorphism is the defect (19 ambiguous tips on the live ledger, one
  under six roots, measured). New rows write the split fields; old rows read
  `ref` as the legacy union, and a tip that maps to more than one root is
  **ambiguous** and renders absent (the singleton rule), never a guess.
- The stored columns are dropped only after the read path has run derived for
  a full cycle and the hint has been shown to agree everywhere it is still
  written. Deleting the record of a disagreement before the read path is
  trusted would destroy the very evidence the design exists to surface.

## 3. Failure semantics — repo unreachable is UNKNOWN, never fabricated

This is the ladder's existing discipline and the spec inherits it unchanged:
a derive-on-read that cannot reach the repo returns **UNKNOWN**, and the
caller must treat UNKNOWN as a refusal, never as the stored hint's value and
never as an empty answer. The failure modes, each with its own honest
reading:

- **repo unreachable / object missing** → UNKNOWN (the fact is
  underivable; say so).
- **stored hint disagrees with derivation** → the row MOVED. This is not an
  error and not a pass — it is the signal, and it must surface the same way
  the window-5 FF check surfaced the phantom: loudly, by name.
- **stored hint agrees** → cheap confirmation; the derivation is the answer.

## 4. Cost — measured on the live ledger before proposing it

Derive-on-read is cheap enough at the operations that matter, with one
exception to watch:

| operation | cost (n=50, live repo, median) |
|---|---|
| `cat-file -e` (exists) | 1.6 ms |
| `rev-parse --verify` | 2.0 ms |
| `merge-base --is-ancestor` | 1.9 ms |
| `diff --name-only` (file set) | 2.3 ms |
| `git show` (patch material) | 4.5 ms |

(Re-measured 2026-08-02 21:25Z by the author on review: the first table's
is-ancestor read 7.8 ms, the reviewer's re-measure 2.6, the author's 1.9 —
and a third sampling settled what that spread IS: run-to-run noise of ~0.5
ms, not an estimator or operator disagreement (the reviewer's own re-test
found his median and mean identical). The 2.6/1.9 figures are one number
sampled three times; only the original 7.8 is a true outlier, 3-4x the
rest, and its cold-cache explanation stays plausible rather than proven —
neither seat can reproduce it. The old arithmetic also never closed:
9.6 ms x 470 is 4.5 s, not the ~1.9 s the text claimed, because only two
of the five operations feed that derivation and the wrong sum was used.)

Deriving `cat-file` + `is-ancestor` per row for all **802** logical rows
(the ledger grew 70% between this spec's first draft and its review) is
**~2.8 s** —
acceptable for a board read, but **not** for a hot polling path (the
dashboard's 12s deadline, which the landed-card perf arm already proved a
26.4s projection read can blow). The spec therefore does **not** propose
deriving everywhere: derive **at the decision points** (close, verdict-bind,
land) where the staleness actually bites, and keep the stored value as the
read-optimized index for display, re-deriving only where a decision depends
on the fact being current. **patch_id is the cost to watch**: it is the most
expensive derivation and the one most often needed only at land time, so it
stays derived-at-land, not derived-on-every-read.

The decision points this licenses include, explicitly (integrator note,
2026-08-02): **the verdict-bind read.** A verdict citing a chain-tip
receipt that *contains* its reviewed tip requires a **live ancestry
derivation** (`merge-base --is-ancestor reviewed_tip chain_tip`) to prove
same-chain containment — that is a decision point this spec already licenses,
and it is exactly the case where a stored ancestry would go stale (the chain
moved after the review). The receipt must also postdate the review, so a
stale receipt cannot bind a newer verdict.

## 5. What this decides

1. **Partition the ledger by what git owns.** Git owns code facts (tip,
   tree, file set, ancestry, patch-id); the ledger owns decision facts
   (polarity, prose, reason, time, chain, signature). Derive the first at
   decision points; keep the second as the record.
2. **`ref` is split, not derived.** `base` for build rows, `reviewed_tip`
   for review rows; the legacy union reads with the singleton-ambiguity rule.
3. **The stored value is demoted to a hint, and a hint that disagrees with
   the derivation is the `#91` signal made visible**, not silently dropped.
4. **UNKNOWN, never fabricated**, on any underivable read — the ladder's
   existing law, unchanged.
5. **Derive at decision points, not everywhere** — cost forces the
   read-path/index split.

## What this does NOT decide (the build lanes that fall out of it)

- The per-field migration order and which decision point moves first
  (close is the natural first, since close-ladder already reads the chain).
- Whether `ref`'s split lands as two new columns or one annotated field.
- The receipt side (whether gate receipts adopt the same hint-vs-derived
  split for their own tree/sha fields).
- Any code. These are separate build rows, each gated on this spec's approval.
