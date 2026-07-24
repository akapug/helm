# Dependencies — why the core needs none, and what each companion buys you

helm's design rule for dependencies is deliberate and load-bearing, so it is
worth stating plainly.

## The core requires zero dependencies

helm runs on the **Python standard library alone**. There is no `setup.py`, no
`pyproject.toml`, no `requirements.txt`, no lockfile, and no build step. One
`git clone` and the CLI runs.

This is not minimalism for its own sake. A substrate that a *fleet of agents
trusts to coordinate* has three hard requirements, and zero-dependency is how
helm meets all three at once:

- **Auditability.** helm carries the bus your agents talk on and the ledger their
  verdicts ride. Its trustworthiness cannot rest on a transitive dependency tree
  you did not read. No third-party package can inject behavior into the
  coordination path, because there are none.
- **Portability.** A seat can be minted anywhere Python 3 runs — a laptop, a
  build box, a container — with no install phase to drift or fail. There is no
  "works on my machine" gap between agents, because there is nothing to install.
- **Longevity.** Standard-library APIs do not rot on a package maintainer's
  timeline. A coordination substrate you want to still work in a year should not
  depend on the release cadence of anything you do not control.

Everything novel in helm — the RAM-first event bus, the BLAKE2b-digested signed
turns, the cross-family gate, the projection model — is built on stdlib
primitives (`hashlib`, `json`, `http.server`, `socket`, `subprocess`, `fcntl`).
The interesting part was never a library; it was the model.

## Recommended companions (all optional, all degrade loudly)

helm is **metaharness- and companion-agnostic**. Each of the tools below adds a
capability, but helm never assumes one is present: when a companion is absent,
the feature it powers degrades to a single informative line and everything else
keeps working. You opt each one in.

| Companion | What it adds | Why you'd reach for it |
|---|---|---|
| **[dregg](https://github.com/emberian/dregg)** | A post-quantum (ML-DSA) signing node for the signed-turn transport. | When you want *cryptographic* provenance on what agents say, above the always-on native BLAKE2b payload digest. dregg is an optional external checkpoint; without it, turns still carry their BLAKE2b digest and `verify` — they are just unsigned and unchained. |
| **cv** | A recall / truth-engine plane for "we solved this before" retrieval. | Durable, cross-session recall so an agent doesn't re-derive what the fleet already learned. |
| **cli-proxy-api** | A local proxy that presents non-Claude model families (Codex/GPT, DeepSeek, Kimi) to your harness as a model endpoint. | Cross-family fleets need every family reachable through one seat model; it is the model-access layer that makes a mixed-family fleet possible (`HELM_PROXY_BIN`). |
| **[orca](https://github.com/stablyai/orca)** | A terminal/pane harness for spawning and driving agent panes. | Recommended companion for pane lifecycle (`spawn`/`resume`/`where`); helm drives it through a thin adapter but assumes nothing about it. |
| **herdr** | A fleet/agent harness, equally supported alongside orca. | The same seat lifecycle through a different metaharness; pick whichever your fleet already runs. |

## The rule, restated

- **Required dependencies: none.** The core is stdlib-only, forever.
- **Recommended companions: opt-in, and each degrades to a loud one-liner when
  absent.** helm never silently assumes a companion is installed, and never
  trades away a working local path to chase an optional one.

If you add code to helm, it obeys the same rule: the core stays stdlib-only, and
anything that reaches for a companion does so behind a capability check that
fails loud, never a hard import that fails the fleet.
