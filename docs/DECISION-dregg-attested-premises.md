# DECISION: dregg-attested premises (meld outcome, 2026-07-18 pre-dawn)

Converged in a fast-meld between the two poles: fable-helm (build-side) ↔ meld-maintainer
(substrate/historical). Owner's Q4 dream ("truths kept in dregg, JIT via whisper"). Full
transcript: `/tmp/helm-meld/channel.md`.

## The decision
A **premise** (a confidence-1.0 truth) is attested on dregg with ZERO new substrate:
1. **Issuer = the USER's own cell.** The WHY (Fable's grounding, stronger than "warmth"):
   `BELIEF_CLAMP` makes confidence-1.0 HUMAN-ONLY by law → the attestable set is *exactly*
   David's stated truths → the user's cell is **semantically exact**, not merely warmer.
   Agents may hold priors (<1.0) but never mint premises.
2. **Mechanism = the self-write SetField receipt** (identical to `docs/PROVENANCE_BRIDGE.md`
   in the meld repo — no new primitive). A premise-capture commits ONE self-targeted turn
   signed by the user's cell, writing the premise DIGEST (BLAKE3 over canonical text) into a
   field lane. Cell-signed, receipted, ordered, independently checkable.
3. **Thin claim, fat corroboration.** The 120-byte lane holds only `(relation=premise,
   subject=<id>, claim={confidence:1.0, digest, issued_at})`; the premise TEXT lives in the
   storehouse (`type: premise`). A checker recomputes the digest from the store and compares.
4. **Capture signs; agents only verify.** Premise capture is a human-present moment (a
   `/premise` turn) → sign at capture; downstream agents just recompute the digest. So the
   user's key is touched only at capture, never in the hot path (key-coupling is cheap).
5. **Retire/supersede = a NEW signed turn** by the same (user's) cell, referencing the prior
   premise's turn-hash + a superseded marker. Never delete (dregg ledger is append-only;
   buildr IA law "an edit compiles to a new event"). **Store seam is 1:1**: the storehouse's
   existing `supersedes`/`replaced_by` fields (tombstone law) already hold the chain — ZERO
   store-schema change.
6. **Belief-evolution becomes attested knowledge.** The supersede chain proves "David
   believed X until T, then Y" — the **drift-report reads the chain** rather than losing the
   history. Belief evolution is itself durable, checkable knowledge.
7. **JIT via whisper**: the existing whisper lane delivers the digest+pointer to any seat;
   the seat resolves the text from the storehouse. No new transport.
8. Re-bases onto Ember's Cert R later — constraint today, dependency never (same as the bridge).

## Status
Design converged + validated against live surfaces: the node's `/api/receipts` +
`/api/turn/{hash}/status` (the independently-checkable surface) are live; the `meld` binary
(target/release/meld) exercises the self-write path. Empirical self-write premise dogfood:
next (advisor). Build: Fable wires premise capture as a user-cell self-write; store fields
already present.

## Addendum — live-proof refinements (2026-07-18, dogfooded on the node)

Advisor proved the mechanic live (self-write finalized at h43, read back
independently). Two build-affecting constraints (relayed; advisor's canonical
wording to follow — their direct edit did not land on this file):

1. **The 32-byte digest rides the WHISPER PAYLOAD slots, never the 8-byte
   heartbeat tag.** Premise self-write = the meld-send SetField path. Do not
   wire premise → heartbeat-tag.
2. **`premise-check` must quote the finality tier** it verified at:
   ingress-immediate vs attested-after-next-height.
