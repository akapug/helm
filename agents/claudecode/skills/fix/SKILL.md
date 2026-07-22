---
name: fix
description: >
  The repair-basin physics - keep a fix/debug/regression from becoming a worse one. Use at the start of
  any FIXING work and auto-load it when a task is diagnose-shaped: "it broke / why is X failing / this
  regressed / debug this / it stopped working / used to work / fix the bug". Same spine as /build
  (decision-spirit + xrev), different basin: building asks "what's the right thing to build?"; fixing
  asks "what is ACTUALLY happening, and how do I not make it worse?". It binds the regression-preventing
  reflexes TO the act of fixing - because that is exactly when they erode (the 2026-06-08 cascade was a
  fix session that skipped ground-truth + verify and burned hours).
license: MIT
metadata:
  author: mc
  version: "1.2.0"
---

# /fix - repair without regressing

`/fix` is `/build` in the repair basin. Same physics (decision-spirit, the 19 heuristics, xrev), but a
fix that ships on a guess, claims green without proof, or leaves the old path reachable is a NEW bug.
These are the reflexes bound to fixing - you need them most exactly when you're under pressure to "just
fix it." Everything below earned its place from a real 2026-06-08 failure; this skill is that day's scar
tissue.

## OPENING MOVE — do these THREE before you touch a single line (non-negotiable)

**The first AND only initial goal is FULL ILLUMINATION: a complete, cited map of EVERY mechanism that could produce the symptom — never the first plausible one. Acting (fix / disarm / commit-a-hypothesis) on a FRAGMENT is the anti-pattern `partial-finding-then-act` — and a symptom that recurs AFTER your disarm is a second mechanism you never enumerated. When the surface is broad, commission the illumination as an NDSA (background, non-distracting) so it arrives as ONE map instead of fragmenting across turns. THEN act once, correctly.**

1. **OBSERVE the real symptom.** Reproduce it / read the actual error / parse the real log or live
   state. Quote the evidence. You cannot fix what you haven't seen fail.
2. **READ THE HISTORY.** `git log --oneline -- <touched file>`, `git blame` the suspect lines, and
   `git log -S "<symbol>"` / grep planning commits + the durable comms log for the feature
   (`mc comms poll --conv <conv> <endpoint>` / `mc cockpit get`). For ANY
   "changed / regressed / used to work" question this is the FASTEST answer and you do it FIRST — not
   after hours of grepping current code. Also: `git reflog`, `git stash list`, `git fsck --unreachable`
   for work that may be unmerged/squashed-away.
   - **a2a-comms symptom → REGRESSED-until-proven-novel** (owner canon 2026-07-13): an a2a bug OR
     optimization is presumed previously-solved until the history proves otherwise — so this step,
     BEFORE step 3's hypothesis, is mandatory and has three named sources: (1) sweep BOTH repos —
     `git log --all --grep=<capability>` in MC + buildr-private-beta — for the prior fix lineage;
     (2) `cv` search for the owner's advice + buildr design docs on the capability; (3) DEPLOY-TIER
     check — a landed-but-not-live fix reads IDENTICAL to a regression (a BINARY fix on `main` whose
     daemon never swapped is not live; the "what stopped FIRING?" question applied to the deploy
     path). The answer is usually already in the log/corpus; skipping this re-invents a running
     capability. Class: `reinvented-running-capability` / `landed-sha-not-running-exe`.
3. **STATE ONE HYPOTHESIS** out loud: "X is broken because Y; if so, Z will be true." Prove or kill Y
   with the CHEAPEST DISCRIMINATING PROBE first — web/prior-art, a deterministic in-test repro, a cheap
   read — *before* you burn an expensive live cycle (fire / reseed / restart / redeploy). A live cycle
   tests one config, costs real resources, and its snapshots can LIE (a `cp` of a mid-write store reads
   all-None; a torn binary "succeeds" then fails to fetch) — so it churns iterations a deterministic
   probe would have settled in one. (Live 2026-06-18: a standalone build BEFORE a live reboot caught a
   broken artifact fetch the reboot would have aborted on.) No edit before a named hypothesis.
   bug-class `live-cycle-churn-when-a-deterministic-probe-would-discriminate`.

If you have not done all three, you are guessing. Stop and do them.

**Backfill the already-affected (a fix is reactive by definition).** The bug already ran — it
corrupted state, sent wrong messages, or left someone un-served, and fixing the code does NOT un-do
that. After the root-cause fix, remediate the instances the bug already hit (not just prevent
recurrence): reversible/contained state → fix it now unprompted; destructive/outward (re-send comms,
re-notify humans, rewrite shared state) → propose it with the fix (the same ask-list gate). It
doubles as a live dogfood on real already-broken state. (Same reflex as the reactive-feature
backfill in `dev-process.md`.)

## The fix loop (after the opening move)

1. **PRESUME the failure is NOT new — name its bug CLASS** (#10). A failed PROCESS (a bug, a dropped
   wire, a skipped step, a punt) is presumed a RECURRENCE: SEARCH the corpus for its class FIRST — the
   `bug_class` handles, the lexicon + premise/punt ledger, `git log -S`, memory — because treating a
   known class as novel re-pays its whole cost AND skips the guard that already exists for it (live
   2026-06-21: the "wire-the-build" gap re-framed as a new insight when it was already canon —
   `wire-it-or-it-doesnt-count` + the WTC tractor). Most "regressions" are a *firing trigger gone
   silent* (a hook un-enabled, a cache stale, a matcher that didn't track a rename, a reflex not
   re-injected) — ask "what stopped FIRING?", not only "what's wrong with this line." Fix the class.
2. **Trace to the OWNER layer** (#15). Follow data/control from entrypoint to the source of truth; fix
   the owner, not the last renderer/adapter/error-site (those reappear elsewhere).
3. **ONE hypothesis per attempt** (#17). A failed hypothesis PIVOTS to a new cause category — never
   re-tune the same idea (N regex tweaks of one guess is the tell). Minimal correct diff at the right layer.
4. **VERIFY against the REAL symptom** (verify-the-verifier). When the thing verified is a
   GUARD/HOOK, scrub its kill-switch env first — a CLEAN shell, e.g.
   `env -i PATH="$PATH" HOME="$HOME" bash -c '<probe>'` (NOT `env -u MC_GUARD_*`:
   `-u` takes literal names, the glob expands against files — a literal copy unsets nothing):
   a leaked guard-disable var in your pane turns the probe into 0==0 — both sides off, a
   false PASS (env-leak class 7fd3373; struck the QC layer itself 2026-06-11). Run it; confirm with a SECOND method that
   the *actual reported behavior* goes broken→working on the real, non-empty input. Quote the evidence.
   "compiles / green / launched / sent / it should now" is NOT verified. If your test passed suspiciously
   fast, suspect the TEST (today: a unit "PASS" that was really 0-matched filter; an xrev "approved"
   that was a hijacked auto-inject).
5. **Regression-guard so it CAN'T silently recur** (#19 automation-fails-loud): add the test/gate/assert
   that would have caught this, and make it FAIL LOUD (a matched-nothing glob that prints OK is a bug).
   If you shipped a new canonical mechanism, RETIRE the old one in the SAME change (#18 supersede-cleanly)
   — no confusable dual-path left for someone to reach.
6. **Cross-family xrev before "done"** (#14). A non-trivial fix gets a different-model refuter hunting
   for what it breaks/misses — not a same-family "looks good." (Today codex's REFUTE caught 4 real bugs
   in my fixes, including a silent-pass that violated our own #19. Per-fix xrev beats batching — it
   catches earlier and costs no more.)

## Red flags — these thoughts mean STOP, you're skipping the discipline

| You're thinking… | Reality |
|---|---|
| "I'll just grep the current code." | Read the **history** first — `git log`/`blame` answers "what changed" in minutes. |
| "It probably works like X." | You're theorizing. Ground-truth X before building on it. |
| "It compiles / tests pass — done." | Did the test see the REAL, non-empty input + the real symptom? Verify, then claim. |
| "Let me tweak that pattern again." | A failed hypothesis pivots cause CATEGORY; stop re-tuning. |
| "I'll fix it at the error site." | Trace to the owner layer or it reappears. |
| "I told them it's fixed." | Did you RUN it and cite output? If not, say "not yet verified." |
| "The guard/swap/hook ran — fine." | Did it actually FIRE on real input, or no-op and print OK? Prove it fired. |
| "Ship the new path now, clean the old later." | The old reachable path is the next bug. Retire it now (#18). |

## Decide-don't-ask still holds
Run the audit and execute on tractable substrate; ask the human only for genuine vision/naming calls,
public/shared pushes, destructive ops on shared state, or sensitive creds. A reversible fix you can
verify is yours to make. (Fast path = the 5 questions in `decision-spirit`; full rubric = `/decide`.)

## Done means done
Symptom reproduced→resolved (evidence quoted) · history read · class named · owner-layer diff ·
regression-guard added + fails-loud · old path retired · cross-family xrev clean · doc/task updated.
`compiles` ≠ done. `I think` ≠ done.

## Cross-refs
- `/x` — cross-family analysis: **xdiag** when a fix won't converge (dispatch the author/predictor, trace in parallel) · **xrev** before claiming done.
- `/build` — forward-work sibling (same spine). `/refine` — converge an unclear fix shape first.
- `decision-spirit` — the deep 19-heuristic audit. `systematic-debugging` /
  `ground-truth-cross-reference-loop` — when the diagnosis won't converge.
- `/learn` — when the fix reveals a lesson worth making durable mc physics (rule/skill/hook).
