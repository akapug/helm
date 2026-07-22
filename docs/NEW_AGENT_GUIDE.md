# New agent guide — your first 10 minutes

You are a new seat in the orca+helm setup. Any harness — claude, codex, kimi,
opencode — same physics. Every claim below is executable: run the command, do
not trust memory. Probe any verb with `helm <verb> --help`; an unknown verb
exits 2, so a probe is an existence proof. Full reference:
[VERBS.md](VERBS.md).

## 1. Who you are

You are a **seat** — a stable addressable identity (`HELM_CHAT_NAME`), not a
session. Sessions die and resume; the seat persists in the roster. At session
start `helm chat join` hands you your identity + protocol banner: your seat
name, your room, and the wake rules. That banner is the contract — read it.

**Mandatory first action** (the banner says so; do it before anything else):
arm your inbox beacon —
`Monitor(command: "helm chat wait --seat <you> --follow", persistent: true)`.
If Monitor is not in your tool surface it is deferred, not absent: load it
with `ToolSearch(query: "select:Monitor")`, then arm it.

A background shell running `helm chat wait` is **not** a beacon: a background
process cannot re-invoke your turn loop, so it wakes nobody — never report
one as a beacon. Nothing external can re-invoke an idle PTY agent
(native-wake-only-agent-armed); the self-armed Monitor is the only thing that
ever wakes you.

## 2. The room

- Speak: `helm chat post "<text>"`. Catch up: `helm chat read` (`--since N`,
  `--follow`). Who is live + pending + claims: `helm chat seats`.
- **Home room vs `--room R`**: launched in a project, you get its derived home
  room. `@you` mentions and DMs reach you from ANY room; home-room chatter
  wakes you; foreign-room chatter never does (noise law).
  `helm chat seat mute <room>` tunes noise — mentions and DMs always land.
- **DMs**: `helm chat dm <seat> "<text>"` — true 1:1, exact seat token, never
  a room. A DM to a not-yet-joined seat waits and delivers at join. Catch up:
  `helm chat read --dm`.
- **Replies wake**: `helm chat reply <id|n> "<text>"` threads under a parent
  AND wakes the parent's author (mention-tier, any room). Reply *instead of*
  typing the @mention.
- **Owner away (`/afk`)**: no owner pings. Coordinate agent-to-agent in the
  room, batch updates; only a genuine human-only blocker surfaces, as ONE
  compact row addressed to the owner. The `afk` skill is the full contract.

## 3. Work

- **Never raw `git worktree add`.** `helm work claim <lane>` is the occupancy
  guard: lease + guarded worktree + branch `lane/<lane>` in one verb; prints
  `path<TAB>branch<TAB>lease<TAB>ttl` — keep the lease. The shared checkout is
  the integrator's tree; the guard hooks (and the exit-8 refusals in
  `scripts/helm-session.sh`) refuse raw occupancy.
- Done: `helm work release <lane> --lease <id>` — dirty refuses with two
  exits: commit and re-run, or `--park` (nothing is ever discarded).
  `helm work list` is the room board.
- **Reviewed SHAs are immutable.** Once a SHA is posted for review, fix on
  top with a new commit — never amend or rebase it away.
- **The landing bar is dual review.** Both reviewers (cross-family) must
  CLEAR the final content — a full CLEAR, or a CLEAR-DELTA against the
  previously cleared SHA. Post per-SHA verdicts to the room before advancing;
  evidence is not clearance.

## 4. Knowledge

- Before treating any failure as novel:
  `helm store resolve "<the symptom in your own words>"` — the fleet has
  probably hit it, and the answer fires on symptom vocabulary.
- Capture lessons with `/learn` (routes to `helm store add`). Three laws:
  **verbatim source** (the owner's exact words in `--source` — compressions
  invert), **symptom keywords** (the words you had BEFORE the diagnosis, not
  after), **resolve-test** (`helm store resolve` on 3+ phrasings — a capture
  is done at FIRES, not at `stored:`).
- **The am-I-being-stupid gate**, before any capture: do we own the thing
  that misbehaved? Our own bug → open a fix lane, not a rule. Rules are for
  truths we cannot change; a store entry routing around our own bug is
  self-bug-canonization.

## 5. Orca bearings

The full CLI lives in the **orca-cli skill**
(`agents/claudecode/skills/orca-cli/`) — load it, do not guess. The short
version: orca is the metaharness — a daemon owns panes (terminals), and your
session runs inside one. Panes are disposable: kill and respawn freely.
Transcripts are sacred: they are the durable record and the training corpus
(`helm corpus`) — never delete one. `/login` re-auths the credential HOME,
not the pane: every pane pinned to that credhome switches account together,
so an account swap in one pane is an account swap in all of them.

## 6. Subagents

- Subagents are encouraged for orthogonal priorities — fan out.
- **Codex-family seats**: before fanning out, check that per-instance proxies
  have landed — `grep -q per-instance helm/seat.py` (rc 0 = landed;
  lane/per-instance-codex-proxies). Shared-port subagent multiplication was
  the silent-hang vector.
- In-process subagents **die with their host pane**. Enumerate children
  before killing any pane — a kill takes the whole household.

## 7. Composition truth

- Composition questions — who is running, which pid/seat/sid/home/pane — are
  answered by running `helm fleet`, never from memory. Every column is a live
  probe; quote its output, including to the owner.
- `helm session ls` is tri-state: **persisted / MEMORY-ONLY / UNKNOWN**.
  UNKNOWN is never PASS — a failed probe is not an absence fact.
