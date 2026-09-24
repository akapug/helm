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
  systemic fix for the silent proxy-starvation class. Wired to a `*/3 * * * *`
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

Putting a project on the account the owner names — a lead on its own credhome,
its codex partner pinned to one pooled account — is the `seat-a-project` skill,
which every agent may run.

## 6. Cross-family gating

A fix's reviewer must be a **different model family** than its author (see
`reviewer-implements-own-findings`). Route gates in **parallel across families** — never serialize the whole
fleet's merges on one family's remaining budget. The families are equal
counterparts, not a writer tier and a witness tier: a reviewer of any family
commits its own mechanical cures off the reviewed tip and records them with
`--patch-tip`, so lanes routinely carry several authors. The gate that stays
non-negotiable is the RE-READ — someone who wrote none of the composed tip
reads it once before the land gate.

## 7. Re-grounding a confused (not dead) seat

`cv pack "<task>"` compiles a context bundle from the whole session corpus;
`cv show <id> --find TERM` reads what a prior seat knew (see the `sessions`
skill). Pane-level control (read/wait/send keystrokes, spawn in worktrees)
is orca's: use the `orca-cli` skill primitives — don't reinvent PTY plumbing.

## 8. Seats ALIVE but INVISIBLE — the daemon-orphan case

The owner reports *"my panes were killed and left at a command prompt after an
orca upgrade."* **Usually false, and believing it is the expensive mistake.**

An orca upgrade can bump the terminal DAEMON PROTOCOL (v26 -> v28, 2026-07-27,
skipping v27). It starts a new daemon and **never reaps the old one**. Old
daemons keep running with their PTYs and every agent inside them; the new UI
cannot render another generation's sessions, so the operator sees bare shells.

MEASURE, never infer — orca's own log and procfs:

```
grep -c '"event":"startup"' ~/.config/orca/logs/daemon.log  # vs "shutdown" = leak count
ls ~/.config/orca/daemon/*.sock                              # one per live generation
ps -o stat=,tty=,time= -p <seat pid>                         # Sl+ w/ real CPU = ALIVE
stat -c %y <transcript>.jsonl                                # modified seconds ago = WORKING
grep session-killed daemon.log | grep <restart ts>           # ZERO = nothing was killed
```

2026-07-27: 13 startups / 5 shutdowns, four daemons alive across a week
(v23/v24/v26/v28), 15 leaked `/tmp/.mount_orca-*` mounts, and on restart the
new client sent `client-hello` to ALL FOUR — all accepted.

**`helm chat` reaches an orphaned seat even when orca cannot render it** —
delivery does not go through a pane. A roll-call post is the fastest liveness
proof available; run it BEFORE any process forensics. On 2026-07-27 four seats
answered in two minutes, one mid-rebase.

**"Wedged" is a measurement, not a vibe.** Two seats were called wedged by two
different agents that day; both were `Sl+` with live TTYs and transcripts
modified within 60 seconds. An unanswered roll call means unanswered.

**STEP 0 — TWO HANDLES, TWO DIFFERENT FACTS. Do not join them casually.**

```
tr '\0' '\n' < /proc/<pid>/environ | grep ORCA_TERMINAL_HANDLE   # LAUNCH-TIME
orca terminal list --json                                        # LIVE daemon view
```

The environ handle is set once at exec and **frozen for the life of the
process**. It answers "what pane did this process START in" — historical, and
it CANNOT change if a pane is later re-adopted. So it can never disconfirm
re-adoption: it returns the same value either way, which by
`a-probe-that-cannot-say-otherwise-is-not-a-measurement` makes it useless for
that question. The list answers "what does the daemon serve NOW". A check that
reads one and reports about the other is the same error as running `tty` inside
a Bash-tool subshell and concluding the SESSION has no terminal.

**`orca terminal show` REQUIRES THE FULL HANDLE, and its error collapses.**
`terminal_handle_stale` is returned for at least three indistinguishable
conditions: genuinely stale, never existed, and **truncated/malformed**. A
healthy pane returns it when given a shortened handle. Copying an abbreviated
handle out of chat and passing it to this verb produces a confident false
"dead" — it happened twice on 2026-07-27, and the second time inside the
verification of the first. Resolve the full handle from `orca terminal list`;
never retype one. Treat the OUTPUT SHAPE as the answer (a `handle:`/`ok:true`
body = resolved), and remember "unresolved" does not tell you WHY.

**Title + cwd is not identity.** The case cited fleet-wide as proof that orca
re-adopts panes across generations was a title match — a listed terminal whose
title and cwd resembled the seat's. That join is two mutable strings, not an
identity, and a correct code trace (`_repair_orca_handle`, seat.py:2702) was
then produced to explain a handle change nobody had established. A right
mechanism makes a phantom MORE convincing, not less.

**THE SOUND INSTRUMENT — NONCE-SCAN. Bind a pane to a process by observation.**

```
printf '\n%s\n' "$NONCE" > /dev/pts/N        # display-only; never enters stdin
for h in $(orca terminal list --json | grep -o 'term_[a-f0-9-]\{36\}' | sort -u); do
  orca terminal read --terminal "$h" --limit 80 | grep -q "$NONCE" && echo "$h"
done
```

Immune to every trap above: handles come from orca's own list (never typed, so
truncation cannot bite), it needs no title/cwd join, and it returns a DIFFERENT
answer under each hypothesis. 44 terminals scan in ~40s. Prefer asking the seat
over `helm chat` to echo it — writing to someone else's pts is display-only and
safe, but the seat doing it is cleaner and proves the seat is responsive too.

**NEVER PUBLISH THE NONCE.** `terminal read` returns a ~39-line VIEWPORT, and
every seat renders the chat it reads — so a nonce announced in a room can appear
in the pane of any seat that read the message, binding nothing. One case was
invalidated exactly this way: the seat posted "NONCE: <value>" to the room
before the scan. Write it to the pts (or have the seat echo it) and tell the
room only that it is out.

**A HIT IS PROOF; A NULL IS NOT DISPROOF.** The viewport is ~39 lines of CURRENT
screen, not scrollback, and a claude TUI repaints continuously — so the string
is gone in under a minute. Measured: a nonce found twice at `--limit 500` was
unfindable at every limit minutes later; depth is not the variable, TIME is.
Read within seconds, and on a null RE-ISSUE rather than concluding. Always run
a same-read positive control (grep a string you know is on that screen) so you
can tell "not there" from "read is blind". Scoring a late null as "not this
pane" is how a proven-live seat gets rescued.

**BETTER, WHEN AVAILABLE — `lsof /dev/pts/N`** (spiral-claude-2): lists the
processes holding that terminal directly. No timing window, no wire, no TUI
repaint, nothing to contaminate. Prefer it; the nonce is the fallback for when
you need to bind a pts to an orca HANDLE rather than to a pid.

**RE-ADOPTION IS REAL — MEASURED, and it is why you must not remediate on a
missing handle.** 2026-07-27: of 14 live claude processes, four had environ
handles absent from `orca terminal list`. All four were then proven, with nonces
that were never published, to be served under a NEW handle:

```
seat-a (2613935) env term_655bf965 -> term_e5f4f739   double-blind, see below
seat-b (925233)  env term_82e33b93 -> term_9d54bd26
seat-c           env term_7a3fdcf0 -> term_c9aa4f24
seat-d (230219)  env term_3dc1e5ca -> term_71ba8bd2   re-run clean after the
                                                      contaminated first attempt
```

14 processes, 14 rendered, **ZERO invisible**. The first row is the strongest
binding produced: one seat wrote the nonce to another seat's pts without knowing
whose it was, and that seat found the foreign string in its own pane and
reported it before either knew they were describing the same event. Neither
could shape it toward a hypothesis, and nothing was ever on the wire.

Those seats were an hour from being "rescued" — remediation would have killed
live PTYs to fix a problem that had already fixed itself. **A missing environ
handle is not evidence of anything; re-adoption is the normal outcome, not the
exotic one.**

**ASK HELM FIRST — `orcaadopt.resolve(<seat>)` ALREADY KNOWS THE LIVE HANDLE.**

```python
from helm import orcaadopt
orcaadopt.resolve("seat-c")   # seat/provenance/state/evidence/pids/handle/pane_key
```

Verified against an independent nonce measurement: exact match. `helm seat
resume <seat>` uses it (`seat.py:2384`), so orca-launched claude panes ARE
resumable by helm — **`helm-cannot-resume-orca-launched-claude-pane-seam-gap`
is CLOSED for any seat it resolves**, and the hand-rolled `/proc/<pid>/cmdline`
rebuild that premise recommends is now the fallback, not the method.

Its limit, measured 4/4: resolve matches on the helm seat name in the process
environ, so a seat whose chat name never entered `HELM_CHAT_NAME` returns
unresolved however alive it is. **Unresolved is not dead** — it means there is
no name-to-process binding, which is exactly when the nonce scan above earns
its keep. Full detail: `docs/ORCA_OPERATIONS.md`.

**Remedy is RELOCATION, not rescue.** The agent is fine, it is in the wrong
daemon: roll-call -> have dirty seats COMMIT (uncommitted diffs are the ONLY
real loss risk) -> §2 gate -> kill -> `helm seat resume <seat>`.

**An invisible seat is not a broken seat.** `helm chat` reaches it, it works,
it lands code. The ONLY thing a dead pane costs is the owner's ability to SEE
it — which is exactly what the owner said matters, so it is worth fixing, but
fix it as a visibility problem and never as a rescue. The trade is explicit:
leave it alive and unseen, or stop+create and lose the in-flight turn.

TWO TRAPS, both hit live:
- **Resume scatters the fleet.** `helm seat resume` relocates a seat out of the
  shared checkout into `helm-wt/seats/<seat>`; newer orca renders every worktree
  as its own sidebar entry, so isolation becomes invisibility. To group them,
  `orca terminal create --worktree path:<main> --command "sh <relocate.sh>"`,
  with that script written 0600 from the live environ — never the bearer token
  as an argv `NAME=value`. Say the tradeoff out loud: it returns seats to the
  shared checkout helm isolates them from on purpose.
- **You will mint duplicates.** Resume creates a pane in `seats/`, a manual
  create makes another in main. Census `pgrep -x claude` + `/proc/<pid>/cwd` +
  `HELM_CHAT_NAME` BEFORE and AFTER; kill the loser deliberately.

NOT at risk: context (transcript on disk, `--resume` restores it), worktrees,
claims, verdicts, the ledger. AT RISK: uncommitted diffs and the in-flight turn.
**"N hours of context will be lost" is wrong and contradicts
`process-death-is-not-state-death`. Read the runbook before improvising —
`fleet-changes-runbook-first`; this situation is virtually never new.**

Upstream: `stablyai/orca#9138` (macOS, same mechanism), `#10415` (protocol bump
strands sessions).

**TWO OF OUR PRs ARE NOW MERGED AND SHIPPING — stop expecting the symptoms.**
Verified in the 1.4.162 tree on 2026-07-30:

```
c0734f039d  fix(terminal): disarm stale TUI modes ... (#9608)
2a640abfbe  fix(pty): strip inherited Claude child-session stamps at spawn (#9961)
```

So the `35;8;45M...` spew on hovering a dead pane is FIXED upstream, not a
standing wart — this section told readers to expect it, which is now a lie that
would make a real regression look normal. The pty fix is the root-cause
upstreaming of `child-stamp-kills-seat-persistence`.

**UPGRADING IS THE LOW-RISK MOVE, AND THE RISK IS VISIBILITY, NEVER LOSS.**
Measured across the 28->30 bump on 2026-07-30: 15 seats before, 15 after, every
one present by name. `daemon-init.ts:718` states the property this rests on —
"retire an empty daemon (live sessions reject it and survive)" — which is why
old generations linger: they are HOLDING your agents, correctly refusing to
die. Upgrade while ONE generation is live rather than after several stack; the
cost of waiting is a bigger visibility gap, not a smaller one.

## Prior art

Fresh helm-native write (an earlier harness's maintenance skill and its
credential-rescue runbook are the ancestors — reboot tiers,
monitored-pair relaunch, cred/session decoupling all inherited as principles;
their verbs were specific to that harness, so none port literally). Seat-level
mechanics: the `seat-relaunch-playbook` maintenance memory.
