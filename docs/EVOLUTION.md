# The self-evolution engine

helm's stores are only alive if something keeps them moving: raw intake gets
drained, beliefs get evidence, drifting beliefs get surfaced, and behavior
that keeps failing gets a reflex. This is the layer most coordination systems
drop — the actuators — and helm treats it as a first-class product surface.

## The loop

```
notice -> name -> layer -> build -> adopt -> verify
```

- **notice** — observers run continuously or on demand: the drain classifier
  (what raw intake accumulated), the drift report (what beliefs moved), the
  doctor (what broke structurally), session recall (what keeps happening).
- **name** — a noticed pattern becomes a named entry: a bug-class term in the
  lexicon, a belief in the priors store, a move in the heuristics store.
- **layer** — the routing law picks the artifact class: belief → prior,
  move → heuristic, term → lexicon, fires-unbidden → reflex, procedure →
  skill, guarantee → hook.
- **build/adopt** — the entry is written; delivery comes free (the one
  resolver already serves it just-in-time).
- **verify** — evidence logs and the drift report close the loop: an adopted
  belief that stops matching reality surfaces itself.

## The anti-rulesurf gate (constitutional)

`helm evolve` **proposes, never mutates**. Every proposal is an explicit verb
run deliberately by the operator or a supervising agent. Self-modifying
belief stores that skip the gate don't learn — they drift. This gate is
inherited from the earliest ancestor design and has survived every
generation's review.

## Mentorship (the pair extension)

The same loop extends from SELF to PAIR: a senior agent observes a junior's
sessions (via the recall plane), critiques the pattern, and *writes customized
reflexes into the junior's store* — then re-observes. The store format makes
this safe: reflexes are terse, typed, per-signal files; the junior's harness
delivers them fresh every turn; retiring one is a one-line status flip.

The mentorship actuator needs only primitives helm already has:

1. read the junior's recent sessions (recall plane),
2. name the recurring miss (a lexicon/bug-class entry),
3. write the reflex into the junior's reflex dir (`helm reflex add`),
4. watch the next sessions for the transition.

The proposing/gating rules apply unchanged: mentor proposes, and the write is
logged with provenance (`source`, `stated_ts`) so the junior's operator can
always see who taught what, when.

## Cold-start economics (why capture must be near-free)

Manual capture into an empty store dies of its own accord: no payoff, no
habit. helm's answer is threefold: the drain makes existing raw intake the
first fill; the interview seeds the who-leg in five minutes; and every
captured entry pays immediately because delivery (JIT injection) already
works. Capture verbs stay one line; the store defaults to silence.
