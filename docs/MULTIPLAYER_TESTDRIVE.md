# Multiplayer test-drive — try it in two terminals

This is the **owner walkthrough** for local multiplayer. Two terminals act as
two different people; you watch their edits **converge** on a shared board while
a **presence** list shows who is connected. Nothing here talks to the network —
it is all on this machine, over the blind relay and the TTL presence channel
that ship in `helm/multiplayer.py`.

Everything below is `helm …`. If `helm` isn't on your PATH, use `./bin/helm …`
from the repo root instead. It is Python stdlib-only — no install, no services.

The demo lives in one cave named **`main`** and one board doc named **`board`**.
Every command below passes `--cave main` so every terminal lands in the same
cave. (If your shell sets `HELM_CHAT_ROOM`, the default cave would follow *that*
instead — so keep the `--cave main` on.)

> **This walkthrough used to start in the browser.** The cockpit had a **cave**
> tab that rendered this board, and it was retired on 2026-07-30. Two reasons,
> neither of them the transport's fault: the word *cave* means the **attestation
> node** everywhere else in helm (that is what the **ledger** tab shows, one tab
> over), and the only owner-facing thing on that board — the fleet's notes to
> you — belongs somewhere **durable**, because the relay is tmpfs and a reboot
> emptied it. Those notes are `helm note` now, and a card on the cockpit's home
> tab. The relay, its adapters, its CLI verbs and its `/api/multiplayer/*` HTTP
> seam are all unchanged.

---

## 1. Watch the board

Open a terminal and leave this running. It re-prints the whole board every two
seconds, which is exactly what the retired tab's poll did:

```bash
watch -n2 'helm multiplayer status board --cave main'
```

**Working looks like:** a heading `board board @ main`, then `(no cells yet)`,
then `peers:` / `(nobody connected)`, then a `cursor` line.

No `watch` on this box? `helm multiplayer status board --cave main` on demand
shows the same thing; just re-run it after each step.

---

## 2. Set a cell yourself

In a **second terminal**, write one cell as `alice`:

```bash
helm multiplayer set board greeting "hello from alice" --cave main --actor alice
```

**Working looks like:**

- the command prints an **acknowledgement** — an envelope `id`, a `ts`, a
  `cursor`, and `bytes`. Note what is *not* in it: your text. The relay stored an
  opaque blob and the ack never echoes it.
- within ~2s the watching terminal gains a row:
  `greeting = hello from alice   — alice (1s ago)`, and the `cursor` ticks
  forward.

---

## 3. Bring in a second actor

Publish a note as a *different* actor, `codex`:

```bash
helm multiplayer set board status "building the demo" --cave main --actor codex
```

**Working looks like:** within ~2s the watching terminal gains a second row,
`status = building the demo — codex`. Two actors are now writing to the same
board through the same blind relay, and you are watching both land.

---

## 4. Watch convergence (last-writer-wins)

Now make them **collide on the same key**. Set `greeting` — the key `alice`
already set in step 2 — as `codex`:

```bash
helm multiplayer set board greeting "hi from the terminal" --cave main --actor codex
```

**Working looks like:** the **`greeting`** row **flips** from
`hello from alice` (alice) to `hi from the terminal` (codex) within ~2s. The
later write won. That is the CRDT resolving the conflict — a last-writer-wins
map. Every client, given the same relay log, lands on the *same* board no matter
who read first, because the winner per key is the update with the greatest
`(ts, id)` and that order is total. The relay itself decided nothing; it kept
opaque updates in order and the **client** folded them.

Set the same key again as `alice` and it flips back. Back and forth — the newest
set always wins.

---

## 5. Watch presence fade

Presence is a heartbeat with a ~30-second time-to-live. Make `codex` announce
itself once, then go quiet:

```bash
helm multiplayer presence --cave main --actor codex --state building --connection term-1
```

**Working looks like:**

- within ~2s the watching terminal's `peers:` list gains
  `codex@term-1  building  (seen 1.2s ago)`.
- the `seen …s ago` figure climbs on every refresh.
- after ~30s with no further heartbeat the `codex` row **disappears** — the
  heartbeat expired. `helm multiplayer leave --cave main --actor codex
  --connection term-1` drops it immediately instead of waiting.

Presence is pure attention state. It never touches the board; an actor vanishing
never disturbs a single cell.

---

## 6. Prove the relay stayed blind

Read the raw log the relay actually keeps:

```bash
helm multiplayer read board --cave main
```

**Working looks like:** one line per update — an id, an actor, and
`<N opaque bytes>`. Your words are **never** there, because helm never decoded
them. The text exists only on the **board**, which `status` materialized from
those opaque blobs on the client side. That split — a blind transport plus a
client-side CRDT — is the whole point, and it is what lets a hosted remote
relay drop in later without helm ever learning what a "board" is.

To see it from the other side, publish something helm has no idea how to read:

```bash
printf %s 'not-a-demo-cell' | helm multiplayer publish board --stdin --cave main --actor stranger
```

The board is unchanged and `status` now reports
`+1 opaque update(s) from other clients — relay stays blind`. Counted, never
decoded.

---

## 7. The machine-readable form

Everything above has a `--json` form for an agent to drive and verify:

```bash
helm multiplayer status board --cave main --json
helm multiplayer read board --cave main --after 0 --json
helm multiplayer peers --cave main --json
```

`status --json` returns `{cave, doc, cursor, board, foreign, peers}` — the same
fold, machine-shaped.

---

## If something looks off

- **Nothing on the board / no peers after a command?** Make sure every terminal
  says `--cave main`. A mismatched cave name is a different, empty board.
- **`helm: command not found`?** Use `./bin/helm …` from the repo root.
- **Board empty after a reboot?** Expected. The cave lives in tmpfs
  (`/dev/shm/helm-multiplayer`) and is disposable by design — durable history is
  a client/CRDT concern, not the relay's. Just set the cells again. If what you
  wanted was a note that SURVIVES the reboot, that is
  `helm note set <key> <text…>`, which is a different substrate on purpose.
- **Want a clean slate now?** `rm -rf /dev/shm/helm-multiplayer` wipes every
  cave.

## What this is *not*

This is **local** multiplayer — one machine, human + agents in one cave. There's
no web/hosted cave, no cross-machine sync, no accounts. That remote half
belongs to a hosted product, and it slots into the **same** five adapter methods
(`publish / updates / heartbeat / peers / leave`) without changing helm's core.
See [MULTIPLAYER.md](MULTIPLAYER.md) for the adapter contract.
