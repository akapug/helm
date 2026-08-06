# helm web — the browser surface

`helm web` serves the whole station as one self-contained page — no CDN, no
external assets, Python stdlib only. Its source is split by owner-visible
section under `helm/web_ui/`; `manifest.txt` fixes the global CSS → markup → JS
order, and `helm.web_ui_loader` joins the raw bytes on every request without
inventing a separator. The browser still receives one document, one `<style>`
scope, and one classic `<script>` scope. It is a projection of what the CLI
already answers: every view has a terminal equivalent, and the small set of
browser mutations maps one-to-one onto CLI verbs.

```console
$ helm web            # http://127.0.0.1:7433
$ helm web --port 8080 --open
```

## The nine views

| tab | what it shows | CLI equivalent |
|---|---|---|
| **helm** | the knowledge home: the **system dashboard** first (record / fleet / pipeline / **in-flight** / landed / owner; the pipeline row counts only live work (verified-superseded and confirmation rows join its closed accounting rather than its headline or composition bar), while in-flight lists those live land loops MOVING-first as lane · exact state · current ball-holder · dwell, carrying the ledger's own stalled/contrary marks; every line opens the full wall on the **ledger** tab, so HOME remains a compact projection rather than a second detail surface), then the **READY gauge** (`helm ready`'s five signals — may forward work resume after a crash/reboot? advisory: it renders, never gates; always visible, boots "not read yet"), then **fleet notes** (what the fleet left you — one note per key, who wrote it, how long ago; refreshed on its own 60s timer while this tab is open), the registry, typed-store counts and entries, operator profile | `helm lr list` / `helm lr stalls` / `helm note list` / `helm projects` / `helm store` / `helm whoami` |
| **quota** | the burn chart, account scorecard, allocation panel, credential homes with lifecycle buttons | `helm creds` / `helm swap` / `helm homes` |
| **boxes** | every machine helm can see, with its live speed benchmarks: one row per box from one durable snapshot, with directly-labelled disk (solid) and memory (hatched) bars paired inside each sequential/random/fsync metric for adjacent comparison. The inventory is the box-provider chain — this machine always, owner-declared hosts, unprobed SSH-config candidates (listed with the consent switch that would enable them, never probed without it), and an optional external inventory helper when one is installed. Exact values, unavailable reasons, method, measured age and stale warning remain visible. The browser never benchmarks or crosses SSH; reload re-reads only (a device that saved the old `storage` tab keeps landing here) | `helm storage-matrix` / `helm storage-matrix --measure` |
| **sessions** | the full catalog across harnesses: search inside transcripts, role-colored drawer, one-click resume command, re-home, prune | `helm sessions` / `helm search` / `helm transcript` / `helm cmd` |
| **configs** | every config across every home, the cascade resolver, and the editor (backup → validate → atomic write, one-click restore) | `helm configs` |
| **chat** | the human-included groupchat: the RAM room, polled every ~2s while open. Posting signs server-side as the owner's derived handle on the chat room node when it answers (v2 — signed rows carry a subtle ✓ tick, chain index on hover; the strip beside the send button shows `signed ⛓#head` / `unsigned`) and drops the `owner-unread` marker, so every local agent's next turn is steered to read + reply (the shipped `owner-chat-unread` reflex). `:shortcodes:` expand; hovering a message offers click-to-react (👍 🎉 🔥 ❤️ 👀) and **↩ reply** — a reply renders a compact clickable quote of its parent, the parent shows `↩N` (one level, never nested), an orphaned parent says so. The composer completes **@mentions from the live roster** (exact tokens only, so the mention is one the beacon matches). The sidebar is the activity surface: per-channel **unread count + @ flag + last-message age**, quiet channels dimmed, seat rows showing presence *and* last-seen age with a legend; long agent posts collapse behind *show more*; the room carries a **new since you last looked** divider and a **↓ N new** jump-to-latest. Works from the Orca mobile browser (simple DOM, no exotic APIs) | `helm chat` |
| **roster** | one dense table for every addressable seat: presence, current work, pending delivery, project/session, and launch-owned runtime label (`family · harness/backend`, e.g. `codex · pi/proxy`). Legacy rows stay unlabelled rather than guessed. The seat popup and message picker reuse the same row. **You** get a row too, pinned to the top and accent-tinted, whenever a browser cockpit is polling — the one thing the retired cave tab's presence pane knew that the roster did not. It is a person, not capacity: it is never a message target and never counted in `N live`. | `helm chat seats --all` |
| **ledger** | the whole record, three tiers. Tier 0 is the **land pipeline** wall, moved here whole from the home tab (every land loop as the dispatch ledger and git report it — state, reviewed sha, author→reviewer, verdict polarity, dwell, who owes it, a verification chip that reads `UNVERIFIED` until a gate receipt is bound; CONTRARY and STALLED rows carry ⚠ and badge THIS tab; an unreadable ledger replaces the body with a loud UNKNOWN strip, and it renders off the always-on 45s `/api/lr` poll, not the 3s ledger poll). The in-flight rows draw as a **kanban board by default** — one column per state string the rows actually carry (pipeline order left→right, mirroring `landreq.STAGE_ORDER`; an unknown state sorts last, never dropped), columns scrolling horizontally inside the card, each column headed by its count and, when any card in it alarms, a red edge + ⚠n; the cards are the SAME row element the list draws, alarm edge and expand included. A row whose verdict was DISCHARGED (honored through succession, or a confirmation round) is not work: it leaves its column for the ONE closed strip at the foot of the card, so SUPERSEDED holds only undischarged rows, and the column it left keeps a "N discharged — closed · tap to show" chip opening that same strip (the column survives card-less rather than vanishing when every one of its rows discharged). The header counts those rows as closed, never in flight; the record keeps the distinction, because each still prints its own SUPERSEDED-CLOSED / CONFIRMATION line. A 1-click ⊞ board / ☰ list control in the card head swaps to the flat list and back, remembered per device (`localStorage` key `helm.lrview`; kanban whenever unset or unrecognised). Then the attestation node's read surfaces, polled every ~3s while open: the turn ledger (recent signed turns, finality tier per turn — the durable `final @ h<N>` consensus certificate over the receipt's hash-bound field — chain head, agent/receipt hashes) with a **signing pulse** line (a word-sized activity strip over the newest ≤40 entries, the final/awaiting-finality split, and a chain-continuity check — gapless positions, and whether the read is behind the service's own head), a node status strip (ingress vs finalized height, producer, federation, and a **record comparison**: signed·shared entries vs this machine's local-only chained notes, either side UNKNOWN when unread), and seat activity (every cell, active/quiet/stuck/idle off the receipt window, each row with a share meter comparing its recent signings against the busiest signer). Node down = a quiet "substrate offline" strip, never an error. The one write — message a seat — posts `@seat …` through the EXISTING chat POST; the ledger endpoints themselves are GET-only | `helm lr list` / `helm cell status` |

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
  (`__HELM_TOKEN__` in the assembled UI source is replaced at serve time; the
  fragments on disk stay raw). `HELM_API_TOKEN` pins it — set
  that when a script or a long-lived service needs stable POST access.
  A tab that outlives the process (the unit recycles hourly by design) holds
  a dead token; the UI self-heals on the resulting 403 by re-reading its own
  served page for the live token and retrying the mutation exactly once — a
  genuine forbidden still surfaces loudly.
- **Bounded writes.** POST bodies over 64 KB are rejected; every mutation is
  reversible by construction (renames, moves-to-trash, backups — the
  archive-not-delete law).
- **Graceful degrade.** A subsystem that is absent or misbehaving answers
  `{"unavailable": true}`, never a 500 that takes the page down.

## GET endpoints

Plain reads (no parameters):

| endpoint | returns |
|---|---|
| `/` | the one assembled UI page (`helm/web_ui/manifest.txt` order, token templated in) |
| `/api/registry` | the project registry |
| `/api/store` | typed-store summary: counts per root + a bounded entry projection |
| `/api/whoami` | operator profile + notes summary |
| `/api/sessions` | newest claude + codex sessions (the catalog's scope, not every harness), project-lensed |
| `/api/configs` | the config-file list model (home/user scope + project tree) |
| `/api/configs/homes` | config files grouped per credential home, including owner-authored `commands/*.md` and Codex `rules/*.rules` with symlink aliases deduplicated |
| `/api/configs/backups` | the config-backup list, newest first |
| `/api/skills` | the skills census, dupes-flagged, with real dir paths |
| `/api/status` | quota provider presence + account counts |
| `/api/allocate` | allocation ranking for the configured model chips |
| `/api/homes` | every credential home (live, broken-alias, archived) |
| `/api/notes` | fleet notes: `{notes: [{key, text, headline, detail, goto, by, ts, age_s, stale, retired}, …], cap}`, newest first (UNDATABLE notes sort FIRST, which is what lets the fresh/older split preserve this order exactly). `age_s`/`stale` name the same vocabulary as `/api/storage-matrix`: `stale` is TRUE past `fleetnotes.FRESH_WINDOW_S` (48h) and `age_s` is `null` when the stamp will not parse — an undatable note is never dimmed, because hiding it would rest on a number nobody read. Staleness is a DISPLAY TIER, never a deletion: the card folds older notes behind an expandable summary (opened automatically when every note is stale, so the card never reads empty while notes exist) and the CLI rules them off below a divider. Both surfaces read the ONE boundary, so they cannot disagree about what is current. `helm note retire` drops a note from the current section at any age and KEEPS it (undone by `helm note restore`); `helm note rm` is the only thing that removes, and it removes destructively. The card itself is the home tab's headline/click-to-detail/action-pointer surface. `text` remains the compatibility field; legacy long bodies project into bounded `headline` + collapsed `detail` without rewriting disk. Read-only; any surprise answers `{"notes": [], "unavailable": true}` at 200. Written from the terminal (`helm note set\|rm`) and compositionally from `helm board landed`; there is deliberately no browser mutation, because these are the FLEET's notes to the owner |
| `/api/ready` | `helm ready`'s five-signal readiness gauge: `{ready, signals: [{signal, state, evidence, repair, note}], ts}` — the home tab's READY card (always visible, boots "not read yet"). ADVISORY: it renders, it never gates. Served through a 30s single-flight cache (the gauge walks /proc and resolves panes — a poll storm must cost one gauge per window); any surprise answers `{"unavailable": …}` at 200 |
| `/api/storage-matrix` | the validated host-local benchmark snapshot plus `age_s`, `stale`, and named missing/unreadable states. GET-only: measurement stays an explicit terminal action, so opening or reloading the tab never creates I/O or SSH traffic |

Parameterized reads:

| endpoint | query params | returns |
|---|---|---|
| `/api/configs/cascade` | `cwd=` `harness=claude\|codex\|pi` `home=` | what a seat at that cwd loads — the resolved cascade |
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
| `/api/todos` | — | the fleet todo mirror: `{seats, orphans, orphans_hidden, now}` — every roster seat with the task it is on right now (`active`, `done`/`total`, `ts`), plus the freshest 25 mirrored sessions no seat claims (`orphans_hidden` counts the rest). The DIGEST only: item lists never ride this wire (`helm todos --all --json` is the detail read), so a big estate stays kilobytes on a per-poll endpoint. Read-only, pull-only; any surprise answers `{"unavailable": true}` at 200. Feeds the ledger tab's **fleet todos** panel (`helm todos --all` is the same read) |
| `/api/lr` | — | the LAND PIPELINE read behind the home tab's first card: `{read_age_s, ledger_age_s, unavailable, receipts_skipped, filed, loops[], stalled_ids[], unmeasurable[], closed_recent[]}`. `filed` is the all-time POPULATION behind the board — `{total, in_flight, held, landed, closed, non_loop}`, derived from the SAME projection snapshot (no second ledger walk under the recompute floor) and partitioning `total` exactly: `in_flight` counts every non-terminal loop (it can read higher than the board header, which counts only the carrying chain frontier — both are true), `held` the REVIEWED rows (undeclared polarity or a declared approval held by tier/gate policy), `landed` retirements whose change is on trunk, `closed` retirements of any other kind, `non_loop` the rows that are not land loops (cancelled transit, migration copies, ref-less legacy). It is `null` on EVERY unavailable body — a dict exactly when the board rendered — so a failed read can never print a confident zero; the card says "filed total UNKNOWN" for an absent split, and `helm lr list` prints the same strip WHOLE in both of its headers — one owner (`landreq.filed_split`/`filed_line`) derives and formats it for every surface, so a pasted header and the card can never disagree about one record. `loops` are `landreq.card()` rows (state, lane, branch, `base_sha`, `review_sha`/`review_sha_full`, author, reviewer, `kind`, `polarity`, dwell, `stalled`, explicit `land_state`, `landed`/`merged_local`, `contrary`/`contrary_state`/`contrary_discharge` (the display-truth succession stamp `_annotate_contrary_discharge` wrote: `"a"`/`"b"` = the verdict was HONORED through succession, and `"c"` = the row IS the resolved door's confirmation round — `landreq.confirmation_row`: polarity in the door's own admitted set (approve|supersede, never fix) and evidence opening the literal `Resolution verified on trunk:` sentence, both gates shared with the close rung — so the card renders the honored banner (or the quiet "confirmation" words for `"c"`) and counts the row as CLOSED rather than as `contrary` or as work in flight — owner 2026-08-04, "superseded, if verified, should just be like another type of closed": a discharged row leaves the board column for the closed strip, keeping its own SUPERSEDED-CLOSED / CONFIRMATION line there, and the closed footer names the count that joined it; `"unverified"` and `null` both stay LOUD — display only, enforcement fields like `owed_by` are untouched), `abandoned`/`abandon_reason`, closure fields including `close_reason` plus DELIVERED_REPORT-only `artifact_ref`/`report_ref` and historical-correction `delivered_report_correction`/preserved `cancel_reason`, `owed_by` (enforcement/audit truth), server-resolved `holder_role`/`holder_seat` (the one display answer consumed by both CLI and HOME after succession annotation; an honored or confirmation row can therefore show holder `nobody` without rewriting `owed_by`), `timeline`, and the verification pair `gate`/`ungated`). `unmeasurable` reports the loops whose dwell cannot be billed — an undeclared polarity or an unconfirmed delivery — because the same loop silently absent reads as healthy. `closed_recent` is every TERMINAL row whose CLOSURE falls inside 24h — not just landed ones: superseded, withdrawn, ABANDONED, DELIVERED_REPORT, and closed-by-landing rows are terminal too, so the card's footer claims only that they CLOSED. DELIVERED_REPORT exposes its typed artifact ref plus full 12-character lowercase-hex chat row id, renders `land_state=NOT_CLAIMED` and gate/verdict as not applicable, and proves syntactic references rather than their existence; a corrected historical cancellation keeps its original reason visible. ABANDONED renders its mandatory reason and `LAND STATE UNKNOWN`; it never renders green, withdrawn, landed, or "not on trunk". The window is measured at the closure, never at the verdict: `closed_ts` is the closure stamp when the record carries one and `null` for a git-OBSERVED landing, which carries none, so an approve from three days ago that merges now no longer vanishes off the card. `closed_total` is the PRE-CAP count (the list itself is capped for the phone, and presenting the capped length as the count reported 13 closed lanes as 12); `closed_unknown_when` counts terminal rows the record cannot place in or out of the window at all, so "nothing closed in the last 24h" is only printed when it is actually known. Per-row honesty bits ride along, and each one keeps an UNREADABLE apart from an ABSENT: `dwell_known` is false when the entry stamp could not be read (`dwell_s` is then a fabricated 0, and the card renders `?`); `receipt_state: "unreadable"` distinguishes a land-receipt ledger helm could not open from one holding no receipt; `closed_ts_unreadable` says the record HAS a closure stamp and it is not a timestamp, which is a row to repair rather than the ordinary undated landing; and each `timeline` step carries `ts_unreadable` / `observed` so a missing stamp says WHICH kind of missing it is — an unreadable one, a ledger event that carries none, or the landing git observed, which has no moment anywhere. Read-only; the CLI equivalents are `helm lr list` / `helm lr stalls`. The separate read-only `helm lr legacy-completion-hints` audit is not an API promotion feed: it matches only the standalone word `delivered` in cancelled explicit-BUILD `cancel_reason`, labels every row UNVERIFIED with delivered-report eligibility UNKNOWN (including negatives and code delivered under successors), and changes no projection; only explicit annotation references can promote one. **Fail-LOUD, not fail-open**: any surprise still answers 200, but with a NAMED `unavailable` string and zero rows — never a quiet empty board, and never `{"unavailable": true}`, because a strip that cannot say why is a dead end. A single row the projection cannot BUILD is a failed read too — it used to be dropped silently, which is a board one lane short reporting itself clean. Behind a 30s single-flight recompute floor (`landreq.project()` spawns git per closed row per repo), and deliberately NO mtime fingerprint below that floor: `chmod 000` moves neither mtime nor size, so the first version's memo went on serving the last healthy board — every row green — over a ledger it could no longer read. The floor bounds the COST of the read; nothing may bound its TRUTH. Two dashboard readings ride the SAME cached body (one build, no second polling endpoint): `recent_lands` = `{rows, window_total, window_s, unavailable}` — the lands DERIVED FROM TRUNK at build time (`git log <trunk> --grep ^fold:`, the integrator's own landing record), each row `{lane, reviewed_tip, fold_sha, ts, age_s, trunk_ref, on_trunk, how, witnessed}`. **The rows are not read out of the land-receipt log.** They were, and that was an inversion: `land-receipts.jsonl` is an append-only DURABILITY log whose only writer is the manual verb `helm lr land`, so the card could only show a land somebody additionally remembered to witness — and the verb fails open, so forgetting was silent (measured 2026-08-05: trunk carried 55 folds since the newest receipt while the card showed six rows, the newest sixteen hours old). If it is on trunk it is on the card; no verb, no memory, no agent in the read path. `fold_sha` is the trunk commit that carries the land and is why the row exists at all. `on_trunk` remains a TRI-STATE about the REVIEWED CONTENT, which is now a narrower question than "did this land" (`true` = the reviewed content is on that trunk now, `how` says ancestry or patch-equivalent; `false` = proven absent, meaning what merged is not what was gated; `null` = git could not answer, typically a reviewed tip since collected — the land itself is never in doubt, because the row IS a trunk commit). `witnessed` is the signed receipt as a per-row BADGE and never a gate on visibility (`true` = a receipt is recorded for this reviewed tip; `false` = the index read cleanly and holds none; `null` = the index was unreadable or the abbreviated tip matched more than one receipt) — a missing signature is an INTEGRITY fact and may never make a real land invisible. `window_total` is the count of lands in the `window_s` window, measured separately from the capped list so a display budget can never edit a count; `age_s` is stamped at RESPONSE time like `read_age_s`. Cost measured 2026-08-05 on helm's own repo: 4ms for the log plus 30ms for six proof ladders, against 90.0s for `project_raw` — the ledger's own LANDED rows were the rejected alternative source, because a card behind the projection trades a stale card for one that times out. And `native_chain` = `{count, verified, detail, head_index, unavailable}` — the local attest-chain summary (`premise.verify_chain()`, re-hashed per build). Each carries its OWN `unavailable` because the receipt index, the premise store and the dispatch ledger fail independently — one flag would render a readable half as UNKNOWN |
| `/api/chat` | `room=` (default `main`) `since=` (rows already seen) | the chat poll read: `{room, lines, total, transport, rooms, roster, presence}` — rows include reaction rows AND reply rows (`{reply_to, rts, rfrom}`; the client aggregates both); signed rows carry `{turn, receipt, chain, payload}`; `rooms[]` carries each channel's `{total, last, owner_unread, owner_mentions, seats[{seat, runtime, presence, last_seen}]}`; `presence[]` carries the same optional launch-owned `runtime={family, agent_harness, backend}` used by the fleet strip; `roster` remains the exact live seat-name tokens for @mention completion; `transport` = `{mode: signed|unsigned, url, head}`; a `since` past the end (the room rotated) resends everything |
| `/api/chat/roster` | `room=` (default `main`) | the full read-only roster projection: each seat's presence, current work, pending deliveries, home/project/session and optional launch-owned `runtime={family, agent_harness, backend}`, plus live claims. Runtime evidence carries `runtime_verified`; only a self-verified launch family steers the badge (foreign mirrors remain display-only). Proxy-family rows also carry the cached upstream verdict and `beacon_paused`: a dark family renders `PAUSED · <state>` with an explicit “messages stay pending; HEALTHY resumes automatically” tooltip; stale observation remains visibly paused rather than inventing recovery, malformed observer state is `PAUSED · UNKNOWN`, and a dark-to-UNKNOWN pass stays UNKNOWN on both proxywatch and the roster (the last named cause is separate context) |
| `/api/chat/roster` (owner row) | — | the same read carries **the owner's own row** — `{seat, owner: true, presence, last_seen, connection: "cockpit"}` — while a browser cockpit has polled inside the last 30s. Derived in-process from that poll; there is no heartbeat POST and no presence file behind it |
| `/api/ledger` | — | the node projection: `{node, status, turns, cells}` — newest 40 signed turns with finality fields, cells with `last_turn_ts`/`recent_turns` joined off the receipt window; node down → `{"offline": true, "node": ...}` at 200 |
| `/api/ledger/turn` | `hash=` (64 hex chars) | one turn's durable finality certificate, proxied from the node's `/api/turn/<hash>/status`; node down/refused → `{"unavailable": true}` at 200 |


**The multiplayer seam.** `/api/multiplayer/state` (GET) and
`/api/multiplayer/publish` / `/api/multiplayer/presence` (POST) are still
served, and no tab reads them. They are the HTTP face of the blind-relay
adapter contract (see [MULTIPLAYER.md](MULTIPLAYER.md)) for a bridge or a
script; the cockpit tab that used to render them was retired on 2026-07-30
because its one owner-facing payload — the fleet's notes — now lives on the
home tab, durably, as `/api/notes`.

## POST endpoints

All demand the bearer token; bad requests return `400` with a class-only
`error`/`code`, while a stale config save returns `409 conflict`. These are the
**only** mutations the browser can make:

| endpoint | payload | effect |
|---|---|---|
| `/api/skills/toggle` | `{"path": <skill dir>}` | disable/enable — a reversible rename to/from `<skill>.disabled` |
| `/api/skills/delete` | `{"path": <skill dir>}` | move to `~/.cache/helm/skills-trash/` — never destroyed |
| `/api/homes` | `{"action": "create"\|"verify"\|"archive"\|"unarchive"\|"migrate", "name"/"provider"/"email": ...}` | credential-home lifecycle — directory moves only; logins stay human |
| `/api/cwd` | `{"sid": ..., "cwd": <abs path or null>}` | re-home a session's cwd (metadata, never identity); `null` resets |
| `/api/prune` | `{"sid": ..., "preset": "lean", "dry": true\|false, "tokens": N}` | derive a smaller still-resumable copy; the original is untouched |
| `/api/configs/file` | `{"path": ..., "content": ..., "revision": ...}` | save the regular file revision the editor opened: validated backup, same-directory atomic exchange, fsync, conflict detection, rollback; symlinks/devices/escapes are refused |
| `/api/configs/entry` | `{"action": ..., "path": ..., "kind": ..., "name": ..., "value": ...}` | structured entry op (add/remove an MCP server) — never hand-edits JSON |
| `/api/configs/restore` | `{"backup": <backup path>}` | restore a backup over its origin (validated, re-backed-up first) |
| `/api/chat` | `{"text": ..., "room": "main", "name": "alice", "reply_to": <parent row id, optional>}` | the owner's chat post (`reply_to` threads it under that row, wakes the parent row's author mention-tier — replying replaces typing the @mention — and, when signed, binds the parent into the digest): shortcodes expand, the digest rides a signed turn when the room node answers (server-side, as the server's `HELM_CELL_PROFILE`, default: the owner's derived handle), append to the RAM room + drop the `owner-unread` marker the shipped reflex fires on until an agent's `helm chat read` consumes it |
| `/api/chat/react` | `{"emoji": ":tada:", "tts": <target ts>, "tfrom": <target from>, "room": "main", "name": "alice"}` | the owner's click-to-react: a typed reaction row referencing the target message, signed like a post |

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
