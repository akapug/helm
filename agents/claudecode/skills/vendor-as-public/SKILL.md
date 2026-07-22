---
name: vendor-as-public
description: >
  Ship a PRIVATE tool/lib as a dependency of another project NOW — pinned, provenanced, at as-public
  quality — without waiting for its OSS launch. Use when a private repo becomes a dep of MC, the
  builders.dev containers, or buildr before its public gate, or when re-vendoring a newer pin. Encodes
  qualify → pin → mechanism-by-consumption → VENDORED.md → patch-upstream-first → additive-contract →
  un-vendor-on-release. NOT for public upstreams (use normal dependency management) or source-forks you
  actively co-develop inside the consumer (that is the subtree workflow, step 3a). Implements the
  adoption-before-publication premise. Aliases: /vendor, /vendor-dep.
license: MIT
metadata:
  author: buildr
  version: "1.0.0"
  origin: tokaware/docs/VENDOR-AS-PUBLIC-SKILL-SPEC.md@951620c
---

# /vendor-as-public — ship a private tool as a dep NOW, publish later

**The heuristic (premise `adoption-before-publication`).** When a private tool is ready to be a
DEPENDENCY, do not gate its adoption on OSS launch. Vendor it into consumers immediately — pinned,
provenanced, at as-public quality — and let publication be a later, independent, human-blessed gate.
`private-repo-as-public` made the REPO safe to flip public; this makes it safe to DISTRIBUTE before
flipping. Waiting for the OSS gate to adopt your own tools is self-imposed drag; vendoring at public
standard costs minutes and every consumer becomes a dogfood surface hardening the eventual release.

## When to use

- A private repo (tool/lib) becomes a dep of another project (MC, bdev containers, buildr) before its
  OSS gate.
- Re-vendoring: the consumer needs a newer pin.
- **NOT for:** public/released upstreams (use normal dependency management), or source-forks you
  actively co-develop inside the consumer (that is a subtree workflow — step 3a below).

## Procedure

1. **Qualify the vendee (as-public audit).** The vendored artifact must meet the `private-repo-as-public`
   bar: no secrets/tokens/PII, no real account or host identities in code/tests/docs, no `.local`/planning
   files, license headers present or a NOTICE covering lineage (fork courtesy: name the upstream, e.g.
   tokaware → emberian/tokeman). Suggested sweep:
   `git grep -iE '(api[_-]?key|token|secret|password)' -- ':!*.lock'` + an identity grep for the operator's
   known names/hosts + a `git ls-files` eyeball for planning docs. **FAIL = fix upstream first, never scrub
   only the vendored copy.**
2. **Pin.** Vendor a COMMIT/TAG, never a branch. The pin goes in the provenance file (step 4) and, for
   binaries, into the artifact name.
3. **Choose the mechanism by WHAT the consumer consumes:**
   - **3a. Source integration** (consumer builds/patches the code): `git subtree add --squash` (the graal-pa
     precedent). Subtree, not submodule — consumers must build without private-remote access.
   - **3b. Tool-as-dep** (consumer runs a binary — the tokaware/container case): vendor a pinned RELEASE
     BINARY + sha256 into the consumer's artifact store or image-build context. Prefer a cross-platform
     artifact when available (tokaware has an APE lane). Never vendor the whole source tree just to run a tool.
4. **Provenance file — `VENDORED.md` beside the artifact.** Mandatory fields: upstream repo + pinned sha/tag,
   version, date, license/lineage status (private; OSS gate + owner), the exact re-vendor command, sha256
   (for binaries), and the owning seat (who re-vendors). An unprovenance'd vendored blob is the failure mode
   this skill exists to prevent.
5. **Patch-upstream-first (HARD).** Never edit the vendored copy in place. A fix goes to the upstream repo →
   new pin → re-vendor (re-run step 1's audit every time). In-place patches fork silently and strand the
   consumer on an unmaintained mutation.
6. **Contract discipline.** If the dep exposes a machine interface (CLI/JSON), the upstream owner declares it
   ADDITIVE-ONLY between the pins the consumer tracks; breaking changes require a coordinated re-pin.
7. **Un-vendor trigger (supersede-cleanly).** The day upstream goes public / releases, consumers switch to
   the normal dependency channel and DELETE the vendored copy + provenance in the same change. A vendored
   copy outliving its reason is drift.

## First application (worked example)

tokaware → bdev containers + MC: default-variant (no-tray) linux binary, pinned tag, sha256 + `VENDORED.md`
in the container build context; contract = `tokaware list|who|suggest|windows|launch-cmd --json`
(additive-only); homes provisioned by the harness, discovery roots via `tokens.toml`
`claude_home_roots`/`codex_home_roots`. Un-vendor at tokaware's OSS gate (operator-gated on
tokeman-long-working + cosmo-disclosure call).

## Cross-refs

- Premise `adoption-before-publication` (this skill's payload) and `private-repo-as-public` (the quality bar
  it rides on).
- Canon `graal-fork-vendor-subtree-while-private` (the 3a source-integration precedent).
- Class `shared-refresh-token-family-across-homes` (why cred FILES are never part of any vendored artifact —
  creds are provisioned, not vendored).
