# `dispatch add` — notification contract

`helm dispatch add <recipient> <lane> --ref <tip> --kind build|review
--new-work|--supersedes <id>` persists a dispatch row in the
durable ledger and tells at most one seat about it. The persistence is
guaranteed; the telling is best-effort, and the difference between them is
durably recorded and projected.

## What `add` guarantees

1. **Persistence first.** The dispatch row is written to the append-only event
   ledger inside the writer lock BEFORE any notification is attempted. The
   obligation exists in durable form regardless of what happens next. A lock
   failure aborts the whole operation — no row without notification, no
   notification without a row.

2. **Notification is best-effort.** After persistence, `add` posts a public
   `@recipient <id[:12]> <context>` to the `main` room. The recipient's beacon
   picks it up on their next wake; the room is public, so any seat can confirm
   it arrived. A failed notification does NOT block or roll back the dispatch.

3. **A failed notification is durably recorded.** When the notification post
   fails (the room is unwritable, the post returns no id, or an exception is
   raised), `add` writes a `notify-failed` event to the event ledger carrying
   the dispatch id, the reason, and a timestamp. That event is durable: it
   survives a crash, the dispatch row itself is immutable, and a subsequent
   read projects `notify_failed` into the land-loop output so the surface is
   never silent about a gap it is carrying.

4. **Idempotent by operation key.** If a dispatch with the same id already
   exists, `_append_dispatch` returns the existing row with `existed=True`
   rather than writing a duplicate. The caller must treat `existed` as
   **never-send-again** — re-notifying here would produce a duplicate `@mention`
   and create a phantom obligation. The caller gets the existing row's current
   state, including any `notify_failed` marker from a prior attempt.

5. **The caller sees the true state.** The return value carries the canonical
   row as it exists AFTER the notify attempt, with delivery status, any
   `notify_failed` marker, and the dispatch id. A caller reading `notify_failed`
   knows the obligation exists but the recipient was never told. A caller
   reading `needs-confirmation` without `notify_failed` knows the mention
   POSTED but delivery to the recipient's beacon has not yet been confirmed by
   a `mark_delivered` event.

## What `add` does NOT guarantee

1. **The recipient received it.** `add` posts to a room; it does not confirm
   the recipient's beacon picked it up. That confirmation is a separate step
   (`mark_delivered`), triggered when the recipient's turn-processed hook reads
   the message and writes a `delivered` event. Until that confirmation exists,
   the delivery reads `needs-confirmation`.

2. **The recipient exists.** `add` writes the dispatch with the recipient name
   as given. If the recipient seat does not exist, has no pane, is renamed, or
   has no beacon armed, the notification post may succeed but the dispatch will
   remain `needs-confirmation` indefinitely. The recipient field is validated
   for format, not for liveness — a dispatch to a seat that never existed is a
   real dispatch that will never be delivered, and the ledger records it
   honestly.

3. **The row is still open by the time the recipient reads it.** A cancel or
   verdict may close the row between `add` writing it and the recipient waking
   to find it. That is correct: the row reflects its canonical durable state,
   and `_reconcile_send` ensures a stale delivery attempt never overwrites a
   terminal verdict.

4. **The notification fires exactly once.** If the notification post succeeds
   but the `notify-failed` write fails (a race between two failures), the
   dispatch may stand without a failure marker while the recipient was actually
   told. That gap is narrower and less dangerous than its opposite (the
   recipient was never told and nothing recorded it), which this contract's
   durability leg prevents.

5. **The notification fires on an already-closed row.** If the dispatch is
   cancelled or verdict-closed between `_append_dispatch` and `_notify_public`,
   the notification may fire anyway — it is a brief TOCTOU window, and an
   extraneous `@mention` pointing at a closed dispatch is less harmful than
   silently dropping a real one. A caller who receives both the mention and
   then discovers the row is closed can read the true state from the ledger.

## States a caller can see

| State | What it means |
|---|---|
| `needs-confirmation`, no `notify_failed` | The mention was POSTED. The recipient has not yet confirmed receipt. |
| `needs-confirmation`, `notify_failed` present | The obligation EXISTS but the mention was NEVER POSTED (or the post returned no id / raised). The recipient was not told. |
| `observed` | The recipient's delivery hook confirmed receipt. |
| `verdict` (approve/fix/supersede) | A verdict closed the obligation. |
| `cancelled` | The sender abandoned the obligation with a reason. |
| UNBILLED-BECAUSE-UNTOLD (`unmeasurable`) | The obligation exists but the reviewer was never told — surfaced in `lr stalls` in its own bucket, never counted as "stalled" and never hidden. |

## The `add`/`send` distinction

- **`send`** persists the dispatch AND sends a DM to the recipient's private
  lane, then marks `delivered` on success. It notifies via `_notify_public`
  as well. Use it when the obligation IS the message.
- **`add`** persists the dispatch and posts a public `@mention` to `main`.
  It does NOT send a DM. Use it when the hand-off happens out-of-band
  (e.g., the reviewer is already in the room, or the dispatch is a
  record-keeping entry that rides an existing conversation).

The contract differs because the delivery path differs: `send` confirms
delivery through the DM response; `add` confirms delivery through the
recipient's eventual beacon confirmation. Both use the same `needs-confirmation`
/ `observed` lifecycle; only the notification mechanism differs.

## When the silence must be loud

If `add` is called from the CLI, the exit line must report the notification
outcome explicitly — one of three states, not one unconditional assertion:

    # notify succeeded: print the mention id, so the operator can verify it landed
    helm dispatch: <id> -> @<recipient> <lane> — PENDING VERDICT / NEEDS CONFIRMATION
    ⚠ @<recipient> notified via main; confirm the message arrived at the recipient.

    # notify failed: the obligation exists, the mention never posted
    helm dispatch: <id> -> @<recipient> <lane> — PENDING VERDICT / NEEDS CONFIRMATION
    ⚠ @<recipient> was NOT notified (mention post failed: <why>). Tell them
    yourself or fix the room and re-verify.

    # notify=False: the caller opted out, owns the hand-off themselves
    helm dispatch: <id> -> @<recipient> <lane> — PENDING VERDICT / NEEDS CONFIRMATION
    ⚠ `add` records the obligation but SENDS NOTHING — @<recipient> has not been
    told. Either `helm chat post` an @<recipient> mention naming <id>, or use
    `dispatch send` next time, which notifies.

A line reading `PENDING VERDICT / NEEDS CONFIRMATION` without one of these
three outcomes is indistinguishable from the notification-having-been-posted
case, which is how a live integrator seat committed the exact bug this lane
exists to fix. The silence is the defect.
