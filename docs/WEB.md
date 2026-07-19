# helm web — the browser surface

`helm web` serves the whole station from one self-contained page — no CDN, no
external assets, Python stdlib only. It is a projection of what the CLI
already answers: every view has a terminal equivalent, and the small set of
browser mutations maps one-to-one onto CLI verbs.

```console
$ helm web            # http://127.0.0.1:7433
$ helm web --port 8080 --open
```

## The four views

| tab | what it shows | CLI equivalent |
|---|---|---|
| **helm** | the knowledge home: registry, typed-store counts and entries, operator profile | `helm projects` / `helm store` / `helm whoami` |
| **quota** | the burn chart, account scorecard, allocation panel, credential homes with lifecycle buttons | `helm creds` / `helm swap` / `helm homes` |
| **sessions** | the full catalog across harnesses: search inside transcripts, role-colored drawer, one-click resume command, re-home, prune | `helm sessions` / `helm search` / `helm transcript` / `helm cmd` |
| **configs** | every config across every home, the cascade resolver, and the editor (backup → validate → atomic write, one-click restore) | `helm configs` |

## Security posture

- **Loopback only.** The server binds `127.0.0.1` (default port **7433**);
  there is no listen-on-all option. Put a reverse proxy with its own auth in
  front if you ever need remote access — helm itself will not do it.
- **Host/Origin validation.** Every request's `Host` (and `Origin`/`Referer`
  when present) must be a loopback literal for the bound port
  (`127.0.0.1:<port>`, `localhost:<port>`, `[::1]:<port>`) — anything else is
  403. This is the DNS-rebinding defense: a hostile page re-resolved to
  loopback never reads a response.
- **The mutation token.** Every POST demands
  `Authorization: Bearer <MUTATION_TOKEN>` — 403 without. The token is minted
  fresh per process and handed to the UI by template substitution
  (`__HELM_TOKEN__` in `web_ui.html` is replaced at serve time; the file on
  disk stays raw). `HELM_API_TOKEN` (legacy `SESH_API_TOKEN`) pins it — set
  that when a script or a long-lived service needs stable POST access.
- **Bounded writes.** POST bodies over 64 KB are rejected; every mutation is
  reversible by construction (renames, moves-to-trash, backups — the
  archive-not-delete law).
- **Graceful degrade.** A subsystem that is absent or misbehaving answers
  `{"unavailable": true}`, never a 500 that takes the page down.

## GET endpoints

Plain reads (no parameters):

| endpoint | returns |
|---|---|
| `/` | the UI (`web_ui.html`, token templated in) |
| `/api/registry` | the project registry |
| `/api/store` | typed-store summary: counts per root + a bounded entry projection |
| `/api/whoami` | operator profile + notes summary |
| `/api/sessions` | newest sessions across every harness, project-lensed |
| `/api/configs` | the config-file list model (home/user scope + project tree) |
| `/api/configs/homes` | config files grouped per credential home |
| `/api/configs/backups` | the config-backup list, newest first |
| `/api/skills` | the skills census, dupes-flagged, with real dir paths |
| `/api/status` | quota provider presence + account counts |
| `/api/allocate` | allocation ranking for the configured model chips |
| `/api/homes` | every credential home (live, broken-alias, archived) |

Parameterized reads:

| endpoint | query params | returns |
|---|---|---|
| `/api/configs/cascade` | `cwd=` `harness=claude\|codex` `home=` | what a seat at that cwd loads — the resolved cascade |
| `/api/configs/tree` | `root=` | the cwd tree of dirs holding project configs |
| `/api/configs/resolve` | `home=` `cwd=` `harness=` | a seat's full load including MCP servers |
| `/api/configs/file` | `path=` | one recognized config file's content + editability (refusals answer 200 with an `error` field) |
| `/api/creds` | `refresh=1` | the live account scorecard |
| `/api/history` | `hours=` (default 168) | quota gauge history rows |
| `/api/burn` | `hours=` (default 48) | burn attribution: hourly buckets joining quota drops to the sessions active then |
| `/api/physics` | `home=` `cwd=` `harness=` | what a seat on that home would load |
| `/api/physics-diff` | `a=` `b=` `harness=` `cwd=` | what differs between two homes' physics |
| `/api/catalog` | `refresh=1` `format=opensession` | the full session catalog (+ an OpenSession-aligned projection) |
| `/api/search` | `q=` `limit=` `scope=` `synthetic=1` | content search inside transcripts |
| `/api/session` | `sid=` `before=` `limit=` `find=` `harness=` | a windowed, role-tagged transcript read |
| `/api/cmd` | `sid=` `account=` `model=` | the pasteable account-aware resume command |

## POST endpoints

All demand the bearer token; all return `400` with an `error` field on a bad
request. These are the **only** mutations the browser can make:

| endpoint | payload | effect |
|---|---|---|
| `/api/skills/toggle` | `{"path": <skill dir>}` | disable/enable — a reversible rename to/from `<skill>.disabled` |
| `/api/skills/delete` | `{"path": <skill dir>}` | move to `~/.cache/helm/skills-trash/` — never destroyed |
| `/api/homes` | `{"action": "create"\|"verify"\|"archive"\|"unarchive"\|"migrate", "name"/"provider"/"email": ...}` | credential-home lifecycle — directory moves only; logins stay human |
| `/api/cwd` | `{"sid": ..., "cwd": <abs path or null>}` | re-home a session's cwd (metadata, never identity); `null` resets |
| `/api/prune` | `{"sid": ..., "preset": "lean", "dry": true\|false, "tokens": N}` | derive a smaller still-resumable copy; the original is untouched |
| `/api/configs/file` | `{"path": ..., "content": ...}` | save a recognized config file: backup → validate → atomic write |
| `/api/configs/entry` | `{"action": ..., "path": ..., "kind": ..., "name": ..., "value": ...}` | structured entry op (add/remove an MCP server) — never hand-edits JSON |
| `/api/configs/restore` | `{"backup": <backup path>}` | restore a backup over its origin (validated, re-backed-up first) |

A scripted example:

```console
$ export HELM_API_TOKEN=$(openssl rand -hex 16)
$ helm web &                       # server now honors this pinned token
$ curl -s -X POST http://127.0.0.1:7433/api/prune \
    -H "Authorization: Bearer $HELM_API_TOKEN" \
    -H "Content-Type: application/json" \
    -d '{"sid": "3f2a", "dry": true}'
```

## Running it as a service

The deployment that runs live is a systemd **user** unit. Install helm on
your PATH first (`ln -s <repo>/bin/helm ~/.local/bin/helm`), then:

```ini
# ~/.config/systemd/user/helm-web.service
[Unit]
Description=helm web — the personal knowledge home UI
After=default.target

[Service]
ExecStart=%h/.local/bin/helm web --port 7433
Restart=on-failure
RestartSec=5
# optional: pin the mutation token for scripted access
# Environment=HELM_API_TOKEN=<your-token>

[Install]
WantedBy=default.target
```

```console
$ systemctl --user daemon-reload
$ systemctl --user enable --now helm-web
$ systemctl --user status helm-web
```

`loginctl enable-linger $USER` keeps it running when you are not logged in.
