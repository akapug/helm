# meldhalf lane eval — the delivery lane (2026-07-20)

Branch `meldhalf/0.2`, 10 commits rebased onto main `3bd1342`. Design:
`~/.helm/helm/prd/2026-07-20-meldhalf-design.md` (§11 = both codex
adversarial rounds and how each ruling landed).

## Gate

`python3 -W error::ResourceWarning -m unittest discover -s tests` after the
round-2 fixes: **909 tests, OK (7 pre-existing live-node skips), warning-
clean.**

## Round 2 (cross-family gate BLOCK → fixed, all three reproduced live)

- **B1 seat-key collision:** codex's exact `api.a`/`api-a` sequence now
  yields `dot1: delivers / dot2: empty / dash: delivers ITS row` through
  `bin/helm` (was: dash None — silent loss). State paths key on slug +
  blake2b8(casefolded seat).
- **B2 copied-SID spoof:** `CLAUDE_SESSION_ID=sA release` without the lease
  → refused rc 1 ("release needs the lease id (the grant's capability)");
  with the printed lease → released rc 0. `--session` no longer exists.
- **B3 matcher:** a Bash-pinned deliver group now fails status coverage and
  install repairs it to the wildcard (or relocates ours past foreign
  co-tenants) — three regressions pinned.

## Hot-path measurement (integrator note 2)

The PostToolUse deliver hook fires on EVERY tool call fleet-wide, so its
unchanged-room fast path was measured through the real entry (`bin/helm chat
deliver --hook-json`, sandbox room, 15 runs):

- **median 31 ms, p90 40 ms, min 27 ms** — dominated by interpreter startup
  (the same floor the fleet already pays per turn for the inject hook).
  The lane's own work on that path is one `open+fstat` against tmpfs plus a
  per-seat `.seen` utime; `cell`/`emoji` imports went lazy in chat.py so the
  hook never pays for the transport it doesn't use. Headroom vs the hook's
  2 s timeout: ~60×.

## Live smoke (real CLI, sandbox room)

- join → hook JSON with identity/protocol context; roster row + cursor
  initialized AT JOIN (a post between join and first boundary delivered).
- @mention delivered at the boundary in the one-write hook shape;
  budget-of-one with `(+N waiting)` collapse; drain to silence.
- **Owner-spoof check:** a CLI post as `david` (no rail stamp) did NOT
  owner-deliver (empty hook output); a web/tui-stamped row does. Mentions
  still work for anyone.
- Claims: grant minted `lease 021793…, fence 1`; bare display-name release
  REFUSED with the binding message; granting-session release succeeded.
- Web `/api/chat/roster` served the seats/claims shape; the seats panel
  renders in the ledger tab (verified on a scratch-port boot pre-rebase).

## Found along the way (fixed in-pass, regression-pinned)

- **chat.read row tear:** a message containing U+2028/U+2029 (voice pastes
  can) split its JSON row for every reader — `splitlines()` → split on
  exactly `\n` in read() and _rotate(). Pinned in tests/test_chat.py.
- **Suppression base-offset bug** (caught by this lane's own tests before
  freeze): after a rotation reset, the cursor could commit BEFORE the
  rid-suppressed row and replay it once the inode matched again — the walk
  base now moves past the suppressed row inside `_tail`.

## Deferred, recorded

Council (embargoed verdicts) → 0.3 with the full C2/C3 spec in the design
doc §11; the verbs answer with a deferral pointer. M11 (idle-wake is opt-in
Monitor arming) and M12 (mentions ≠ ask/reply state; `reply_to` over row ids
is the cheap path) are documented 0.2 limitations.

## Parity claim

With the seats panel + message-a-seat live, every `:8900` viewer capability
has a helm home — the retirement parity gate is SATISFIED; the flip remains
the owner's call.
