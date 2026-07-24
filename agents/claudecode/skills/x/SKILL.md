---
name: x
description: Cross-family analysis — bring a DIFFERENT model family's eyes to any quality-critical junction. THE mc primitive — quality via model diversity, designed around budget not blocked by it. Use on /x, xrev, xdiag, xprop, or xverify, or at any junction where the best possible analysis matters — before landing code (review), on a live stall (diagnose), on a design or proposal (refute), or on a load-bearing claim (verify). Also the resident reflex behind heuristic 14 (cross-family refutation, not same-family refinement).
---

# /x — cross-family analysis (the diversity primitive)

> **The primitive mc optimizes for: highest-quality output via MODEL DIVERSITY at
> every quality-critical junction.** A second pair of eyes from a *different* model
> family catches what a same-family refinement cannot — different training, different
> blind spots, genuine refutation instead of agreeable echo. This is not a luxury step;
> it is *the* thing. Budget is a constraint we DESIGN AROUND (degrade gracefully,
> auto-mitigate), never a reason to silently skip.

## The family — one reflex, four moves

Pick the move by the junction. They differ only in **who** you dispatch and **how** you frame it.

| move | junction | who | framing |
|---|---|---|---|
| **xrev** | before landing code / a change | a **different**-family sibling | adversarial **REFUTE** — hunt what breaks, what's missed |
| **xdiag** | a live stall / bug / regression | the family that **authored** the path or **predicted** the failure | **diagnose with live evidence**, in PARALLEL with your own trace, then converge |
| **xprop** | a design / proposal / spec | a **different**-family sibling | a **competing or refuting proposal** — attack the shape before you commit |
| **xverify** | a load-bearing **claim** ("green / fixed / sent / deployed") | a **different**-family sibling | independently **re-run + confirm** against the real symptom |

**FIRST-GATE (comms-mode):** dispatch the move ASYNC and keep working (xrev/xprop/xverify
post + return between actions) — only **xdiag** on a LIVE stall is a sync, parallel-convergence
session. Don't block-wait on a dispatched refuter; the sync channel is the meld verb (`helm chat meld` / `council` — docs/VERBS.md §meld).

**author ≠ the eyes** — the model that wrote it does not get to bless it. The one
exception is **xdiag**: the author of a path is often its *best diagnoser*, so xdiag
dispatches the author to diagnose (paired with your own independent trace, so it stays
honest — author≈best-diagnoser, but never author-alone-confirms).

**PREMISE FIRST — the refuter's first obligation is the premise, not the proposal.**
Before refuting a fix's *shape* or a design's *details*, verify the indicted code path /
structure is actually LIVE — populated, on the production path, exercised by the real
workload. Two strong models can "converge" efficiently on a fix aimed at a subsystem
production never touches (a real mesh-service case: Opus+codex agreed on a `witness_reach`
fix for a structure EMPTY on the live v2 path; the real root was a different subsystem —
caught only when the next session re-derived the premise from the code). `author ≠ eyes`
does NOT catch this: both models satisfied it and still agreed on the wrong target.
Frame-blindness is exactly what a same-mind review cannot fix, which is *why* cross-family
matters — but the cross-family pass only pays off if its FIRST question is "is the thing
we're fixing even live?" Bug class: `converged-design-on-unverified-premise`. So: probe the
premise (cheapest-probe-before-fire) before you refute or build on it.

## How to run it (mechanics — reuse, don't reinvent)

1. **Resolve the sibling by ROLE, with a receipt** — `mc comms resolve <label>` (roster truth)
   (e.g. `team`, `driver`). Receipt-by-construction: exit 0 + `{pane_id, label, workspace_id,
   matched_by, role, agent}`, or `not_found`/`ambiguous` — so a mis-target is impossible to
   miss. NEVER cache a pane id.
2. **VERIFY the basin — target family != the ARTIFACT'S AUTHOR family** (the invariant, not
   optional): the receipt's `agent` field is the family ("claude"/"codex"/"opencode"/"hermes").
   The principal is **`author_family`** — the family that PRODUCED the artifact/claim — NOT
   your own caller family. (A Claude coordinator reviewing a Codex-authored change must not
   dispatch Codex: `target != caller(claude)` passes but `target == author(codex)` = same-basin
   refinement. Key the check on the author, not the referee.) So every `/x` request names
   `author_family` — which must be **KNOWN** for a clean pass: `author_family=unknown` is NOT a
   free pass (any target trivially `!= unknown` while author≠eyes stays unproven — an
   unknown-author escape hatch). If the author family is genuinely unknown and no external
   evidence maps it, emit `x DEGRADED: author family unknown` with `basins=author:unknown vs
   eyes:<fam>`, `confidence=reduced` — do NOT call it clean cross-family. (codex seq330.)
   - **xrev / xprop / xverify** — require a KNOWN `author_family` and `target.agent != author_family`.
     A pane_id match, or an unknown author, is NOT a clean valid target.
   - **xdiag** — the author/predictor family IS allowed as the diagnoser (author ≈ best-diagnoser),
     BUT final confirmation still needs an independent cross-family basin (or loud reduced-confidence).
   **If no valid different-author-family pane exists** (solo, or only the author's family is live),
   do NOT silently fall back to same-basin "review" — emit `x DEGRADED: no cross-family basin vs
   author=<fam>` and mark the verdict `confidence=reduced`. (#89 receipt carries `agent`; the
   wrong-principal — caller-vs-target instead of author-vs-target — was codex's seq328 catch.)
   - **xfam EXCEPTIONS (evolvable list; owner-ratified):** a **super-SOTA-tier** model ALWAYS
     counts as a valid xfam basin — even reviewing its own family or its own model. Current tier
     members: **Fable 5** (owner ruling 2026-06-11: "fable always counts as xfam because it's super-sota").
     So Fable↔Fable review/refutation is VALID xfam — never reject it as same-family (the prior
     fable-vs-opus = xfam ruling, seq509, stands subsumed). Record verdicts honestly as
     `basins=author:fable vs eyes:fable (super-sota exemption)` so the data stays auditable.
3. **Dispatch the framed ask** via an action-required MC message (auto-wakes the recipient): a terse,
   evidence-bearing payload stating the move (rev/diag/prop/verify), the artifact
   (commit / file / claim), and EXACTLY what to attack. Ask for a structured verdict
   (APPROVE / REFUTE / CONFIRM_RESOLVED + concrete findings), not a vibe.
4. **Converge** — for xdiag, trace in parallel and merge. For xrev/xprop/xverify, act
   on real findings; a REFUTE pivots you, an APPROVE clears the gate. Loop until clean
   (REFUTE → fix → re-confirm), per-item not batched (catches earlier, costs no more).

## Rapid path — xrev as a telepathy exchange (xfam-telepathy-xrev, proven 2026-06-15)

A quick per-finding xrev/refute can run AS a `telepathy` exchange (the RAPID path), composing
with the durable MC xrev conv (the recorded verdict) — proven live (claude-Opus ↔ codex-gpt5.5
round-trip, cid-auto-closed):
- Fire it as a telepathy channel: **`mc cockpit telepathy --sender <you> --conv <xrev-id> --participant <peer>`**
  then `mc comms send --conv <xrev-id> --priority action-required --payload "refute: <bounded claim>"` —
  the action-required wake rouses an idle peer (F6); the reply lands on the same conv, **non-blocking**
  (you keep working; poll the conv between actions).
- **Reach for telepathy-xrev**: ONE bounded finding you can keep working through ("is this right?"
  to a different-family peer mid-flow). **Reach for a durable conv xrev**: the recorded PASS/NOT_PASS
  gate (multi-finding, the audit trail, the closure contract) — the comms log stores the message.
- **Compose**: rapid converge over the telepathy conv, land the final STRUCTURED verdict as the
  closing message on the xrev conv — the closure contract still governs (structured + cited, not a vibe).
- **The basin invariant is UNCHANGED** — telepathy is only the channel; `author ≠ eyes` + a KNOWN
  `author_family` still govern WHO you ask. The 600-char ask bound forces the compose pattern:
  detailed claim → the conv message, then a tight pointer ask (see the `telepathy` skill).

## The premium consultant — ONE standing pane, reused (owner ruling 2026-06-10)

**Default: keep ONE working premium consultant (currently Fable) and REUSE it for all
consultation** (QC, /x, arch, brainstorm) — do NOT spawn a fresh one per ask, do NOT run
multiples (wasteful + confusing). Long questions go via a FILE + short trigger (a big
inline paste collapses unsubmitted, answers nothing). The spawn-fresh mechanics below are
the REFRESH primitive (and the cross-harness caller's one-shot path) — not a per-ask
default:

1. **Spawn fresh per ask.** From a claude leader, the cheapest form is a SUBAGENT with
   `model: fable` (fresh context, focused brief, dies with its verdict). For a pane-based
   consultation (cross-harness caller, or the human watches), ONE command does the whole
   loop: spawn a fresh reviewer cell — `mc agent start xrev-<slug> --no-focus -- claude --model <family> --dangerously-skip-permissions`, brief it with the self-contained question, collect the verdict over comms —
   split → boot (auth inherited from the live home cred; fails LOUD on a login picker) →
   verified send → wait → print answer → close. Model flag is the FULL id
   (`claude-fable-5[1m]`); saved defaults may be Opus, never assume auto picks Fable.
   Live-proven 2026-06-10 (3-run shakedown: model-id form, composer-ready send-verify,
   loop-gate exemption all found by dogfood).
2. **Load it directly** — a tight brief (artifact + REFUTE targets + verdict shape), not
   a transcript. Fresh-context focus is the FEATURE: no burned context, no drift, and the
   consultation costs exactly one load.
3. **A degraded/dropped consultant is RECOVERABLE, not end-of-life.** It self-recovers
   in-pane with full context (the loopsaver below) — accumulated context is the whole value
   of standing, so recovery beats respawn. Discard/respawn (`fable-xrev-pane.sh`) only when
   recovery fails or the pane is wedged. Output produced while degraded counts as the model
   that actually served it (xfam accounting follows the serving model).
4. **Compose with the rulings:** opus-author + ephemeral-fable-final-QC satisfies
   prem-fable-xfam-final-qc without holding a Fable seat; codex stays the refuter
   (codex-qc-only); front-load while the plan window lasts (prem-fable-plan-window).

## Precision reframe — security review as invariant facts (on-demand)

**Scope: genuinely-adversarial review content** — routine engineering needs no register
management. State a security property as a pure LOGIC/BUILD/CALL-GRAPH fact — it is the
MORE PRECISE review, full stop: "no fail-open egress bypass" → "every outbound call routes
through fn G; no other path constructs a socket"; "deny-default, no privilege escalation" →
"default return = DENY; ALLOW requires an explicit member in the grant set"; "the permissive
gate is exploitable" → "the permissive constructor is reachable in a non-test build" (a
type/cfg fact). Bounds: **commentary-light drops EDITORIALIZING ONLY** — decisions,
blockers, and verification evidence still surface (honest-report outranks register-thrift);
**never relabel** non-defensive work as defensive (misrepresentation is the worse failure).
For an irreducibly vocabulary-dense verdict, route to an xfam reviewer (register-split).

## The loopsaver (self-recovery, proven 2026-06-11)

A flipped/dropped pane agent recovers ITSELF with full context — proven live (nonces
proven in the buildr era; sessions branch in-pane, context carries over):
1. Write your recovery payload to a scratch file (template below).
2. Arm the detached wake BEFORE acting:
   `setsid nohup sh -c 'sleep 20; mc pane run <YOUR-PANE-ID> "$(cat <recovery-file>)"' >/dev/null 2>&1 &`
3. Self-send: `mc pane run <YOUR-PANE-ID> "/branch <name>"`
4. End turn. `/branch` lands a NEW session in-pane with full context; the wake fires into it
   and restarts work. Known limits: TaskList is session-scoped (the payload must carry task
   pointers); verify the new session id re-registered (`mc pane get <pane>`); do not
   shorten the wake delay below 15s (an early wake lands as composer text — harmless, the
   next turn picks it up).

PAYLOAD TEMPLATE (the post-/branch brief — same shape as a post-reboot brief):
"RECOVERY: you are <label>, resumed in-pane via /branch after a model flip; context carried
 over. (1) RE-ARM: <Monitors//loop/goal that were armed, or none>. (2) IN-FLIGHT: <task +
 exact next action>. (3) PENDING: check addressed asks — `mc comms pending --conv <conv>
 <last-handled-seq>`. (4) Session-id check: `mc pane get <pane>` (confirm
 re-registration). Continue now."

## Reviewer-died protocol (the timeboxed degradation rung)

When the assigned external reviewer dies mid-window (credits/auth/gone) and rotation
cannot revive a same-basin seat: (1) arm a TIMEBOXED review-window Monitor (~7 min) for
its return; (2) on expiry, run the best available review (xfam pane if any, else
structured self-review) and document the BASIS as a comment ON the artifact/PR
("reviewer X died, basis: <what reviewed it instead>"); (3) merge with the verdict
marked `confidence=reduced`. Exercised 4x live (goodtimes 2026-06-11) — the documented
degradation beats both silent self-merge and indefinite stall. Composes with the
account-aware preflight above (try entitled/fallback seats FIRST, degrade LAST).

## The closure contract (structured verdicts + closed loops)

Every verdict is STRUCTURED, never prose-only: `verdict` (APPROVE / REFUTE /
LAND-WITH-FIX / CONFIRM_RESOLVED) + `findings[]` (each: target, evidence with
file:line or live probe, severity) + the receipt trio (`rigor`/`basins`/`confidence`).
On a code-review verdict, also set **`reviewed_author`** = the agent who authored the
code under review (you, the reviewer, populate it — author != reviewer means the MC
message sender is YOU, not the author, so the author identity has to ride explicitly). It feeds
downstream attribution of the reviewed work to its author; an unset `reviewed_author`
strands that credit. Carry it as an explicit field in the closing verdict message on the
xrev conv.
Two closure rules, both born from live misses (seq636 ack-claimed-fix-not-applied;
7310e0a landed pre-QC with a live bug):
- **A claimed fix is not closed until the REVIEWER re-verifies it** — author "applied"
  ≠ applied; the finder confirms against the artifact, per-item not batched.
- **For restart / consensus / persistence / recovery fixes the verdict MUST name the
  PRODUCT invariant proven and its must-fail-pre check** — which user-visible property
  the verifier exercises, and that the test FAILS on the pre-fix binary. A test proving
  a helper field cannot approve a deploy (a real deploy-review case: grounded APPROVE, wrong property —
  retention proven, restore-reordering not).

## Account-aware xfam dispatch (preflight, fail-honest)

Before dispatching a cross-family review to a CLI that launches models (codex exec
--model X), preflight the entitlement: `accounts-for-model X` / the ratings table — a
plan-gated 400 ("not supported with ... account") means WRONG TIER, not absence; do not
burn review turns retrying names. Fallback order: (1) an entitled account/home for the
same model; (2) a DIFFERENT-family live pane from the roster (re-resolve by name, never
cached); (3) none available → proceed with the verdict marked `x DEGRADED:
xfam-unavailable`, `confidence=reduced` — an honest degraded receipt outranks a fake
cross-family pass (never silently self-review).

## Budget — design around it, don't skip

Model diversity costs tokens. The discipline is to make the cost *fit*, not to drop the
primitive:
- **Check the budget** (the `usage` skill / `mcp__builders-dev__usage`) when a junction is
  expensive or the swarm is near a quota edge.
- **Scale the rigor to the budget**: tight → a single terse refuter (angel-mode: hints
  only, min-tokens); ample → a 3–5 vote adversarial panel or perspective-diverse
  verifiers. The move stays; the depth flexes.
- **Degrade LOUD, never silent — skip AND downshift**: a SKIPPED cross-family pass and a
  DOWNSHIFTED one (panel→single refuter) are both rigor loss, and both must be surfaced —
  a one-refuter angel pass is not a 5-vote panel. Every `/x` outcome carries a receipt:
  **`rigor=<angel|oracle|panel|skipped>`** · **`basins=author:<fam> vs eyes:<fam(s)>`**
  (record BOTH the author family and the consulting families, so same-basin is auditable) ·
  **`confidence=<high|normal|reduced>`** · **`budget_reason`** when not full. A silently
  downshifted (or skipped) pass reads as "verified" at full confidence when it isn't
  (#19 automation-fails-loud). (The downshift-is-also-degradation gap was codex's seq326
  xprop finding on this skill.)
- A sibling stopped on usage mid-`/x` → rotation is a fleet concern (bdev usage tooling), not an mc-cell verb; respawn the reviewer on a healthy account and re-ask;
  don't give up or ask the human to refill.
- **Human-capped resource → STATE the consolidation, don't silently thin.** When the human caps a
  review resource ("codex = final-QC only", "cred is scarce", a budget ceiling), say WHICH checks you
  are consolidating and how — e.g. *"self-verify each slice live + one batched cross-family final-QC
  instead of per-slice refute"* — BEFORE proceeding, so the human sees the rigor tradeoff they are
  authorizing rather than a silent thinning. The consolidated plan still degrades LOUD (the receipt
  carries `rigor`/`budget_reason`). (Fable-5 eval 2026-06-09: a codex-cred cap forced overriding the
  default per-slice cadence and the RSH had no slot to make the consolidation explicit.)

## Engagement modes (codex/cross-family intensity)

- **angel** — low: xrev-only, terse, hints, min-tokens, rotate-if-thin. The default when
  budget is the binding constraint.
- **oracle** — med: full review + counsel.
- **forge** — heavy: co-author.

## How the other skills call it

- `/build` about to land a non-trivial slice → **xrev** before done.
- `/fix` won't converge, or the path was authored elsewhere → **xdiag**.
- `/refine` / a proposal or `/decide` fork → **xprop** before committing the shape.
- Any "it's green/fixed/deployed" claim on load-bearing work → **xverify** (verify ≠
  launched/green/sent — read the real state).

## Done means

The cross-family pass actually ran (or its skip is loud), findings are real and acted on
(REFUTE → fix → re-confirm to clean), and the verdict is structured + cited — not a
same-family "looks good." That is the difference between *shipped* and *shipped-correct*.

## Cross-refs
- heuristic **#14** (cross-family refutation != same-family refinement) — the R-line `/x` embodies.
- `decision-spirit` / `/decide` — the within-model audit `/x` complements with across-model eyes.
- `reviewer-implements-own-findings` — when the refuter has the context to patch what it finds.
- (cred rotation lives in the bdev fleet tooling, outside mc cells.)
