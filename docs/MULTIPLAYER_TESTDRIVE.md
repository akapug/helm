# Multiplayer test-drive — try it tomorrow

This is the **owner walkthrough** for local multiplayer. You open one web view;
a terminal (you, or an agent) publishes; you watch two actors' edits **converge**
on a shared board while a **presence** panel shows who's connected. Nothing here
talks to the network — it's all on this machine, over the blind relay and the
TTL presence channel that already shipped in `helm/multiplayer.py`.

Everything below is `helm …`. If `helm` isn't on your PATH, use `./bin/helm …`
from the repo root instead. It's Python stdlib only — no install, no services.

The demo lives in one cave named **`main`** and one board doc named **`board`**.
Every terminal command below passes `--cave main` so it lands in the exact cave
the web tab shows by default. (If your shell sets `HELM_CHAT_ROOM`, the CLI's
default cave would follow *that* instead — so keep the `--cave main` on.)

---

## 1. Open the surface

```bash
helm web --open
```

A browser opens to the helm cockpit. In the top nav, click **cave**.

**Working looks like:** three panels appear —
- **presence** (left): within ~2 seconds a row **`david · cockpit · watching`**
  shows up with a **green dot**. That's *you* — opening the tab makes you a live
  peer. The `cave` / `board` boxes at the top read `main` / `board`.
- **board** (middle): "no cells yet — set one below", with a key box, a value
  box, and a **set →** button.
- **relay log** (right): "no updates yet".

Leave this tab open for the rest of the walkthrough.

---

## 2. Set a cell yourself

In the **board** panel, type a **key** of `greeting` and a **value** of
`hello from david`, then click **set →** (or press Enter).

**Working looks like:**
- the **board** immediately shows a row: **`greeting  hello from david  david`**.
- the **relay log** on the right gains one line: a short id, `david`, and a byte
  size like `81 B` — **not** your text. The relay stored an opaque blob; it never
  reads your words. The **cursor** near the top ticks forward.

---

## 3. Bring in a second actor from a terminal

Open a **second terminal**. Publish a note as a *different* actor, `codex`:

```bash
helm multiplayer set board status "building the demo" --cave main --actor codex
```

Watch the still-open **cave** tab (don't touch it).

**Working looks like:** within ~2 seconds, without any refresh —
- a new board row appears: **`status  building the demo  codex`**.
- the relay log gains a second line attributed to `codex`.

Two actors — one in the browser, one in a terminal — are now writing to the same
board through the same blind relay, and you're watching both land.

---

## 4. Watch convergence (last-writer-wins)

Now make them **collide on the same key**. In the terminal, set `greeting` —
the key you already set as `david` in step 2 — but as `codex`:

```bash
helm multiplayer set board greeting "hi from the terminal" --cave main --actor codex
```

**Working looks like:** on the board, the **`greeting`** row **flips** from
`hello from david` (david) to **`hi from the terminal` (codex)** within ~2s. The
later write won. That's the CRDT resolving the conflict — a last-writer-wins map.
Both clients, given the same relay log, land on the *same* board regardless of
who polled first. The relay itself never decided anything; it just kept the
opaque updates in order and the **client** folded them.

Set the same key again from the browser (board panel: key `greeting`, any new
value, **set →**) and it flips back to `david`. Back and forth — the newest set
always wins.

---

## 5. Watch presence fade

Presence is a heartbeat with a ~30-second time-to-live. From the terminal, make
`codex` announce itself once, then go quiet:

```bash
helm multiplayer presence --cave main --actor codex --state building --connection term-1
```

**Working looks like:**
- within ~2s a second presence row appears: **`codex · term-1 · building`** with
  a **green** dot (fresh).
- after ~10s with no further heartbeat, its dot turns **amber** (quiet).
- after ~30s the `codex` row **disappears** entirely — the heartbeat expired.

Your own `david · cockpit` row stays green the whole time, because the open web
tab re-heartbeats you every 2 seconds. Close the cave tab (or switch to another
tab) and, ~30s later, you'd fade out too. Presence is pure attention state — it
never touches the board; an actor vanishing never disturbs a single cell.

---

## 6. Prove the relay stayed blind

Look at the **relay log** panel again. Every row is `id · actor · N B` — an id,
who published, and a **byte size**. Your actual text is **never** shown there,
because helm never decoded it. The words only exist on the **board**, which your
*browser* materialized from those opaque blobs. That split — a blind transport
plus a client-side CRDT — is the whole point, and it's what lets a builders.dev
remote relay drop in later without helm ever learning what a "board" is.

---

## 7. (Optional) drive and read it entirely from the terminal

You don't need the browser to see the same truth. Any terminal can materialize
the board and list peers:

```bash
helm multiplayer status board --cave main
```

**Working looks like:** the current board printed key-by-key (matching what the
web tab shows), then a `peers:` list, then the `cursor`. Add `--json` for the
machine-readable form. This is the exact same fold the browser runs — an agent
can drive the board with `helm multiplayer set …` and verify it with
`helm multiplayer status …`, while you watch the web view move.

---

## If something looks off

- **Nothing on the board / peers after a terminal command?** Make sure both the
  web tab's `cave` box and every terminal command say `main`. A mismatched cave
  name is a different, empty board.
- **`helm: command not found`?** Use `./bin/helm …` from the repo root.
- **Board is empty after a reboot?** Expected. The cave lives in tmpfs
  (`/dev/shm/helm-multiplayer`) and is disposable by design — durable history is
  a client/CRDT concern, not the relay's. Just set cells again.
- **Want a clean slate now?** `rm -rf /dev/shm/helm-multiplayer` wipes every
  cave; reload the tab.

## What this is *not*

This is **local** multiplayer — one machine, human + agents in one cave. There's
no web/hosted cave, no cross-machine sync, no accounts. That remote half is
builders.dev's, and it slots into the **same** five adapter methods
(`publish / updates / heartbeat / peers / leave`) without changing helm's core or
this cockpit tab. See [MULTIPLAYER.md](MULTIPLAYER.md) for the adapter contract.
