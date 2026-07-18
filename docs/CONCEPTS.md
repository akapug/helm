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
load-bearing: the interview fills it, agents read it, warmth follows.

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
  guard). The default.
- **dormant** — never injected until re-confirmed; where decayed beliefs go.

The salience law governs all three: **empty on no match**. Salience is the
scarce resource; spend it only when newly relevant.

## Exploration: attested premises

Premises (confidence 1.0) are provable, standing truths — which makes them
candidates to live as *attested records* in a verifiable substrate and be
delivered just-in-time by contextual whisper rather than sitting in files.
helm's store keeps `type`/`load_class` orthogonal to *placement* precisely so
a premise's backing store can migrate (file → attested record) without any
consumer changing. Design is open; the store schema already permits it.
