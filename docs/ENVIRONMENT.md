# helm environment variables

helm takes no config file for its knobs — every knob is an environment
variable, and every one has a working default. A fresh clone needs **none of
these** set. Facts about one host that the source must not carry are files
under the helm home instead; see [host facts](#host-facts-files-not-variables).

**The env transition law (the "env2" pattern):** helm absorbed two predecessor
tools, and their variables are honored forever as fallbacks where a row names
one. Such variables resolve `HELM_<name>` first; if unset, the listed legacy
spelling (`MELD_*` for the home/substrate lineage; for the session/quota
lineage, `<NAME>_*` of the predecessor this host's [local names](#local-names)
declare, and nothing where they declare none) is read; only then does the
default apply. A row marked
`—` or **strictly no legacy fallback** reads only its current spelling. Legacy
names are **read, never written** — new code always spells `HELM_*`. `helm
doctor` reports which overrides are in effect.

## the home

| variable | default | read by | legacy fallback |
|---|---|---|---|
| `HELM_HOME` | `~/.helm` | everything (the root resolver; anchored on `$HOME`, never cwd) | `MELD_HOME` |
| `HELM_PROFILE_HOME` | — (unset: the adopted homes, each project's authored `adopt` path) | `helm whoami` — merges its profile scaffold from an external user-profile at `$HELM_PROFILE_HOME/user-profile/profile.json` when one exists; no such file, and the profile starts fresh | — (**strictly no legacy fallback**) |

## the version-control seam

`helm/vcs.py` is the `Vcs` interface plus today's only backend, `GitVcs`. It
exists so a second backend (jj/jujutsu, which `helm doctor` has named as "on the
radar" since the beginning) is later an ADDITION rather than a rewrite of every
call site. It covers the git calls of `helm work` (worktree + lane lifecycle),
`helm ship`, `helm capsule`, the handoff/now probes and the lane gc verdict
reads — **not** yet every git call in helm; the remainder is enumerated and
count-pinned by `DirectSpawnAuditTest` in `tests/test_vcs.py` and summarized in
[ARCHITECTURE.md](ARCHITECTURE.md).

Selection is per-**target** (the repo or checkout being operated on, never the
cwd a verb was typed in), and the default is spelled out rather than implied:
`HELM_VCS` wins; else the nearest `.jj`/`.git` marker walking **up** from the
target; else **git**. Only `GitVcs` is built today, so a `jj` or unrecognized
selection resolves to git and says so in one line on stderr — a VCS preference
can never take a helm verb down.

| variable | default | read by | legacy fallback |
|---|---|---|---|
| `HELM_VCS` | **`git`** — the explicit default (unset means git, not "undecided"); `jj` and any unknown value also resolve to git today, with a one-line stderr note | `helm/vcs.py` — the backend behind `helm work`'s worktree lifecycle, `helm ship`, `helm capsule`, the handoff/now git probes and the lane/gc verdict reads | — (**read strictly as `HELM_VCS`**: the env2 legacy fallback exists only for variables that replaced a predecessor tool's spelling, and this knob is new — no `MELD_VCS` is honored) |

## the typed store

| variable | default | read by | legacy fallback |
|---|---|---|---|
| `HELM_ADOPTED_DIR` | `~/.claude/projects/<slug-of-$HOME>/memory` | the store resolver — the live Claude memory dir helm adopts in place; the override is how tests point it at a tmp dir | `MELD_ADOPTED_DIR` |
| `HELM_PROJECT_ALIASES` | — (empty: no built-in aliases) | `helm drain` — comma-separated `handle=canonical` pairs routing legacy project handles to registry projects (full contract below) | — |

**The `HELM_PROJECT_ALIASES` contract.** Syntax is
`handle=canonical,handle=canonical` — exactly one `=` per entry; whitespace
around either side is stripped; handles are case-folded to lowercase; the
target is case-sensitive and MUST name an existing registry project (a
case-mismatched target is warned on stderr with the registry spelling and
dropped; an unknown target is dropped loudly too). A malformed entry — no
`=`, an empty handle, an empty target, or a second `=` — is rejected loudly
on stderr and never crashes the drain. Duplicates: within the env var the
FIRST definition of a handle wins (later ones are rejected loudly); a handle
that also appears in a project record's authored `aliases` list resolves to
the AUTHORED target (warned when the two disagree). Matching precedence at
route time: canonical registry names always beat aliases; among aliases the
LONGEST handle wins regardless of source or listing order (equal lengths
tie-break alphabetically) — `old-api-roadmap.md` reaches an `old-api` alias,
never `old`. This matters because `drain --apply` copies to the matched
project and then deletes the intake file: an ambiguous match is destructive.

## discovery (auto-map + session catalog)

| variable | default | read by | legacy fallback |
|---|---|---|---|
| `HELM_SCAN_ROOTS` | `~/dev` (the two-tier walk covers `~/dev/<org>/<repo>` too; set your own colon-separated roots) | the repo-scan tier of `helm sync` — colon-separated roots under which git repos register as shelf projects. Also the string gate of the argv-guard's tree rung: only a path under one of these roots is ever checked against the registry (docs/HOOKS.md, the argv-guard row) | — |
| `HELM_CLAUDE_ROOTS` | — (extra roots; `~/.claude/projects` is always scanned) | the session catalog — colon-separated additional Claude transcript roots | `<NAME>_CLAUDE_ROOTS` |
| `HELM_CODEX_WEEKLY_CEILING_PCT` | `90` | the fleet-wide SOFT CEILING on a pooled codex account's LONGEST window (the weekly one). `helm proxywatch --post` (the timed pass, and the only one that reads the pool or writes the `codex-pool-budget` snapshot) posts `FAMILY-BUDGET-LOW` once when the pool crosses it; `helm dispatch send` to a codex recipient warns at mixed headroom and REFUSES (`--force` overrides) only when every pooled account was read and every one is at or past it. Out-of-range or unparseable falls back to the default rather than raising — a typo here must never be able to refuse the fleet's work | — |
| `HELM_CODEX_ROOTS` | — (extra roots; `~/.codex/sessions` and `~/.codex-homes` are always scanned) | the session catalog — additional Codex transcript roots | `<NAME>_CODEX_ROOTS` |
| `HELM_CATALOG` | — (`scanner` forces the built-in scanner, skipping the `cv` recall index as catalog source) | the session catalog | `<NAME>_CATALOG` |

## configs

| variable | default | read by | legacy fallback |
|---|---|---|---|
| `HELM_CONFIG_ROOTS` | `~/dev` | `helm configs` — colon-separated roots for the project-scope config tree walk | `<NAME>_CONFIG_ROOTS` |
| `HELM_SKILL_DECK` | — (no deck; nothing provisioned) | `helm homes prepare claude <email>` — a directory of skill dirs (each carrying a `SKILL.md`), symlinked into every new claude home's `skills/` so any credential loads the same setup; existing entries are never touched | — |

## the training corpus

| variable | default | read by | legacy fallback |
|---|---|---|---|
| `HELM_CORPUS_DEST` | `~/corpus-archive` | `helm corpus status` — the dated, copy-only transcript archive root (backup + status + the shipped systemd timer). Point it at a larger local mount (e.g. a `/run/media/<you>/…` archive disk) to host the training corpus off the system drive; the manifest/cursor travels with the dest, so switching mounts re-archives from zero there. Extra transcript roots reuse `HELM_CLAUDE_ROOTS`/`HELM_CODEX_ROOTS` above | — |

## chat

| variable | default | read by | legacy fallback |
|---|---|---|---|
| `HELM_CHAT_DIR` | `/dev/shm/helm-chat` (tmpfs — the rooms live in RAM), via `home.surface_dir` in trust order: explicit `HELM_CHAT_DIR` wins; else a REDIRECTED `HELM_HOME` (realpath off `~/.helm`) isolates the surface at `<HELM_HOME>/helm-chat`, so an isolated estate never resolves the live fleet bus (#117); else the shared tmpfs default | `helm chat` + the web chat panel — the room dir (one `<room>.jsonl` + `<room>.owner-unread` marker per room, plus the RAM-side transport caches `.cells.json`/`.node-token`); the override is how tests point it at a tmp dir | — |
| `HELM_CHAT_EVENT_DIR` | a durable bus-keyed `chat-event-receipts` root under the live `~/.helm`, regardless of which `HELM_HOME` or explicit/derived route reached that bus | internal `chat.post(..., event_id=...)` retry receipts only — each canonical chat bus gets a private ledger independent of tmpfs rotation/reboot; hermetic or separately isolated estates override the durable root here | `MELD_CHAT_EVENT_DIR` |
| `HELM_CHAT_NAME` | — (the best local identity guess: session id, then user) | `helm chat post` — the `from` name on every message; `helm --human` posts as it (else the derived owner handle: git user.name, else the login) | — |
| `HELM_CHAT_ROOM` | unset; join/launch derive the current git project's canonical Helm name, while project-less seats remain un-homed | chat commands, join/delivery hooks, `helm launch`, and multimodel seat presets — explicit values win; a homed seat's inbox is allowlisted to `{home, main}` while a project-less un-homed seat retains the legacy all-room inbox | `MELD_CHAT_ROOM` |
| `HELM_CHAT_ROOM_SOURCE` | — | internal launch provenance. `derived` marks a project default so a later SessionStart cannot overwrite an operator `helm chat seat rehome`; explicit rooms carry no derived marker. Room + source resolve atomically from one namespace, so a preferred `HELM_CHAT_ROOM` never inherits stale `MELD_*` provenance | `MELD_CHAT_ROOM_SOURCE` |
| `HELM_CHAT_NODE_URL` | `http://127.0.0.1:8898` (after the node-state file's `url`) | the chat ROOM NODE — chat v2's signed transport. **Set-but-empty disables the signed transport entirely** (the hermetic-test/ops kill switch); the node migration repoints this at the team node | `MELD_CHAT_NODE_URL` |
| `HELM_CHAT_NODE_BIN` | the binary the last `helm chat node up` recorded (refused, never replaced, when that file is gone or its sha256 changed); with nothing recorded, `dregg-cave-node` on `PATH`, else `~/.local/bin/dregg-cave-node` | `helm chat node up` — the room-node daemon binary; `up` records the one it installs | `MELD_CHAT_NODE_BIN` |
| `HELM_CHAT_NODE_BOOT_WAIT_S` | `600` | `helm chat node up` — seconds to wait for the room node's API after starting the unit. A rebased node runs its full verified-runtime (Lean) init before binding the API (measured 150-154 s on a fast host); the fee-loop build answers in under a second. The wait ends at once if the unit's process exits, so a refusal is reported in seconds, not after the full wait; a wait that runs out with the process still running reports `still initializing verified runtime`, not a failure. Unparseable or non-positive falls back to the default. `helm chat node status`, `helm doctor` and `up` itself call a process that has run longer than this without its API answering `hung` (a FAIL; `up` says so at once instead of waiting) — floored at the default, so lowering the wait for a fast build never makes a rebased node's 150 s init read as hung; raising it for a slow host raises that threshold too | `MELD_CHAT_NODE_BOOT_WAIT_S` |
| `HELM_CHAT_LOG` | on (`0`/`off`/`no` disables) | `helm chat log-flush` + the `helm --human` exit flush — the log-after leg's operator switch. Off means the rooms are tmpfs with NO durable copy, so `helm doctor` and `helm brief` both say so rather than reading silent | `MELD_CHAT_LOG` |
| `HELM_LANDER` | the INTEGRATOR role, resolved against the live roster | the one place a role with no per-seat addressee is turned into a name: the dispatch land/author nudges and the chat durability watchdog's alarm. **A value set here is OBEYED** — it is the escape hatch for the case where the ordinary resolution is wrong, including where the roster is wrong, so it is not re-confirmed against the roster before being honoured. A name nothing answers to is not silent either: the send door resolves every addressee and says ABSENT when no roster row carries it. Membership then picks the channel — a DM to a seat the roster says has JOINED, an addressed `#helm` post when it says ABSENT, both when it cannot say | — |
| `HELM_INTEGRATOR_SEAT` | `helm-integrator`, confirmed against the live roster (else the one rostered seat whose name ends `-integrator`) | the integrator role wherever helm resolves it: escalations (`helm chat post --to-integrator`, a superseded lane's triage note, the rogue-compute and silent-drop alerts) and the sweeps that key unowned work to it. A value that names no rostered seat is refused, never replaced by a guess; an escalation then posts its text with `[UNROSTERED: ...]` and the reason | `MELD_INTEGRATOR_SEAT` |
| `HELM_CHAT_OWNER_NAMES` | the derived owner handle + the unix login (csv override) | the delivery lane (`helm chat deliver`) — senders whose posts reach every seat at tool boundaries WITHOUT an @mention (the owner steers mid-flight) | — |
| `HELM_CHAT_DELIVER` | on (`0`/`off`/`no` disables) | the delivery lane's kill switch — the PostToolUse nudge goes silent fleet-wide; the room, roster and inject lanes are untouched | — |
| `HELM_CHAT_FAUCET_CELL` | — (a recorded refusal at this node, else the local unit's `fee_well` boot line) | `helm doctor`, `helm chat node status`/`refuel` — names the chat node's faucet cell outright, for the configured node only | `MELD_CHAT_FAUCET_CELL` |
| `HELM_CHAT_FAUCET_LOW_GRANTS` | `5` | `helm doctor`, `helm chat node status`/`refuel` and the low-faucet wake — the faucet reads LOW below this many grants (a grant is the largest refused `need` recorded at this node in the last week, else 697). Unparseable or below 1 falls back to the default. LOW is also the line `refuel` treats as "already funded" | — |
| `HELM_CHAT_NODE_OWNER` | — (none: the wake names the integrator alone and says no owner is set) | the low-faucet wake — the seat woken beside the integrator, once per low spell. A name the roster does not carry is not woken, and the wake says so | — |

## local multiplayer

| variable | default | read by | legacy fallback |
|---|---|---|---|
| `HELM_MULTIPLAYER_DIR` | `/dev/shm/helm-multiplayer` | `helm multiplayer` — the tmpfs root for opaque per-document update logs and the separate presence snapshots | — |
| `HELM_MULTIPLAYER_CAVE` | `HELM_CHAT_ROOM`, else `main` | `helm multiplayer` — the default local collaboration cave | — |
| `HELM_MULTIPLAYER_ACTOR` | `HELM_CHAT_NAME`, else the normal seat identity | `helm multiplayer` — the default update/presence actor | — |
| `HELM_MULTIPLAYER_CONNECTION` | the harness session id, else actor | `helm multiplayer` — the presence connection identity; two tabs/sessions under one actor remain independent | — |
| `HELM_MULTIPLAYER_BACKEND` | `local` | `helm multiplayer` — selected registered relay/presence adapter pair | — |

## fleet notes

| variable | default | read by | legacy fallback |
|---|---|---|---|
| `HELM_FLEET_NOTES` | `~/.helm/_global/fleet-notes.json` | `helm note` + the cockpit's `GET /api/notes` — the durable keyed map of what the fleet left the owner. On DISK beside the other `_global` state on purpose: its predecessor was a cell on the tmpfs multiplayer board, so every reboot erased the notes that existed to explain the reboot. The override is also how tests keep the real home untouched | — (**read strictly as `HELM_FLEET_NOTES`** — the verb is new, so there is no predecessor spelling to honor) |

## storage matrix

| variable | default | read by | legacy fallback |
|---|---|---|---|
| `HELM_STORAGE_MATRIX` | `~/.helm/_global/.state/storage-matrix.json` | `helm storage-matrix` + the cockpit's read-only storage API — the host-local rebuildable benchmark snapshot. The override is how tests keep the real home untouched | — |
| `HELM_STORAGE_MATRIX_HOSTS` | unset — the box-provider chain supplies the inventory (local floor; the optional external inventory helper's Linux build/standby rows when it is installed; consented SSH-config hosts) | `helm storage-matrix --measure` via the declared rung of the chain (`helm/boxes.py`) — comma-separated SSH aliases that limit or explicitly request remote boxes; naming a host here IS the consent to probe it. Names are validated before SSH; a host no richer rung knows still gets a probeable row | — |
| `HELM_BOXES_SSH_CONFIG` | `~/.ssh/config` | the chain's candidate rung — which SSH client config supplies `Host` NAMES (only the names are ever read, never any other value). Tests point it at a fixture | — |
| `HELM_BOXES_SSH_CONSENT` | unset — SSH-config candidates are LISTED, never probed | the explicit opt-in that makes candidates probeable: `all` (or `1`) consents every candidate; a comma-separated host list consents exactly those | — |
| `HELM_BOXES_EXTERNAL_CLI` | `fab`, discovered on PATH | the chain's external rung — the optional cross-box inventory helper shelled for `nodes --json`. Absent is SILENT: this box alone is a complete inventory, not a partial one | — |

## accounts and quota

| variable | default | read by | legacy fallback |
|---|---|---|---|
| `HELM_PROVIDER` | `native` | provider selection — `native` reads the vendors' own usage endpoints directly (stdlib-only, the default); `cli` opts into an external quota CLI | `<NAME>_PROVIDER` |
| `HELM_QUOTA_CLI` | — (unset: no CLI quota provider; `HELM_PROVIDER=cli` without it falls back to native, loudly) | the CLI provider — which binary to shell (only used with `HELM_PROVIDER=cli`) | `<NAME>_QUOTA_CLI` |
| `HELM_ALLOCATION_RULES` | `~/.config/helm/allocation.json` (a declared predecessor's `~/.config/<name>/` honored) | allocation ranking — operator prefer/avoid rules per model substring | `<NAME>_ALLOCATION_RULES` |
| `HELM_PROBE_LOOP` | — (`1` starts the background re-probe loop lazily on the first history read) | the native provider's burn-history collector | `<NAME>_PROBE_LOOP` |
| `HELM_ALLOC_MODELS` | `fable,opus,gpt-5.5` | the web quota view — which model chips the allocate panel offers | `<NAME>_ALLOC_MODELS` |
| `HELM_CRED_BACKUP_ROOT` | `~/.cred-backups` | `helm cred backup` / `switch-guard` / `heal` — the credential snapshot root (`0700` dirs, `0600` files). Point it at an encrypted volume if you want the snapshots there; the override is also how tests keep the real root untouched | — |
| `HELM_CREDHOME_ORCA_SYNC` | on | `helm launch` / `helm sessions resume --go` onto a named credhome — `0`/`off` skips the Orca freshness check and sync before exec (the launch says so); `helm cred sync-orca` ignores it. Orca's store is read at `$ORCA_USER_DATA_PATH/claude-accounts` (else `$XDG_CONFIG_HOME/orca`, `~/.config/orca`) | — |
| `HELM_CLAUDE_CLI` | the newest version under `~/.local/share/claude/versions/` | `helm keepalive --apply` — the Claude Code executable the grant's OAuth client id and user agent are read from. helm ships neither value: a CLI that cannot be found or read refuses the grant and says why | — (**strictly no legacy fallback**) |

## the web app

| variable | default | read by | legacy fallback |
|---|---|---|---|
| `HELM_API_TOKEN` | a fresh random token per process | `helm web` — pins the anti-CSRF mutation bearer (useful for scripted POSTs or a long-lived service; see [WEB.md](WEB.md)) | `<NAME>_API_TOKEN` |

(`__HELM_TOKEN__` in the assembled UI fragments is not an environment
variable — it is the template placeholder the server substitutes the live
token into at serve time.)

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
| `HELM_CELL_BIN` | **unset ⇒ off for a bare process** (no auto-resolution — never a `PATH`/sibling probe) — but every `helm seat launch`/`spawn` EXPORTS it into the seat's environment (`seat.py`: `~/.local/bin/dregg-client-sign`, the signer default), so fleet seats run with the transport ON; only a hand-started process is opt-in | the OPTIONAL `helm cell` a2a transport — **not** attestation | `MELD_CELL_BIN` |

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

## the long-tail re-rank (all optional — see `helm relevance`)

The settings live in `<helm home>/_global/relevance.json` (host facts, below).
Two variables reach the outside evaluator's credential. The value is only ever
put in a request header: it is never printed, logged, written or passed in
argv. A turn reaches the evaluator only when its project's AUTHORED registry
residency is `may-leave-lan` (`helm projects residency`); these variables do
not change that.

| variable | default | read by | legacy fallback |
|---|---|---|---|
| `HELM_JEV_CRED_FILE` | — (then `jev.cred_file` in `relevance.json`) | `helm/relevance.py` — the path of a file holding one `KEY=value` line (markdown escapes stripped) | `MELD_JEV_CRED_FILE` |
| `AI_GATEWAY_API_KEY` | — | `helm/relevance.py` — the gateway key itself; wins over any file | — |

## multimodel seats

| variable | default | read by | legacy fallback |
|---|---|---|---|
| `HELM_PROXY_BIN` | `~/.local/bin/cli-proxy-api`, else `cli-proxy-api` on `PATH` | the seat module (`helm/seat.py`) — the local Anthropic-wire proxy binary | `MELD_PROXY_BIN` |
| `HELM_PROXY_FORK_DIR` | `~/.helm/_global/proxy-fork/CLIProxyAPI` | `helm proxy-fork-watch` — the durable CLIProxyAPI source clone containing `cmd/helm-upstream-check`; it must be on local branch `helm/upstream-tracking` | — (**strictly no legacy fallback**) |
| `HELM_AUTOCOMPACT_THRESHOLD` | `80` | proxy-seat autocompact — context percentage at which `/compact` is injected | — |
| `HELM_AUTOCOMPACT_ASSUME_WINDOW` | `200000` (`0`/`off` disables fallback) | proxy-seat autocompact — Claude Code's assumed context window when a family does not declare `max_context` | — |
| `HELM_AUTOCOMPACT_FRESH_S` | `21600` (6h) | proxy-seat autocompact — maximum transcript age accepted as live context | — |
| `HELM_AUTOCOMPACT_LATCH_TTL` | `900` (15m) | proxy-seat autocompact — repeat delay for a manual-action alert only. Injected or already-pending `/compact` stays latched until the recorded context drops or the session changes; time alone never authorizes a duplicate injection | — |
| `HELM_AUTOCOMPACT_TIMER` | unset = install (`0`/`off`/`no`/`false` turns the install off) | proxy-seat autocompact — the off switch for the `helm-autocompact.timer` install that `helm seat launch`/`spawn`/`resume` and `helm seat autocompact --install-timer --apply` do. Off, no unit file is written and no `systemctl` runs: the seat verbs print one NOTE line and continue, and `--install-timer --apply` exits 1. Set it where a process must not change the host's scheduler (the test suite sets `0`) or where another scheduler runs `helm seat autocompact --once` | — |
| `HELM_AUTOCOMPACT_CLAUDE` | `1` (`0`/`off`/`no` restores the old skip) | proxy-seat autocompact — whether a claude-model seat is measured and compacted like every other seat. ON since the resume leg landed; the skip existed because a deliberate `/compact` ended the turn and nothing restarted it, which `helm seat resume-turn` now does. Set `0` on a fleet whose `SessionStart` resume-turn hook is not installed | — |
| `HELM_RESUME_TURN` | `1` (`0`/`off`/`no` disarms) | the post-compaction resume leg (`helm/resumeturn.py`) — the kill switch | — |
| `HELM_RESUME_TURN_SETTLE_S` | `3.0` | resume leg — composer-settle grace the detached child waits before injecting. The MEASURED floor is 0 (36/36 injections submitted at hook+0.02s through hook+3.0s against a real Claude Code 2.1.220 TUI on a pty); the default is margin for the metaharness seam and a loaded host, and a detached child pays nothing for it | — |
| `HELM_RESUME_TURN_DEBOUNCE_S` | `120` (2m) | resume leg — a second `SessionStart(compact)` inside this window is the SAME compaction re-firing (a compaction itself takes 100–190s), so it is a silent no-op, never a second injection; it is also the launch and act horizon of a DEAF-IN-EFFECT repair attempt, which is refused as a stale mint once it is this old, at its launch and again at every keystroke | — |
| `HELM_ACT_DEADLINE_S` | `2.0` | act doors (`helm/harness.py`) — the declared monotonic window from the start of an authorized keystroke's final capture (every certified dependency, then one composer read) to the START of the send that carries the keystroke. A capture that has not returned by then, or a window that closes first, restarts the door and then refuses with nothing acted on, naming how long the capture ran (dependencies versus composer). Derived from a measured `orca terminal read` round trip (max 0.80s of 40 on the loaded hub, 2026-09-14) | — |
| `HELM_ACT_SEND_S` | `5.0` | act doors — the client-side bound on the one CLI send that carries an act's keystroke: the timeout of the client subprocess, which starts inside the act callback when the subprocess is launched. It is separate from `HELM_ACT_DEADLINE_S`, the capture admission window that ends when the act callback starts. Neither of them, nor their sum, is a guaranteed deadline from the capture to the send's return or to the keystroke's effect in the terminal: the work before the launch, process creation and termination, and scheduling are not bounded by the subprocess timeout. A send killed at the timeout is accounted OBSOLETE-AUTHORIZATION with its landing unknown, and a server that received it before the kill still lands it. Every act's receipt publishes the measured capture-to-send-start and capture-to-send-return | — |
| `HELM_RESUME_TURN_SPIRAL_S` | `900` (15m) | resume leg — a genuinely NEW compaction this soon after a resume means the resume is feeding the spiral: stop injecting, alert loudly instead | — |
| `HELM_RESUME_TURN_MAX` | `3` | resume leg — resumes permitted per rolling window before the leg stops injecting and alerts | — |
| `HELM_RESUME_TURN_WINDOW_S` | `3600` (1h) | resume leg — the rolling window the cap counts over, so the cap re-arms with time instead of latching a seat off forever | — |
| `HELM_HANDOFF_EVENT_FRESH_H` | `2` | the handoff contract (`helm/handoff.py`) — the EVENT window: how recently a handoff must have been authored to count as authored FOR the compaction now firing. Distinct from the standing 48h freshness constant, because a compaction's floor is the compaction EPISODE and not the session (a session start never moves, so the standing gate widens as a session ages) | — |
| `KIMI_API_KEY` | — | `helm seat add kimi` only — the Moonshot outbound key for the kimi proxy-key seat. A **raw provider variable**: no `HELM_` prefix, no legacy fallback, **not** auto-loaded from any `.env`. Export it (or pass `--key-from <env-file>` carrying a `KIMI_API_KEY=` line) for the one `add` run; the key is baked into the seat's 0600 `config.yaml` and never read from the environment again | — |

The generated per-seat `config.yaml` (minted by `helm/seat.py` `_config_yaml` /
`_config_yaml_key`, re-minted on every `seat add`) carries resilience knobs that
are **not optional** — hand-editing a live config without teaching the generator
means the next re-mint silently strips the fix. One deserves its own note:
`transient-error-cooldown-seconds: 5`. cli-proxy-api benches a credential on ANY
transient upstream error (408/500/502/503/504) for a cooldown whose fork default
is **60 seconds** — and a config value of `0` *means that default* rather than
disabling it (`conductor.go:89,149-152`; `-1` = never bench). A codex seat runs
a tiny 2-credential pool, so one transient blip benches a cred for a full
minute; two blips in a window bench both and every request 503s with "no
available client" — a storm born at auth-selection, not at the upstream. That is
distinct from quota: real quota exhaustion is a clean 429 with its own cooldown,
which this knob does not touch. 5s keeps a genuinely sick upstream from being
hammered while letting a blipped cred return 12× faster. Do not remove the line
or "simplify" it to `0`.

## the review findings pass

The qwen27 findings pass (`helm/findingspass.py`, `docs/VERBS.md` under
`helm dispatch`) reads every review row once on the owner's local model and
leaves a NOTE on the row — never a review, an approval or a gate.

| variable | default | read by | legacy fallback |
|---|---|---|---|
| `HELM_QWEN27_FINDINGS` | on (`0`/`off`/`no` disables) | the pass's switch — `helm dispatch send`/`add` of a review row and `retip` start one detached, queued read, and its answer lands on the row as a `findings-note`. Off starts nothing and records nothing. The test suite declares `off` at `tests/__init__.py`, because every arm that files a review row would otherwise start a real read | — (**strictly no legacy fallback**) |
| `HELM_LOCAL_REVIEW_SCRIPT` | `local-review-script` in the [local names](#local-names); unset there too, the row gets one `NOT RUN` line naming both | the pass — the reader script, run IN PLACE from its owner's checkout and never copied. A missing file is one `NOT RUN` line on the row | — (**strictly no legacy fallback**) |
| `HELM_QWEN27_FINDINGS_TIMEOUT_S` | `3600` | the pass — the wall bound on one run of the script. Past it the script's process group is stopped and the row gets one `NOT FINISHED` line. A value that is not a positive integer is ignored | — (**strictly no legacy fallback**) |

## the upstream-change watcher

`helm upstream-watch` reads each new Claude Code release and files the helm
tweaks that survive a refute (see [VERBS.md](VERBS.md)). Its two operator knobs
are the off switch and the dry-run switch; the rest point it at its inputs.

| variable | default | read by | legacy fallback |
|---|---|---|---|
| `HELM_UPSTREAM_WATCH` | `1` (on; `0`/`off`/`no`/`false` turns it off) | `helm upstream-watch` — the OFF SWITCH. A pass, `--dry-run` and `--bundle` each print `disabled` and read, run and write nothing. `--install-timer` still works, so the timer can stay installed while the watcher is off | — |
| `HELM_UPSTREAM_WATCH_DRY_RUN` | unset (`1`/`on`/`yes`/`true` forces it) | `helm upstream-watch` — makes EVERY pass a dry run, exactly as `--dry-run` does: every stage runs, the model runs included, and the digest and the tasks it would file are printed; nothing is filed, posted or written, and the state does not move. Set it in the timer's environment to trial the watcher unattended | — |
| `HELM_UPSTREAM_WATCH_VERSIONS_DIR` | `~/.local/share/claude/versions` | `helm upstream-watch` — where Claude Code keeps one executable per release, each named by its version | — |
| `HELM_UPSTREAM_WATCH_CHANGELOG_URL` | the raw `CHANGELOG.md` of `anthropics/claude-code` (`upstream_watch.CHANGELOG_URL`) | `helm upstream-watch` — the CHANGELOG fetched each pass; `http`, `https` and `file` URLs | — |
| `HELM_UPSTREAM_WATCH_CLAUDE` | `claude` on `PATH` | `helm upstream-watch` — the `claude` program the propose and refute runs execute. `--install-timer` writes the resolved absolute path into the unit, because a user unit's `PATH` is not a shell's | — |
| `HELM_UPSTREAM_WATCH_CLAUDE_HOME` | the caller's own `CLAUDE_CONFIG_DIR`, else Claude Code's default home | `helm upstream-watch` — the Claude Code home, and so the OAuth login, the model runs use. Every other `CLAUDE*` variable and every `ANTHROPIC_*` variable is removed from their environment, so no API key, auth token or base URL reaches them. `--install-timer` writes it into the unit when it is set | — |
| `HELM_UPSTREAM_WATCH_MODEL` | `opus` | `helm upstream-watch` — the `--model` of every propose and refute run | — |
| `HELM_UPSTREAM_WATCH_EFFORT` | `high` (`none` omits the flag) | `helm upstream-watch` — the `--effort` of every run | — |
| `HELM_UPSTREAM_WATCH_TIMEOUT_S` | `1500` | `helm upstream-watch` — the limit on one model run, in seconds; a run past it fails the pass, so the release is read again next time | — |

## operational defaults

`HELM_COMPOSE_LAND_V1` defaults to **disabled**. Only the exact value `1`
enables the prospective bounded-CONCUR compose/landed-close writers. Deploy
compatible landed-proof-v3 and canonical bounded-manifest/fold readers and
confirm compatibility externally **before** enabling; this operator assertion is
not rollout evidence. The bounded canonical-manifest writer also honors this
switch. Existing project composition-proof activation must be active and
readable; activation alone does not establish reader compatibility. Replay and
the canonical fold consumer never read the switch. There is no legacy spelling or global CONCUR authorization change.
See [the prospective contract](VERBS.md#prospective-bounded-concur-exception).

| variable | default | read by | legacy fallback |
|---|---|---|---|
| `HELM_METAHARNESS` | unset ⇒ **AUTODETECT** — inside a herdr session (`HERDR_ENV` set) herdr first, else orca first then herdr, whichever binary is on `PATH`; `none`/`off` = pane ops OFF even with both installed (the explicit owner choice ORCA_OPERATIONS.md leans on); `orca`/`herdr` = that adapter or nothing (a missing binary is never substituted) | the harness adapter (`harness.py detect()`) — headless (native pid+git), `orca` (the Orca editor), `herdr` (herdr daemon) | — |
| `HELM_SUITE_GUARD` | unset ⇒ resolved by name off `PATH` (`fab-suite-pretooluse`) | absolute path to the EXTERNAL local-suite guard — the one hook executable helm does not ship (`hooks.SPECS` row `suite-guard`, `external`, `optional`). Pin it when the guard is not on `PATH`; a pinned path that is not an executable file resolves to nothing rather than silently installing a dead hook. Neither pinned nor on `PATH` ⇒ the guard is NOT CONFIGURED and optional: every other hook installs and the install reports it as "not configured (optional)". Configured but unresolvable on this host ⇒ the spec is left out of every write (`hooks.resolved_specs`), every install door returns a SHORTENED result loudly and nonzero rather than claiming a full contract, and `helm doctor` reports it as one loud row, because no `hooks install` could close a MISSING BINARY. **NO `MELD_` FALLBACK, uniquely among helm's variables**: this one is read with a bare `os.environ.get`, never `home.env`, so `MELD_SUITE_GUARD` is NOT accepted — a legacy value there used to resolve, and a DEAD one used to mask a valid `PATH` guard. An external guard is a security boundary; its pin has exactly one spelling. The pin must be ABSOLUTE: a relative path is refused, not resolved, because it would name a different file in every process's cwd | — |
| `HELM_GATE_LANE_SUITE` | unset | the lane-suite escape of `helm gate run`'s one-whole-suite door, read where a flag cannot reach: `fab gate` asks helm its `--plan` question on the hub in the caller's own environment and does not forward its flags. Its value is the WHY (whitespace folded, 96 characters kept). Set, it admits a whole suite in a lane room and writes a `gate-lane-suite` event; pass `--label 'lane-suite: <why>'` too so the node's receipt carries it. A direct `helm gate run --lane-suite --why TEXT` puts the why on the label itself (task/3039) | — (**strictly no legacy fallback**) |
| `HELM_GATE_AGAIN` | unset | `1` is `helm gate run --again` for the same reason: it reruns a tree whose last whole-suite receipt is RED (a suspected flake). A GREEN tree refuses a rerun whatever this says | — (**strictly no legacy fallback**) |
| `FAB_ID` | unset (fab-run exports it into every Fab job) | Fab's own run id, read by helm, never set by it. `helm gate run` with no mode flag runs its whole suite SERIAL while it is set: inside a Fab job the flags Fab forwarded ARE the scope Fab publishes the receipt under (its spoke reads `--sliced` to pick the slice scope), so no flag there means the serial scope whatever room the node's snapshot looks like (`gate.suite_mode`, task/3039). `--sliced` still runs slices. `helm gate equiv` also reads it as the run id its observation records | — (Fab's name, not helm's) |
| `HELM_HOOK_ALARM_DIR` | unset ⇒ `$XDG_RUNTIME_DIR`, else `$TMPDIR`, else `/tmp`, plus a per-user name token | the per-tool-call timeout alarm's rate limit (`hooks.hookalarm`), read by BOTH halves — the Python alarm in `hookrun`/`posttoolrun` and the POSIX-sh `124)` arm `hooks.spec_command` generates, which runs after helm has been killed and can call no helm code. The first timeout of a hook class in a ten-minute window prints in full; repeats print nothing and are counted, and the next line that speaks carries the arrears. Name a directory to get a PRIVATE window (every test arm about this door does). A gate-suite child (`HELM_GATE_SUITE_CAP` set) that names no directory always speaks and writes nothing: a suite is not a fleet, and durable cross-process suppression there would silence later arms and read as a missing diagnostic | — |
| `HELM_HOOK_INTERP_RECORD` / `HELM_HOOK_INTERP_KEY` | unset | set ONLY by `bin/helm-hook`, for a helm hook child it had to start through `#!/usr/bin/env python3` because no interpreter was recorded for the `python3` this `PATH` finds: the record file (`_global/.state/hook-interp` in the helm home) and that `python3`. `cli._record_hook_interpreter` writes `<python3> TAB <sys.executable>` there and removes both from its environment, and later hooks exec the recorded interpreter with `-S`. Not a knob: setting them by hand only asks helm to record | — |
| `HELM_STOPPROBE_SLOW` | `1.0` | seconds a Stop ladder must run before `helm/stopprobe.py` records its rung boundaries (was 3.0 before task/3040). Each record also carries the box's 1-minute `load` and, when the stop-facts projection exists, its `snap_age`. A value that is not a finite non-negative number is ignored | — |
| `HELM_STOPPROBE_LOG` | unset ⇒ `pause-ops/stopprobe.log` in the helm home | where the Stop ladder's slow-run records go; the override exists for a live proof that must write a REAL record somewhere other than production | — |
| `HELM_STOP_GUARD` | on | `helm chat stop-guard` (the `Stop` hook) — set `0` to disable the whole idle gate. Per-check switches below take the same `0`; `seats.py stop_guard` is the sole authority on what latches (each row below states its own). `stop_hook_active` suppresses every block while the harness is already continuing, but still surfaces the inbox and claim-evidence WARNs because warnings cannot re-enter the stop path | — |
| `HELM_STOP_GUARD_INBOX` | on | the undelivered-INBOX block — @mentions/owner rows past the seat's delivery cursor; latched once per pending-fingerprint, so a re-stop on the SAME rows passes | — |
| `HELM_STOP_GUARD_CLAIMS` | on | the claim-LEASE block — live leases held by the stopping session (session-bound: no session in the hook JSON ⇒ no check). Latched on the held-set fingerprint (resource + lease id + TTL band): a re-stop on the same set compresses to a one-line warn; a changed set — a new lease, a release, a re-claim, or a remainder crossing the 120s alarm — re-prints the full block; an all-exempt stop clears the memory, an unwritable latch degrades the block to the warn, and a delegation/gate proof turns it into a warn | — |
| `HELM_STOP_GUARD_LEASE_TTL` | on | the renewed-lease discrimination inside the claims rung — a `gatelock:` lease its FIFO renewer provably keeps alive is reported as renewed rather than told to release; `0` restores the bare release command | — |
| `HELM_STOP_GUARD_DELEGATION` | on | the delegation exemption's producers AND readers — documented subagent PostToolUse/SubagentStop evidence that lets a held lease pass as a delegated build (the WARN names its proof); `0` disables both, so the lease blocks like any other | — |
| `HELM_STOP_GUARD_BEACON` | on | the NO-ARMED-BEACON block — a launched fleet seat with no live `helm chat wait` process blocks only while it sends/receives canonically owed dispatch work; unreadable obligations fail closed, a measured zero makes the beacon optional and re-arms when work appears, and process absence must be PROVEN | — |
| `HELM_STOP_GUARD_SPIRAL` | on | the REVIEW-SPIRAL block — one lane review-dispatched at 3+ distinct tips in 12h; the block quotes the `helm chat meld invite` cure; latched per (lane, round-count), two rounds warn instead | — |
| `HELM_STOP_GUARD_SEAM` | on | the UNTESTED-COMPOSITION block — the worktree this seat stands in (plus its leased lane rooms) and another LIVE worktree of the same repo under a DIFFERENT holder both authored the same code file, both halves are verified, and nothing has run against the two together. Signal is GIT + /proc (worktree registry, cwd occupancy, branch diffs) with helm leases ADDED where they exist, so it works in a repo with no helm bookkeeping at all. Verified means ONE bar: each half's CURRENT TREE carries an admissible whole-suite gate receipt, so a repo that mints no receipts has no green halves and the rung is silent there. A conflicting merge is still a seam. Discharged only by an admissible same-repo receipt at the composed tree (`git merge --no-ff <peer>` then gate — the receipt binds the TREE, so throwing the merge away keeps the discharge), or at the container's own tree when one branch already carries the other; a commit trailer discharges nothing. Latched per COMPOSED seam set — a new peer re-arms it AND so does a MOVED half, because a moved half is a different untested composition; an unwritable latch degrades to the warn. A room whose work is already on the trunk is not a half (`_merged` — LANDED by ancestry OR by patch identity, the same authority `helm work release` prints, so an unreaped branch whose work landed under a rebased sha stops counting). A receipt only counts if it is ADMISSIBLE: whole-suite (focused and custom-argv rows are inadmissible — a focused claim is its scope, not its tree), clean before and after, and unmoved across the run; a half is green only when its CURRENT TREE was tested. Holder resolution is lease, then roster cwd, then environ — environ last because it is blind to the claude-native family — and an UNKNOWN holder never buys the same-seat exemption. Also carries the WARN-only `SEAM RUNG BLIND SPOT` disclosure: seats sharing ONE worktree are invisible to this rung, so a seat stopping in a room several seats answer to is told once per arrangement. The stop computes none of this: it reads the `helm web` resident's seam facts (docs/HOOKS.md, "Stop facts"), and a reading that is not EXACT is one UNKNOWN warn, never a block | — |
| `HELM_STOP_TIMING_AFTER` | 0.7 × the stop ladder's own reserve (`seats_stop_budget.BUDGET_S`) | seconds of quiet before the Stop guard's rung-timing trace speaks. The trace is BUFFERED: a ladder that finishes inside its budget prints nothing (its boundaries are already in `stopprobe.log` once it has gone slow), and once elapsed crosses this threshold the whole buffer is flushed in order and every later boundary streams — so a ladder about to be killed by the outer `timeout` has still left its evidence. A one-shot timer fires the flush at the threshold, so a rung that stalls SILENTLY for the whole budget still leaves a trace. `0` streams from the first line (the debugging posture); `off` (case-insensitive) disables the STREAM entirely at any elapsed time, leaving the buffer and the end-of-ladder report untouched — the test suite declares `off` at `tests/__init__.py`, because a flush keyed on wall time lands inside whichever arm happens to hold a `redirect_stderr` and reddens arms that pin stderr byte-for-byte; a malformed value is ignored and the default stands | — |
| `HELM_STOP_GUARD_NDP` | on | the NON-DISTRACTION-PROTOCOL block — 3+ live claim leases connected to the stopping seat with ZERO subagent delegation this whole session; names both hand-out verbs (Agent tool / `helm dispatch send`); latched once per load-state bucket (a re-stop passes, a doubled queue re-arms, one delegation silences it), an unwritable latch degrades to the compact warn | — |
| `HELM_STOP_GUARD_PUNT` | on | the DRESSED-DECLINATION block — the turn's last message declines action wearing an excuse from a named class while nothing is open on the owner-ask ledger (`punt.py gate_lines`); latched per finding fingerprint | — |
| `HELM_STOP_GUARD_WIRING` | on | the BUILT-BUT-NOT-WIRED block — this checkout added a module nothing reaches; fires on stop only, latched once per distinct debt (fingerprinted on the module list) | — |

| `HELM_STOP_GUARD_CLAIME` | on | the claim-evidence WARN — count/SHA/proof/landed claims need the matching successful measurement in this turn; one transcript read binds findings to latch identity, typed non-message metadata is ignored, malformed message envelopes surface a latched `CLAIM-EVIDENCE SKIPPED` warning. It also reads what the turn PUBLISHED (`helm chat post`/`chat dm`/`dispatch send`/`dispatch verdict` bodies, which ride inside a tool input and appear in no assistant message), and warns there on ONE shape only: a claim indexed to the present instant (`currently`, `right now`) whose measurement instant is undisclosed, older than 20 minutes, or ahead of the clock. That shape has no tool anchor by design — it asks whether the artifact says WHEN, never whether a measurement happened | — |
| `HELM_STOP_GUARD_WHISPER` | on | the stop-whisper — one budgeted continuation line per stop, once per (signal, level) fingerprint, fail-closed to nothing | — |
| `HELM_STOP_GUARD_INDEX` | on | the silent mechanical leg — `helm index cap --apply` and the throttled scratch reaper, best-effort, never blocks, never prints. It no longer runs on each stop: the `helm web` resident runs it every minute (`stopfacts_resident.mechanical`), under this same switch | — |
| `HELM_STOP_FACTS_LEG` | on for the `helm web` bound to the console port (7433), off for any other | whether this `helm web` runs the STOP-FACTS leg: the one writer of `_global/web-cache/stop-facts.json`, the facts every seat's Stop reads instead of folding the dispatch ledger and asking git per lease (helm/stopfacts_resident.py). `1` runs it in a server on another port, `0` keeps it out of the console server. Only one process writes (a LOCK_NB lock held for the process's life), so a second server that runs it only waits | — |
| `HELM_HOOK_T0` | set by `bin/helm-hook` | the hook wrapper's start, read from `/proc/uptime` with no fork, exported to the hook child. `procage.hook_elapsed` charges the time since it to the handler's budget (the hook dispatcher's alarm and the Stop ladder's deadline), so interpreter startup is spent from the same clock the outer `timeout` runs on. Not an operator setting: a value older than an hour, in the future or unparseable is ignored | — |
| `HELM_PROC` | `/proc` | every process census — `helm/seats.py` (which sessions a live process references), `helm/hooks.py` (pane coverage), `helm/seat.py` (which process holds a seat's proxy port). Points the walk at a synthetic tree, which is how the hermetic tests get a fleet with no processes | `MELD_PROC` |
| `HELM_SCRATCH_DIR` | `~/.helm/_global/scratch` | `helm sessions` — persisted per-session logs, env capture, heartbeats, and delivery receipts | — |
| `HELM_SCRATCH_GC` | on (`0`/`off`/`no` disables) | the automatic dead-session scratch reaper on the Stop hook's silent-mechanical lane — it APPLIES (`scratch.py` stop-pass runs `gc(apply=True)`), guarded three ways: pressure-gated (every mount under WARN **and**, while helm writes to any RAM-backed mount, the HOST's own memory under its thresholds ⇒ it reaps nothing), throttled to one pass an hour, bounded + liveness-before-age. Only the CLI `helm scratch gc` is dry-run by default (`--apply` reaps) | — |
| `HELM_SCRATCH_GC_TIER2` | on (`0`/`off`/`no` disables) | the SECOND TIER alone, inside `HELM_SCRATCH_GC` — the leg that reaps a direct child of a *live* session's scratch when nothing holds it open, no live cwd sits in it, it is not a harness-owned name (`tasks/`, any dot-name) and its newest write is older than a short pressure-escalating ttl (24h at rest, 4h at warn, 1h at critical). Set this to `0` to keep tier one (whole dead-session trees) and switch off the inside-a-live-session leg | — |
| `HELM_SCRATCH_GC_SEATS` | on (`0`/`off`/`no` disables) | the THIRD pressure plane of the scratch reaper, inside `HELM_SCRATCH_GC`: each pass (the automatic leg at most once a minute fleet-wide, and every `helm scratch gc`) reads every seat's own `agents-*.slice` NOW — THROTTLED when a process of the slice is stalled in the kernel's over-high throttle or the slice's `memory.events` high count rises across a sampled quarter second, NEAR at 90% of `memory.high` with at least a quarter of that ceiling in unreclaimable `shmem` (a slice that high in page cache reads HIGH, a quiet badge that neither reaps nor wakes; an unreadable `memory.stat` reads HIGH-UNREAD, the same safe action under its own name, with shmem published as null) — and a pressing seat's unheld second-tier units of at least 32M on a RAM mount reap at ttl 0, biggest first, until the picks project the slice under 80%. It overrides the hourly throttle, because the throttled seat never reaches its own Stop hook, and it wakes the seat's lead and the integrator once per spell. Set this to `0` to switch off this plane in the reaper — its read, its escalation and its wake together; the `helm chat seats` mark and the web roster badge are a separate read and stay | — |
| `HELM_SEAT_PRESSURE` | on (`0`/`off`/`no` disables) | the present-tense seat memory reading of THIS box (`seatceiling.fleet_pressure`): one /proc walk and three small reads per `agents-*.slice`, plus a quarter-second sample when a slice is past 80% of its `memory.high`. Off answers every reading of the host's own `/proc` or `/sys/fs/cgroup` with no slices and no trouble, and reads nothing: the `helm chat seats` memory mark and the web roster memory badge are silent, and the scratch reaper's third plane escalates nothing. A reading of any other tree (a fixture) is taken as always. The test suite declares `off` at `tests/__init__.py` and refuses a host reading in a test process that lost it, because every roster, web and scratch arm that built a report otherwise read the live box | — (**strictly no legacy fallback**) |
| `HELM_SCRATCH_TMPDIR` | on (`0`/`off`/`no` disables; any other value is a PATH) | the launched-seat TMPDIR routing — `launch_tmpdir()` (`scratch.py`) points every spawned seat's `TMPDIR` at a mount helm chose, created `0o700`. It resolves to **DISK unconditionally** (`<disk root>/tmpdir-overflow/tmpdir`), never the ambient tmp and never the chat-bus RAM mount, because the harness derives its own per-uid scratch root from `TMPDIR` and that estate is unbounded and unattributable — no reaper can take it back off a RAM mount. Takes effect at a seat's NEXT launch; the reaper keeps scanning every root the routing has picked, and `HELM_SCRATCH_DIR` ADDS its root to the front of that set rather than replacing it — a seat keeps the env it launched with, so a root a live seat is still writing into must stay in the scan set. If no non-volatile root can be found the route falls back to RAM and SAYS SO, in the routing table (`helm scratch`) and in a `helm doctor` row | — |
| `HELM_GATE_CANARY_LAUNCH` | `fab gate` | the argv prefix (split like a shell line) `helm gate canary run` uses to gate trunk's tip for each kind its tree lacks, as `<prefix> --repo ROOM --serial` or `--sliced`; it must bring each receipt home to this ledger, which `fab gate` and `helm gate run --box NAME` both do | — |
| `HELM_GATE_CANARY` | unset | `1` declares a whole-suite run the nightly canary's: the one-suite-per-tree door then admits a run of the OTHER kind (serial/sliced) than the tree's last whole-suite receipt, labelled `canary`; a same-kind rerun is refused as always. Set by `helm gate canary run` on its launcher, and read by the hub's plan question the way `HELM_GATE_AGAIN` is | — |
| `HELM_GATESLICE_LEAKS` | `report` (`helm gate run --sliced` sets `fail`) | the slice runner's audit of what each module leaves behind — process state (environment, cwd, threads, handlers, rebound code, ...) AND module data (every `helm.*`/`tests.*` global and class attribute re-bound, added, removed or changed in content); `fail` makes each leaking module one more ERROR, `off` disables both. A cache that is process-wide by design is declared in its OWN module as `_GATESLICE_MUTABLE = {"NAME": "one-line reason"}` | — |
| `HELM_SPAWN_SEND_DELAY` | `5` (seconds) | pane-boot grace before onboarding keystrokes are sent to a spawned seat | — |
| `HELM_CACHE_DIR` | `~/.helm/_global/cache` | shared caches — cred fingerprints, live-session indices, proxy pidfiles, transport receipts | — |
| `HELM_REMEMBER_DIR` | `~/.remember` | the handoff contract's LEGACY read-fallback (`handoff.py legacy_now_path`) — `<dir>/now.md` is read until retired; no `helm remember` verb exists; the override is a test seam | — |
| `HELM_VERDICT_ROOM` | unset ⇒ the room the verdict belongs to (the dispatch flow's current project room — attestations follow the work, not `#main`) | dispatched gate verdicts — `helm dispatch` announces attestations there; setting it is the deliberate-centralization override (`dispatches.py`) | — |
| `HELM_SEAT_ALIASES` | — | comma-separated `kind:alias=canonical` identity declarations (kind `pane` or `owner`); alias evidence can only SUGGEST and REFUSE, never retarget — a canonical target must be an exact roster/spawn/live seat, a `pane:` alias must name its Git-registered deterministic seat worktree, and any malformed entry fails loudly | — |

## host facts (files, not variables)

Some values name one operator's host: an endpoint on the LAN, a credential
home reserved for one seat. The source carries none of them. Each is a JSON
object in `<helm home>/_global/`, read on every call (an edit needs no
restart). An absent file configures nothing; a file that does not read as a
JSON object is an error, never read as absent.

| file | shape | read by |
|---|---|---|
| `endpoints.json` | `{"<key>": "http(s)://<host>:<port>/v1"}` ([example](endpoints-config.example.json)) | `helm/seat_catalog.py` `pool_base_url` — a pool row with `base_url_from` takes its URL from here. Unset, that family is unavailable: `helm seat add` refuses and names the file and the key, and its route binds no proof. The `qwen27` family reads the key `qwen27` |
| `probe-reserved-homes.json` | `{"<home label>": "<why>"}` | `helm doctor --probe-memory` — a listed credential home is never borrowed for the live probe unless it is the caller's own. A file that does not read reserves every home but the caller's |
| `preread.json` | [example](preread-config.example.json) | `helm preread` — its readers' and judge's endpoints |
| `local-names.json` | `{"<key>": "<name>"}` ([example](local-names.example.json)) | `helm/localnames.py` — the names of the operator's own projects, hosts and predecessor tool; see [local names](#local-names). Two keys are read at import, not per call, and a file that does not read configures nothing while `helm doctor` names it |
| `endpoints.json`, key `relevance` | `"relevance": "http://<host>:<port>"` | `helm/relevance.py` — the local relevance scorer service (`helm relevance serve`). Unset, the local tier answers "unconfigured" and every turn it would score is counted as failed |
| `relevance.json` | [example](relevance-config.example.json) | `helm/relevance.py` — the long-tail re-rank's mode (`off` / `shadow` / `live`; absent is `off`), the hook's wait bound, the candidate count, the outside evaluator's credential-file path and threshold, and `local_by_choice`. A file that does not read, or names an unknown mode, is `off` |

## non-HELM integration variables

`CLUSTERVISION_CLAUDE_ROOTS` — CV's platform-separated additional Claude
`projects/` roots. Helm preserves any caller-provided roots and appends every
minted family/instance seat root to every CV subprocess, so proxy-seat
transcripts participate in the one recall index without a second scanner.

## local names

A few behaviours depend on a name only the operator's machine knows: the tool
helm replaced there, the provider name a live proxy config was minted under,
the directory a deployed gate runs from. The source carries none of them. Each
is one key of a JSON object at `<helm home>/_global/local-names.json`
([example](local-names.example.json)), read through `helm/localnames.py`;
every key is optional, and without it the behaviour is off or takes its
neutral default. `helm doctor` names an unreadable file, an unknown key and a
value of the wrong shape.

| key | read by | without it |
|---|---|---|
| `predecessor` | the legacy fallbacks above, the predecessor's cache seed, its home archive, and keepalive's writer census | no legacy spelling or directory is read |
| `qwen27-provider` | `helm/seat_catalog.py` — the qwen27 pool row's key, so the minted proxy config and every route bind it (read at import) | `local-llamacpp` |
| `deploy-dir`, `deploy-project` | `helm doctor`'s deployed-artifact canon | no deployed gate, no row |
| `local-review-script` | the review findings pass, after `HELM_LOCAL_REVIEW_SCRIPT` | the pass notes that no script is named |
| `fab-node` | an operator's A/B eval rig, as its `--node` default (the rig does not ship with helm) | `--node` is required |
| `generic-keywords` | the knowledge store's generic-word set (read at import) | English only |
