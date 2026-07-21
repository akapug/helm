# Attested premises — the native chain (+ optional anchor)

A premise is a confidence-1.0 truth: human-stated, entirely load-bearing (the
store's belief clamp keeps agent-held beliefs at ≤0.99 forever, so the
confidence-1.0 set is *exactly* the operator's stated truths — see
[CONCEPTS.md](CONCEPTS.md)). Because premises are standing, provable claims,
helm can do better than keeping them in files: `helm premise` also records a
tamper-evident digest of each one into a **native, append-only hash chain**,
and `helm premise-check` re-verifies it on demand — offline, with no external
service.

## The default is: no node, no binary, full function

Start here, because it is the posture a fresh clone is in and the posture it
stays in: **helm attests entirely by itself, with stdlib only.** There is no
meld binary, no bundled service, and no network dependency. Capture writes the
premise to the typed store AND appends a native attestation record; both land
offline. A dregg node, if one happens to be reachable, is an *optional external
checkpoint* — never required, and never allowed to *prevent* capture: it is a
**bounded best-effort on the capture path** (a small timeout, ~2s, overridable
with `HELM_NODE_ANCHOR_TIMEOUT`); on any delay or failure the native record
still stands and the anchor is queued. Attestation is a first-class helm
feature, not an upgrade you have to install.

## Native-primary — and why it is honest

The primary proof is a **local hash chain**, not a signature from a remote
service. This is a deliberate honesty choice:

- An agent-run capture cannot be signed by the owner's cell — only the owner,
  capturing with their own key, could do that.
- The stdlib-feasible dregg ingress signs turns with the **node operator's**
  cell, not the user's. Labelling those "the user's cell signed this premise"
  would be false.

So helm does not pretend. The proof is the **quote plus the chain**:
provenance (owner said X, at T, source) bound into a record whose hash links
to its predecessor. That verifies offline and is the primary evidence. When a
dregg anchor is present it is labelled exactly for what it is — *"dregg node
`<url>` anchored digest at turn `<hash>`"* — a node-anchored external
checkpoint, **never** a user-cell signature.

## The native chain

`<helm-home>/_global/.state/attest-chain.jsonl` — one JSON record per line,
append-only. Each record's tamper-evident **core** carries:

| field | meaning |
|---|---|
| `v` | schema/evidence version (`2`) |
| `op` | `create` \| `supersede` \| `retire` |
| `premise_id` | the entry id |
| `root`, `project` | provenance root (global/project/adopted) + project name |
| `digest` | the canonical statement digest (`prem:b2b:<blake2b-256 hex>`) |
| `ts`, `source` | capture timestamp + source (`human`) |
| `attest_by` | the **recording label** (profile/agent) — provenance, *not* a signer |
| `supersedes`, `supersedes_record` | the superseded id + its record hash (a supersede op links the chain) |

and then the structural fields: `prev` (the predecessor record's `rec_hash`,
`""` at genesis), `rec_hash`, and `chain_index`.

```
rec_hash = blake2b256( canonical_serialization(core) + prev )
```

Reorder, insert, or edit any record and every subsequent `rec_hash` fails to
recompute — that is the tamper evidence. A local chain is tamper-evident
**relative to a retained head**: an attacker who controls the entire local
history could rewrite both the records and the head, so git, another retained
copy, or the optional dregg anchor supplies the external commitment.

Environment (each `HELM_*` wins; the legacy `MELD_*` name is accepted
READ-ONLY as a migration fallback — the full table is in
[ENVIRONMENT.md](ENVIRONMENT.md)):

| variable | meaning | default |
|---|---|---|
| `HELM_NODE_URL` | OPTIONAL dregg node for the external anchor | `http://127.0.0.1:8899` |
| `HELM_NODE_TOKEN` | OPTIONAL bearer for a gated node | — |
| `HELM_NODE_ANCHOR_FEE` | computron fee budget the anchor turn stamps — dregg charges the `EmitEvent` a real cost and REFUSES a turn whose `fee` is below it (a `fee: 0` submit **never commits**), so this matches dregg's `DEFAULT_ANCHOR_FEE` | `1000` |
| `HELM_NODE_ANCHOR_TIMEOUT` | bound (seconds) on the optional anchor POST on the capture path | `2` |
| `HELM_CELL_PROFILE` | the recording label (provenance, not a signer) | `helm-test` for attestation |

> **Honest note on the live anchor.** With `fee: 0` dregg's executor rejects the
> anchor turn (`computron budget exceeded`), so helm now stamps dregg's supported
> fee (`1000`). Whether a turn actually *commits* still depends on the node: the
> submitting operator cell must hold enough computrons (funded once via the node's
> faucet at devnet bring-up), and a node may reject an anonymous/advisory-agent
> anchor outright. A live, funded-cell commit is **not proven in this
> environment**; the fee fix removes the guaranteed-underfunded rejection, and the
> native chain remains the primary proof regardless.

## Who "recorded" — the provenance label

`attest_by` is a **provenance label**, not a cryptographic signer. With no
profile configured it is the test label `helm-test` — visibly not the user.
The owner's own label rides when `HELM_CELL_PROFILE` is set to their profile.
It records *who vouched* for the entry; it never claims a cell signature the
native chain does not carry.

## Capture → digest → record → check

**Capture** (`helm premise <id> | <statement>`):

1. The premise is written to the typed store — same path and byte shape as
   `helm store add premise`.
2. The statement is **canonicalized**: Unicode NFC → double quotes become
   single (mirroring the store's own serialization, so a digest recomputed
   from the stored file always matches) → whitespace runs collapse to one
   space → trimmed.
3. The **digest** is computed, algorithm-tagged: `prem:b2b:<blake2b-256 hex>`
   — self-describing and upgradeable (a future `prem:b3:` payload coexists;
   old records stay verifiable).
4. A **native record** is appended to the chain (`op: create`), linked to the
   current head. This is the primary proof and always lands, offline.
5. The entry is annotated with `attest_payload`, `attest_ts`, `attest_by`,
   `attest_record` (the native `rec_hash`), and `attest_chain_index`.
6. **Best-effort:** if a dregg node is reachable, the record hash is posted to
   its thin ingress as an external anchor; on success the entry also gains
   `attest_anchor` (the honest label) and `attest_anchor_turn`. On failure the
   native record still stands and the anchor is queued for retry.

**Check** (`helm premise-check <id>`) reports the evidence tiers *separately
and honestly*:

| tier | meaning |
|---|---|
| `digest MATCH` / `MISMATCH` | the stored statement still hashes to the attested payload (payload binding) |
| `native chain VERIFIED` / `BROKEN` | the record recomputes, links to its predecessor, AND its **hashed fields bind to this exact premise** — premise id, canonical statement digest, operation, root/project, supersession link. A record that is internally valid but commits a *different* claim (a foreign premise's record, or the old record after the statement changed) reads `BROKEN`, never `VERIFIED` (the **primary** proof) |
| `native chain NOT ATTESTED` | no native record exists (captured with `--no-attest`, or a legacy entry) — the primary proof is *absent*, distinct from present-but-broken |
| `external anchor turn OBSERVED` | a reachable node still shows a turn with the stored hash **exists** — this does *not* prove that turn commits this record's hash (`attest_anchor_turn` is mutable frontmatter, swappable for any real turn), so it is turn-existence only, never independent re-verification, until dregg exposes payload disclosure |
| `external anchor unverified` | node unreachable or the turn is not found — the native proof still stands |
| `external anchor none (native-only)` | no external checkpoint was ever taken |

**Exit contract** (so automation can never mistake absence of the primary proof
for success):

- `0` — VERIFIED: the digest matches AND the native record recomputes AND binds
  to this exact premise.
- `1` — BROKEN: a record is present but the digest mismatches, the record is
  tampered, or it commits a different/foreign claim.
- `3` — NOT ATTESTED: the primary (native) proof is *absent* — `--no-attest` or a
  legacy entry with no record hash. A deliberate informational state, on its own
  non-success exit so it is never confused with a verified premise.

The anchor is never allowed to fail the check — it is a bonus, not the basis.

## Queue and retry (the OPTIONAL anchor only)

The native record is the proof and always lands, so nothing is ever "pending"
about the primary attestation. Only the **external anchor** can be pending:
when no node is reachable, an anchor row is appended to
`<helm-home>/_global/.state/attest-queue.jsonl`. `helm premise --retry-queue`
re-attempts the anchor once a node appears — each success annotates
`attest_anchor*` and drops the row; failures (and rows whose entry has since
left the store) stay.

## Backfill — recording the corpus that predates attestation

Entries captured before the native chain existed (the adopted corpus included)
get a record appended **in place**:

- `helm premise --attest-existing <id> [--project P]` — digest per the same
  contract from the entry's *current* stored statement, one native record,
  then annotation only: the byte diff is exactly the `attest_*` lines,
  wherever the file lives. Never a rewrite, never a twin, never through `add`.
  Only live confidence-1.0 entries qualify.
- `helm premise --attest-sweep [--dry] [--limit N]` — every live certain entry
  lacking a native record, across all roots. Offline and **free**: no
  computrons, no faucet, no bearer token. `--dry` reports the count and writes
  nothing. A best-effort anchor is attempted per entry (fail-open, queued when
  no node answers).

## Supersession is a chain, not an edit

Superseding an attested premise appends a **native supersede record** whose
`supersedes_record` points at the prior premise's record hash — append-only,
mirroring the store's record law (files are kept; status flips). Belief
history thereby becomes a provable chain — *held X until T, then Y* — which the
drift report reads as attested belief evolution rather than lost history.

`helm premise --supersede <old-id> <new-id> | <statement> [| keywords [|
domain]]` does all three legs in one verb:

1. **Store** — the new premise is captured and the old one is tombstoned
   through the store's own lifecycle (`status: delete_eligible`, the existing
   `replaced_by`/`supersedes` backpointers — zero schema change, the file
   stays). Local; lands with no node.
2. **Native record** — ONE `op: supersede` record links `supersedes_record` to
   the old premise's `rec_hash`; the new entry's frontmatter carries
   `attest_supersedes_record`. Also offline, always lands.
3. **Anchor** — best-effort external checkpoint, exactly as for a plain
   capture. A never-attested predecessor is stated honestly — the chain starts
   at the new premise.

An edit that would orphan an attestation is refused at capture: re-stating a
LIVE attested premise with a *different* statement points you at `--supersede`
(an identical re-statement is recognized as already attested and records
nothing new).

**Reading the chain.** `helm premise-check --chain <id>` walks the chain
through any link (back via `supersedes`, forward via `replaced_by`),
re-verifies every digest, every record's native-chain integrity, and every
hop's linkage, and prints the attested biography — *held X until T, then Y*.
Exit 0 means every digest matches, every record verifies, and no link is
broken; a store-only (unbacked) hop prints loudly but is a stated design
state, not corruption.

**Drift reads it too.** A superseded premise whose native linkage verifies
(offline — the drift path never calls a node) reports as `EVOLVED … attested
chain`, exactly once per hop; a store-only supersession reports as unbacked.

The store's lifecycle writers carry the `attest_*` keys through rewrites
(evidence, retire) — a lifecycle update never orphans the entry's pointer
keys. The attestation truth lives in the native chain; the file keys are the
convenient pointer back to it (record hash, chain index), and `verify_chain`
recomputes the whole ledger from `attest-chain.jsonl` alone. The normative
spec for the digest + record contract is the `helm/premise.py` docstring.

## Sequencing: git history vs the external anchor

Two tamper-evidence layers serve two different moments, deliberately:

- **Solo-operator history** wants git and the native chain: the supersession
  history — *who believed what, when* — rides content-addressed commits and a
  local hash chain with zero daemons. For one operator on their own machines,
  that is the right weight, and it is what ships by default.
- **Second-party proof** is what the optional dregg anchor adds: the day a
  belief must be proven *to someone else* (cross-machine finality), an
  external commitment to the chain head matters. That anchor is honestly a
  *node* commitment, not a user signature — and it is strictly optional, so
  helm runs fully without it and takes it only when it pays.

The digest + native-chain check in `helm doctor`/`premise-check` stays
regardless — it catches accidental drift cheaply, offline.

## Chat rides the same digest shape

`helm chat` v2 reuses the same algorithm-tagged digest shape
(`chat:b2b:<blake2b-256>`) for groupchat rows. Chat's optional signed-row
transport is a separate, explicit opt-in and is **not** the attestation path;
premise attestation needs no binary and no node.
