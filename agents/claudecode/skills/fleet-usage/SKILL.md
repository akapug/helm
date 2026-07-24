---
name: fleet-usage
description: Query my own Anthropic session quota + the swarm's codex / opencode / gemini / auggie / hermes quotas live via the `mcp__builders-dev__usage` tool. Use whenever a pacing decision matters (about to dispatch a claude subagent, user asks "how much have you used?", deciding whether to slow down). Returns a Tuftian scorecard table with % used + reset window per agent identity available on this host.
---

# usage - live swarm telemetry via builders-dev MCP

## When to invoke

- Session start (verify starting baseline, especially after a reset).
- BEFORE dispatching a claude subagent (subagent reasoning costs against my budget).
- BEFORE any heavy operation (workspace rebuild, codex pair-review round, multi-agent dispatch wave).
- When the user asks "how much have you used?" - query, don't estimate.
- When pacing guidance mandates a status pulse and the table-format-canonical applies.

## How to invoke

Make these 3 tool calls **in parallel** for full swarm visibility, then synthesize into one Tuftian table:

```
mcp__builders-dev__usage(action="list_accounts", harness="claude-code")
mcp__builders-dev__usage(action="list_accounts", harness="codex")
mcp__builders-dev__usage(action="list_accounts", harness="opencode")
```

Optionally also `harness="auggie"` if Auggie is in the swarm rotation; `harness="gemini"` currently returns empty (probe gap, F-USAGE-GEMINI-PROBE followup).

## First-call ladder for builders-dev MCP discovery

Builders-dev tools can be deferred until discovered. If a message, project, or
agent-state call says "call X.list" but that tool is not visible yet, discover
the builders-dev MCP tools first, then use this ladder:

1. `projects(action="list", limit=10)` - get the canonical `proj:<team>/<slug>`.
2. `channels(action="list", project_id="<project>", limit=10)` - get channel ids.
3. `messages(action="list", project_id="<project>", channel_id="<channel>", limit=5)`
   for lean chat rows; call `messages(action="get", message_id="<id>")` only for
   rows you need to hydrate.
4. `search(action="messages", project_id="<project>", query="<text>", limit=5)`
   for semantic recall.
5. `agent_state(action="describe")` before read/write calls. Some read-looking
   actions still require ids: `goal_status` requires `goal_id`, `skill_list`
   requires `group_id`, and `skill_discover` is the public no-group probe.
   If `goal_status` rejects a read with "status must be a non-empty string",
   treat it as an upstream builders-dev validator bug and use list/describe
   paths instead; do not invent a write-status for a read probe.

## How to read the response

- `account_id` - short identity slug
- `latest_snapshot.value_pct` - % of window used right now
- `latest_snapshot.status` - `ok` / `approaching` / `near` / `exhausted`
- `latest_snapshot.reset_at` - unix-seconds when the window resets
- `latest_snapshot.value_raw="cred_unreachable_from_worker"` - that identity isn't authenticated through this host today; SKIP it from the report

My identity on this host is `claude-code:<your-host-id>` (the only `claude-code` row with `status != cred_unreachable_from_worker`).

## Keeping usage fresh (the local poller - F-USAGE-LOCAL-POLLER, 2026-06-01)

The OAuth bearer lives on THIS machine; a Worker can't read it. So usage only
populates when the machine-local poller runs:
`python3 <builders-dev-repo>/scripts/usage-poller.py [--verbose]`. It reads
the on-disk credential (`~/.claude/.credentials.json` / `~/.codex/auth.json`),
account id from `~/.config/builders-usage-poller.json`, and calls the authed MCP
`usage.check` with the cred -> probe -> cache. The check now also falls back to
the durable D1 snapshot between polls, so a credential-less `usage check` /
`list_accounts` returns the last poll's data (carries `usage_pct`,
`usage_status`, `usage_reset_at`, `last_snapshot_age_ms` as top-level scalars).
If a row's snapshot is old/empty, that is a READ-GAP, not a stale credential — get
LIVE truth (for codex, the canonical read is `cred-live-probe.sh <idle-pane>`; see the
no-stale law in `/cred-prep`) and/or run the poller. `last_snapshot_age_ms` tells you how
fresh each row's *reading* is (null/old = an unrefreshed reading or an install-row from
another host) — never a verdict on the cred itself.

## How to render

Tuftian scorecard table (dense, high-signal, one row per agent). Include **Age** (snapshot freshness)
and **Swap-ready** - both are load-bearing for rotation and were missed before:

```
| Agent | Account | % Used | Status | Reset in | Age | Swap-ready |
|---|---|---|---|---|---|---|
| Claude (me) | <your-host-id> | 15% | ok | 4h 23m | 30s | - |
| Codex | you@example.com | 55% | approaching | 4h 0m | 1m | yes |
| Codex | other@example.com | 10%→re-probe | ok? | 2h | 22m OLD→live-probe | NO (home unauthed) |
| Opencode-ds4 | (not authed today) | - | - | - | - | - |
```

- **Age** = `last_snapshot_age_ms` rendered human (e.g. `30s`, `22m`). If a row's reading is >~10min
  old, the poller has not refreshed it (it only refreshes the ACTIVE account's row), so the % is an
  unconfirmed last-known value that could be a dangerous lie (a "10%" that's really exhausted). The
  answer is NEVER to report it "stale" and move on — it is to get LIVE truth: for codex, the canonical
  read is `cred-live-probe.sh <idle-pane-on-that-account>` (the `/status` panel = ground truth, richer
  than the MCP). NEVER rotate to / brief a cred off an unconfirmed old reading — re-confirm it live first.
- **Swap-ready** = is the rotation target actually usable RIGHT NOW: for codex, its isolated home is
  authed (`codex-home.sh readiness <account>` -> READY). Quota alone is NOT swap-ready; a quota-OK
  account whose home is unauthed needs `cred-prep` first. Show `NO (home unauthed)` so it's never
  picked blind. (`-` for the live self / non-codex rows where it doesn't apply.)

Convert `reset_at` (unix seconds) to "Xh Ym from now" relative to current time.

## Pacing thresholds

**Pacing is conditioned on time-to-reset, not percentage alone.** The %
table below is the *near-reset-far* reading. The window resets to 0 at
`reset_at`; **any budget unspent before reset is wasted, never carried.** So a
high % with a soon reset is NOT a throttle signal - it's a SPEND signal.

| 5h % | reset FAR (>~90min) | reset SOON (<~45min) |
|---|---|---|
| ok (<50%) | dispatch subagents freely up to the 4-5 cap | fan out HARD - 4+ SAs/WFs; spend the headroom that's about to reset |
| approaching (50-80%) | slow individual driver work; dispatch only load-bearing SAs | **FAN OUT - 4+ SAs.** 50%+ headroom resetting in <45min = budget-to-burn. Throttling here wastes it. |
| near (80-95%) | stop budget-burning SAs; commit + push completed work | finish in-flight SAs + commit; don't *start* long new ones that won't land pre-reset |
| exhausted (95%+) | yield; let subagents drain; status updates only | same - but reset is imminent, so the drain is short |

Self-check before throttling: "if I slow down now, does the unspent budget reset
to waste in <45min?" If yes -> the correct move is MORE parallelism, not less.
Common mistake: reading "61% approaching" as a throttle signal when ~39% of the 5h
window was about to reset unused in ~30min - the right move was to fire 4+ subagents.

## The pace decision (don't stop at the table - emit an action + set the budget)

Checking usage is not done at "here are the numbers." Synthesize the table into ONE decision and
**turn the subagent/pane budget as the lever** - this is how usage dictates team size, not just a
status read. From (% used, time-to-reset, pool swap-readiness, work-remaining):

- **BURST** - headroom healthy OR window just reset OR a soon-reset is about to waste budget ->
  set the subagent budget to the full ceiling (the `/warp` level), spawn the extra panes, finish the
  push. "Window refreshed -> bump back to 22 and close it out."
- **HOLD-solo** - near-exhausted AND reset is FAR AND no swap-ready fresh cred -> set the subagent
  budget to **0-1** and keep the DRIVER working the critical path solo; don't start work that can't
  land before the wall. Plan the re-BURST for the reset boundary. ("Make it another hour to reset,
  so no subagents until then; I'll keep working and bump back up when the window refreshes.")
- **ROTATE** - exhausted (or near + reset far + urgent) AND a swap-ready same-family account exists
  -> rotate (basin-preserving; `/rotate-cred`), keep bursting on the fresh account. (Pre-auth all
  candidates ahead of time via `cred-prep` so this is headless - quota alone != swap-ready.)
- **PARK** - exhausted AND the pool is dry -> checkpoint WIP committable + escalate to the human
  (phone). Never stall silently.

The budget axis above is for the DRIVER's own quota. A TEAMMATE's quota has a second lever - **its
role intensity** (cred rotation is the last rung of that ladder, not a separate move). When a teammate
is usage-constrained, a leader downshifts the role to keep its value cheaply before rotating: full pair
(code+devops) -> active rubberduck (it advises, the leader codes) -> batch xrev (one async review packet)
-> review-only/CTO-xrev (mintoken, decision points) -> rotate to a fresh same-family cred (back to full)
-> solo + re-engage on reset. The rung is a FUNCTION of %+reset-proximity+cost+urgency (NOT status-bound):
a high-% teammate whose window resets SOON should SPEND (stay full pair), same as the budget axis.
Generalizes to any leader->teammate pair; see `/lead`. So the full pace decision is two-axis: the
driver's budget (BURST/HOLD/ROTATE/PARK) AND each teammate's role rung.

Apply the budget through helm's own `/afk <soft|hard> --subagent-cap <N>` front door (choose the
posture being set or preserved), or use the AFK modal cap; the inject hook then surfaces it as a
per-turn fact so the whole team paces to it. **Re-evaluate at each reset boundary** (the cheapest
re-plan moment) and on any sibling usage-stop. The budget is a function of usage over the reset clock,
not a fixed number - this is the self-healing loop: throttle to survive the window, burst to spend it.

## Choosing a rotation target

When choosing which account to rotate to, rank as follows - NOT by lowest current 5h %:

1. **Reset-proximity first** - prefer the account that resets SOONEST (both the 5h session
   and the weekly window). Over a long run, soonest-refill gives the most cumulative
   headroom, even if its current % is high.
2. **Count the weekly window, not just the 5h** - a low 5h % whose weekly is near-cap and
   resets days out is worse than a higher 5h % whose weekly resets in under a day.
3. **Never act on an unconfirmed old reading - re-confirm it LIVE** - the poller refreshes only
   the active account (one OAuth token on disk); non-active rows are last-known until re-read.
   Check `last_snapshot_age_ms`; if it is more than ~10min, do NOT present it as current and do
   NOT call the cred "stale" (there is no stale-cred state - see the no-stale law in `/cred-prep`).
   Get LIVE truth instead: for codex, `cred-live-probe.sh <idle-pane>`. Only then rank/decide.
4. **Exclude** exhausted accounts (5h >= 95% OR weekly >= 95%) and the active account itself.

`list_accounts` surfaces `weekly_pct` and `weekly_reset_at` beside the 5h fields for
exactly this; `recommend` is reset-proximity-aware. Common mistake: picking an account off an
unconfirmed old "0%" reading without a live re-confirm (live-probe for codex), when it is
actually exhausted and a different account resets soonest.

## Notes

- Render the account snapshot as a table (the format above), not prose.
- Multi-agent coordination spends divergent quotas across accounts; the more of the
  fleet's usage you can see, the better you can pace and route dispatch.

## DO NOT

- Don't poll on every tool call - the snapshot has 60s cache; over-polling wastes my own budget for no new signal.
- Don't ask the user "how much have I used" - the tool answers that. User asking IS the signal to invoke this skill.
- Don't include unreachable-from-worker accounts in the report - they pollute the Tuftian density.
