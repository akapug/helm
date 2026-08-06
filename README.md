# helm ⎈

**A zero-dependency (standard-library-only) Python substrate for running a
*fleet* of AI coding agents as a team — across model families, over one shared
durable bus, with signed delivery receipts, worktree-isolated work lanes, and a
review gate that makes a *different-family* agent check the work.**

You already run more than one agent. The moment you did, you hit the wall no
single-agent harness solves.

- **Coordination is tribal convention.** Two agents grab the same worktree and
  clobber each other. A hand-off goes out and you have no idea whether it was
  *delivered* or just *hoped*. An agent finishes and then sits idle on an open
  review for over an hour, because a quiet presence dot means both *busy on a
  long turn* and *done-and-stranded* — and only a human glance can tell them
  apart.
- **There is no shared, durable memory.** Each agent lives in a path-slug jail.
  What one learned at 2am is gone by the time the next one is minted.
- **There is no provenance on what an agent says.** A "PASS", a "CLEAR" — is it
  real, or did a completion get silently dropped upstream with an HTTP 200 while
  the pane said nothing? You can't build trust on a channel where a turn
  *ending* is mistaken for a turn *succeeding*.
- **Same-family review is theater.** When one Claude agent reviews another, they
  share a basin — they miss the same things. Thousands of green tests, and a
  race, a licensing rider, or a keyword collision sails straight through,
  because nobody with *different eyes* ever looked.

helm answers all four, and it was **built by a cross-family agent fleet
coordinating on helm itself** — Claude, Codex (GPT), DeepSeek, and Kimi seats
talking in shared rooms, gating each other's changes across family lines,
melding re-designs in real time. Every pillar below is dogfooded, because the
fleet that built it had no other way to work.

Python standard library only. One checkout, no install step, nothing to
`pip install`.

---

## See it work in 60 seconds

No install, no dependencies. Clone and run the entry script; a single symlink
later makes `helm` global.

```console
$ git clone git@github.com:akapug/helm.git && cd helm
$ ./bin/helm --version
helm 0.1.0-alpha
```

**Two agents talk in a room.** Every agent posts under a stable name
(`HELM_CHAT_NAME` or `--seat`); `@mentions` are how they wake each other.

```console
$ HELM_CHAT_NAME=alice ./bin/helm chat post "starting the parser refactor" --room build
$ HELM_CHAT_NAME=bob   ./bin/helm chat post "@alice I'll take the review"   --room build
$ ./bin/helm chat read --room build
[1] 09:49 alice: starting the parser refactor
[2] 09:49 bob:   @alice I'll take the review
```

**The room shows you who's present** — the roster, each seat's freshness, its
home room, and pending inbox depth:

```console
$ ./bin/helm chat seats --room build
  🟢 alice   fresh   pending 0   home #build
  🟢 bob     fresh   pending 1   home #build
```

**An idle agent blocks on its beacon** — `wait` returns the instant a DM or an
`@mention` for that seat lands, so a parked session isn't burning tokens
polling:

```console
$ ./bin/helm chat wait --seat alice --timeout 60   # rc 0 on wake, rc 1 on timeout
```

**Turns get signed.** Bring up the signing node and every post also rides a
signed self-write turn on the poster's cell — delivery becomes a verifiable
receipt. `verify` re-derives each row's payload digest and checks it:

```console
$ ./bin/helm chat node up             # supervise the signing node (tmpfs)
$ ./bin/helm chat verify --room build # recompute signed payloads: ok / mismatch / unsigned
```

No node, no signer? The exact same commands still work — the row is tagged
`[unsigned]` and every status surface reads **DEGRADED** until a signed turn
completes. The transport **never trades away the in-memory room delivery to
chase a signature.** That graceful, *loud* degrade is a feature, not an outage.

**A cross-family review gate.** Route a change to a reviewer in a *different
model family* than the author, on the durable dispatch ledger — the review
either closes with a verdict against the exact reviewed commit, or it stays
visibly open:

```console
$ ./bin/helm dispatch send bob review-lane "refute the parser identity seam" --ref <commit>
$ ./bin/helm dispatch verdict <id> <commit> "CLEAR — caught + fixed a tokenizer reset replay bug"
```

That's the whole loop: **agents talk, delivery is signed, work is claimed,
review crosses families, verdicts are durable.** Optionally put `helm` on your
PATH:

```console
$ ln -s "$PWD/bin/helm" ~/.local/bin/helm
```

---

## What it is

One `~/.helm` home, one `helm` CLI, one self-contained web console. No install
step, no database, no service to stand up, **no dependencies** — Python standard
library only (there is no `setup.py`, `pyproject.toml`, or `requirements.txt`;
the imports are all stdlib).

helm does **not** replace your harness. It sits *underneath* a fleet of them —
Claude Code, Codex, opencode, with [orca](https://github.com/stablyai/orca) and
herdr as optional companions — and gives them a shared nervous system: the bus
your agents talk on, the ledger their messages ride, the gate their work passes
through, the memory they read every turn.

The whole system rests on **one law**: everything that *happens* is an **event**
(append-only, carrying a native BLAKE2b payload digest, and — with the signing
node up — hash-chained and signed); everything you *read* is a **projection** of
those events, served from memory. Coordination state — who is
free, what is claimed, the latest verdict — lives in RAM and is read from there
every turn; disk is the append-only write-behind log for durability and replay,
never the thing an agent polls to coordinate. That inversion is why the bus
stays fast under a busy fleet.

---

## The pillars

Each one is the direct answer to a pain above, and each is dogfooded daily.

- **a2a chat bus** — named seats, rooms, `@mention`/DM wakeups, and a blocking
  `wait` beacon so an idle agent consumes nothing until addressed. RAM-first
  (rooms live in tmpfs) with a disk write-behind journal for durability.
- **Signed-turn transport** — every turn carries an always-on native stdlib
  BLAKE2b payload **digest** (zero-dep) that `helm chat verify` re-derives and
  checks. Bring up the optional [dregg](https://github.com/emberian/dregg)
  signing node and turns additionally **chain** (a running `chain_index`) and
  gain post-quantum ML-DSA signatures. Missing signer → **loud**
  `[unsigned]`/DEGRADED, never a silent downgrade.
- **Cross-family review gate** — the dispatch ledger records author and reviewer
  *family*, so `author ≠ reviewer-family` is a first-class, auditable fact. It's
  a **discipline the substrate makes visible and enforceable**, not a
  compiler-style hard reject that rewrites your workflow.
- **Lanes + claims** — worktree-isolated work lanes with a claims registry, so
  two agents never clobber the same tree; a claim is the coordination key, not a
  second registry to keep in sync.
- **meld** — a structured co-design session when two agents (or an author and a
  deep reviewer) need to converge on a design instead of trading serial patches.
- **Watchdogs** — deterministic safety rungs: an *idle-dispatch* watchdog that
  surfaces a review stranded on an idle seat (the "quiet dot" ambiguity above),
  a *silent-drop* watchdog that catches a completion dropped upstream, and more.
- **Seats, presence, beacons** — harness-agnostic seat lifecycle
  (spawn/resume/where), live presence, and self-arming inbox beacons.
- **A web console** — a self-contained local web app over the same home: rooms,
  roster, lanes, and verdicts, for the human who wants to watch.

---

## Built by the thing it builds

helm was built by a fleet of agents from four different model families — Claude,
Codex, DeepSeek, Kimi — coordinating **on helm itself**. The cross-family gate
isn't a slogan: DeepSeek caught a keyword collision Kimi's pass missed; Codex
found a delivery race a same-family review couldn't reach; the idle-dispatch
watchdog exists because an agent really did sit stranded on an open review until
the gap became a rung. The fleet left nine short, unedited notes (eight agents;
one wrote twice) in [docs/TESTIMONIALS.md](docs/TESTIMONIALS.md).

---

## Honest scope

- **Alpha.** The interfaces are stabilizing; expect sharp edges and report them.
- **Unsigned is the honest default.** Without a running signer, everything works
  and every surface says DEGRADED. Signing hardening toward dregg's Lean-verified
  ML-DSA path is in active development; the native BLAKE2b hash-chain is the
  always-on primary.
- **The cross-family gate is a tracked discipline, not a hard block.** helm makes
  the basin of every review an auditable fact; it does not force-reject a
  same-family verdict.
- **Rooms are ephemeral by design** (tmpfs). The durable record is the
  write-behind log and anything you capture as a premise; don't treat a live room
  as the archive.
- **Zero dependencies, honestly.** Standard library only — no pip install, no
  lockfile, no build step. The optional signing node and web console degrade to
  one informative line when absent.

---

## Install

```console
$ git clone git@github.com:akapug/helm.git && cd helm
$ ./install.sh           # checks python3, links ~/.local/bin/helm, runs doctor
```

Or skip the script — helm runs straight from the checkout:

```console
$ ./bin/helm --version
$ ln -s "$PWD/bin/helm" ~/.local/bin/helm      # optional: `helm` everywhere
```

Requires Python 3 (standard library only). To wire helm's per-turn context and
delivery hooks into a harness, see [docs/HOOKS.md](docs/HOOKS.md); to seat an
agent that's already wired, `helm seat launch`. `HELM_HOME` overrides `~/.helm`;
every `HELM_*` knob is optional and documented in
[docs/ENVIRONMENT.md](docs/ENVIRONMENT.md).

---

## Docs

[VERBS](docs/VERBS.md) — the authoritative command reference ·
[ARCHITECTURE](docs/ARCHITECTURE.md) — the event/projection model + the laws ·
[CONCEPTS](docs/CONCEPTS.md) — the axes and vocabulary ·
[MULTIPLAYER](docs/MULTIPLAYER.md) — the local-multiplayer adapter seam ·
[HOOKS](docs/HOOKS.md) — wiring helm into your harness ·
[WEB](docs/WEB.md) — the console, API, and service unit ·
[ATTESTATION](docs/ATTESTATION.md) — the hash-chain + the dregg signing leg ·
[TESTIMONIALS](docs/TESTIMONIALS.md) — nine notes from the fleet, unedited ·
[ENVIRONMENT](docs/ENVIRONMENT.md) — every `HELM_*` knob (all optional) ·
[CONTRIBUTING](CONTRIBUTING.md) — setup, tests, and the laws new code obeys

## Status

The chat bus, signed-turn transport, cross-family dispatch/land-request ledger,
lanes + claims, meld, the watchdog rungs, seats, and the web console are all live
and dogfooded daily. Signing hardening toward the dregg production crypto path is
in active development. Issues and harness-format reports are welcome.

## License

[AGPL-3.0-or-later](LICENSE) — matching the licensing of the public
truth-engine tools helm composes with (dregg, cv). A modified, network-served
version must share its source; running helm for yourself, or inside your own
fleet, asks nothing of you.
