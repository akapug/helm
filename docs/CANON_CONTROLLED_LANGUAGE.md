# Canon as Controlled Language

**Thesis (owner-ratified, 2026-07-29):** ASD-STE100 (Simplified Technical English — the
aircraft-maintenance-manual controlled language) and the helm canon/lexicon are the
**same kind of thing**: a controlled vocabulary that kills ambiguity by constraining
language into a *checkable, deconflicted policy*. We already built the substrate — the
learn-ladder (lexicon + prior/premise + heuristic + reflex) plus symptom-keyed
store-resolve retrieval. STE names the **discipline** we never applied to it: one
canonical term per concept, synonyms *mapped* to it rather than competing with it, and a
lint that certifies the constraint mechanically.

This document designs that upgrade. It is honest about what is **proven in the code
today** versus **aspirational** (designed here, not yet built). Every seam is cited to
`file:line` so the build agent can start without re-deriving.

---

## 0. Status legend

| Tag | Meaning |
|---|---|
| **[PROVEN]** | Verified in the running code / measured this session. |
| **[TRACED]** | Read in the source; behavior follows by construction, not run. |
| **[ASPIRATIONAL]** | Designed here. Not built. Seam named. |

The load-bearing measured facts this design answers to:

- **[PROVEN] The DF-split is real and already bites.** `resolve_prompt` scores each
  entry `confidence × Σ(1/df[p])` over matched probes, where `df[p]` counts how many
  *candidate entries* carry probe `p` (`helm/store/resolve.py:46-55,128-161`). Two
  entries carrying the same concept under different terms halve each other's weight for
  every shared probe. The resolver's own docstring documents the workaround authors
  already reach for — hand-padding `account`+`accounts`, `address`+`addressed`+
  `addressing` as separate probes (`resolve.py:74-91`).
- **[MEASURED] CD re-tuned store keywords ~5 times in one session** because a concept was
  split across synonyms, so premises ranked low and missed on resolve. That is the
  DF-split degrading our own retrieval — the exact ASD-STE100 synonym-drift problem.
- **[PROVEN] The lexicon rung has no deconfliction.** `_LEX_DEFAULTS` has **no
  `synonyms`, `aliases`, or `canonical` field** (`helm/store/load.py:113-117`); lexicon
  is **exempt from the same-id refuse** (`helm/store/cli.py:30-31`); the only similarity
  signal is `_near_dup` — same-type statement Jaccard ≥ 0.80, **WARN-only, never blocks**
  (`helm/store/index.py:179-193`). Two different terms for one concept get zero warning.
- **[PROVEN] The vocabulary rung is the least-invested.** In the curated `_global` tier:
  **17 lexicon vs 271 priors vs 78 heuristics** (ratio 0.06); store-wide prior:lexicon is
  4:1. This is exactly the rung a controlled-language upgrade lands on.
- **[TRACED] Discovery is passive, and one path pushes *against* canonicalization.** The
  coinage-3-strikes detector nudges an agent to `define` a term after N distinct turns
  but checks only whether **that exact term** is stored, **not whether a canonical
  synonym already exists** (`helm/inject/__init__.py:58-69`) — it manufactures
  synonym-split.

---

## 1. The two-component frame

STE has exactly two components. The mapping onto helm is clean, and the *one structural
difference* drives every design decision.

**Component A — the controlled DICTIONARY → the canonical concept registry.**
STE's ~900 approved words are a substitution table with locks: one meaning per word, one
part of speech per word, **one term per concept** — and the banned synonyms are listed
*mapped to their canonical replacement* (`commence → start` is a directed edge, not just
an exclusion). helm's counterpart is the **lexicon rung plus every concept namespace**:
bug-class slugs (`identified-fix-filed-not-fixed`), premise IDs
(`child-stamp-kills-seat-persistence`), the rendered board keys
(`tasks`/`landed`/`owner_gated_queue`/`burn_down_0_2`), terrain/component names. Two
layers map exactly: STE's **closed core** = helm's operating vocabulary (verb names,
store types, the reflexes) — curated, rarely changed; STE's **open technical-name
extension** = domain terms that accrete as the fleet learns.

**Component B — the WRITING RULES → store-entry hygiene.**
STE's ~65 grammar rules (sentence length, active voice, one instruction per sentence,
approved tenses) map to entry hygiene: one premise per entry (STE "one instruction per
sentence"), atomicity + length, a required provenance tag `[MEASURED]/[TRACED]/
[INFERRED]`, required symptom keys, the hedge/vacuity bans already encoded in the STE
lint's `BANNED`/`MARKETING` lists (`agents/claudecode/skills/ste-writing/ste-lint.py:3-16`).

**The one structural difference — and it drives everything:**

> An aircraft manual is **write-only** — a technician reads it, never queries it by
> symptom. The canon is **read-write** — agents query it. So STE only needs the *ban*
> direction of the synonym table; the canon needs **both directions**, and the "banned"
> table becomes its most valuable asset.

Two consequences, both design requirements rather than options:

1. **The synonym table is bidirectional.** *Normalize on write* (a synonym in a store
   entry / board key / dispatch → rewrite to canonical, same as STE). *Index on read* (an
   agent describing a symptom in their own words → the same alias table is the
   symptom-keyed retrieval index; many phrasings routing to one canonical entry). Throwing
   the synonym list away — the naive STE port — would delete precisely the bridge that
   lets a future agent who only knows the symptom find the one canonical entry.
2. **The dictionary grows at runtime**, so the canon needs something the manual does not:
   an **admission gate**. That gate is `/learn` — resolve-tested before it counts. The
   resolve-test *is* dictionary admission: a term enters the approved set only once
   retrieval proves it resolves.

The one-line invariant: **front-load judgment into the die** (`/learn`: author the
canonical entry, wire its aliases, resolve-test it, dedup against existing concepts),
then **every downstream use is stamped mechanically** by a membership + alias + board-key
+ identifier linter.

---

## 2. DECONFLICTION — canonical-term-per-concept + the synonym map

### 2.1 The model

The lexicon rung gains a **canonical entry per concept** with a first-class alias set.
Concretely, three new fields on `_LEX_DEFAULTS` (`load.py:113-117`):

- `aliases` — CSV of synonym **terms** for this concept (the nouns an agent or the owner
  might *say*). Example: canonical `board-key-drift`, aliases
  `stale-console, console-drift, unrendered-key, ad-hoc-key`.
- `canonical` — on a **stub** alias entry only: a back-pointer to the canonical `term`.
  Most aliases live inline in the canonical entry's `aliases` CSV; a `canonical:`
  stub exists only when a synonym is *itself* a term someone will `helm store get` by
  name, and we want that lookup to redirect rather than 404.
- `alias_triggers` — CSV of the retrieval **keyword forms** each alias contributes (the
  word-boundary probes; `stale`, `console`, `unrendered`). These join the canonical
  entry's `keywords` in its probe set. This is the field the resolver reads; `aliases` is
  the field the *linter and translator* read.

The distinction matters: **term synonyms** (`aliases`) are what people *say*;
**trigger probes** (`alias_triggers` folded into `keywords`) are what the resolver
*matches*. One canonical entry owns both.

Deconfliction is enforced, not merely warned. At add-time (`helm/store/cli.py:359-385`,
adjacent to the existing `_near_dup` call and the lexicon-specific candidate guard) a new
lexicon gate runs a **concept-collision check**: if the incoming term's `alias_triggers`
overlap an existing canonical entry's probe set above threshold, the add is **refused with
the canonical name printed** — "concept already canonical as `board-key-drift`; add this
term as an alias with `helm store alias board-key-drift stale-console`, or `--force-new`
if genuinely distinct." This is the one place lexicon stops being exempt from collision
logic; `_GUARD_TYPE` (`cli.py:30-31`) keeps the *same-id redefine* lane open (redefinition
is still lexicon's only update path), but adds the *different-id-same-concept* refuse.

### 2.2 How this fixes the DF-split CD measured

The failure, precisely (`resolve.py:46-55`):

- **Before.** Concept C lives in two entries `lex-foo`, `lex-bar` sharing trigger `k`.
  `df[k] = 2`. Each entry contributes `confidence × 1/2` for `k` instead of `× 1/1`.
  Worse, both compete for the **cap-4** slots (`resolve.py:149-161`), so a premise about C
  is crowded out; the agent sees *one* of the two terms with **no signal a canonical form
  exists**. To make the premise rank, the author pads its keywords with more phrasings of
  C — each phrasing either high-`df` (diluted) or a guess that misses the turn's actual
  words. That is CD's five re-tunes.

- **After.** C is **one canonical document**. Its aliases fold into *its* probe set, so
  the duplicate document leaves the `df` denominator. `df[k]` drops toward **1** — it stays
  above 1 only for probes that legitimately span *distinct* concepts, which is `df`
  weighting doing its correct job. The full `1/df` weight now accrues to the single
  canonical entry, and it holds a cap-4 slot on its own merit.

- **The compounding win for premises (CD's actual pain).** Premises no longer guess
  phrasings. A premise keys on the **canonical term** — a rare probe (`df` near 1, high
  weight) — and relies on the canonical *lexicon* entry to catch the varied surface
  phrasings via `alias_triggers` and teach the agent the canonical word. Once the agent
  writes the canonical word, every premise keyed on it resolves at full weight. The
  re-tune loop is replaced by a one-time act: wire the alias on the canonical entry.

Worked numbers (illustrative, consistent with `resolve.py` scoring):

| | df[`console`] | canonical entry weight for a "console went stale" turn | cap-4 pressure |
|---|---|---|---|
| Before | 8 (2 lexicons + 1 premise + 5 others) | split across 2 lexicons, ~`1/8` each | 2 near-dup lexicons compete |
| After | 3 (1 canonical + 2 legit-distinct) | one entry, `1/3`, plus the rare canonical term probe `df≈1` | one canonical entry, no self-competition |

The mechanism is **[TRACED]** (the scoring is verified; the fix is the arithmetic
consequence of removing the duplicate document from `_df_map`). The *field + probe-fold* is
**[ASPIRATIONAL]** — the seams are §2.4.

### 2.3 Write-normalize vs read-index — the bidirection

The same alias table is read three ways. This is the design's spine:

| Direction | Consumer | What the table does |
|---|---|---|
| **Normalize** | write path (store add, board-key set, dispatch, PR text) | synonym → canonical; the entry/key/message lands on the one approved noun |
| **Index** | agent read path (`resolve_prompt` per turn) | any alias trigger fires the one canonical entry; agent "never searches for what water to swim in" |
| **Translate** | owner read path (§5) | fleet-shorthand ↔ owner-plain-language, decoded from the same map |

Persist the map; never discard it (the naive STE port discards). It *is* the store's
superpower — symptom-keyed retrieval is STE's ban list run backwards.

### 2.4 Seams (deconfliction)

1. **Fields** — add `aliases`, `canonical`, `alias_triggers` to `_LEX_DEFAULTS`
   (`load.py:113-117`); parse them in `_parse_lexicon` (`load.py:151-172`); serialize in
   `write_lexicon` (`helm/store/write.py:106-151`). Note the existing **[PROVEN] bug** the
   parser must not reintroduce: `meme:true` is silently dropped because `_LEX_DEFAULTS`
   has no `meme` key — any new field lacking a default is lost on rewrite. Add the keys.
2. **Probe fold** — `_probes` (`resolve.py:16-35`) currently returns `{id} ∪ keywords` for
   lexicon. Extend to `{id} ∪ keywords ∪ alias_triggers`. This is the one line that makes
   aliases fire the canonical entry and collapses the `df` denominator.
3. **Collision gate** — new lexicon branch beside `_near_dup` and the candidate guard
   (`cli.py:359-385`); reuse `_near_dup` (`index.py:179-193`) as the similarity primitive,
   but keyed on **probe-set overlap**, not statement Jaccard (statement-Jaccard misses
   synonym-split by construction — different wordings score < 0.8).
4. **`alias` verb** — new `helm store alias <canonical> <synonym> [--triggers csv]`
   (`cmd_store`, `cli.py:101-706`) that appends to the canonical entry instead of minting
   a new `lex-*.md`.

---

## 3. CHECKABILITY — the canon lint

STE's power is the **checkability split**: judgment is spent once in the die; every stamp
after is mechanical. The canon inherits the split exactly. A new verb `helm store lint`
(aka `helm clarity`) — sibling to `check_lexicon_dead_vocabulary`
(`helm/doctor.py:193-219`), wired into `CHECKS` (`doctor.py:519`) and callable standalone
— runs three checks.

### 3.1 The three checks

**(a) Synonym drift** — *a non-canonical term used for a concept that already has a
canonical lexicon entry.* Scan store writes and fleet comms; for each noun/term token,
look it up in the alias map. If it is a known alias, **flag it and name the fix** ("used
`stale-console`; canonical is `board-key-drift`"). This is the canon's Rule 1.2 — the
highest-leverage check, because it doesn't just flag, it *substitutes*. It is exactly the
STE lint's `BANNED` substitution table (`ste-lint.py:7-12`), sourced from the store's
alias map instead of a hardcoded list.

**(b) Undefined high-frequency jargon → new-lexicon candidates.** Count term frequency
across comms/entries. A term that appears often, is **not** a dictionary member, and is
**not** a known alias is a candidate for admission. Surface it: "`herdr-host` used 14×,
undefined — `helm store add lexicon` or map as alias?" This is the admission gate's radar.
It requires the **`hits` counter to become live** (today `hits` is a **[PROVEN] dead
counter** — written `prev.get("hits") or "0"` at `cli.py:487` / `write.py:124`, **never
incremented**); §4.3 makes it live.

**(c) Term hygiene** — extends the existing dead-vocabulary check. Flag the **[PROVEN]
live artifacts**: `lex-stdlib-only.md` has `term: stdlib-only:` (trailing colon → malformed
probe/id); `lex-drain.md` has `kind: 1.0` (a confidence value mis-filed into the kind
slot). Add: trailing-punctuation in `term`, a numeric `kind`, an `aliases` entry that is
*also* a live canonical term (an alias must not out-rank its own canonical).

### 3.2 Deterministic vs judgment — the load-bearing split

The residue is always the same shape: **membership and pattern are deterministic;
meaning, identity, truth, and admission are judgment.** The lint fixes the *form* of
slop; it can never make a hollow entry true.

| Check | Deterministic (lint certifies, zero inference) | Judgment (lint surfaces; a human/`/learn` decides) |
|---|---|---|
| Membership | is this token an approved dictionary member? — set lookup | — |
| Synonym→canonical | is it a **known alias**? — map lookup; names the fix | is this token *really* that concept in *this* sentence? (word-sense) |
| Frequency | count occurrences; is it undefined? — counter + set | is this jargon worth admitting, and under what canonical name? |
| Board-key | is a rendered key a member of the console's key set, or ad-hoc? — set lookup (the "console renders specific board keys" note is this check by consequence) | — |
| Identifier shape | is this a resolved id or an invented/short prefix? — resolve against registry (the "never type a sha, resolve it" + "never-invent-hex" notes are literally this) | — |
| Entry hygiene | provenance tag present? length ≤ bound? one `\|`-arity? — presence/count/regex | is the claim atomic and non-vacuous? is it *one* idea? |
| Concept identity | probe-set overlap ≥ threshold — set math (a *signal*, not a verdict) | do two canonical entries actually **denote the same concept** and need merging? (ontology dedup) |
| Freshness | — | is the stored premise **still true**? ("stale terrain = bug ⊥ state" — a lint certifies a term is approved, never that the premise still holds) |

The deterministic column is the lint. The judgment column is `/learn` and the owner —
it is *surfaced* by the lint (as a candidate, a warning, an overlap signal) but never
*decided* by it. Admitting a new canonical term is a **governance act, not a check**.

### 3.3 Where the lint runs

- **Write-path gate (blocking, deterministic subset only):** at store add-time
  (`cli.py:359-385`) the synonym-drift + hygiene checks refuse or normalize. Blocking is
  restricted to the deterministic column — a lint must never block on judgment.
- **Comms lint (advisory):** a `helm store lint --comms <text>` pass over dispatch / board
  / PR text, run by the author or a periodic sweep. Advisory: it *suggests* the canonical
  term (it can't know word-sense).
- **Standing doctor check:** the hygiene + orphaned-alias checks in `CHECKS`
  (`doctor.py:519`), WARN-level, so drift that slips the gate is still caught in the
  health pass.

---

## 4. ENFORCEMENT / RSH — a lexicon nobody reaches for is dead

**feature-and-rsh-must-both-be-wired.** A canonical registry that agents don't *perceive*
and don't *use* is a dead reference file. Today discovery is **[TRACED] passive-injection-
only**, and the coinage nudge actively manufactures synonym-split. Three RSH legs close
the loop.

### 4.1 PERCEIVE — the vocabulary reaches the agent

- **Injection already renders the canonical term** — `_entry_line_full` emits
  `"TERM <id>: <definition>"` per turn when the turn text matches
  (`helm/inject/_entries.py:77-78,155-168`). With §2's probe fold, an agent using *any
  alias* now trips the canonical entry and **sees the canonical term rendered back**. This
  is the "never search for what water to swim in" property, finally delivered by
  construction — the alias catches the phrasing, the render teaches the canonical word.
  **[TRACED] render exists; [ASPIRATIONAL] the alias-fold that makes off-vocabulary
  phrasings reach it.**
- **Forward-lookup surface** — new `helm store canon <concept-text>` verb (`cmd_store`,
  `cli.py:101-706`): given free-text describing a concept, return the canonical term. This
  is the query the store lacks today (`resolve` is keyword-match, `get` needs the id
  already). It is the agent's explicit "what do we *call* this?" — the dictionary lookup.

### 4.2 USE — writing lands on canonical

- **Normalize-on-write** at the add-gate (§3.3): a synonym in a new entry is rewritten to
  canonical before it lands. Board-key and dispatch normalization ride the same gate.
- **Fix the coinage nudge — the anti-canonicalization bug.** `helm/inject/__init__.py:58-69`
  nudges "define this coinage" after `COINAGE_STRIKES` turns, checking only *exact-term*
  existence. Change it to check **concept existence** first (via §4.1's `canon` lookup /
  the alias map): if a canonical entry already covers the concept, the nudge becomes **"use
  the canonical term `X`"** instead of "mint a new `lex-*.md`." One line of policy flips
  the detector from a synonym-split *source* into a canonicalization *driver*.

### 4.3 CLOSE THE LOOP — self-maintenance earns its name

"Self-maintaining" is only real if the vocabulary *converges* from use. Make the
**[PROVEN] dead `hits` counter live**: increment it on every resolve/inject fire
(`resolve_prompt` return path, `resolve.py:161`; render path,
`inject/_entries.py:155-168`). Live telemetry then drives three maintenance signals with
no human in the loop:

- **High-hit aliases confirm the canonical** — an alias that fires often is a real
  synonym; keep it wired.
- **Zero-hit lexicon entries are dedup/retire candidates** — surfaced by the lint as "197
  entries, N never fired in 30 days."
- **High-hit *undefined* jargon** (§3.1b) is the admission radar — the store tells you
  what to canonize next.

This is the convergence engine: alias table + hit telemetry + lint. Without live `hits`,
"self-maintaining" is a slogan; with it, the lexicon curates itself and only escalates
the **judgment** residue (merge? admit? still-true?) to `/learn` and the owner.

---

## 5. The AX/UX translation layer

The same controlled vocabulary is a **translation layer** in two directions at once, off
the one alias map.

**AX — concept ↔ canonical-term, for agents.** An agent thinks a concept in whatever words
it reaches for. The alias map (read/index direction, §2.3) routes those words to the one
canonical entry; the injection render (§4.1) hands back the canonical term; the agent
writes canonical. Downstream, every store entry, board key, dispatch, and chat line is
deconflicted — one name per thing — so the *next* agent's retrieval is clean. AX is the
**machine-facing** face of the table: normalize on write, index on read.

**UX — fleet-shorthand ↔ owner-clarity, for the owner.** The fleet speaks in dense
canonical slugs (`child-stamp-kills-seat-persistence`, `burn_down_0_2`). The owner wants
plain language, and sometimes speaks in *their own* shorthand. The **same map, read the
translate direction**, is the owner's decoder: fleet slug → its lexicon `definition`
(the plain-language gloss the owner reads); and owner phrasing → canonical (so an owner
instruction lands on the term the fleet actually keys on). The owner console can render a
board key *with its canonical definition inline*, so a stale/ad-hoc key is not just
"unrendered" but "unrecognized — did you mean `owner_gated_queue`?"

One table, three readings — **normalize** (write), **index** (agent read), **translate**
(owner read). The definition field is what makes the owner-read work: it is the
human-legible side of every canonical term, which is why the lint's judgment column cares
about definition *quality*, not just presence.

---

## 6. The efficient DEFAULT + the per-user EVOLUTION

**Strategic frame (owner, 2026-07-29, load-bearing):** the controlled STE-efficient base
is the **default starting point** for how agents talk — extremely efficient, deconflicted
— and the canon/lexicon system **evolves a user-specific layer** from there over time.
Owner verbatim: *"I want you talking like we evolve for you to talk starting from an
extremely efficient base, and that is how any new user using him will probably want it."*

So this is **not a fixed style.** It is two halves, and **both must ship**:

**Half A — the efficient controlled DEFAULT (ships with the product).**
The **closed core**: helm's operating vocabulary (verb names, store types, reflex names,
board keys) shipped pre-canonized, one term per concept, out of the box. Plus the
**STE-efficient base voice defaults** — the anti-slop bans already in `ste-lint.py`
(`BANNED`/`MARKETING`/`PHRASAL`, lines 3-16) applied to **docs and technical output**, so
a **new user** gets crisp, deconflicted, low-fluff communication on day one with zero
setup. This is what a new user wants: efficiency by default.

**Half B — the per-user EVOLUTION mechanism (the canon/lexicon *is* this mechanism).**
Over time the **open extension** accretes *this* user's terms. The user's phrasings become
aliases mapped to canonical; the base adapts to how *this* user talks and thinks. The
engine is the machinery already designed above:

- `/learn` admits the user's terms (die-side judgment, resolve-tested).
- The alias map absorbs the user's shorthand (index/translate directions).
- Live `hits` telemetry (§4.3) lets the base *converge* toward this user's real vocabulary
  — high-hit user coinages get canonized, unused base terms fade.
- The lint keeps the growing per-user layer deconflicted.

The default is the same for everyone; the evolution is per-user, and it rides the exact
same rungs. **The controlled base is the floor a new user starts on; the canon/lexicon is
the mechanism that raises a personalized layer on top.** Ship the floor; ship the lift.

---

## 7. Personality scope — where the line is

**HARD CONSTRAINT: STE controls the NOUNS, not the VOICE.** The gregarious-inquisitive-
curious premise stays. Strict-STE-on-everything is explicitly **WRONG** here. The
controlled layer sits on **concepts**; the voice stays free. The line is exact:

| CONTROLLED (one approved form) | FREE (agent's own) |
|---|---|
| The **term** an agent lands on for a technical concept (seat/cell/pane/lane — each is one concept→one name, or distinct concepts each with one name) | The **sentences** around that term — phrasing, register, warmth |
| Identifiers: bug-class slugs, premise IDs, board keys, terrain/component names | Exploration, curiosity, tangents, how an agent *reasons out loud* |
| Store-entry **grammar hygiene**: provenance tag, atomicity, length, `\|`-arity — the deterministic Component-B rules | The chat **voice** — the personality premise is preserved verbatim |
| Anti-slop in **docs/technical output** (the `BANNED`/`MARKETING` lists) | The base voice is *efficient-but-alive*, never *stripped* |

The precise rule: **STE controls the term an agent picks for a concept; it does not
control the prose the agent writes with it.** A `helm clarity` lint that flagged voice —
"too warm," "too curious" — would be the wrong tool aimed at the wrong target. The lint
sees a **noun**, checks it against the registry, and stops. Word-sense (is `fall` used in
its one approved meaning) and whether a paragraph *makes good sense* are judgment, out of
the lint's reach and out of the controlled layer's scope.

Even the **base voice** (Half A) is efficient, not lobotomized: crisp and low-fluff
because the *marketing slop* is banned, not because the *personality* is. The per-user
layer (Half B) then evolves the register toward how this user likes to be talked to. At no
point does the controlled vocabulary reach the voice.

---

## 8. Build order — seams summary

Front-load the die; each row after is a mechanical stamp.

| # | Change | Seam | Status |
|---|---|---|---|
| 1 | Add `aliases`/`canonical`/`alias_triggers` fields (+ fix the dropped-`meme` class of bug: every new field needs a default) | `load.py:113-117`, `_parse_lexicon` `load.py:151-172`, `write_lexicon` `write.py:106-151` | [ASPIRATIONAL] |
| 2 | Fold `alias_triggers` into the lexicon probe set — the DF-split fix | `_probes` `resolve.py:16-35` | [ASPIRATIONAL] |
| 3 | Lexicon collision gate (probe-overlap refuse; print canonical) + `helm store alias` verb | `cli.py:359-385`, `cmd_store` `cli.py:101-706`; reuse `_near_dup` `index.py:179-193` | [ASPIRATIONAL] |
| 4 | `helm store lint` / `helm clarity` — synonym-drift + undefined-jargon + hygiene; deterministic subset blocks at add-time | new sibling to `check_lexicon_dead_vocabulary` `doctor.py:193-219`; wire `CHECKS` `doctor.py:519` | [ASPIRATIONAL] |
| 5 | `helm store canon <concept-text>` forward-lookup verb | `cmd_store` `cli.py:101-706` | [ASPIRATIONAL] |
| 6 | Fix coinage nudge: concept-existence check, not term-existence | `helm/inject/__init__.py:58-69` | [ASPIRATIONAL] |
| 7 | Make `hits` live — increment on resolve/inject fire; drives self-maintenance telemetry | `resolve.py:161`, `inject/_entries.py:155-168`; counter `cli.py:487`/`write.py:124` | [ASPIRATIONAL] |
| 8 | Fix live hygiene artifacts flagged by check (c) | `lex-stdlib-only.md` (`term: stdlib-only:`), `lex-drain.md` (`kind: 1.0`) | [PROVEN defect; fix ASPIRATIONAL] |

The retrieval scoring these attach to (`_df_map` + `resolve_prompt`) and the absence of
any deconfliction field are **[PROVEN]**. Everything in the Status column marked
aspirational is designed here and not yet built; the seams are exact so the build is a
straight line, not a re-derivation.
