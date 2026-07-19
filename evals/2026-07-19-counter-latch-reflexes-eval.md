# Eval — counter/latch reflex signals v2 (2026-07-19)

Card: `counter-latch-reflexes` (NOW). Closes the top NOT-built reflex gap: the
ancestor's dynamic reflexes were hardcoded Python; here every steer, threshold,
and cooldown is an editable, retirable `.md` store entry driven by the
`record.py` per-session counters.

## What shipped

- `reflex.py` gains counter-family signal kinds on top of v1
  (`prompt`/`marker-file`/`every-turn`):
  - `counter` (generic; `counter:` + `threshold:`),
  - named aliases with field-tested defaults: `stalled` (passive-streak ≥ 6),
    `thrash` (loop-streak ≥ 3), `drift` (dirty-streak ≥ 8), `stuck`
    (stuck-streak ≥ 3, `escalate: 3`).
- `latch: true` (valid on any counter signal): fire ONCE per episode; the latch
  persists at `reflex-state/<sid>/latch.json` and clears on the inverse event
  (a forward op resets passive-streak, a commit resets dirty-streak, a recovery
  clears stuck). `escalate: N` re-fires as the streak worsens.
- Optional `pattern` (prompt-text gate — fixed-text law, never tool output) and
  `window` (counter freshness, seconds).
- Seeded pack (source: helm-default, editable/retirable like any reflex):
  `stalled-driver`, `loop-thrash`, `uncommitted-drift`, `stuck-commonsense`.
  The comms-fabric-entangled ancestors (meld-salience, bb-first) are dropped.
- `helm reflex smoke [--session S]`: the LIVE read-only surface — real counters,
  which counter reflexes fire, each one's idle/armed/FIRE state, no mutation.

## Laws honored

- **Fail-open**: no threaded `session_id` (or missing counters) → every counter
  signal is silently absent (v1 degrade), never a raise. The live `inject`
  path, which does not yet thread a session, degrades cleanly to v1.
- **Salience / specificity**: default-silent; a counter reflex with no counter
  or threshold ≤ 0 is skipped, never wallpaper; latched signals fire on the
  transition, not every turn — "a false-firing reflex is coordination theater."
- **Fixed text**: counter `pattern` matches the prompt only; no interpolation of
  tool/user output into a steer.
- **State discipline**: ONE pruned-on-read JSON per session inside the
  recorder's session dir — NOT the ancestor's 8,354-file per-term spray; gc
  TTL-sweeps `reflex-state/<sid>` at age 30, so latch state never ships
  cross-machine. A fresh session re-arms every latch (documented, correct).

## Measurement (fire → heed)

Delivery is measurable off the inject fire-ledger (reflex ids per turn) exactly
as the prompt/marker reflexes are. The heed signal for a counter/latch reflex is
distinctive and cheap: a fire followed within a few turns by the counter
CLEARING (passive-streak → 0 on a forward op, dirty-streak → 0 on a commit,
stuck-streak → 0 on recovery) is a heeded steer; a fire followed by the streak
continuing to climb is an ignored one. This is a stronger outcome marker than
the prose lanes get — the recorder already writes the ground truth.

## Verification

Hermetic unit tests (`tests/test_reflex.py`, `CounterLatchTest` / `AddCounterTest`
/ `SmokeTest`) plant counters and a real session state dir and cover: threshold
gate, latch-once + re-arm lifecycle, stuck escalation, missing-counter fail-open,
live `record.counters` read, level-triggered (no-latch) firing, prompt-gate,
window freshness, pruned-on-read latch file, `persist=False` no-mutation,
session-scoped re-arm, add-verb roundtrip + specificity guards, and a live
read-only smoke against real recorded counters.

## Remaining seam (other lane)

The live per-turn firing through `inject` needs the inject lane to thread the
hook's `session_id` into `reflex.fire(text, project=project, session=session)`
at `inject.py` (gather + `_explain`). `reflex.fire` already accepts `session`;
until then, counter reflexes fire in tests and via `helm reflex smoke`, and the
inject path degrades to v1 (no crash). A web deck chip (Reflexes component) is a
follow-up for the web lane — the data seam (`reflex.counter_spec`,
`reflex.fire(..., persist=False)`, `record.counters`) is ready.
