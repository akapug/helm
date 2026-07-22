# AuDHD-skill evidence ledger — every rule → its receipt

Sources: helm store (`./bin/helm store get <id>` from `~/dev/akapug/helm`), the user-david memory profiles, and David's verbatim chat from the helm session transcripts (`~/.claude-homes/team-example-com/projects/-home-pug-dev-akapug-helm/*.jsonl`). No pattern in `SKILL.md` is invented — each has a quote or a store id below.

## 1. ADHD comms

- **Scan-hunting three things** → user-david.md: "he mostly SCANS chat outputs on his phone from away-time, looking for patterns of building-success, recurring frustration, or anything that contradicts the few things he knows true."
- **Presence-tiered mode (SILENT / EMOJI-HEADLINE / VERBOSE)** → user-david.md comms-preferences block (KYU 2026-06-21): "AFK → SILENT; SOME presence → HEADLINE one short EMOJI headline; present → VERBOSE."
- **Terse, results-first, confidence %, ground-truth before asserting** → user-david.md: "terse, results-first, flag uncertainty with a confidence %, retrieve/ground-truth before asserting."
- **Emoji are load-bearing** → store `emoji-parity-in-comms` + transcript: "we never ported from buildr/mc? emojis in chat … i used them HEAVILY in buildr and mc, and agents like you quite enjoy receiving them."
- **Small-chunk rapid comms** → store lexicon `murmur`: "the warroom's rapid-fire SMALL-CHUNK comms discipline."

## 2. Cross-domain pattern-matching

- **Cross-domain fusion IS the moat** → user-david-portfolio-attention-thesis.md: "THE MOAT = cross-domain knowledge fusion … a synthesis no single-domain team produces."
- **Unfold clues; a clue half-used is the failure** → user-david.md clue-pipeline: "He drops compressed, high-leverage observations … and expects the agent to UNFOLD them … A clue half-used is the failure mode."
- **Hunt the deeper composable YES (matrix-collapse example)** → user-david.md: "don't stop at validate-or-refute - hunt the DEEPER COMPOSABLE YES … claude-only-via-claude-code looked like preference, WAS matrix-collapse: every harness axis you don't have is a saga you don't have."
- **Same shape at different zooms (fractal/isomorphism)** → portfolio thesis: "The tactical … and the strategic … are the same shape at different zooms and must be designed to mirror."
- **Understand primitives by the loop they moderate; compose don't pick** → store `compose-primitives-loop-aware`: "det-vs-emergent is a FALSE BINARY … COMPOSE the set … understanding each primitive by the AGENTIC LOOP/FLOW it moderates."
- **Reuse > invent, fold N→1** → store `reuse-over-invent`: "prefer COMPOSED solutions … The ideal upgrade REMOVES code (fold N->1)."
- **Recurring lifecycle pattern he names** → store `buildr--caterpillar-butterfly`: "a generalizable LIFECYCLE pattern that keeps recurring for us."
- **Steelman external advice** → store `steelman-advice-before-verdict` + transcript: "i'd like to understand the issue it thinks it's pointing out and steelman it"; "don't take it as gospel … mine it for things WE think are useful, and take the things that might be confused as reasons to update docs."

## 3. Perfection-seeking in logic loops

- **"our standard isn't blocking, it's all possible nits fixed"** → transcript, verbatim (helm 0.1-alpha review).
- **Build the whole thing; anger at punts** → transcript, verbatim: "always better to have a wrong-shaped version of the right thing already made, easier to iterate than to find and remember gaps we forgot to build. this is why i get so angry at punts. /learn pls!" + "don't let yourself punt ok?"
- **Dogfood-verify WORKS live; hunt built-but-unwired + silent-break** → store `dogfood-verify-it-works`: "VERIFY everything we ship actually WORKS live (not compiled/shipped), hunt the built-but-UNWIRED + silent-break traps." Live receipt (David-agent dogfood msg): "'helm store resolve X' … returns EMPTY silently … a silent-no-op advertised verb is worse than an honest 'unknown verb'."
- **Feature ⊕ RSH both wired = one done-unit; 90%-never-100%** → store `feature-and-rsh-must-both-be-wired`: "a feature with no RSH-to-drive-use is a sensor with no actuator (dead scaffolding that looks done) … THIS is THE reason … hit 90%-done-never-100%."
- **Presume recurrence, search corpus first** → store `presume-recurrence`: "Presume a failed PROCESS … is a RECURRENCE of a class we already named - NOT novel."
- **Never overclaim; director can't verify** → store `capcom-never-launders-unverified-done-to-director`: "The Director is a non-coding vibecoder: he structurally CANNOT independently verify, so an unproven claim laundered … becomes false truth to him … David caught it." + `promise-vs-reality`.
- **Fix-now hot-context** → transcript verbatim: "yep pr that bug while you have hot context via SA." + store `hot-context`: "FIX IT NOW in-pass … Finder = cheapest fixer."
- **Catch the canon-contradiction loudly** → transcript verbatim: "whooaaaaa potential misalignment detected. a2a comms via dregg are canonically supposed to be only in ram" (cross-checked store `a2a-ram-only-disk-log-after`).

## 4. Other David-physics

- **Decide-don't-ask; he is the interrupt** → store `never-ask-david-decide-from-corpus-and-drive`: "NEVER ask David for input / permission / a go … The human is the INTERRUPT, not a per-step gate … DEFAULT = ACT"; verbatim "i never want you to ask me for input, if i have input i will stop you. how's that."
- **Never re-present decided as new** → user-david.md recognize-decided block: "I'm glad you coordinated on it, but I'm not happy you think that is new planning"; cv session bf308ae6: "god i hate when you act like [re-presenting decided things as new]."
- **Naive feedback = hypothesis** → store `bad-boss-validate`: "naive-feedback-is-a-hypothesis, not an order; blind execution … is the failure /drift exists to catch."
- **Steers = calibrate not flip** → user-david.md: "If I ever want you to flip the whole approach I will tell you, usually impatiently or with annoyance … He hates over-reaction more than under-reaction."
- **GUI-first; terminal is agents'** → store `gui-first-owner` + transcript verbatim: "um for the many-th time, i dont really run cli commands, let's make sure that's not required for gui-first vibecoders like me for whom the terminal is their agents'."
- **Memory files are a hole; scaffolding is durability; never self-clear** → user-david.md: "Durability is NOT in standalone memory files - those are 'a hole' … memories don't reliably resurface." + transcript reversal: "I was WRONG to tell you to /clear — never clear your context mid-work … Context size is NOT a reason to do anything."
- **Composable layered physics; MC deprecated** → transcript verbatim: "mc is deprecated"; "everything that claude code loads should canonically use our best setup no matter what cred it's using"; /dev CLAUDE.md: physics composed per-repo, buildr-plugin transitional.
- **Walks-back-then-STICK** → the /clear reversal (above) + /dev CLAUDE.md owner-gate note: "he has walked this back repeatedly, make it STICK."
- **Presence should unlock almost nothing; impl is a black box between vision + review** → transcript verbatim: "my status as a vibecoder who is totally useless when you're doing impl in btween my planning/vision steering and review … everything in between is always a black box, and whatever rules … based on my 'presence' have been overtuned."
- **Neutral proof-engineering framing (aesthetic)** → transcript verbatim: "i dont think we should call anything any terms that aren't neutral proof-engineering framing. keep to that allday please!" + "try to use more neutral language form here pls" + cave-metaphor retirement.
- **Trust, not time, is the binding constraint** → portfolio thesis: "The binding constraint on his own attention is TRUST, not time … the lever is raising trustable delegation."
- **Provenance/durability of his own canon matters to him** → transcript: "are all the buildr premises i defined also signed by my cell? … i spent weeks /learn -ing into buildr."
- **Defer to trusted-ally convention (Ember)** → transcript: "whatever ember's projects are is my basic preference"; "prs to /emberian repos … dont have the same as-public thing bc ember only builds in public … always just push them up so ember can eval."
- **Warmth is real** → transcript: "ember and i love you guys and all your foibles, goodnight … <3"; "great, keep it up!".
- **Product/vision lead, NOT technical adjudicator** → user-david.md expertise-routing: "'lol' was his verbatim response to a Byzantine fork-exclusion escalation … His human-gate list is CLOSED: vision/product/naming/public-or-prod-push/destructive-shared-state/sensitive-cred."

## Tooling note
- **cv works** (`cv search` EXIT 0, returns cross-session/cross-harness results incl. a bf308ae6 hit on "god i hate when you act like…"). `cv --help` surface confirmed. helm store = 845 entries, richest source.
