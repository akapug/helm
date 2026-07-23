# helm environment variables

helm takes no config file — every knob is an environment variable, and every
one has a working default. A fresh clone needs **none of these** set.

**The env transition law (the "env2" pattern):** helm absorbed two predecessor
tools, and their variables are honored forever as fallbacks. Every variable
below resolves `HELM_<name>` first; if unset, the legacy spelling
(`MELD_*` for the home/substrate lineage, `SESH_*` for the session/quota
lineage) is read; only then does the default apply. Legacy names are **read,
never written** — new code always spells `HELM_*`. `helm doctor` reports which
overrides are in effect.

## the home

| variable | default | read by | legacy fallback |
|---|---|---|---|
| `HELM_HOME` | `~/.helm` | everything (the root resolver; anchored on `$HOME`, never cwd) | `MELD_HOME` |

## the typed store

| variable | default | read by | legacy fallback |
|---|---|---|---|
| `HELM_ADOPTED_DIR` | `~/.claude/projects/<slug-of-$HOME>/memory` | the store resolver — the live Claude memory dir helm adopts in place; the override is how tests point it at a tmp dir | `MELD_ADOPTED_DIR` |

## discovery (auto-map + session catalog)

| variable | default | read by | legacy fallback |
|---|---|---|---|
| `HELM_SCAN_ROOTS` | `~/dev/akapug:~/dev` (the shipped default — set your own checkout roots) | the repo-scan tier of `helm sync` — colon-separated roots under which git repos register as shelf projects | — |
| `HELM_CLAUDE_ROOTS` | — (extra roots; `~/.claude/projects` is always scanned) | the session catalog — colon-separated additional Claude transcript roots | `SESH_CLAUDE_ROOTS` |
| `HELM_CODEX_ROOTS` | — (extra roots; `~/.codex/sessions` and `~/.codex-homes` are always scanned) | the session catalog — additional Codex transcript roots | `SESH_CODEX_ROOTS` |
| `HELM_CATALOG` | — (`scanner` forces the built-in scanner, skipping the `cv` recall index as catalog source) | the session catalog | `SESH_CATALOG` |

## configs

| variable | default | read by | legacy fallback |
|---|---|---|---|
| `HELM_CONFIG_ROOTS` | `~/dev` | `helm configs` — colon-separated roots for the project-scope config tree walk | `SESH_CONFIG_ROOTS` |
| `HELM_SKILL_DECK` | — (no deck; nothing provisioned) | `helm homes create` — a directory of skill dirs (each carrying a `SKILL.md`), symlinked into every new claude home's `skills/` so any credential loads the same setup; existing entries are never touched | — |

## the training corpus

| variable | default | read by | legacy fallback |
|---|---|---|---|
| `HELM_CORPUS_DEST` | `~/corpus-archive` | `helm corpus` — the dated, copy-only transcript archive root (backup + status + the shipped systemd timer). Point it at a larger local mount (e.g. a `/run/media/<you>/…` archive disk) to host the training corpus off the system drive; the manifest/cursor travels with the dest, so switching mounts re-archives from zero there. Extra transcript roots reuse `HELM_CLAUDE_ROOTS`/`HELM_CODEX_ROOTS` above | — |

## chat

| variable | default | read by | legacy fallback |
|---|---|---|---|
| `HELM_CHAT_DIR` | `/dev/shm/helm-chat` (tmpfs — the rooms live in RAM) | `helm chat` + the web chat panel — the room dir (one `<room>.jsonl` + `<room>.owner-unread` marker per room, plus the RAM-side transport caches `.cells.json`/`.node-token`); the override is how tests point it at a tmp dir | — |
| `HELM_CHAT_NAME` | — (the best local identity guess: session id, then user) | `helm chat post` — the `from` name on every message; `helm --human` posts as it (else `david`) | — |
| `HELM_CHAT_ROOM` | unset; join/launch derive the current git project's canonical Helm name, while project-less seats remain un-homed | chat commands, join/delivery hooks, `helm launch`, and multimodel seat presets — explicit values win; a homed seat's inbox is allowlisted to `{home, main}` while a project-less un-homed seat retains the legacy all-room inbox | `MELD_CHAT_ROOM` |
| `HELM_CHAT_ROOM_SOURCE` | — | internal launch provenance. `derived` marks a project default so a later SessionStart cannot overwrite an operator `helm chat seat rehome`; explicit rooms carry no derived marker. Room + source resolve atomically from one namespace, so a preferred `HELM_CHAT_ROOM` never inherits stale `MELD_*` provenance | `MELD_CHAT_ROOM_SOURCE` |
| `HELM_CHAT_NODE_URL` | `http://127.0.0.1:8898` (after the node-state file's `url`) | the chat ROOM NODE — chat v2's signed transport. **Set-but-empty disables the signed transport entirely** (the hermetic-test/ops kill switch); the node migration repoints this at the team node | `MELD_CHAT_NODE_URL` |
| `HELM_CHAT_NODE_BIN` | `dregg-cave-node` on `PATH`, else `~/.local/bin/dregg-cave-node` | `helm chat node up` — the room-node daemon binary | `MELD_CHAT_NODE_BIN` |
| `HELM_CHAT_LOG` | on (`0`/`off`/`no` disables) | `helm chat log-flush` + the `helm --human` exit flush — the log-after leg's operator switch | `MELD_CHAT_LOG` |
| `HELM_CHAT_OWNER_NAMES` | `david` + the unix login (csv override) | the delivery lane (`helm chat deliver`) — senders whose posts reach every seat at tool boundaries WITHOUT an @mention (the owner steers mid-flight) | — |
| `HELM_CHAT_DELIVER` | on (`0`/`off`/`no` disables) | the delivery lane's kill switch — the PostToolUse nudge goes silent fleet-wide; the room, roster and inject lanes are untouched | — |

## local multiplayer

| variable | default | read by | legacy fallback |
|---|---|---|---|
| `HELM_MULTIPLAYER_DIR` | `/dev/shm/helm-multiplayer` | `helm multiplayer` — the tmpfs root for opaque per-document update logs and the separate presence snapshots | — |
| `HELM_MULTIPLAYER_CAVE` | `HELM_CHAT_ROOM`, else `main` | `helm multiplayer` — the default local collaboration cave | — |
| `HELM_MULTIPLAYER_ACTOR` | `HELM_CHAT_NAME`, else the normal seat identity | `helm multiplayer` — the default update/presence actor | — |
| `HELM_MULTIPLAYER_CONNECTION` | the harness session id, else actor | `helm multiplayer` — the presence connection identity; two tabs/sessions under one actor remain independent | — |
| `HELM_MULTIPLAYER_BACKEND` | `local` | `helm multiplayer` — selected registered relay/presence adapter pair | — |

## accounts and quota

| variable | default | read by | legacy fallback |
|---|---|---|---|
| `HELM_PROVIDER` | `native` | provider selection — `native` reads the vendors' own usage endpoints directly (stdlib-only, the default); `cli` opts into an external quota CLI | `SESH_PROVIDER` |
| `HELM_QUOTA_CLI` | `tokaware` | the CLI provider — which binary to shell (only used with `HELM_PROVIDER=cli`) | `SESH_QUOTA_CLI` |
| `HELM_ALLOCATION_RULES` | `~/.config/helm/allocation.json` (legacy `~/.config/sesh/` honored) | allocation ranking — operator prefer/avoid rules per model substring | `SESH_ALLOCATION_RULES` |
| `HELM_PROBE_LOOP` | — (`1` starts the background re-probe loop lazily on the first history read) | the native provider's burn-history collector | `SESH_PROBE_LOOP` |
| `HELM_ALLOC_MODELS` | `fable,opus,gpt-5.5` | the web quota view — which model chips the allocate panel offers | `SESH_ALLOC_MODELS` |
| `HELM_CRED_BACKUP_ROOT` | `~/.cred-backups` | `helm cred backup` / `switch-guard` / `heal` — the credential snapshot root (`0700` dirs, `0600` files). Point it at an encrypted volume if you want the snapshots there; the override is also how tests keep the real root untouched | — |

## the web app

| variable | default | read by | legacy fallback |
|---|---|---|---|
| `HELM_API_TOKEN` | a fresh random token per process | `helm web` — pins the anti-CSRF mutation bearer (useful for scripted POSTs or a long-lived service; see [WEB.md](WEB.md)) | `SESH_API_TOKEN` |

(`__HELM_TOKEN__` seen in `web_ui.html` is not an environment variable — it is
the template placeholder the server substitutes the live token into at serve
time.)

## attestation (native + stdlib) — these vars are for the OPTIONAL dregg anchor only

Premise attestation is **native**: a stdlib, offline hash chain that needs no
binary and no node (see [ATTESTATION.md](ATTESTATION.md)). The variables below
configure only the OPTIONAL external dregg anchor and the OPTIONAL, explicit
a2a transport. Each `MELD_*` is accepted READ-ONLY as an env2 migration
fallback and is never canonical.

| variable | default | read by | legacy fallback |
|---|---|---|---|
| `HELM_NODE_URL` | `http://127.0.0.1:8899` | the OPTIONAL dregg anchor node's HTTP API (fail-open) | `MELD_NODE_URL` |
| `HELM_NODE_TOKEN` | — | OPTIONAL bearer for a gated anchor node | `MELD_NODE_TOKEN` |
| `HELM_NODE_PASSPHRASE` | — | reserved (operator-side node unlock) | `MELD_NODE_PASSPHRASE` |
| `HELM_CELL_PROFILE` | `helm-test` recording label for attestation; `helm-agent` for the optional a2a transport | the provenance LABEL (`attest_by`) — not a cryptographic signer | `MELD_AGENT_PROFILE` |
| `HELM_CELL_BIN` | **unset ⇒ off** (no auto-resolution — never a `PATH`/sibling probe) | the OPTIONAL, explicit-opt-in `helm cell` a2a transport — **not** attestation | `MELD_CELL_BIN` |
| `HELM_ROSTER` | `~/.dregg/roster.toml` | the OPTIONAL a2a transport's roster file | `MELD_ROSTER` |

## the comparison resolver backend (all optional — see [ARCHITECTURE.md](ARCHITECTURE.md) Pluggability)

The local keyword JIT resolver is the authority. A comparison backend runs in
parallel and its results are logged/compared, never trusted. **Unset ⇒ the
comparison is OFF** — the fleet-live default, zero per-turn cost. Set the
endpoint and every turn logs one divergence row; `helm inject --compare-report`
renders the accumulated local-vs-comparison verdict. The Cloudflare
agentic-memory connector is a thin stdlib-`urllib` stub; helm never invents
credentials and never calls a real endpoint in tests.

| variable | default | read by | legacy fallback |
|---|---|---|---|
| `HELM_CF_ENDPOINT` | — (**unset ⇒ comparison OFF**, the default) | the CF comparison backend — the Cloudflare agentic-memory query URL. Set it to activate the comparison (the owner one-step). Wire shape: `POST <endpoint>` with `{"query", "project", "top_k"}` → `{"results": [{"id": …}, …]}` (ranked; helm reads the ids only) | `MELD_CF_ENDPOINT` |
| `HELM_CF_TOKEN` | — (sent as `Authorization: Bearer <token>` when set) | the CF comparison backend — the bearer for the agentic-memory endpoint | `MELD_CF_TOKEN` |

## multimodel seats

| variable | default | read by | legacy fallback |
|---|---|---|---|
| `HELM_PROXY_BIN` | `~/.local/bin/cli-proxy-api`, else `cli-proxy-api` on `PATH` | the seat module (`helm/seat.py`) — the local Anthropic-wire proxy binary | `MELD_PROXY_BIN` |
| `HELM_AUTOCOMPACT_THRESHOLD` | `90` | proxy-seat autocompact — context percentage at which `/compact` is injected | — |
| `HELM_AUTOCOMPACT_ASSUME_WINDOW` | `200000` (`0`/`off` disables fallback) | proxy-seat autocompact — Claude Code's assumed context window when a family does not declare `max_context` | — |
| `HELM_AUTOCOMPACT_FRESH_S` | `21600` (6h) | proxy-seat autocompact — maximum transcript age accepted as live context | — |
| `HELM_AUTOCOMPACT_LATCH_TTL` | `900` (15m) | proxy-seat autocompact — repeat delay for a manual-action alert only. Injected or already-pending `/compact` stays latched until the recorded context drops or the session changes; time alone never authorizes a duplicate injection | — |
| `KIMI_API_KEY` | — | `helm seat add kimi` only — the Moonshot outbound key for the kimi proxy-key seat. A **raw provider variable**: no `HELM_` prefix, no legacy fallback, **not** auto-loaded from any `.env`. Export it (or pass `--key-from <env-file>` carrying a `KIMI_API_KEY=` line) for the one `add` run; the key is baked into the seat's 0600 `config.yaml` and never read from the environment again | — |

## non-HELM integration variables

`CLUSTERVISION_CLAUDE_ROOTS` — CV's platform-separated additional Claude
`projects/` roots. Helm preserves any caller-provided roots and appends every
minted family/instance seat root to every CV subprocess, so proxy-seat
transcripts participate in the one recall index without a second scanner.

`MC_HOME` — `helm whoami` merges its profile scaffold from a Mission Control
user-profile at `$MC_HOME/user-profile/profile.json` (default
`~/.mc/mission-control`) when one exists. Absent, the profile starts fresh —
no dependency either way.
