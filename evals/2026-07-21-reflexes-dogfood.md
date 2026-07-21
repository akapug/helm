# Reflexes dogfood proof — the counter/latch lane is live (2026-07-21)

Lane fable-reflexes, branch `reflexes/100`. The bar demanded: recorder-fed
signals PROVEN live + a real session-shaped latch→fire→clear record. Ground
truth first, honestly: most of the machinery was already landed — this eval
assembles the PROOF and adds the two genuinely missing pieces (the unplanted
end-to-end test class; the two injection-estate surfaces).

## Already-done (verified, not rebuilt)

- Counter/latch machinery (010c249) reads record.py's real counter names —
  `passive-streak` / `loop-streak` / `dirty-streak` / `stuck-streak` match
  reflex.py's COUNTER_SIGNALS exactly; no shape drift.
- `helm record` wiring: 5/5 claude homes, both event legs (PostToolUse +
  PostToolUseFailure), live sessions accumulating counters.
- The fire-ledger already records reflex fires under `fired.reflex` (an
  earlier day-review probe read the wrong key and called this a gap — it is
  not; correcting the record here).

## Live-estate evidence (the fleet, today, unstaged)

The inject ledger (`_global/.state/inject-ledger.jsonl`) at review time:
**103 of 349 rows carry reflex fires**, every signal family represented on
real sessions:

- counter/latch: `stalled-driver` ×17, `uncommitted-drift` ×11 — fired by
  real recorder streaks in real sessions (including this builder's own);
- prompt pack: `punt-tell` ×49, `compaction-continuity` ×13,
  `correction-language` ×11;
- marker: `owner-chat-unread` ×9 (the owner's posts steering agents);
- dynamic coinage reflexes: ~40 distinct `coinage:*` one-shot fires.

`helm reflex smoke` over live sessions shows the latch semantics working in
the wild: e.g. a session at `passive-streak=38/6` reads **armed but quiet**
— it fired once at the threshold crossing (ledger row exists) and stays
latched while the episode continues. Once per episode, as designed.

## Controlled cycle (real CLI, live estate, throwaway sid)

Session `dogfood-reflex-1784611851`, all through `bin/helm`:

1. 4× identical `cargo test --all` events → `helm record --hook-json` →
   `loop-streak=3`; smoke: `FIRE loop-thrash 3/3`.
2. Real turn: `helm inject --hook-json` → the steer delivered:
   `REFLEX: Same command re-run with no change — …do not re-run.`
   Ledger row: `fired.reflex = ['loop-thrash']`.
3. Next turn: inject emits **zero** reflex lines — latched (ledger `[]`).
4. Inverse event (`git log -1`, a different command) → `loop-streak=0`;
   smoke: `idle`; the latch dropped — the episode can re-fire next streak.

## The unplanted end-to-end tests (new, hermetic)

`tests/test_reflex.py::CounterEndToEndTest` — real hook events through
`record.record()` → real `counters.json` → `reflex.fire(session=)` with NO
planted counters (every prior test planted the dict): thrash
latch→fire→clear→re-fire; stalled fires-once; drift fires and `git commit`
clears the latch; stuck escalates at threshold+3; generic `counter` signal is
level-triggered without latch. 5 tests, all green under the warning gate.

## The injection-estate closers (bar B)

- **Discovery attribution:** `helm inject --explain` now tags every line —
  pinned, JIT (fit/over-cap/cooldown), WHO, reflex — with the root it came
  from (`← adopted | helm-global | adopted-project | project`). Test-pinned.
- **Pinned starvation surfaced:** `helm brief`'s knowledge section now
  carries `pinned starvation: N of M never fired: …` off
  `store.pinned_stats()` whenever starved always-entries exist (silent when
  fed — empty-section law). Test-pinned both ways.

## Bar verdicts (judgment, stated honestly)

- **Reflexes → 100%** for the bar's stated scope: machinery + seeded pack +
  counter/latch live-proven + record wiring + this dogfood record. The lane
  stays open-ended by nature (new reflexes are data, not code).
- **Injection estate → 100% of its 0.2 scope**: WHO lane (landed earlier),
  hook-JSON + project scoping (landed), discovery attribution + starvation
  surfacing (this lane). Codex/opencode harness reach is **0.3 by the
  owner's scope ruling** (`helm-0.2-scope-opencode-0.3-meldhalf-in`), not a
  0.2 remainder.

Gate: full suite warning-clean on the branch (1009 baseline + this lane's
tests).
