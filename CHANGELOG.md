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
