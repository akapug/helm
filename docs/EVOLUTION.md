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

## The observer roster (`helm evolve [--project P]`)

| observer | reads | proposes |
|---|---|---|
| sync | project scan | new-project registrations (the one documented write: registry + scaffold) |
| drain | raw intake | `helm drain --apply` routing / dup sweeps |
| drift | priors + snapshot (peek, never consumed) | the summary line, plus per-belief paste-ready commands off the structured feed: contradicted certainty → `store supersede`, tier fall → `store evidence +Δ` (Δ computed to re-cross), decayed → `store retire`; rises stay quiet; one command per belief; `--project` rides every command when scoped |
| fire: wallpaper | inject fire-ledger (+ .1 rotation) | a live jit entry firing in >50% of non-silent turns (min 20) → tighten keywords or demote, with the numbers (`store evidence` for beliefs, `store get` for the rest) |
| fire: dead-weight | fire-ledger | live jit entries with ZERO fires over ≥200 turns → ONE batched dormancy-review proposal, never per-entry spam |
| fire: silence | fire-ledger + store | ≥95% silent turns (min 20) over a populated store → `helm hooks status` (injection may be dark) |
| reflex | fire-ledger reflex lane | a reflex re-firing within 3 turns of firing, ≥3 episodes → steer not landing; reword or retire. Honesty: the ledger logs fires, not heeds — proximity re-fire is the only proxy, and the proposal says so |
| reflex: dead | fire-ledger reflex lane | live reflexes with ZERO fires over ≥200 turns → ONE batched review proposal (dead signal or simply unprovoked — never auto-retired) |
| record | reflex-state counters (record.py) | a tool-outcome tell — stuck-streak ≥3 or loop-streak ≥3 — recurring across ≥3 sessions → inspect (`helm record status`) and capture the fix as a lesson (`helm coach`). Honesty: counters hold each session's LAST state (a resolved streak reads 0), so this undercounts, never overcounts |
| whoami | profile + notes | the five-minute interview |

Behavior proposals are ranked by evidence strength, capped at 10 per cycle,
and the cycle output names its data window ("over N turns since T"). The
ledger and recorder observers tolerate absence and garbage (no ledger/state,
no claims) and read estate-wide — fire and tool behavior are not
scope-sliced, but the store and reflex sets resolve under the cycle's scope.

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
