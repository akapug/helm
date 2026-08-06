# Canon-as-Controlled-Language — Ranked Build Lanes

Companion to `docs/CANON_CONTROLLED_LANGUAGE.md`. That document designs the
upgrade and cites every seam; this one cuts it into **buildable lanes, ranked by
leverage**, so the build agent picks the top of the list and ships.

Ranking rule: a lane ranks by how much measured pain it removes per unit of
build, and by how many downstream lanes it unblocks. The pain of record is
**CD's DF-split** — `resolve_prompt` halves a concept's retrieval weight for
every probe two synonym entries share (`resolve.py:46-55,128-161`), which cost
CD ~5 keyword re-tunes in one session. **Lanes that fix that resolve problem are
flagged `[FIXES CD-RESOLVE]` and sort to the top.**

Each lane records: the exact change, the file:seam, why it is high-leverage,
size (S/M/L), GUARD (deterministic, blocks/normalizes by construction) vs
JUDGMENT-ASSIST (surfaces a candidate for a human/`/learn` to decide),
dependencies, and the verification that closes it.

---

## Ranking at a glance

| Rank | Lane | Size | Kind | Fixes CD-resolve? | Depends on |
|---|---|---|---|---|---|
| 1 | Lexicon synonym-map + probe-fold | M | Deterministic substrate | **YES — the fix** | — |
| 2 | `helm store alias` verb + collision gate | M | GUARD | Sustains it | 1 |
| 3 | `helm store lint` / `helm clarity` | L | Judgment-assist (+ deterministic blocking subset) | Guards against regress | 1, 2 |
| 4 | Coinage nudge: concept-check not term-check | S | Judgment-assist (deterministic concept-check) | Kills the split *source* | 1, 5 |
| 5 | `helm store canon <text>` forward-lookup | S | Judgment-assist | Enables 4 + AX | 1 |
| 6 | Make `hits` live + retire hygiene artifacts | M | Deterministic | — (convergence + proven defects) | 1 |

Build order = rank order. Lanes 4 and 5 are a fast-follow pair (5 unblocks 4).
Lane 6's two file fixes are already-identified defects and, per standing canon
(fix-in-pass, never defer), get corrected the moment their lane is opened — or
sooner, since they are two-line edits.

---

## Lane 1 — Lexicon synonym-map + probe-fold  **[FIXES CD-RESOLVE]**

**This is the whole point.** It is the union of §8 rows 1 and 2, shipped as one
lane **on purpose**: the fields are inert without the fold, and the fold is
impossible without the fields. Splitting them strands 100% of the payoff, so
they are one atom.

**Exact change**
- Add three fields to `_LEX_DEFAULTS`: `aliases` (CSV of synonym *terms* an agent
  or owner might say), `canonical` (back-pointer on a stub alias entry only),
  `alias_triggers` (CSV of the word-boundary *probe forms* each alias
  contributes). Parse them in `_parse_lexicon`; serialize them in
  `write_lexicon`. **Fix the field-default-class bug in the same pass**: a key
  with no `_LEX_DEFAULTS` entry is silently dropped on rewrite (the proven
  `meme:true` loss) — every new field ships with a default, and add a test that
  a round-trip preserves all three.
- One-line retrieval change: `_probes` returns `{id} ∪ keywords` for lexicon
  today; extend to `{id} ∪ keywords ∪ alias_triggers`. That single union folds
  every alias into the canonical entry's probe set.

**Seam**
- `helm/store/load.py:113-117` (`_LEX_DEFAULTS`), `:151-172` (`_parse_lexicon`)
- `helm/store/write.py:106-151` (`write_lexicon`)
- `helm/store/resolve.py:16-35` (`_probes` — the one union line)

**Why high-leverage** — This is the arithmetic that removes CD's pain by
construction. Before: concept C lives in `lex-foo` + `lex-bar` sharing trigger
`k`, so `df[k]=2` and each entry scores `conf × 1/2` for `k`
(`resolve.py:53,153`), and the two near-dups fight for the same cap-4 slots
(`resolve.py:156-161`). After: C is one canonical document, its aliases fold
into *its* probes, the duplicate leaves the `df` denominator, `df[k]→1`, and the
full `1/df` weight returns to the one entry — which now holds a cap-4 slot on its
own merit. Premises stop guessing phrasings: they key on the rare canonical term
(`df≈1`, top weight) and lean on the canonical lexicon entry's `alias_triggers`
to catch the varied surface words. CD's five re-tunes collapse to one act — wire
the alias.

**Size** M. Data-model + parse + serialize + one retrieval line + round-trip test.

**Kind** Deterministic substrate. Not a guard and not an assist itself — it is
the retrieval mechanism every guard below rides on. Its behavior is fully
determined (probe-set membership, `1/df` scoring).

**Depends on** nothing. First lane.

**Verification (closes the lane)** — Reproduce CD's split live: pick the one
concept CD re-tuned (the console / board-key drift cluster is the worked example
in §2.1), hand-author its aliases into the canonical `.md`, and run
`helm inject --explain` on a "console went stale" turn **before and after** the
fold. Assert the *effect*: the canonical entry's `df` for the shared probe drops
and its score rises above the cap-4 line — not merely that nothing raised. This
is the measured proof the design promises.

---

## Lane 2 — `helm store alias` verb + collision gate  (sustains the fix)

**Exact change**
- New verb `helm store alias <canonical> <synonym> [--triggers csv]` that
  appends the synonym to the canonical entry's `aliases`/`alias_triggers`
  instead of minting a competing `lex-*.md`. This is how aliases get authored
  ergonomically once Lane 1 makes the field real (Lane 1's proof hand-edits
  frontmatter; this verb makes it a workflow).
- New lexicon collision gate at add-time: if an incoming lexicon term's
  `alias_triggers` overlap an existing canonical entry's probe set above
  threshold, **refuse the add and print the canonical name** — "concept already
  canonical as `board-key-drift`; add as an alias, or `--force-new` if genuinely
  distinct." This is the one place lexicon stops being collision-exempt for the
  *different-id-same-concept* case; the *same-id redefine* lane
  (`_GUARD_TYPE` exemption, `cli.py:30-31`) stays open.

**Seam**
- `helm/store/cli.py:101-706` (`cmd_store` — new `alias` sub-verb)
- `helm/store/cli.py:~359-385` (new lexicon branch beside the existing
  `_near_dup` call and the candidate guard)
- reuse `helm/store/index.py:179-193` (`_near_dup`) as the similarity primitive,
  **but keyed on probe-set overlap, not statement Jaccard** — statement-Jaccard
  misses synonym-split by construction (different wordings score < 0.8, the
  proven WARN-only threshold).

**Why high-leverage** — Lane 1 fixes the concepts already split; this stops new
splits at the source and makes deconfliction a one-liner instead of a manual
frontmatter edit. It converts a one-time cleanup into a self-sustaining
invariant: from here on, an attempt to coin a second term for a live concept is
refused with the canonical name in hand.

**Size** M.

**Kind** GUARD. The collision refuse is a hard, deterministic block (probe-set
overlap is set math). The verb is neutral tooling that rides with it.

**Depends on** Lane 1 (the fields must exist, parse, and serialize).

**Verification** — Add a term whose triggers overlap a live canonical entry;
assert the add is refused **and the canonical name is printed** (assert the
message, not the absence of a raise). Add a genuinely distinct term; assert it
lands. Round-trip an aliased entry through the verb and confirm inject renders
the canonical term for an alias-only turn.

---

## Lane 3 — `helm store lint` / `helm clarity` — the canon lint

**Exact change** — New verb `helm store lint` (aka `helm clarity`), a sibling to
`check_lexicon_dead_vocabulary`, wired into `CHECKS` so it also runs in the
health pass. Three checks:
- **(a) Synonym drift** — a non-canonical term used for a concept that already
  has a canonical entry: look the token up in the alias map; if it is a known
  alias, flag it **and name the fix** ("used `stale-console`; canonical is
  `board-key-drift`"). This is the STE `BANNED` substitution table sourced from
  the store's own alias map instead of a hardcoded list.
- **(b) Undefined high-frequency jargon** — a term used often, not a member, not
  a known alias → surface it as an admission candidate ("`herdr-host` used 14×,
  undefined — add lexicon or map as alias?"). Requires live `hits` (Lane 6) to
  be meaningful; ship the frequency scan now, let it sharpen when 6 lands.
- **(c) Term hygiene** — extends the dead-vocabulary check: flag trailing
  punctuation in `term`, a numeric `kind`, and an `aliases` entry that is itself
  a live canonical term (an alias must not out-rank its own canonical).

**Seam** — new sibling to `helm/doctor.py:193-219`
(`check_lexicon_dead_vocabulary`); wire into `CHECKS` at
`helm/doctor.py:~517-519`. Deterministic subset also called at the add-gate
(`cli.py:~359-385`) to block/normalize; advisory `--comms <text>` pass over
dispatch/board/PR text.

**Why high-leverage** — This is the **owner-visible checkability surface** and
one of the three lanes the brief names outright. It is where the STE
"judgment-once-in-the-die, mechanical-stamp-after" split becomes a shipped tool.
It ranks below 1 and 2 because its synonym-drift check has nothing to check
against until aliases exist (Lane 1) and are authorable (Lane 2) — it *guards*
the fix rather than *making* it.

**Size** L. Three checks, a new verb, add-gate wiring, doctor wiring, a comms
pass.

**Kind** Judgment-assist, with a deterministic blocking subset. Membership /
alias-lookup / frequency / shape are deterministic and may block at add-time;
concept-identity, word-sense, still-true, and admission are surfaced to `/learn`
and the owner, **never decided by the lint**. Blocking is restricted to the
deterministic column — the lint must never block on judgment.

**Depends on** Lanes 1 + 2. Check (b) sharpens after Lane 6.

**Verification** — Seed one known alias in a comms string → assert (a) flags it
and prints the canonical fix. Seed a high-frequency undefined token → assert (b)
lists it as a candidate. Run against the two live artifacts below → assert (c)
flags `term: stdlib-only:` and `kind: 1.0`.

---

## Lane 4 — Coinage nudge: concept-existence, not term-existence  (kills the split *source*)

**Exact change** — The 3-strikes coinage nudge currently gates on whether the
**exact term slug** is already stored: it builds a `known` set from entry ids and
terms and does `if key in offered or key in known: continue`. So a term whose
*concept* is already canonical under a different word still trips the "define
this new coinage" nudge — the detector manufactures synonym-split every N turns.
Change the check to **concept existence first**: before nudging, look the
candidate up via the alias map / the `canon` lookup (Lane 5). If a canonical
entry already covers the concept, flip the nudge from "mint a new `lex-*.md`" to
**"use the canonical term `X`."**

**Seam** — `helm/inject/_whisper.py:108-156`, specifically the `known`-membership
test at **`_whisper.py:134`** (this is the real decision seam; the design's
`inject/__init__.py:58-69` pointer is the docstring for the same feature). Reuse
Lane 5's `canon` resolver / the Lane 1 alias map.

**Why high-leverage** — Highest leverage *per line changed*. This is the one code
path that actively **produces** the DF-split on a timer; every other lane cleans
up after it. One policy flip turns a synonym-split source into a
canonicalization driver. It ranks 4th only because it needs the alias map (Lane
1) and a concept lookup (Lane 5) to have something to check against.

**Size** S. One decision point, fail-open preserved.

**Kind** Judgment-assist driven by a deterministic concept-check. The
concept-exists test is deterministic; the output is an advisory reflex-lane nudge
("use `X`"), not a hard block — coining is still allowed, just steered.

**Depends on** Lane 1 (alias map) and Lane 5 (`canon` lookup). Fast-follow to 5.

**Verification** — With a canonical concept seeded and its alias unstored, drive
the candidate term to `COINAGE_STRIKES` distinct turns; assert the nudge says
"use canonical `X`", not "define a new term". With a genuinely novel term, assert
the original "define it" nudge still fires. Assert the message text (not merely
that no exception raised).

---

## Lane 5 — `helm store canon <concept-text>` forward-lookup verb  (enables 4 + AX)

**Exact change** — New verb `helm store canon <free-text describing a concept>`
that returns the canonical term for that concept. This is the query the store
lacks today: `resolve` is keyword-match over entries, `get` needs the id already
— neither answers "what do we *call* this?". It reads the alias map in the
index/translate direction.

**Seam** — `helm/store/cli.py:101-706` (`cmd_store` — new `canon` sub-verb),
reading the Lane 1 alias/probe data.

**Why high-leverage** — Small, but it is the keystone the PERCEIVE leg and Lane 4
both need: the explicit "what is the approved noun?" lookup. It also *is* the AX
face of the translation layer (§5) — agent-words → canonical term. Ranked just
below 4 in the table because 4 is where its payoff lands, but **build 5 first**
within the pair (it unblocks 4).

**Size** S.

**Kind** Judgment-assist (an agent-facing query surface; it answers, it does not
enforce).

**Depends on** Lane 1.

**Verification** — Query with an alias phrasing of a seeded concept → assert it
returns the canonical term. Query with unrelated text → assert an empty/"no
canonical" result, not a wrong guess.

---

## Lane 6 — Make `hits` live + retire the two hygiene artifacts

**Exact change**
- `hits` is a **proven dead counter** — written `prev.get("hits") or "0"` and
  never incremented. Increment it on every resolve/inject fire so the vocabulary
  can converge from use: high-hit aliases confirm a real synonym, zero-hit
  entries become retire candidates, high-hit *undefined* jargon is the admission
  radar that sharpens Lane 3(b).
- Fix the two proven live defects in the same pass (identified-fix → fix
  in-pass, never file): `lex-stdlib-only.md` has `term: stdlib-only:` (trailing
  colon → malformed probe/id); `lex-drain.md` has `kind: 1.0` (a confidence value
  mis-filed into the kind slot).

**Seam** — increment at `helm/store/resolve.py:161` (the `resolve_prompt` return
path) and `helm/inject/_entries.py:155-168` (the render path); counter written at
`cli.py:487` / `write.py:124`. File fixes: `lex-stdlib-only.md`, `lex-drain.md`.

**Why high-leverage** — This is what lets the design honestly say
"self-maintaining": alias table + hit telemetry + lint is the convergence
engine. Without live `hits` it is a slogan. It ranks last only because 1–5 must
exist for the telemetry to describe something; the two file fixes are pure
in-pass debt repayment and could be pulled forward independently at any time.

**Size** M (the increment must not slow the hot inject path — write-behind /
batched, fail-open, same discipline as the ledger).

**Kind** Deterministic. `hits` is deterministic telemetry that *enables* Lane 3's
judgment surfaces; the two file edits are deterministic defect repair.

**Depends on** Lane 1 (so telemetry attributes to canonical entries, not
still-split ones). The two file fixes depend on nothing.

**Verification** — Fire a resolve/inject for a known entry twice; assert its
`hits` incremented by 2 across a round-trip (assert the count, not the absence of
error). Assert the inject hot path shows no measurable regression. Re-run Lane
3(c) → assert both artifacts now pass.

---

## Notes on the cut

- **Why Lane 1 merges §8 rows 1+2.** Fields without the fold change nothing on
  retrieval; the fold without fields cannot compile. The measured DF-split payoff
  only exists when both land together, so they are one lane. This is the only
  merge — every other §8 row is its own lane.
- **The two named "candidate lanes" from the brief** (deconfliction-model and
  resolve-upgrade) are exactly this Lane 1, deliberately fused for the reason
  above. The third named candidate (canon-lint) is Lane 3.
- **What ships the CD fix end-to-end:** Lane 1 delivers it; Lane 2 keeps it from
  eroding; Lane 4 stops the automated source that recreated it. If only one lane
  can be built, build Lane 1 — it is the fix, proven against CD's own split.
