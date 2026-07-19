# Attested premises — the substrate leg

A premise is a confidence-1.0 truth: human-stated, entirely load-bearing (the
store's belief clamp keeps agent-held beliefs at ≤0.99 forever, so the
confidence-1.0 set is *exactly* the operator's stated truths — see
[CONCEPTS.md](CONCEPTS.md)). Because premises are standing, provable claims,
helm can do better than keeping them in files: `helm premise` also commits a
signed digest of each one to a verifiable ledger, and `helm premise-check`
re-verifies it on demand.

## The default is: no substrate, full function

Start here, because it is the posture a fresh clone is in: **helm works fully
without the attestation substrate.** Capture stores the premise in the typed
store either way; a missing binary or unreachable node is one informative
line ("attestation pending (substrate unavailable)") plus a queued retry —
never a traceback, never a lost premise. Every other helm surface is
independent of this leg. Attestation is an upgrade, not a dependency.

## The moving parts

- **The ledger node** — a small append-only verifiable ledger running as a
  local service (default `http://127.0.0.1:8899`). It orders signed turns
  into a receipted chain and answers two read surfaces helm uses:
  `/api/receipts` (the chain head) and `/api/turn/<hash>/status` (one turn's
  finality). helm never talks to it directly for writes.
- **The client binary** (`meld`, from helm's substrate-lineage predecessor —
  see [ATTRIBUTION.md](../ATTRIBUTION.md)) — signs and submits turns. It is
  **not bundled** with helm: point `HELM_CELL_BIN` at a build, or put `meld`
  on your `PATH`. `helm cell` wraps its verbs
  (`join|accept|send|recv|heartbeat|roster`) with helm-named env mapped in;
  `helm cell status` is the one-stop liveness check.
- **The identity profile** — a keypair under `~/.dregg/profiles/<name>.json`,
  named by `HELM_CELL_PROFILE`. Its **cell** (the ledger identity derived
  from the public key) is minted on first use by `helm cell join` and cached.

Environment (each `HELM_*` wins; without it the binary sees its own legacy
variable — the full table is in [ENVIRONMENT.md](ENVIRONMENT.md)):

| variable | meaning | default |
|---|---|---|
| `HELM_CELL_BIN` | the client binary | `meld` on PATH |
| `HELM_NODE_URL` | the node's HTTP API | `http://127.0.0.1:8899` |
| `HELM_NODE_TOKEN` | bearer for the node's submit endpoint | — |
| `HELM_NODE_PASSPHRASE` | unlock alternative to the token | — |
| `HELM_CELL_PROFILE` | the signing identity | `helm-test` for attestation |

## Who signs — the identity law

Confidence 1.0 is human-only by construction, so the attestable set is
precisely the operator's stated truths — which makes **the operator's own
cell** the semantically exact signer, not just the warm choice. Two rules
follow:

- An agent- or test-run capture must never impersonate the operator. With no
  profile configured, premise attestation signs as the **test profile
  `helm-test`** — visibly not the user's cell. (Cell passthrough verbs keep
  the binary's own default, `meld-agent`.)
- The operator's cell activates on their **first personal capture** with
  `HELM_CELL_PROFILE` set to their own profile — `helm cell join` mints the
  cell on first use, by design. From then on, their premises are their
  signature.

The signing profile is recorded on every entry as `attest_by`, so a check
always shows who vouched.

## Capture → digest → check

**Capture** (`helm premise <id> | <statement>`):

1. The premise is written to the typed store — same path and byte shape as
   `helm store add premise`. This step never depends on the substrate.
2. The statement is **canonicalized**: Unicode NFC → double quotes become
   single (mirroring the store's own serialization, so a digest recomputed
   from the stored file always matches) → whitespace runs collapse to one
   space → trimmed.
3. The **digest** is computed, algorithm-tagged:
   `prem:b2b:<blake2b-256 hex>` — 73 ASCII bytes, self-describing and
   upgradeable (a future `prem:b3:` payload coexists; old attestations stay
   verifiable).
4. The digest rides a **self-write turn**: the profile sends it to its own
   cell through the proven send path (the whisper payload slots — never the
   tiny heartbeat tag; that is a decision-record law). The ledger orders and
   receipts it. Thin claim, fat corroboration: the ledger holds only the
   digest; the text stays in the store.
5. The entry is annotated with `attest_payload`, `attest_ts`, `attest_by`,
   `attest_turn`, `attest_receipt`, `attest_chain_index`.

**Check** (`helm premise-check <id>`): recompute the digest from the *stored*
statement, compare it to the attested payload (MATCH/MISMATCH — a mismatch
means the stored text no longer says what was attested), then ask the node
for the turn's status and quote the **finality tier it proves**:

| tier | meaning |
|---|---|
| `attested-after-next-height` | consensus-final at an attested height — the strong tier |
| `ingress-immediate` | receipted on the node, not yet consensus-final |
| `unverified (node unreachable)` | the store-side digest check still ran; finality unknown |
| `unverified (turn not found on the node)` | no ledger corroboration for the recorded turn |

Exit status follows the digest: 0 on MATCH, 1 on MISMATCH.

## Queue and retry

When the substrate is down at capture time, the attestation is appended to
`<helm-home>/_global/.state/attest-queue.jsonl` and the premise reports
"attestation pending". `helm premise --retry-queue` replays it: each success
annotates the stored entry and leaves the queue; failures (and rows whose
entry has since left the store) stay queued. Nothing expires.

## Backfill — attesting the corpus that predates attestation

Entries captured before the substrate leg existed (the adopted corpus
included) are attested **in place**:

- `helm premise --attest-existing <id> [--project P]` — digest per the same
  contract from the entry's *current* stored statement, one self-write turn,
  then annotation only: the byte diff is exactly the `attest_*` lines,
  wherever the file lives. Never a rewrite, never a twin of an adopted entry,
  never through `add` (so the supersede-guard cannot trip). Only live
  confidence-1.0 entries qualify — the attestable set is exactly the
  operator's stated truths.
- `helm premise --attest-sweep [--dry] [--limit N]` — every live certain
  entry lacking a recorded turn, across all roots, sequentially. `--dry`
  reports the certain-set count and a computron estimate. The sweep unlocks
  the node **once** and rides the minted bearer for every send (the unlock
  endpoint rate-limits 5/60s and counts successes), auto-refuels via the dev
  faucet on an insufficient-balance refusal, retries each failure once, and
  lets a persistent failure fall to the attest-queue — it never crashes.
  Already-attested entries are skipped (idempotent), and queue rows made
  stale by a direct attestation are pruned at the end of the pass.

## Supersession is a chain, not an edit

Retiring or superseding an attested premise is a **new signed turn**
referencing the prior one — append-only, mirroring the store's record law
(files are kept; status flips). Belief history thereby becomes a provable
chain — *held X until T, then Y* — which the drift report can read as
attested belief evolution rather than lost history.

The store's lifecycle writers carry the `attest_*` keys through rewrites
(evidence, retire) — a lifecycle update never orphans the entry's receipt
annotations. The attestation **truth still lives on the ledger**; the file
keys are the convenient pointer back to it (turn hash, receipt, chain index),
recoverable from the queue/receipts if a file is ever hand-edited without
them. The design record behind all of this is an
internal decision document; its ratified shape is summarized in
[CONCEPTS.md](CONCEPTS.md) and implemented in `helm/premise.py` (whose
docstring is the normative spec for the digest contract).

## Sequencing: git history vs the ledger

Two tamper-evidence layers serve two different moments, deliberately:

- **Solo-operator history** wants git: the planned authored/derived split
  (`helm ship`/`pull`) git-backs the authored chain, and the supersession
  history — *who believed what, when* — rides content-addressed commits with
  zero daemons. For one operator on their own machines, that is the right
  weight.
- **Second-party proof** is what the ledger adds: the day a belief must be
  proven *to someone else* (multi-operator helm, cross-machine finality), a
  self-writable git history is no longer evidence — signed turns on the
  substrate are. That is why the substrate ships optional and propose-only:
  helm runs fully without it, and it is sequenced to the moment it pays.

The digest check in `helm doctor` stays regardless — it catches accidental
drift cheaply, whichever history layer is carrying the record.

## Chat rides the same substrate

`helm chat` v2 reuses this exact pattern for the groupchat: each post's
digest (`chat:b2b:<blake2b-256>`, the same algorithm-tagged shape) rides a
signed self-write turn on the poster's cell — on a **separate room node**
whose data-dir lives on tmpfs, so a chat turn never lands on a
disk-persisted chain (premise `a2a-ram-only-disk-log-after`). Thin claim,
fat corroboration, twice over: the ledger holds digests, the store holds
premise text, the RAM room holds chat text. See the chat section of
[VERBS.md](VERBS.md).

## The node migration (one node per team)

The target topology is ONE node per local team — RAM-hot (tmpfs data-dir)
with disk as **log-after**: restore-on-boot, a snapshot after every
attestation turn (`helm/cell.py` fires `~/.local/bin/dregg-cave-snapshot`
after each successful `send_self`), an interval snapshot timer, and a
snapshot on unit stop. The interim second chat node dissolves into it.

The migration is `scripts/node-migration.sh` — run `--dry-run`
first (it exercises every gate for real; the probe mints append-only turns).
Its hard gates: a full
tarball backup before anything; the restore-on-boot path **proven before the
flip**; `premise-check` MATCH on every attested premise before *and* after;
a post-flip durability cycle (a fresh turn must survive snapshot -> stop ->
tmpfs wipe -> cold boot); a paste-ready rollback
(`--rollback`); the whisper daemon stopped around the flip and verified
back. A digest MISMATCH on any attested premise aborts the migration — edit
drift must be re-attested (supersession is a chain, not an edit) before the
chain moves homes.
