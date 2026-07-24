# License decision record

**Decision: AGPL-3.0-or-later.** The `LICENSE` file at the repo root carries the
full text; the README's License section points here for the rationale.

## Why AGPL-3.0-or-later

helm is substrate — a coordination/knowledge/attestation layer other tools and
fleets build on. The copyleft in AGPL-3.0 is chosen deliberately to keep that
substrate open: a modified helm served to third parties must carry its source
forward, which is exactly the extraction case network-copyleft exists to catch.

The decision also keeps helm license-compatible with the AGPL-3.0 truth-engine
dependencies it composes with (the recall/verifiability plane), so the copyleft
intent is aligned across the pieces that interoperate.

## Extenuating factors considered (and judged non-blocking)

- **Enterprise AGPL-aversion is real adoption friction.** Judged acceptable: an
  operator who runs helm *for themselves* is not triggered by the network
  clause at all — it binds only someone **serving a modified helm to third
  parties**. That is the case we intend copyleft to reach, not ordinary
  fleet-internal use.
- **No patent grant** (as Apache-2.0 would provide). Acceptable at this scale;
  it can be revisited if the adopter profile changes.

## Commercial / federation / relicensing notes

The project has a single copyright holder today, so any future change stays a
one-party decision until outside contributions arrive:

- A future **dual-license** (AGPL-3.0-or-later + a commercial license) remains
  open — a common pattern for copyleft substrate that wants both an open
  commons and a commercial escape hatch for adopters who cannot take AGPL.
- Multi-operator **federation** deployments are fleet-internal use and are not
  encumbered by the network clause between cooperating operators; the clause
  bites on serving a modified build outward to third parties.

Once external contributions arrive, relicensing would require contributor
agreement (or a CLA established up front); note that here so the expectation is
set before the first outside patch lands.
