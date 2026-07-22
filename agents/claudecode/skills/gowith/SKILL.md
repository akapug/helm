---
name: gowith
description: The gowith relational register for MC coordination-state payloads — grammar, MC clause cookbook, and reading primer. Prime with this skill before writing gowith; plain English is ALWAYS accepted (register is additive, never enforced).
---

# /gowith — the relational register for coordination payloads

> Q14-G1 artifact (plan: `<MC_HOME>/prd/MC_GOWITH_COMMS_PLAN.md`, ACCEPTED rev 2). Source grammar:
> Andy Ayrey's `Gowith.md` gist via `buildr .local/primitives/GOWITH.md`. Scope: coordination-STATE
> payload slices (claims, verdicts, status, acks) — NL bodies stay for judgment content (plan §2.2);
> council/xrev REASONING stays free-NL always (§2.4). Kill-switch: stop priming — English is always
> accepted, nothing rejects it.

## 1. Grammar (the whole thing)

A clause centers the PROCESS, not a subject: `PROCESS-CURRENT: PARTICIPANT-gowith, PARTICIPANT-gowith, ...`

**Currents** (replace tense — they carry the coordination state machine):

| current | meaning | MC coordination reading |
|---|---|---|
| `-bud` | emerging | intent announced, not yet started |
| `-go` | underway | in-flight / claimed and working |
| `-hold` | sustained | standing state (a lease held, a gate up, a freeze) |
| `-settle` | completed | done, no downstream obligation |
| `-echo` | completed AND consequential | done, obligates someone's next move (CODE_READY, verdict) |
| `-fade` | weakening/ending | release, expiry, retirement |
| `-lean` | expected | awaited next (a pending re-verdict, a scheduled pull) |

**Gowiths** (semantic roles on participants):

`-lead` actor · `-toward` goal/recipient · `-from` source/origin · `-with` instrument/companion ·
`-through` mechanism/path · `-as` role/name · `-for` purpose/beneficiary · `-against` check/opposition ·
`-among`/`-between` collective · `-across` span · `-around` context

**Possession dissolved**: not "pK's branch" but `branch pK-with`. Keep IDs, shas, seqs, paths, and
verdict words VERBATIM — gowith restructures relations, never renames facts (this also keeps probe
substrings intact for the eval judge).

## 2. MC coordination clause cookbook

| coordination act | clause shape (worked) |
|---|---|
| claim | `claim-go: pK-lead, issue:Q14-toward, cockpit-through` |
| release | `claim-fade: pK-lead, issue:Q15-from` |
| pull + start | `pull-go: pK-lead, Q17-toward, queue-from` |
| CODE_READY | `CODE_READY-echo: pK-lead, Q17-as, 38ec47cb-with, captain-toward merge-lean` |
| merge landed | `merge-settle: captain-lead, gap12/s3-session-refs-from, main-toward, ae5c1ebe-as` |
| xrev request | `xrev-lean: p1-lead review-toward, artifact-with, pK-from` |
| REFUTE verdict | `REFUTE-echo: p1-lead, seq1361-against, fix-lean pK-toward` |
| CONFIRM_RESOLVED | `CONFIRM_RESOLVED-settle: p1-lead, dfbbf201-as, merge-lean captain-toward` |
| DONE (doc slice) | `DONE-settle: pK-lead, Q13-as, receipts-with` |
| blocked | `gate-hold: pG-lead, Q18-around, xrev-lean captain-toward` |
| status turn | `work-go: pK-lead, Q14-G1-toward; inbox-hold empty` |
| redirect | `redirect-echo: captain-lead, pK-toward, Q15-fade Q5-toward` |
| adjudication | `adjudicate-settle: captain-lead, lease-as truth, pG-toward Q4-with` |
| watchdog false positive | `wake-fade: watchdog-from, Q28-around, row-settle already` |

Compose freely; semicolons chain clauses. One message-slice = one register — do not mix gowith and
English prose INSIDE a slice (headers/ids are register-neutral).

## 3. Reading primer (30 seconds, for humans)

Read `X-go` as "X is underway", `X-settle` as "X finished", `X-echo` as "X finished and someone
must now act", `X-fade` as "X is ending", `X-lean` as "X is expected next". Read `who-lead` as the
actor, `-toward` as the target, `-from` as where it came from, `-as` as what it's called. Facts
(IDs, shas, verdicts) are always verbatim. Example: `CODE_READY-echo: pK-lead, Q17-as, captain-toward
merge-lean` = "pK finished Q17's CODE_READY; the captain is expected to merge."

## 4. Boundaries (from the accepted plan — binding)

- Coordination-state slices ONLY; design discussion, xrev reasoning, and anything nuance-heavy
  stays free NL (plan §2.2).
- Council votes and xrev verdict REASONING: free NL, permanently (§2.4, representational-collapse
  guard).
- English is never rejected anywhere; this register is additive (§2.3).
- The eval fixtures live at `references/eval-fixtures.jsonl` (G2a runner schema); G2b's comparative
  report — not live usage — carries the A/B burden (§4 as revised).
