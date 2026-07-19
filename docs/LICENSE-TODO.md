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

## Ground-truthed recommendation (2026-07-19, for owner approval)

The "match mc/buildr" instinct dissolves on inspection: their LICENSE is
**herdr's dual-license (AGPL-3.0-or-later + commercial) inherited by the
fork** — an upstream obligation, not an owner decision, and it does not
reach helm (fresh stdlib-Python code; the lineage is conceptual descent,
zero herdr code). The MC skill deck itself ships per-skill `license: MIT`.

**Recommendation: Apache-2.0.** It matches the stated values — maximally
open and adoption-friendly like MIT, plus the explicit patent grant and
contributor patent-retaliation clause that make serious downstream adopters
(and future multi-operator deployments) safe to build on it, plus tidy
NOTICE mechanics for the lineage credit ATTRIBUTION.md already carries.
MIT remains the simpler alternative if brevity outweighs the patent grant.
AGPL is NOT recommended for helm: its network-copyleft would burden the
exact audience (operators embedding helm in their own fleets) the product
courts.

Awaiting: owner approval (and optionally an OSS-license review from ember,
the resident license expert) — then LICENSE lands at the repo root with a
one-line README section, closing the public-flip blocker.

## DECIDED (2026-07-19): AGPL-3.0-or-later

Owner rule: "whatever ember's projects are is my basic preference unless
there are extenuating circumstances." Ground truth: emberian/dregg and
emberian/cv are both AGPL-3.0 — so helm is AGPL-3.0-or-later, matching the
truth-engine family it composes with.

The extenuating factors considered and judged NON-blocking: (a) enterprise
AGPL-aversion is real adoption friction, but helm's operator runs it for
themselves — the network clause only binds someone SERVING a modified helm to
third parties, which is exactly the extraction case copyleft exists to catch;
(b) no patent grant à la Apache-2.0 — acceptable at this scale. The owner is
the sole copyright holder today, so a future relicense or herdr-style
dual-license (AGPL + commercial) stays a one-party decision until outside
contributions arrive. The earlier Apache-2.0 recommendation is superseded by
the ember-match rule.
