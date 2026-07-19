# License — owner decision required before any public flip

This repo currently ships **no license**, which means all rights reserved: a
fresh consumer legally cannot run modified copies, redistribute, or
contribute. That is a blocker for the public flip and for any external
contributor, and it is an **owner call** — this file records the option space,
not the decision.

## The realistic candidates

- **MIT** — shortest, most familiar, maximally permissive. No patent grant.
- **Apache-2.0** — same practical permissiveness, plus an explicit **patent
  grant** and a contributor patent-retaliation clause, plus tidier
  NOTICE/attribution mechanics.

## Recommendation

**Apache-2.0.** helm is substrate-ish — a knowledge/attestation layer other
tools build on — and for infrastructure that expects downstream integration,
the explicit patent grant materially lowers adopter risk at essentially zero
cost. MIT is the right answer only if maximal brevity outweighs that.

## When it lands

1. `LICENSE` file at the repo root (full text, year + owner line).
2. Replace the `License: TBD` line in README with the real one.
3. Delete this file.

Until then the repo stays private; nothing may be redistributed.
