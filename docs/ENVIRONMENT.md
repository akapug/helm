# helm environment variables

helm takes no config file — every knob is an environment variable, and every
one has a working default. A fresh clone needs **none of these** set.

**The env transition law:** a handful of variables accept a legacy `MELD_*`
spelling as a fallback, honored forever. Such variables resolve `HELM_<name>`
first; if unset, the listed legacy spelling is read; only then does the
default apply. A row marked `—` or **strictly no legacy fallback** reads only
its current spelling. Legacy names are **read, never written** — new code
always spells `HELM_*`. `helm doctor` reports which overrides are in effect.

## the home

| variable | default | read by | legacy fallback |
|---|---|---|---|
| `HELM_HOME` | `~/.helm` | everything (the root resolver; anchored on `$HOME`, never cwd) | `MELD_HOME` |

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
| `HELM_SCAN_ROOTS` | `~/dev` (the two-tier walk covers `~/dev/<org>/<repo>` too; set your own colon-separated roots) | the repo-scan tier of `helm sync` — colon-separated roots under which git repos register as shelf projects | — |
| `HELM_CLAUDE_ROOTS` | — (extra roots; `~/.claude/projects` is always scanned) | the session catalog — colon-separated additional Claude transcript roots | — |
| `HELM_CODEX_ROOTS` | — (extra roots; `~/.codex/sessions` and `~/.codex-homes` are always scanned) | the session catalog — additional Codex transcript roots | — |
| `HELM_CATALOG` | — (`scanner` forces the built-in scanner, skipping the `cv` recall index as catalog source) | the session catalog | — |

## configs

| variable | default | read by | legacy fallback |
|---|---|---|---|
| `HELM_CONFIG_ROOTS` | `~/dev` | `helm configs` — colon-separated roots for the project-scope config tree walk | — |
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
| `HELM_CHAT_NODE_BIN` | `dregg-cave-node` on `PATH`, else `~/.local/bin/dregg-cave-node` | `helm chat node up` — the room-node daemon binary | `MELD_CHAT_NODE_BIN` |
| `HELM_CHAT_LOG` | on (`0`/`off`/`no` disables) | `helm chat log-flush` + the `helm --human` exit flush — the log-after leg's operator switch | `MELD_CHAT_LOG` |
| `HELM_CHAT_OWNER_NAMES` | the derived owner handle + the unix login (csv override) | the delivery lane (`helm chat deliver`) — senders whose posts reach every seat at tool boundaries WITHOUT an @mention (the owner steers mid-flight) | — |
| `HELM_CHAT_DELIVER` | on (`0`/`off`/`no` disables) | the delivery lane's kill switch — the PostToolUse nudge goes silent fleet-wide; the room, roster and inject lanes are untouched | — |

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
| `HELM_PROVIDER` | `native` | provider selection — `native` reads the vendors' own usage endpoints directly (stdlib-only, the default); `cli` opts into an external quota CLI | — |
| `HELM_QUOTA_CLI` | — (unset: no CLI quota provider; `HELM_PROVIDER=cli` without it falls back to native, loudly) | the CLI provider — which binary to shell (only used with `HELM_PROVIDER=cli`) | — |
| `HELM_ALLOCATION_RULES` | `~/.config/helm/allocation.json` | allocation ranking — operator prefer/avoid rules per model substring | — |
| `HELM_PROBE_LOOP` | — (`1` starts the background re-probe loop lazily on the first history read) | the native provider's burn-history collector | — |
| `HELM_ALLOC_MODELS` | `fable,opus,gpt-5.5` | the web quota view — which model chips the allocate panel offers | — |
| `HELM_CRED_BACKUP_ROOT` | `~/.cred-backups` | `helm cred backup` / `switch-guard` / `heal` — the credential snapshot root (`0700` dirs, `0600` files). Point it at an encrypted volume if you want the snapshots there; the override is also how tests keep the real root untouched | — |

## the web app

| variable | default | read by | legacy fallback |
|---|---|---|---|
| `HELM_API_TOKEN` | a fresh random token per process | `helm web` — pins the anti-CSRF mutation bearer (useful for scripted POSTs or a long-lived service; see [WEB.md](WEB.md)) | — |

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
| `HELM_PROXY_FORK_DIR` | `~/.helm/_global/proxy-fork/CLIProxyAPI` | `helm proxy-fork-watch` — the durable CLIProxyAPI source clone containing `cmd/helm-upstream-check`; it must be on local branch `helm/upstream-tracking` | — (**strictly no legacy fallback**) |
| `HELM_AUTOCOMPACT_THRESHOLD` | `80` | proxy-seat autocompact — context percentage at which `/compact` is injected | — |
| `HELM_AUTOCOMPACT_ASSUME_WINDOW` | `200000` (`0`/`off` disables fallback) | proxy-seat autocompact — Claude Code's assumed context window when a family does not declare `max_context` | — |
| `HELM_AUTOCOMPACT_FRESH_S` | `21600` (6h) | proxy-seat autocompact — maximum transcript age accepted as live context | — |
| `HELM_AUTOCOMPACT_LATCH_TTL` | `900` (15m) | proxy-seat autocompact — repeat delay for a manual-action alert only. Injected or already-pending `/compact` stays latched until the recorded context drops or the session changes; time alone never authorizes a duplicate injection | — |
| `HELM_AUTOCOMPACT_CLAUDE` | `1` (`0`/`off`/`no` restores the old skip) | proxy-seat autocompact — whether a claude-model seat is measured and compacted like every other seat. ON since the resume leg landed; the skip existed because a deliberate `/compact` ended the turn and nothing restarted it, which `helm seat resume-turn` now does. Set `0` on a fleet whose `SessionStart` resume-turn hook is not installed | — |
| `HELM_RESUME_TURN` | `1` (`0`/`off`/`no` disarms) | the post-compaction resume leg (`helm/resumeturn.py`) — the kill switch | — |
| `HELM_RESUME_TURN_SETTLE_S` | `3.0` | resume leg — composer-settle grace the detached child waits before injecting. The MEASURED floor is 0 (36/36 injections submitted at hook+0.02s through hook+3.0s against a real Claude Code 2.1.220 TUI on a pty); the default is margin for the metaharness seam and a loaded host, and a detached child pays nothing for it | — |
| `HELM_RESUME_TURN_DEBOUNCE_S` | `120` (2m) | resume leg — a second `SessionStart(compact)` inside this window is the SAME compaction re-firing (a compaction itself takes 100–190s), so it is a silent no-op, never a second injection | — |
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

## operational defaults

| variable | default | read by | legacy fallback |
|---|---|---|---|
| `HELM_METAHARNESS` | unset ⇒ **AUTODETECT** — inside a herdr session (`HERDR_ENV` set) herdr first, else orca first then herdr, whichever binary is on `PATH`; `none`/`off` = pane ops OFF even with both installed (the explicit owner choice ORCA_OPERATIONS.md leans on); `orca`/`herdr` = that adapter or nothing (a missing binary is never substituted) | the harness adapter (`harness.py detect()`) — headless (native pid+git), `orca` (the Orca editor), `herdr` (herdr daemon) | — |
| `HELM_STOP_GUARD` | on | `helm chat stop-guard` (the `Stop` hook) — set `0` to disable the whole idle gate. Per-check switches below take the same `0`; `seats.py stop_guard` is the sole authority on what latches (each row below states its own). `stop_hook_active` suppresses every block while the harness is already continuing, but still surfaces the inbox and claim-evidence WARNs because warnings cannot re-enter the stop path | — |
| `HELM_STOP_GUARD_INBOX` | on | the undelivered-INBOX block — @mentions/owner rows past the seat's delivery cursor; latched once per pending-fingerprint, so a re-stop on the SAME rows passes | — |
| `HELM_STOP_GUARD_CLAIMS` | on | the claim-LEASE block — live leases held by the stopping session (session-bound: no session in the hook JSON ⇒ no check). Latched on the held-set fingerprint (resource + lease id + TTL band): a re-stop on the same set compresses to a one-line warn; a changed set — a new lease, a release, a re-claim, or a remainder crossing the 120s alarm — re-prints the full block; an all-exempt stop clears the memory, an unwritable latch degrades the block to the warn, and a delegation/gate proof turns it into a warn | — |
| `HELM_STOP_GUARD_LEASE_TTL` | on | the renewed-lease discrimination inside the claims rung — a `gatelock:` lease its FIFO renewer provably keeps alive is reported as renewed rather than told to release; `0` restores the bare release command | — |
| `HELM_STOP_GUARD_DELEGATION` | on | the delegation exemption's producers AND readers — documented subagent PostToolUse/SubagentStop evidence that lets a held lease pass as a delegated build (the WARN names its proof); `0` disables both, so the lease blocks like any other | — |
| `HELM_STOP_GUARD_BEACON` | on | the NO-ARMED-BEACON block — a launched fleet seat with no live `helm chat wait` process is a seat nothing can wake; once per beacon-loss episode, and absence must be PROVEN | — |
| `HELM_STOP_GUARD_SPIRAL` | on | the REVIEW-SPIRAL block — one lane review-dispatched at 3+ distinct tips in 12h; the block quotes the `helm chat meld invite` cure; latched per (lane, round-count), two rounds warn instead | — |
| `HELM_STOP_GUARD_NDP` | on | the NON-DISTRACTION-PROTOCOL block — 3+ live claim leases connected to the stopping seat with ZERO subagent delegation this whole session; names both hand-out verbs (Agent tool / `helm dispatch send`); latched once per load-state bucket (a re-stop passes, a doubled queue re-arms, one delegation silences it), an unwritable latch degrades to the compact warn | — |
| `HELM_STOP_GUARD_PUNT` | on | the DRESSED-DECLINATION block — the turn's last message declines action wearing an excuse from a named class while nothing is open on the owner-ask ledger (`punt.py gate_lines`); latched per finding fingerprint | — |
| `HELM_STOP_GUARD_WIRING` | on | the BUILT-BUT-NOT-WIRED block — this checkout added a module nothing reaches; fires on stop only, latched once per distinct debt (fingerprinted on the module list) | — |

| `HELM_STOP_GUARD_CLAIME` | on | the claim-evidence WARN — count/SHA/proof/landed claims need the matching successful measurement in this turn; one transcript read binds findings to latch identity, typed non-message metadata is ignored, malformed message envelopes surface a latched `CLAIM-EVIDENCE SKIPPED` warning | — |
| `HELM_STOP_GUARD_WHISPER` | on | the stop-whisper — one budgeted continuation line per stop, once per (signal, level) fingerprint, fail-closed to nothing | — |
| `HELM_STOP_GUARD_INDEX` | on | the silent mechanical leg — `helm index cap --apply` best-effort in-process on each stop; never blocks, never prints | — |
| `HELM_PROC` | `/proc` | every process census — `helm/seats.py` (which sessions a live process references), `helm/hooks.py` (pane coverage), `helm/seat.py` (which process holds a seat's proxy port). Points the walk at a synthetic tree, which is how the hermetic tests get a fleet with no processes | `MELD_PROC` |
| `HELM_SCRATCH_DIR` | `~/.helm/_global/scratch` | `helm sessions` — persisted per-session logs, env capture, heartbeats, and delivery receipts | — |
| `HELM_SCRATCH_GC` | on (`0`/`off`/`no` disables) | the automatic dead-session scratch reaper on the Stop hook's silent-mechanical lane — it APPLIES (`scratch.py` stop-pass runs `gc(apply=True)`), guarded three ways: pressure-gated (every mount under WARN ⇒ it reaps nothing), throttled to one pass an hour, bounded + liveness-before-age. Only the CLI `helm scratch gc` is dry-run by default (`--apply` reaps) | — |
| `HELM_SCRATCH_TMPDIR` | on (`0`/`off`/`no` disables) | the launched-seat TMPDIR routing kill switch, NOT a path — `launch_tmpdir()` (`scratch.py`) points every spawned seat's `TMPDIR` at a mount helm chose: healthy ambient tmp keeps it, a pressured one overflows to DISK (`tmpdir-overflow`), never the chat-bus RAM mount; the path override is `HELM_SCRATCH_DIR` above | — |
| `HELM_SPAWN_SEND_DELAY` | `5` (seconds) | pane-boot grace before onboarding keystrokes are sent to a spawned seat | — |
| `HELM_CACHE_DIR` | `~/.helm/_global/cache` | shared caches — cred fingerprints, live-session indices, proxy pidfiles, transport receipts | — |
| `HELM_REMEMBER_DIR` | `~/.remember` | the handoff contract's LEGACY read-fallback (`handoff.py legacy_now_path`) — `<dir>/now.md` is read until retired; no `helm remember` verb exists; the override is a test seam | — |
| `HELM_VERDICT_ROOM` | unset ⇒ the room the verdict belongs to (the dispatch flow's current project room — attestations follow the work, not `#main`) | dispatched gate verdicts — `helm dispatch` announces attestations there; setting it is the deliberate-centralization override (`dispatches.py`) | — |
| `HELM_SEAT_ALIASES` | — | comma-separated `kind:alias=canonical` identity declarations (kind `pane` or `owner`); alias evidence can only SUGGEST and REFUSE, never retarget — a canonical target must be an exact roster/spawn/live seat, a `pane:` alias must name its Git-registered deterministic seat worktree, and any malformed entry fails loudly | — |

## non-HELM integration variables

`CLUSTERVISION_CLAUDE_ROOTS` — CV's platform-separated additional Claude
`projects/` roots. Helm preserves any caller-provided roots and appends every
minted family/instance seat root to every CV subprocess, so proxy-seat
transcripts participate in the one recall index without a second scanner.

`HELM_PROFILE_SCAFFOLD` (a `HELM_*` variable, listed here beside the external
integration it serves) — optional. When set to the full path of an external
scaffold `profile.json`, `helm whoami` merges that scaffold into the operator
profile when the file exists. Unset (or no such file), the profile starts
fresh — no dependency either way.
