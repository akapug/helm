# Changelog

## Unreleased (0.2)

- `helm todos` — the seat todo mirror: what every agent in the fleet is
  working on right now, without asking it. The recorder gains a todo leg
  that captures the CURRENT list off `TodoWrite` **and** the `Task*` family
  (`TaskCreate`/`TaskUpdate`/`TaskUpdateTODO` — what the live fleet actually
  emits; a create's id is parsed out of its tool_result string) into
  `todos.json` beside `counters.json` — bounded, atomic, no subprocess, and
  walled off behind its own `try` so a broken mirror can never cost the
  counters/command-log/edit-targets that back the stop-whisper. DIGEST +
  PULL by law: `helm todos` / `helm todos --all` / `GET /api/todos` / the
  roster row + `helm chat seats` read it on demand, and PUSH is a
  rate-capped exception — one room line per MEANINGFUL transition (a task
  finished, or an idle seat picking up a new in-progress task), collapsed to
  the latest state, at most once per 5 min per seat, nothing at all when
  nothing materially changed, `HELM_TODO_POST=0` to silence. Never an
  @mention, never a DM, and **never a wake**: every `@` is stripped from the
  posted text, and the row rides the new `ambient` class — a row
  `seats.deliverable()` drops before every wake rule, including the
  home-room rule that would otherwise have handed the line to every seat on
  a `helm launch --room team-x` team. It renders everywhere and wakes
  nobody. The owner's parity surface is the ledger tab's **fleet todos**
  panel; `/api/todos` carries the digest only (item lists ride
  `helm todos --all --json`) and caps unclaimed sessions at the 25 freshest.
- Chat replies + the owner-surface UX pass. `helm chat reply <id|n> <text…>`
  (and `post … --reply-to`) threads a message under a parent by REUSING the
  stable row id — additive `{reply_to, rts, rfrom}` (plus `rtext` only for a
  pre-id parent), no second identity. Signed replies BIND the parent through a
  disjoint algorithm tag (`chat:reply:b2b:` over RS-joined, injectively escaped
  parent fields + text) — never an
  in-band prefix on the plain-post payload, which attacker-chosen text could
  forge into a free re-parenting. Plain posts stay byte-identical, so every
  signed row already on disk verifies unchanged; `payload_for()` is the one
  shape-dispatching recomputer and `helm chat verify` re-derives it (honest
  scope: self-consistency, not remote re-verification — the node still cannot
  disclose a turn's payload). Rendering is one level, builders.dev style — a
  compact parent quote, a `↩N` count, graceful orphans — in `helm chat read`,
  the journal, and the web panel. Threading does NOT touch beacon-wake:
  `seats.deliverable()` never reads the pointer, so a reply wakes exactly what
  its text alone would have woken (asserted as a law over the full scope
  matrix). The web chat surface also gains: per-channel unread/mention badges
  with last-activity age and dimmed quiet rooms, readable seat rows (age +
  legend, distinguishing-tail truncation), collapse for long agent posts,
  @mention completion from the live roster, an unread divider and
  jump-to-latest.

  Two defects found by adversarial re-verification and fixed in the same
  slice: (1) a rotated-out parent could resolve to its **same-second twin** —
  one seat posting twice inside a second shares `ts|from`, rotation drops the
  oldest half, and the `(rts, rfrom)` fallback then quoted the wrong message
  under the reply and hung a phantom `↩N` on an innocent row; the fallback now
  fires only when there is no id to honor, in `chat.parent_of` AND the web
  panel's `chatParent` (they must agree). (2) `helm chat verify` reported
  `legacy` — the one never-alarming state — when a signed reply's recorded
  payload was **stripped**, which was the cheapest re-parenting forgery
  available; a signed row that is a reply and carries no payload is now
  MISMATCH, because `reply_to` and the recorded payload shipped together and
  the combination cannot occur honestly. Follow-up review closed the same
  twin hole for **pre-id** parents: `(ts, from)` was already ambiguous before
  rotation and could still resolve to the wrong survivor. Such replies now
  bind and match `rtext`, with every ts|from candidate retained; zero or
  multiple exact matches render an orphan. It also escaped literal RS bytes
  inside digest fields, preventing author/text field sliding from preserving
  a signed digest after an edit.
- The SAFE `/login` — `helm cred`. Hitting a session limit and running
  `/login` must not be a problem to do, and it cannot be redirected (a live
  session's CLAUDE_CONFIG_DIR is fixed), so the write is made truthful,
  non-destructive and reversible instead. Identity now comes from CONTENT
  (`cred.account_of` reads `<home>/.claude.json`'s oauthAccount, mtime-cached,
  fail-closed — an unreadable file claims NO account rather than guessing from
  the directory name), and `homes.py`'s identity reader, `helm launch --home`,
  the keepalive audit log and `helm doctor` all read it, so a dir name can no
  longer speak for an account. `helm cred list` is the owner-visible truth
  surface (DIR NAME | ACTUAL ACCOUNT | verdict); `helm cred backup [--all] --apply`
  snapshots credentials + identity into `~/.cred-backups/<folded-email>/<ts>/`
  (0700 dirs, 0600 files, idempotent, newest-20 retention);
  `helm cred switch-guard [--install] --apply` is the pre-login guard (explicit verb,
  or wired as a SessionStart hook in every claude home); `helm cred heal`
  restores a drifted home — DRY-RUN BY DEFAULT, backing up the current
  occupant first, verifying after, and refusing when a live session holds the
  home (`/proc` probe, re-checked immediately before the write), when there is
  no `/proc` to prove it free, when no snapshot exists, or when the restore
  would leave byte-copies of one refresh token in two homes (the revocation
  bomb). Secrets never surface: credential bytes are copied and compared,
  never printed, logged, or placed in an error string; the only derived value
  written is a 12-hex sha256 fingerprint. doctor gains the loud drift row and
  a `no cred backup for <account>` row. Keepalive — the one place helm writes
  credential files — now snapshots a pre-image before every rotation and logs
  the ACCOUNT beside the home name.

- `helm cred` hardened under adversarial review (five findings, all fixed):
  (1) heal now REFUSES with `no-preimage` when the current occupant cannot be
  snapshotted — it used to evict anyway, deleting the only copy of a live
  credential, which is the exact loss the verb exists to prevent;
  (2) `restore` is all-or-nothing across `.credentials.json` and
  `.claude.json` — both are staged before either is committed, and a failed
  commit rolls the credentials file back, so a half-written restore can no
  longer leave a home holding one account's tokens under another's identity
  block; (3) `restore` REFUSES a present-but-unparseable `.claude.json`
  instead of rewriting it from `{}` (that file holds the home's whole state —
  projects, MCP servers, history — and the old path silently destroyed it);
  (4) a leftover temp file can no longer block a restore permanently (staging
  uses random exclusive sibling names, not a recycled PID name); (5) the
  switch-guard now rides `Stop` as well as
  `SessionStart`, because a live session refreshes its own credentials and the
  grant ROTATES the refresh token — a session-start-only pre-image is dead
  hours before the `/login` it exists for, and keepalive cannot cover the gap
  (it skips every home with a live holder). heal additionally flags
  `stale_pre_image` when a snapshot's own access token had already expired,
  the temporal twin of the shared-family revocation bomb. Two more: snapshot
  dirs are now CLAIMED with an exclusive `mkdir` (one account can occupy two
  homes and the guard runs per turn, so two backups could land on the same
  name in the same second and the loser's error path deleted the winner's
  finished pre-image); and `helm cred list` + doctor's drift row now report
  whether the EVICTED account is recoverable — the BACKUPS column counts the
  ARRIVING account, which reads as `0` at exactly the moment the owner needs
  to know the evicted one is safe (live estate: `cto-example-invalid`
  now says plainly that nothing was ever snapshotted for it).

- Second independent CRED-SAFE-SWITCH review closed the remaining safety gaps:
  every credential mutation, including `backup`, guard installation, and
  `keepalive`, is now dry-run unless `--apply`; keepalive takes its stable
  pre-image before the rotating network grant; backup brackets identity and
  credential reads so a concurrent `/login` cannot cross-file a snapshot;
  restore rejects symlinks and snapshot identity/digest/length mismatches, then
  performs durable two-file commit with exact
  bytes/mode/absence rollback at every staging/rename/fsync boundary; heal
  re-probes holders after capture and at commit, with permission/read/PID-reuse
  uncertainty refusing; lossy folded-name collisions have exact-account
  counting/retention and ambiguous heal selection refuses; and CLI,
  doctor, keepalive log, invalid-path, non-UTF8 and exception surfaces report
  class/reason only, never credential or token-shaped values. Native quota
  usage and command-mint attribution now share `cred.account_of`, so content
  identity is consistent end-to-end.

- Stop-whisper slice 2 — the verify-grounding rungs: the contextual
  continuation ladder gains three signals read from record.py's own logs
  (one bounded read, fail-closed): a RED gate (a test-runner's latest run
  exited nonzero — stopping on a known-red gate is the premature stop the
  lane exists for; a green rerun silences it), UNVERIFIED edits (code files
  edited, tree dirty, no gate ever ran — doc-only sessions never arm it),
  and UNBANKED green (every latest gate run green, tree still dirty —
  commit is the named next step). Same laws as slice 1: one 240B line per
  stop, once-per-fingerprint latch, salience order, kill-switch,
  fail-closed to silence.

- Multi-model in ONE claude-code process (the proven per-agent-frontmatter
  mechanism): `helm router` — a stdlib-only transparent router at
  ANTHROPIC_BASE_URL that forwards `claude-*` requests VERBATIM to
  api.anthropic.com (claude-code's own OAuth, never an API key, no
  substitution) and conducts non-claude models to their seat's CLIProxyAPI
  with the seat token, logging every request's model/route to a paste-safe
  conductor log. Plus `helm seat launch|smoke --multi`: the mixed-fleet
  launch shape (drops the CLAUDE_CODE_SUBAGENT_MODEL blunt pin, mints
  per-model probe agents) and a smoke fan-out leg that passes only when the
  conductor log shows both probe models on the wire.

- `helm codex` — codexhome roster + proxy cred pooling as a first-class
  verb (the manual night codified): `list` classifies ultra/team from the
  token plan claim with aliases and same-account dirs folded, `pool`
  translates a home's auth.json into the seat proxy's hot-reloaded auth-dir
  (0600, idempotent, stale-exp warns never refuses), `unpool` fail-open,
  `pooled` shows what the :8317 proxy can draw on. Sources read-only
  forever; token material never printed.
- Evolve's last two behavior-observer legs — dead reflexes (live, zero
  ledger fires over ≥200 turns, one batched review line) and recorder
  signatures (a stuck/loop-thrash tell recurring across ≥3 sessions' last
  counters proposes a captured lesson); propose-only as ever. The drain-v2
  upgrade pass ran LIVE: 194 dark typed-prefix files upgraded to real
  priors/lexicon (archive-first net + receipt), the adopted store's episodic
  pile down from 257 to 37.
- `helm corpus` — backup/status for the training-corpus transcript archive:
  every harness's transcripts copied append-only into dated archive dirs,
  incremental by manifest, fail-open per file, fail-safe on space, with a
  daily systemd timer in `scripts/`.
- Attestation is dregg-primary in production: owner web posts and every
  multimodel seat route through the dregg-native client signer under distinct
  named profiles, leaving hybrid-signed, consensus-final cave turns. The local
  blake2b chain / unsigned RAM path remains temporary fail-open coordination
  scaffolding while dregg is unavailable — never the target architecture.
- Kimi proxy-key seat — API-key provider families join the seat roster
  (`helm seat add kimi`, key baked into the seat's 0600 config, never read
  from the environment again) behind the same proxy as the OAuth seats.
- tokaware parity — creds crosscheck against a local-session second source,
  shared-refresh-token-family hygiene in list/verify/doctor, `helm attribute`
  (token-effort rollup) + `helm who` (pid→cred attribution, evidence-only),
  hermetic providers.py test coverage, and a git-presence doctor row.
- The A2A delivery lane — meld's agent-facing half collapsed onto the chat
  room: deliver/join hooks reach a seat mid-autonomous-turn at tool
  boundaries, TTL claims whose lease nonce is the capability, the seats
  panel in the ledger tab, and `helm launch` (the metaharness seam).
- Web UI Tuftian pass — one type scale, aligned nav stats, uniform
  attention pills, collision-free chart legend, decluttered account table;
  plus the ledger tab (the turn-ledger viewer ported in) and a proper
  'home' landing tab.

## 0.1.0-alpha — 2026-07-19

The founding release: the whole steering station, built and fleet-deployed in
its first day.

### The home
- `~/.helm` — per-project knowledge chains (premises / heuristics / lexicon /
  prd / journal / evals / archive) + a global chain; existing knowledge homes
  adopted by symlink, never copied.
- Auto-map: real projects discovered from what agents actually did, across
  Claude Code, Codex, and OpenCode; worktrees collapse to their repo; scratch
  dirs filtered; a shelf tier registers on-disk repos with no agent activity.

### The knowledge engine
- One typed store (prior / premise / lexicon / heuristic / reference /
  episodic) with one JIT resolver over every root; confidence with evidence
  logs; premises are human-only by construction.
- The drain: raw memory intake classified and routed to typed homes, with
  verified rollback nets and receipts. The drift report: contradictions,
  tier-crossings, and decays — silent when steady.
- Reflexes (signal → steer) and `helm inject`: one budget-capped, salience-
  gated per-turn context call any harness hook can make.
- Attested premises: captures commit a signed digest to a verifiable ledger;
  `premise-check` recomputes and quotes the finality tier; supersession forms
  a provable chain.

### The cockpit
- Sessions: every local session, all harnesses, project-grouped; content
  search inside transcripts; windowed transcript reads; re-homing;
  resume-optimized copies; one-paste resume commands; session capsules
  (the git era a session ended on).
- Accounts: live quota scorecard, rollover rescue (`swap`), credential-home
  lifecycle with identity verification and reversible archives.
- Configs: every config across every home and project, the load cascade
  resolved per seat, and a safe editor (backup → validate → atomic).
- The web app: four views (home / quota / sessions / configs) in one
  self-contained page — charts, transcript drawer, team tray, config editor —
  with a per-process mutation token on every write.

### The map
- Lineage: the project family tree with git-proven edges, external anchors,
  and a read-only ranked archive report.

### Operations
- `helm doctor` (read-only health), `helm evolve` (proposes, never mutates),
  skills census with divergence detection, `python3 -m helm`, PATH shim,
  systemd web service, fleet hook deployment with per-home backups.
