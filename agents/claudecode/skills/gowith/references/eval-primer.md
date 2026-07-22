# gowith eval primer (FROZEN — G2b judge input)

<!--
  This is the ONLY gowith teaching text a recipient sees in a primed gowith trial.
  It is FROZEN and deliberately LEAK-FREE (Q34/G2b condition F1):
    - it carries §1 grammar + §3 reading rules from the /gowith skill, and
    - a fully SYNTHETIC worked example about a FICTIONAL item (issue:Z99, pane zz9),
      never a corpus record — so no eval fixture's answers are handed to the recipient.
  It deliberately OMITS the skill's §2 coordination-clause cookbook: those worked
  shapes (claim/release/CODE_READY/verdict) mirror many real fixtures and would leak.
  The runner logs this file verbatim into the judgelog and counts its tokens as the
  priming denominator for the amortized ≥20% token gate. Do not add real ids/shas/seqs.
-->

## Grammar (the whole thing)

A clause centers the PROCESS, not a subject: `PROCESS-CURRENT: PARTICIPANT-gowith, PARTICIPANT-gowith, ...`

**Currents** (they replace tense — the coordination state machine):

| current | meaning |
|---|---|
| `-bud` | intent announced, not yet started |
| `-go` | in-flight / underway |
| `-hold` | a standing state held (a lease, a gate, a freeze) |
| `-settle` | done, no downstream obligation |
| `-echo` | done AND obligates someone's next move |
| `-fade` | releasing, expiring, ending |
| `-lean` | expected next / awaited |

**Gowiths** (semantic roles on participants):
`-lead` actor · `-toward` goal/recipient · `-from` source/origin · `-with` instrument/companion ·
`-through` mechanism/path · `-as` role/name · `-for` purpose/beneficiary · `-against` check/opposition ·
`-among`/`-between` collective · `-across` span · `-around` context

**Possession is dissolved**: not "zz9's report" but `report zz9-with`. IDs, shas, seqs, paths, and
verdict words stay VERBATIM — the register restructures relations, it never renames facts.

## Reading rules

Read `X-go` as "X is underway", `X-settle` as "X finished", `X-echo` as "X finished and someone
must now act", `X-fade` as "X is ending", `X-lean` as "X is expected next". Read `who-lead` as the
actor, `-toward` as the target, `-from` as the origin, `-as` as what it is called. Facts (IDs,
shas, verdicts) are always verbatim.

## Worked example (synthetic — fictional item)

`deploy-echo: zz9-lead, issue:Z99-as, ops-toward rollout-lean`
= "zz9 finished issue:Z99's deploy; the ops target is now expected to roll out."

Here the current `-echo` says the deploy is done and obligates a next move; `zz9-lead` is the actor,
`issue:Z99-as` names what was done, `ops-toward` is the recipient, and `rollout-lean` is the awaited
next step. (zz9, Z99, and this deploy are invented for teaching — they appear in no real message.)
