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

## accounts and quota

| variable | default | read by | legacy fallback |
|---|---|---|---|
| `HELM_PROVIDER` | `native` | provider selection — `native` reads the vendors' own usage endpoints directly (stdlib-only, the default); `cli` opts into an external quota CLI | `SESH_PROVIDER` |
| `HELM_QUOTA_CLI` | `tokaware` | the CLI provider — which binary to shell (only used with `HELM_PROVIDER=cli`) | `SESH_QUOTA_CLI` |
| `HELM_ALLOCATION_RULES` | `~/.config/helm/allocation.json` (legacy `~/.config/sesh/` honored) | allocation ranking — operator prefer/avoid rules per model substring | `SESH_ALLOCATION_RULES` |
| `HELM_PROBE_LOOP` | — (`1` starts the background re-probe loop lazily on the first history read) | the native provider's burn-history collector | `SESH_PROBE_LOOP` |
| `HELM_ALLOC_MODELS` | `fable,opus,gpt-5.5` | the web quota view — which model chips the allocate panel offers | `SESH_ALLOC_MODELS` |

## the web app

| variable | default | read by | legacy fallback |
|---|---|---|---|
| `HELM_API_TOKEN` | a fresh random token per process | `helm web` — pins the anti-CSRF mutation bearer (useful for scripted POSTs or a long-lived service; see [WEB.md](WEB.md)) | `SESH_API_TOKEN` |

(`__HELM_TOKEN__` seen in `web_ui.html` is not an environment variable — it is
the template placeholder the server substitutes the live token into at serve
time.)

## the attestation substrate (all optional — see [ATTESTATION.md](ATTESTATION.md))

| variable | default | read by | legacy fallback |
|---|---|---|---|
| `HELM_CELL_BIN` | a known dev build path, else `meld` on `PATH` | `helm cell` / `helm premise` — the ledger-node client binary | `MELD_CELL_BIN` |
| `HELM_NODE_URL` | `http://127.0.0.1:8899` | the ledger node's HTTP API | `MELD_NODE_URL` |
| `HELM_NODE_TOKEN` | — | bearer for the node's submit endpoint | `MELD_NODE_TOKEN` |
| `HELM_NODE_PASSPHRASE` | — | unlock alternative to the token | `MELD_NODE_PASSPHRASE` |
| `HELM_CELL_PROFILE` | `meld-agent` for cell passthrough; `helm-test` for premise attestation (a deliberate split — see ATTESTATION.md) | the signing identity profile in `~/.dregg/profiles` | `MELD_AGENT_PROFILE` |
| `HELM_ROSTER` | `~/.dregg/roster.toml` | the substrate roster file | `MELD_ROSTER` |

## multimodel seats

| variable | default | read by | legacy fallback |
|---|---|---|---|
| `HELM_PROXY_BIN` | `~/.local/bin/cli-proxy-api`, else `cli-proxy-api` on `PATH` | the seat module (`helm/seat.py`) — the local Anthropic-wire proxy binary | `MELD_PROXY_BIN` |

## one non-HELM variable helm reads

`MC_HOME` — `helm whoami` merges its profile scaffold from a Mission Control
user-profile at `$MC_HOME/user-profile/profile.json` (default
`~/.mc/mission-control`) when one exists. Absent, the profile starts fresh —
no dependency either way.
