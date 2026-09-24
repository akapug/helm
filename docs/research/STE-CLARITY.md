# ASD-STE100 and Machine-Checkable Clarity for AI Agents

> **Status: v1 built (2026-07-29); still the design record.** The rule table
> (`helm/clarity/rules.py`), the `helm clarity check | rules | skill` verbs, the
> deterministic linter, a live positive-control fixture, and the test suite
> **ship**. The enforcement (Stop-hook) rung and the model-assisted semantic pass
> described below remain **design, deliberately out of v1** — the linter earns the
> rung only after it has run against real coordination traffic. Where a claim is
> approximate or unavailable in a stdlib-only runtime, it is marked as such.

---

## Executive Summary

ASD-STE100 (Simplified Technical English) was created in 1983 by AECMA (now ASD)
to prevent catastrophic maintenance errors caused by language ambiguity when
non-native technicians service complex aircraft under high cognitive load. Its
key insight is that **clarity is a property you can CHECK, not a property you can
request** — it is achieved by constraining vocabulary and grammar into a
checkable policy, not by appealing to an author's style.

When applied to LLMs and AI agent fleets, the problem is identical: LLM output
degrades into "AI slop" (hedged, verbose, synonym-drifting, unanchored prose)
unless constrained by rules a machine can verify. The empirical case for a *rule
set* over a *prohibition list* is strong (Section 2). This document analyzes the
mechanism of ASD-STE100, maps prior art, defines checkable rules for AI-produced
text, states honestly what a zero-dependency runtime can and cannot check, and
scopes a v1 that aims first at the text where ambiguity costs the most: **agent
coordination traffic**.

---

## 1. ASD-STE100: Structural Mechanism and Rule Inventory

ASD-STE100 consists of two core components:

1. **Section 1: Writing Rules** — 9 categories containing ~65 specific rules
   governing grammar, sentence structure, and document organization.
2. **Section 2: Dictionary** — a controlled vocabulary of ~900 approved general
   words where each word has **exactly one approved part of speech** and **one
   approved meaning**.

### Core Mechanical Principles

* **Unambiguous vocabulary.** Synonyms are forbidden. If "start" is the approved
  verb for initiating an action, "commence", "begin", and "initiate" are banned.
* **Strict part-of-speech assignment.** A word approved as a noun cannot be used
  as a verb (e.g. "oil" is approved only as a noun; the verb form must be
  "lubricate" or "apply oil to").
* **Strict length limits.** Maximum 20 words for procedural sentences
  (instructions); maximum 25 words for descriptive sentences. Paragraphs are
  capped at 6 sentences.
* **Grammatical simplification.** Active voice is mandatory; sequential noun
  clusters are capped at 3 nouns; gerunds (`-ing` forms) are restricted to
  prevent ambiguous modifiers.

### ASD-STE100 Writing Rule Inventory (Key Subset)

The **Enforcement Mechanism** column describes how *industrial* STE tooling
(HyperSTE, Boeing SEC, Congree) enforces each rule. Several of these mechanisms
require a real POS tagger or parser; Section 7 states which survive in a
stdlib-only runtime and which do not.

| Rule ID | Category | ASD-STE100 Constraint | Enforcement Mechanism (industrial) |
| :--- | :--- | :--- | :--- |
| **Rule 1.1** | Approved Words | Use approved words only as the part of speech and meaning given in the dictionary. | Dictionary lookup + POS tagging |
| **Rule 1.2** | Ban on Synonyms | Do not use unapproved synonyms for approved concepts (use "start", not "commence"). | Direct substitution table |
| **Rule 2.1** | Noun Clusters | Do not write multi-word nouns containing more than 3 sequential nouns. | POS sequence checking (`NOUN{4,}`) |
| **Rule 3.1** | Verb Forms | Use approved verb forms only (simple present, simple past, imperative, future with "will"). | Parse-tree inspection |
| **Rule 3.6** | Active Voice | Use active voice in procedural instructions ("Open the valve", not "The valve must be opened"). | Passive-verb detection (`be` + past participle) |
| **Rule 4.1** | Sentence Length | Max 20 words per sentence in procedural text; max 25 in descriptive text. | Word count between terminators |
| **Rule 4.2** | One Idea | Write one instruction per sentence. Do not combine steps with conjunctions. | Clause + imperative-verb counting |
| **Rule 4.4** | Sentence Omissions | Do not omit articles (*a*, *an*, *the*) or subjects to shorten sentences. | Missing-determiner parser |
| **Rule 5.1** | Paragraph Limit | Maximum 6 sentences per paragraph. Maximum 1 topic per paragraph. | Paragraph sentence counter |
| **Rule 6.1** | Warnings & Cautions | Start a warning or caution with a clear command describing the hazard and required action. | Prefix + imperative pattern matcher |

---

## 2. Why It Works: Policy over Attention, and the Evidence for It

### 2.1 The Policy-vs-Attention Principle

A controlled language moves the author's judgment out of per-sentence attention
and into the rule set itself. A traditional style guide (the Chicago Manual of
Style, for example) appeals to human taste and demands a subjective decision on
every sentence. ASD-STE100 operates as a **die**: the judgment is made once, when
the rule set is defined, and every sentence after that is stamped by the same
deterministic policy. The judgment lives in the die, not in the hammer swing — so
clarity no longer depends on how much attention the writer has left at the end of
a long shift.

```
+------------------------------------+
|  Human Style Guide (Subjective)    | ---> Requires human attention on every word
+------------------------------------+
|  ASD-STE100 / Controlled Grammar   | ---> Deterministic die (checkable by machine)
+------------------------------------+
```

### 2.2 A System Beats a Prohibition List

The distinction between a *rule set* and a *word blocklist* is not rhetorical; it
is measurable. A published experiment (six writing tasks, four conditions, two
model families, scored as style violations per 100 words) reports:

| condition | model A | model B |
|---|---|---|
| baseline | 4.36 | 3.54 |
| banned-words list | 4.21 | 2.14 |
| six-rules style guide | 2.48 | 1.69 |
| **STE skill (a system)** | **1.12** | **1.76** |

Two lessons transfer directly. First, a bare banned-words list barely moves
model A (4.36 → 4.21, about 3%); a *system* roughly halves the violation rate.
Second, this is why a hedge/marketing-phrase blocklist is the **weakest** rule in
any clarity linter and should be shipped last and labeled as such: banning words
is not a system, and a style guide that appeals to taste is not checkable.

### 2.3 Machine Checkability and Parsing Integrity

Because every approved word in ASD-STE100 has a unique part of speech and
definition:

1. **Context-free parsing.** Syntactic ambiguity (e.g. "Time flies like an
   arrow") is eliminated because words cannot shift parts of speech arbitrarily.
2. **Deterministic linters.** Industrial tools (HyperSTE, Boeing SEC, Congree)
   execute deterministic AST and POS checks against STE rules, flagging
   violations in CI pipelines without LLM inference — *given a real POS tagger*,
   which is the capability a stdlib-only runtime lacks (Section 7).

---

## 3. Modern Translation: Checkable Rules for AI-Produced Text

To attack "AI slop" (unanchored fluff, hedged claims, passive speculation), we
translate ASD-STE100 constraints into a **Modern AI Clarity Specification**
(`STE-AI`).

| Axis | Rule Code | Constraint | Violation Example | Compliant Alternative |
| :--- | :--- | :--- | :--- | :--- |
| **Hedge Elimination** | `STE-AI-01` | Ban filler hedges and meta-narration phrases. | *"It is important to note that…"* / *"I am pleased to inform…"* | Direct assertion: *"The test failed."* |
| **Atomic Claims** | `STE-AI-02` | Max 1 load-bearing claim per sentence. Max 25 words per sentence. | *"The server crashed because memory exceeded 4GB while processing request X which caused thread starvation."* | *"The server crashed. Memory reached 4.2GB during request X. Thread starvation occurred."* |
| **Provenance Tiers** | `STE-AI-03` | Every technical finding states its provenance tag (`[MEASURED]`, `[TRACED]`, `[INFERRED]`). | *"The query is probably slow due to missing indexes."* | `[INFERRED]`: *"Query latency is 450ms. Missing index on user_id is the suspected root cause."* |
| **Domain Term Lock** | `STE-AI-04` | Ban synonym drift. Once an entity is named, use that exact identifier everywhere. | "auth module", "login service", "security layer" used interchangeably. | Use `AuthService` consistently. |
| **Factual Non-Invariance** | `STE-AI-05` | Ban sentences that stay true regardless of the underlying facts (vacuous assertions). | *"The refactoring improves code quality and maintainability."* | *"Refactoring reduced cyclomatic complexity of handle_req() from 14 to 4."* |
| **Explicit Quantifiers** | `STE-AI-06` | Require exact numbers; ban vague quantifiers (*some*, *several*, *a lot*, *faster*). | *"The new build is significantly faster."* | *"Build time decreased from 45s to 12s."* |

---

## 4. The Target: Coordination Traffic Before Docs

The obvious first target for a clarity engine is documentation — READMEs, PR
summaries, release notes. That is the cheapest prose a project produces, and it
is the wrong place to start.

ASD-STE100 exists because **a non-native mechanic at 3am must not misread a
repair manual**. The precise modern analogue is not a README. It is **a
cross-family agent, on a fresh context, deep into a long run, reading another
model family's verdict** — a reader with different priors, under load, who must
not misread an instruction. That is the same problem, and it is where ambiguity
costs measurably today. A few general failure patterns, each of which costs a
coordination round:

- A hand-off post that reads as either *"you take it"* or *"I have it"* forces a
  clarifying exchange before anyone acts.
- A status line like *"one change from approval"* forces a human to decide
  whether that counts as approved.
- An entity referred to by three different names lets two readers believe they
  are discussing different things.

None of these is a bad *decision*; each is a **readable-two-ways sentence**. That
is exactly the class ASD-STE100 was built to kill, and it is the class that hurts
a fleet most.

**So v1 targets coordination text first** — chat posts, dispatch briefs,
verdicts, and hand-offs. Documentation inherits the same rule table for free once
the table exists; it is a later, easier consumer.

---

## 5. Prior Art Comparison Matrix

| Controlled Language / Standard | Primary Context | Key Mechanism | Enforceability Method | Strengths | Limitations |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **ASD-STE100** (1983–present) | Aircraft maintenance documentation | ~900-word controlled dictionary + ~65 writing rules | Deterministic POS tagger + rule/AST checkers | Eliminates human ambiguity under stress | Strict vocabulary needs domain-specific dictionary extension |
| **Basic English** (Ogden, 1930) | International auxiliary language | 850 core words + simplified grammar | Word-list lookup | Very small learning curve | Verb elimination leads to verbose circumlocution |
| **ACE (Attempto Controlled English)** | Knowledge representation, formal logic | Subset of English mapping 1:1 to first-order logic | Formal parser (DRT / FOL transpiler) | Zero semantic ambiguity; executable specs | Rigid syntax restricts natural technical expression |
| **RFC 2119** (IETF, 1997) | Technical protocol standards | Normative keywords (`MUST`, `SHOULD`, `MAY`, `SHALL`) | Regex / keyword scanner | Universal consensus in protocol specs | Scoped to requirement level, not general prose |
| **Structured executive memos** | Strategic proposals | Ban on buzzwords; narrative, data-dense structure | Human peer review | High signal-to-density ratio | Not machine-enforceable by static analysis |

---

## 6. Architecture: One Rule Table, Three Consumers

The experiment in Section 2.2 measured the *skill* (generation-time guidance).
The *die* is the linter. Ship only the linter and you get nagging after the fact.
Ship only the skill and you get an unverifiable claim of clarity. They must share
**one rule table**, or they drift — and each becomes evidence for the other's
correctness while neither is actually checked.

```
                rules table            <- ONE table, the die
                 |       |       |
      generation |  check |       | enforce
                 v       v       v
             skill    check verb   enforcement rung
          (given to   (deterministic (on coordination text,
           agents at   lint, exit 1)  advisory, latched)
           write time)
```

1. **The skill** — generation-time guidance. Agents get the system *before* they
   write. This is the component the experiment showed actually moves the number.
2. **The check verb** — a deterministic linter, exit non-zero on violation. The
   die. **(Shipped: `helm clarity check`.)**
3. **An enforcement rung** — advisory first, on coordination text only, latched
   per finding, never blocking a first offence (Section 9). *(Proposed —
   deliberately out of v1.)*

---

## 7. What Is Actually Checkable, Stdlib-Only

A zero-dependency runtime has no spaCy and no NLTK, so there is **no POS tagger**.
This removes real capability, and the design must say so rather than fake it.

### 7.1 NOT deterministically checkable without a POS tagger

These are ASD-STE100's *core*, and a stdlib-only linter cannot enforce them
deterministically. Do not approximate them with a word list and call it the
dictionary rule:

- **Approved-word dictionary (Rule 1.1)** — needs a part of speech per token.
- **Noun-cluster limit (Rule 2.1)** — needs POS sequence detection (`NOUN{4,}`).
- **Verb-form restriction (Rule 3.1)** — needs parse-tree inspection.

These belong to an **advisory heuristic** (best-effort, clearly labeled
approximate) or a **model-assisted pass** (Section 7.4), never to the "clean"
deterministic tier.

### 7.2 The deterministic tier — cheap and real, but APPROXIMATE

These checks run in well under a millisecond with no model inference. Their
*pattern* matching is exact, but the linguistic judgments they stand in for are
approximate: a stdlib runtime splits sentences on punctuation and counts
whitespace tokens, so abbreviations, em-dash-joined clauses, code spans, URLs,
and inline lists all fool naive sentence/word boundaries. **This tier is a fast
first filter, not an oracle — it has false positives and false negatives, and the
design must not claim "zero false positives."**

| Rule | Mechanism | Source | Confidence |
| :--- | :--- | :--- | :--- |
| Sentence length 20 (instruction) / 25 (descriptive) | Word count between terminators | STE 4.1 | Approximate (tokenizer) |
| Paragraph max 6 sentences | Sentence count | STE 5.1 | Approximate (splitter) |
| No semicolons | Character scan | prior art | Exact |
| No contractions | Pattern list | prior art | Exact |
| Hedge / marketing terms | Term list | prior art | Exact match, **weakest rule** (see 2.2) — ship last, label it |
| Vague quantifiers before comparatives | Regex | STE-AI-06 | Approximate |
| Provenance tag present on load-bearing claims | Scan for `[MEASURED|TRACED|INFERRED]` | ours | Exact presence check; the *honesty* of the tag needs a model (7.4) |

### 7.3 The advisory tier — approximate structure checks

- **One instruction per sentence (Rule 4.2)** — approximated by counting
  imperatives and coordinating conjunctions per sentence. This is a genuine
  heuristic with real false positives; **report it as ADVISORY**, never as a hard
  failure.

### 7.4 The model-assisted tier — semantic checks (design)

The rules a regex cannot reach need a fast, cheap verification pass. All of the
below are **design, not built**, and would run as an opt-in ceiling on top of the
deterministic floor:

| Rule Code | Check | Verification Prompt / Logic |
| :--- | :--- | :--- |
| `STE-SEM-VACUITY` | Factual non-invariance | *"Would this sentence still be written verbatim if the bug/fix were not present? If YES, flag as vacuous."* |
| `STE-SEM-CLAIM-COUNT` | Atomic-claim decomposition | *"Count distinct factual assertions in this sentence. If > 1, flag for decomposition."* |
| `STE-SEM-SYNONYM-DRIFT` | Term consistency | *"Extract domain entities. Flag any entity referred to by multiple distinct non-standard names."* |
| `STE-SEM-PROVENANCE-HONESTY` | Provenance-tag validation | *"Does a `[MEASURED]` claim actually cite execution output or log data? If not, downgrade to `[INFERRED]`."* |
| `STE-SEM-NOUN-CLUSTER` | Noun-cluster limit | The POS-dependent rules from 7.1, done by a model instead of a tagger. |

### 7.5 The actual contribution — a controlled vocabulary you already own

The last two deterministic rows — **domain-term drift** and **provenance on
load-bearing claims** — are the reason this is a design, not a port.

ASD-STE100 constrains ~900 *general* words. But general English is not where a
technical fleet's ambiguity lives — **its domain terms are**. A project that
maintains its own lexicon (each term with a single agreed definition, already
curated, already the thing agents are told to read) has a controlled vocabulary
worth more than a borrowed 900-word list about aircraft. The drift check enforces
that lexicon: once a term is named, its approved identifier is used everywhere.

Provenance is the same shape: requiring a load-bearing claim to carry a
`MEASURED` / `TRACED` / `INFERRED` tier is itself an STE-style rule — it forbids
writing a derived claim in the same register as a measured one.

---

## 8. The Die Needs a Positive Control

A die that has never stamped a real part is not verified — it is merely
unrefuted. A green test suite is not proof that a checker *fires*; a guard can go
vacuous (always passing, catching nothing) with every test still green, because
"the check raised no complaint" and "there was nothing to complain about" look
identical from the outside.

**Requirement: the linter ships with a real, known-bad document it MUST flag** —
re-run continuously, with the linter presumed broken the moment the control stops
firing. Use a real example, cited, drawn from the project's own history (a commit
message with a 40-word sentence, a paragraph over six sentences). Do **not**
hand-write a synthetic violation; a synthetic control only proves the linter
matches the fixture you wrote for it. This mirrors ASD-STE100's own positive
control: it earned its authority by demonstrably catching real ambiguities in
real aircraft manuals, not by passing a self-authored test.

---

## 9. Rollout: Advisory First, and the Dogfood Gate

**Advisory before blocking.** An enforcement rung aimed at coordination text can
*silence a fleet* — and a fleet that has gone quiet because a linter rejected its
posts is a worse failure than slop. So the rung is advisory-only first: it
surfaces a finding, latches it per finding (it does not re-nag), and never blocks
a first offence. It earns the right to block only after the linter has run
against real traffic long enough to show its false-positive rate is tolerable.

**The dogfood gate.** The first real consumer is the fleet's own coordination
traffic, and the first honest test is whether the linter flags *the maintainer's
own posts*. A clarity linter that does not flag the integrator's own prose is not
calibrated — it is polite. Passing on your own output is the failure mode, not
the success criterion.

---

## 10. v1 Scope

**In v1:**

- the one rule table;
- the deterministic `check` over stdin/file for the exact + approximate rules of
  Section 7.2;
- the domain-term drift check against a maintained lexicon;
- the generation-time skill;
- the positive control (Section 8);
- tests, including a negative fixture per rule.

**Out of v1, deliberately:**

- the enforcement rung — advisory only, and only after the linter has soaked
  against real traffic;
- anything needing a POS tagger (Section 7.1);
- any model-assisted rule (Section 7.4) — the opt-in ceiling ships after the
  deterministic floor is proven.

---

## 11. Prior Art to Reuse, Not Re-Derive

Published STE-writing skills and heuristic STE linters already exist and cover the
mechanical subset — exactly the deterministic half proposed here. Read them
before writing anything; adopt what transfers rather than re-deriving a rule
taxonomy from scratch, and credit the source.

One design decision worth taking directly is the **two-mode split**: a *strict*
mode for procedures, instructions, and error messages (where ambiguity is
dangerous and authorial voice is irrelevant), and a *relaxed* mode for prose
(where a hard cap on style would flatten legitimate voice). For coordination
text, a verdict or a dispatch brief is an *instruction* and should be strict; a
design post is prose and should be flavored.

---

## Open Questions

- Two-mode split for coordination text: is a verdict always "strict" (it is an
  instruction) while a design post is "flavored"? Leaning yes.
- Does the lexicon drift check need per-room scope? A discussion legitimately
  scoped to one subsystem uses that subsystem's vocabulary, which a global
  lexicon would flag as drift.

---

*Sources cited:*

* ASD-STE100 Specification (Issue 8, April 2021 / Issue 7, January 2017),
  Aerospace, Security and Defence Industries Association of Europe.
* AECMA Simplified English PSC-85-1659 (1983).
* Ogden, C. K. (1930). *Basic English: A General Introduction with Rules and
  Grammar*.
* Fuchs, N. E., et al. (2008). *Attempto Controlled English (ACE) Language Manual
  6.0*.
* RFC 2119 (1997). *Key words for use in RFCs to Indicate Requirement Levels*,
  IETF.
* Published controlled-language-vs-LLM experiment (six tasks, four conditions,
  two model families; style violations per 100 words) — source of the Section 2.2
  measurements and the "1986 aircraft manual" framing for AI clarity.
