---
name: fleet-maintenance
description: The fleet-maintenance runbook — the SOP for any agent holding the maintenance seat over a helm fleet. Use when a seat looks stalled/dark/dead, a proxy or pane needs relaunch, a cred ran dry mid-work, the owner asks "is the fleet healthy?", before ANY kill/relaunch/swap/inject of a sibling, or when standing up a periodic fleet health check. Triggers: "seat X is quiet", "pane died", "proxy down", "is anyone stalled?", "restart the fleet", "rescue this lane".
---

# fleet-maintenance — keep the fleet alive without destroying its work

The two failure modes are asymmetric: **false-dead destroys in-flight work;
false-alive only waits.** Every rule below exists because one side of that
asymmetry burned us (a rescue-commit once destroyed a live security lane).

## 1. Stall triage — authoritative vs artifact signals

Split every liveness signal into two classes before concluding ANYTHING:

| AUTHORITATIVE (act on these) | ARTIFACT (never conclude from these) |
|---|---|
| proxy down / not serving traffic (`helm seat status`) | file mtime ("quiet for hours") |
| harness death-notification (orca/herdr fires on completion AND death) | dirty worktree ("left work behind") |
| `sessions/<pid>.json` in the seat's config dir | no recent commits |
| chain/commit watermark stalled **on a lane you OWN the deadline for** | "no recent commits" (any other lane) |

- Harness completion-notification **silence is POSITIVE evidence of a running
  agent** — a reviewer mid-task is SUPPOSED to hold an incoherent tree.
- On an **authoritative failure**, intervene FAST but match the verb to WHAT
  died: a dead **proxy** (agent process still alive) is fixed by respawning the
  proxy (`helm seat doctor --ensure`, §2 rung 1) — ZERO agent state lost; only a
  dead **agent** warrants a resume. Use `helm seat where` to tell them apart.
  Do not wait politely — but do not relaunch a live agent whose only casualty
  was its proxy.
- On **artifact-only quiet** with authoritative signals silent: ASK the owner
  of the work (`helm chat post` to its room) or drive a parallel slice. Never
  rescue. `helm work gc` is for lease-less orphan rooms, never a live owner's tree.

Canon: `proactive-resume-on-authoritative-stall`, `process-death-is-not-state-death`,
`a-dirty-tree-is-intentional-until-its-owner-says-otherwise` (all 1.00, helm store).

## 2. Intervention ladder — state-preserving first

Match the rung to the DISEASE — each preserves as much state as its disease
allows (NOT a strict order: a dead proxy is cheaper to fix than a dead agent,
and `/clear` deliberately discards context a resume would keep).

1. **Proxy respawn** (`helm seat doctor --ensure`) — proxy dead but the agent
   process is ALIVE (confirm with `helm seat where`): respawns ONLY the proxy,
   ZERO agent state lost incl. mid-turn context. The cheapest rescue there is —
   never resume a live agent whose only casualty was its proxy.
2. **In-place recovery** — 400 ctx-overflow → inject `/clear` in the pane
   (`ctx-window-recovery-is-clear`); `helm seat autocompact` pre-empts at ~90%.
   `helm watchdog` DETECTS the wedge (a2a alert) — the /clear is interactive,
   never forged by a background process. (Discards conversation context — but a
   400 already made it unrecoverable.)
3. **`helm seat resume <seat>`** — the default rescue when the AGENT PROCESS
   died. Relaunches via the detected metaharness with `claude --resume/--continue`:
   context + worktree survive, ~0 work lost. What a human dev does when
   claude-code dies.
4. **Kill-first reseed** (`helm seat spawn <seat>`, `--print` to dry-run) —
   only when CONTEXT was the disease (corrupt/poisoned seat). Spawn self-reap
   can miss a live pane → kill first or you get a double seat. Fresh spawn
   loses everything in-context; reserve it.
5. **Never** a destructive rescue-commit on an artifact signal.

Before ANY kill/relaunch/swap/inject (fleet rule, no exceptions):
- **Intent-log first**: post what you're about to do and why to the room.
- **Confirm transcript persistence**: `helm session doctor` — UNKNOWN is not
  proof of loss, but do not kill until the transcript is proven persisted. If
  UNKNOWN PERSISTS (never resolves to persisted), snapshot the pane scrollback
  and escalate to the owner before acting — a permanent UNKNOWN must not
  deadlock the maintainer.
- **Verify after**: `helm seat where <seat>` (pid/handle/liveness) + a beacon
  post from the reborn seat. A dropped resume must be CAUGHT, not silently lost.
- Codex panes are **DISPOSABLE, transcripts sacred** — reseed with better
  settings rather than nursing a sick pane; preserve gpt-5.5 model pin on
  codex restart.

## 3. Ground truth — what is the fleet, actually

Never from memory. A row you can't prove is UNKNOWN, not alive.

```
helm chat seats        # live roster census: freshness, pending, working-lane, claims
helm who               # pid→cred attribution: every live claude/codex process
helm seat status       # per-family proxy up/down, pid, port, cred validity
helm seat doctor       # read-only seat health: binaries, creds, autocompact/context%
helm session ls|doctor # persistence tri-state (persisted/UNKNOWN — not proof of loss)
helm doctor            # global estate health, read-only
```

**Live composition + watchdog verbs (landed — cite the SHA when you invoke them):**
- `helm fleet` (c4507e7) — composition ground-truth: every live claude process
  → seat/sid/daemon/stamps/home, all live-probed; probe failure gates the
  verdict, fails closed (a row it can't prove is UNKNOWN, never alive).
  ANSWER FLEET-COMPOSITION QUESTIONS BY RUNNING THIS, never from memory.
- `helm seat doctor --ensure` (b028672) — proxy watchdog: auto-respawns a
  silently-dead proxy, startup-grace so it never SIGTERMs a booting one. The
  systemic fix for the codex-2 silent-starvation class. Wired to a `*/3 * * * *`
  cron for continuous supervision.
- **Proxy-CPU canary** (0aadbed, in `helm seat doctor` output) — a proxy pid at
  sustained-high CPU while siblings idle = a THRASHING backend, the leading
  indicator BEFORE it goes silent. WARN-only (never kills), startup-graced.

## 4. Fail-loud health — verify work HAPPENS, not reachability

Reachability-as-healthy is the vacuous-green class (dregg ran 4h silently
unsigned; transport_status said "signed" on a GET that proved nothing). A
health check must verify a **work watermark advanced** — a commit landed, a
chain/beacon watermark moved, a probe round-tripped through the actual signing
path — never just "port answers". If the check can pass while the fleet
produces nothing, it is not a health check.

`helm rearm` is the land-to-live leg: after landing code, report which
long-lived processes still hold pre-HEAD code; `--apply` SIGTERMs only stale
waiters. `--apply` IS a kill — the §2 pre-kill gates (intent-log + transcript
persistence) apply to it too.

## 5. Cred exhaustion — the swap leg

```
helm creds             # live headroom scorecard, all providers
helm swap <home|email> # seat ran dry: print resume-under-healthier-account blocks
helm codex pooled|capacity   # proxy cred pool falls through usage caps
```

Swap the cred, resume the SAME session under the healthier account — cred
rotation and session identity are decoupled. A "STALE" claude cred is a stale
usage snapshot, not auth death (creds are 1-year auths).

## 6. Cross-family gating

A fix's reviewer must be a **different model family** than its author (load
`/x`). Route gates in **parallel across families** — never serialize the whole
fleet's merges on one family's remaining budget.

## 7. Re-grounding a confused (not dead) seat

`cv pack "<task>"` compiles a context bundle from the whole session corpus;
`cv show <id> --find TERM` reads what a prior seat knew (see `/recall`).
Pane-level control (read/wait/send keystrokes, spawn in worktrees)
is orca's: use the `orca-cli` skill primitives — don't reinvent PTY plumbing.

## Prior art

Fresh helm-native write; reboot tiers, monitored-pair relaunch, and
cred/session decoupling are inherited as principles. Seat-level
mechanics: the `seat-relaunch-playbook` maintenance memory.
