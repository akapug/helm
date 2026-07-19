# helm concepts — the two axes, disambiguated

Two taxonomies grew up sharing the phrase "the fourth leg", and the overload
caused real design debt. helm names them apart, permanently.

## Axis 1 — PERSONALIZATION (the four legs of "the substrate learns you")

What a knowledge layer must know about its *operator* to feel personal:

| leg | question it answers | helm home |
|---|---|---|
| **did** | what happened — episodes, sessions, project history | harness transcripts + recall index (referenced, never copied) |
| **believes** | what the operator holds true or likely | the priors store (typed entries with confidence) |
| **means** | what the operator's words mean — their idiolect | the lexicon |
| **who** | who the operator *is* — voice, autonomy, style, goals | `~/.helm/_global/know-your-user/` |

**The fourth leg is WHO.** Personalization without a live `who` leg is what
makes a coordination layer feel like an admin panel. In helm, `who` is
load-bearing: the interview fills it, agents read it, rapport follows.

## Axis 2 — CONTENT CLASS (what a stored entry *is*)

Every entry in the typed store declares its class in frontmatter (`type:`):

| type | what it is | confidence |
|---|---|---|
| **prior** | a belief, confidence-weighted, updated on logged evidence | 0.05–0.99 for beliefs; **1.0 = a premise** |
| **heuristic** | a *move* you apply — "when X, do Y" | always 1.0; the only question is whether its trigger fires |
| **lexicon** | a term and its meaning | capture-confidence only |
| **reference** | harvested external material worth keeping | n/a |
| **episodic** | dated raw record — journals, session residue | n/a; never injected |

**A premise is not a leg and not a type of its own** — it is the special case
of a prior at confidence 1.0 (entirely load-bearing, human-stated). One store,
one file class, one confidence spectrum; `class: certain` is *derived*, never
authored.

## The routing law (belief vs move vs reflex)

> A premise/prior is a belief you **hold** — it gates decisions; confidence is
> meaningful. A heuristic is a move you **apply** across domains — confidence
> is always 1; the only question is whether its trigger-pattern fires this
> turn. A reflex is a heuristic **operationalized** to fire on every turn or
> tool call — delivery is guaranteed by the harness hook; only the heeding is
> probabilistic.

And the artifact-class law that routes a capture to its home:

```
verb → skill · fact → memory · term → lexicon · guarantee → hook
move → heuristic · belief → prior · fires-unbidden → reflex
```

## Load class (when an entry reaches context)

Orthogonal to type. `load_class:` ∈

- **always** — injected every turn. Budget-capped, pin-earned, kept tiny.
- **jit** — injected only when the prompt matches the entry's id or a
  *specific* keyword (generic-word matches are rejected — the anti-wallpaper
  guard). The default. Ranking is **DF-weighted**: each matched keyword
  contributes 1/df — df being how many entries carry it, computed over the
  in-memory store per call — summed, then confidence-weighted. A keyword
  shared by many entries is a weak signal; a rare one is strong, so one
  distinctive match outranks a pile of shared ones. Ties break
  most-recently-updated first, never alphabetically.
- **dormant** — never injected until re-confirmed; where decayed beliefs go.

The salience law governs all three: **empty on no match**. Salience is the
scarce resource; spend it only when newly relevant.

## Attested premises (shipped)

Premises (confidence 1.0) are provable, standing truths — so they live as
*attested records* on a verifiable ledger, not just files. The design
converged 2026-07-18 between the build and substrate poles, was ratified in
that convergence, and was live-proven on the node the same day; it ships as
`helm premise` / `helm premise-check` (operational detail:
[ATTESTATION.md](ATTESTATION.md)). The shape:

- **The operator's own cell signs** premise capture and supersession. This is
  semantically exact, not just warm: confidence 1.0 is human-only by law, so
  the attestable set is precisely the operator's stated truths.
- **Thin claim, fat corroboration**: the ledger holds only a signed digest of
  the canonical premise text; the text stays in the storehouse. A checker
  recomputes the digest — match means attested.
- **Supersession is a new signed turn** referencing the prior turn-hash
  (append-only, never delete — the store's record law, mirrored — retire keeps the file). Belief
  history becomes a provable chain: *held X until T, then Y* — which the
  drift report can read as attested belief evolution.
- **Zero new substrate**: rides the existing self-write receipt shape;
  delivery rides the existing whisper lane. `type`/`load_class` stay
  orthogonal to placement, so no consumer changes when a premise's backing
  migrates from file to attested record.

One activation note: captures sign as a visibly-test profile until the
operator sets their own — the operator's first personal capture with their
profile configured is what activates user-cell signing, by design. And the
substrate is an upgrade, never a dependency: without it, premises store
normally and attestations queue for retry.
