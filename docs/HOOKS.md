# Wiring `helm inject` into your harness

`helm inject` is the one active-fire surface: a hook calls it once per turn
with the prompt text; helm returns the context worth injecting — the pinned
lane (budget-capped), just-in-time typed-store matches, and live reflex
steers. No match, no output, no cost. The same call works from every harness,
which is what makes your knowledge fire wherever you work.

## The self-closing path: `helm hooks install`

For Claude Code you never hand-wire this — helm installs its own hook:

```console
$ helm hooks install            # every claude home + every seat, incl. ~/.claude
helm hooks: inject (UserPromptSubmit): timeout 10 /path/to/helm/bin/helm inject --hook-json || true
helm hooks: deliver (PostToolUse): timeout 2 /path/to/helm/bin/helm chat deliver --hook-json || true
helm hooks: join (SessionStart): timeout 5 /path/to/helm/bin/helm chat join --hook-json || true
  you-example-com              add    backup: none — new file
  (default-claude)             update backup: ~/.cache/helm/config-backups/…
helm hooks: seats (fleet delivery — deliver + join + stop-guard + resume-turn):
  codex                        add    backup: none — new file
helm hooks: 2 of 2 claude homes covered
helm hooks: 1 of 1 seats covered (fleet delivery)
$ helm hooks status             # per-home + per-seat coverage table, read-only
$ helm hooks install --dry      # the would-be diff per home/seat, nothing written
```

The installer merges the whole hook estate — 9 entries per home (one per
`hooks.SPECS` row; the count is test-pinned against that tuple) — into
each home's `settings.json`. Together they close the loop: turn start + tool
boundary + shell-argv gate + session start + idle gate + compaction
continuity + compaction resume.

| event | command | what it carries |
|---|---|---|
| `UserPromptSubmit` | `helm inject --hook-json` (timeout 10) | the per-turn context lane (pinned + JIT + reflexes) |
| `PostToolUse` (`*`) | `helm chat deliver --hook-json` (timeout 2) | the delivery lane plus silent delegation producer: @mentions nudge between tools; a documented subagent `agent_id` event at an exactly claimed lane records lease/session/holder-bound interval evidence |
| `SubagentStop` (`*`) | `helm chat delegation-stop --hook-json` (timeout 2) | tombstones that exact full-session/`agent_id` independently of claim-lock contention; the next claim-locked read rejects and prunes it, so teardown never races another agent's producer |
| `SessionStart` (`*`) | `helm chat join --hook-json` (timeout 5) | the autojoin: roster presence row + the seat's identity as session context (incl. the mandatory beacon-arm directive) |
| `SessionStart` (`*`) | `helm seat resume-turn --hook-json` (timeout 5) | the RESUME leg: on `source == "compact"` only, fork a detached child that waits out the composer settle and injects the seat's own handoff `NEXT:` back into its pane. If identity or delivery cannot be proven, the alert reports the measured recovery route: armed inbox beacon, pane-input fallback, or UNKNOWN — never an unconditional instruction. Source-gated inside the verb, not by the matcher — the wildcard group is the shape proven live across the estate |
| `Stop` | `helm chat stop-guard --hook-json` (timeout 5) | the idle gate: BLOCKS a stop on undelivered mentions/owner rows (once per pending-fingerprint — never an infinite loop), on claim leases held by the stopping session (except an exact live child or recent active-subagent event bound to the same room/lease/holder incarnation), or on **NO ARMED BEACON** — a launched fleet seat with no live `helm chat wait` process is a seat nothing can wake (once per beacon-loss episode; the block names the exact `Monitor(...)` call), or on a **REVIEW SPIRAL** — one lane review-dispatched at 3+ distinct tips in 12h is round three, which the store forbids by name, so the block quotes the literal `helm chat meld invite <peer> "<lane>: …"` cure (latched per lane+round-count; two rounds warn instead); WARNs when the outgoing count/SHA/proof/landed claim lacks this turn's matching measurement (one transcript snapshot binds the finding and latch identity; typed transcript metadata is ignored, malformed message envelopes surface a latched `CLAIM-EVIDENCE SKIPPED` rather than clean), and to arm the beacon on a clean stop. `stop_hook_active` suppresses blocks but still surfaces inbox + claim-evidence WARNs. Silently runs `helm index cap --apply` and the throttled, pressure-gated scratch reaper (`helm scratch gc`). Kill: `HELM_STOP_GUARD=0`, the 12 per-check `HELM_STOP_GUARD_*=0` switches (the complete register is the ENVIRONMENT.md table: `INBOX/CLAIMS/LEASE_TTL/DELEGATION/BEACON/SPIRAL/PUNT/WIRING/CLAIME/WHISPER/INDEX/NDP`), reaper `HELM_SCRATCH_GC=0` |
| `PreToolUse` (`Bash`) | `helm chat argv-guard --hook-json` (timeout 2) | the shell-substitution GATE on every Bash call: a chat/dispatch body composed as a double-quoted shell argument lets the SHELL execute backticked content before helm exists — measured three times in two days, once running `git clean` in the shared checkout from inside the message warning about it. Helm cannot see consumed backticks; this hook reads the Bash command BEFORE any shell runs, the one place they are visible. Also covers `git commit`/`tag -m` messages — the same hazard one surface out |
| `PreCompact` | `helm handoff check --hook-json` (timeout 5) | the continuity lane: capture the now-snapshot + nag when no handoff artifact exists, so the window that survives compaction never starts blind (see VERBS.md, the handoff contract) |
| `SessionEnd` | `helm handoff check --hook-json` (timeout 5) | the same continuity check on the last exit a session gets — the safety net for a session that ends without ever compacting |

**TWO rows are GATES, not lanes: `Stop` (stop-guard) and `PreToolUse` (argv-guard).** The seven other hooks are generated with `|| true` so a missing or wedged helm can never hold a turn. A gate cannot be: it returns **rc 2 to BLOCK**, and `|| true` rewrites that to 0 — the harness then lets the agent proceed anyway. Measured 2026-07-26, the stop-guard was returning 2 with **72 undelivered messages** while every turn ended cleanly, and the owner's report was exactly right: *"ive never once seen them actually fire to stop you from ending a turn"*. A gate is generated as `timeout N helm … ; rc=$?; case "$rc" in 2) exit 2 ;; 0) ;; 124) <alarm "THE GUARD TIMED OUT after Ns"> ;; 127) <alarm "THE GUARD IS MISSING — nothing executable at <path> … repair with: helm hooks install"> ;; *) <alarm "THE GUARD FAILED rc=$rc"> ;; esac; exit 0`, where every alarm ends "— this <stop|tool call> is ALLOWED and UNCHECKED" — it propagates the refusal, and every other outcome still exits 0 so a helm crash fails open. **The arm set is exhaustive on purpose.** The rc-124 leg exists because a `timeout` kill prints NOTHING, so an unchecked stop was indistinguishable from a clean allow. The rc-127 leg exists because "a crash at least leaves a traceback" was FALSE for the case that mattered: a hook whose helm path no longer exists exits **127**, which was neither 2 nor 124 and so fell straight through to `exit 0`. On 2026-08-04 eight settings files named a deleted lane room's `bin/helm`, both gates among them, and four credential homes ran unguarded in silence (see **helm_bin and the shared checkout** below). A guard that could not run is a strictly worse state than one that timed out, and only the timeout was reported. The `*)` default means the next unhandled code is loud by construction rather than after the next outage. Fail-open is right for a lane and fatal for a gate.

**AN ALARM IS TWO CHANNELS, AND STDERR IS NOT THE ONE THAT COUNTS.** Every arm above exits 0 by the fail-open law, and the harness contract is explicit about what that means: *"Stderr from a hook that exits 0 goes to the debug log only, never the transcript, and Claude never sees it."* So from 2026-08-04 until this was fixed, all three warnings — including the ones written to cure that very outage — were announced to nobody. An independent cross-family review found it, and the arms that were supposed to prove visibility could not: they captured the SUBPROCESS's stderr, which proves the text was written, not that anyone can read it. `_gate_alarm` now emits the same sentence on **both** channels — stderr for the debug log, and JSON on stdout, which is *"only processed on exit 0"* and is therefore available exactly when a gate fails open. `systemMessage` reaches the OWNER; `hookSpecificOutput.additionalContext` reaches CLAUDE, and both gate events carry it (Stop renders at the end of the turn, PreToolUse next to the tool result). **The exit code does not move** — that is the whole point: the guard still fails open, it just stops doing it in silence. Two rules for anyone editing this: the sentence is written ONCE and rendered to both channels (two hand-kept copies is how a hardcoded "this stop" ended up in front of a PreToolUse reader), and rc 0 / rc 2 emit NOTHING on stdout — a pass must stay quiet, and on exit 2 the harness IGNORES stdout and reads stderr instead, so emitting there would drop the refusal's reason entirely.

**helm_bin and the shared checkout.** The absolute path baked into every generated hook is the **shared checkout's** `bin/helm`, resolved through `work._lanes.find_root` (git's `--git-common-dir`), never `dirname(dirname(__file__))`. A seat running a hook-touching verb from its lane room would otherwise write that room's path into every credential home and every seat config — `_merge_event` is the one place that reaches all of them — and a lane room is deleted when its lane lands. Two independent rails hold this: `hooks.helm_bin()` **raises** rather than return a room path, and `hooks.refuse_lane_room_commands()` runs on the merged candidate **immediately before any write**, so a caller that composes its own command text (`record.py` does) still cannot persist one. The write guard refuses rather than repairs — silently rewriting an entry the merge law does not own would be a clobber — and it names the file and the offending path.

The two continuity entries OMIT a matcher on purpose: they must fire on EVERY
compaction and EVERY session end, never gated to one trigger. `helm handoff
check --hook-json` is FAIL-OPEN TOTAL — rc 0 always, silent when the contract
is satisfied, and it captures `_global/now.md` on the same trigger.

**Seats are part of the estate.** A full `install` (no `--home` filter) also
wires the **delivery lane** (`hooks.DELIVERY_SPECS`: `PostToolUse` deliver +
`SubagentStop` delegation-stop + `SessionStart` join + `SessionStart`
resume-turn + `Stop` stop-guard + `PreToolUse` argv-guard — **not** inject)
into every multimodel seat's isolated
`CLAUDE_CONFIG_DIR` (`<helm_home>/_global/seats/<family>/claude`), so a
launched codex/kimi/… seat receives `@<family>` and owner posts under its
family name (`seat launch` exports `HELM_CHAT_NAME=<family>`; a seat's own
settings are never touched) and cannot idle past NEW inbox rows — see the
`stop-guard` contract in VERBS for what that does and does not cover: the gate
blocks once per pending-fingerprint, so a re-stop on the SAME rows passes and a
seat CAN idle past a static inbox it has already been shown once.
`helm hooks status` prints a `seats (fleet delivery)` block plus
`seat delivery: N of M seats`, and `helm hooks install` reports
`N of M seats covered (fleet delivery)`.

Same laws for every entry: MERGE-preserving (existing hooks — `helm record`'s
PostToolUse/PostToolUseFailure legs, a seat's foreign hooks — and settings keys
are never clobbered), idempotent (re-install reports `ok`), on configs.py's
safety rails. Every mutation now uses **content-revision compare-and-swap**:
read exact bytes + opaque revision, re-derive the full domain merge from that
snapshot, write only with `expected_revision`, then re-read and verify the exact
committed revision. A pre-commit conflict or a foreign write immediately after
Helm's commit is preserved and retried from its newer bytes, at most three
attempts. Three conflicts refuse loudly; non-conflict storage/shape failures do
not retry. Backups remain recovery artifacts, but Helm never unconditionally
restores one over a revision another writer may have produced. Semantic no-ops
preserve the file's exact formatting.

`--home NAME` narrows to one home (and skips seats); `helm doctor` reports
`inject coverage: N of M claude homes` so a gap in HELM'S OWN ESTATE can't hide
— and that is the whole of the claim. Orca's `orca agent hooks on|off|status`
edits the same `settings.json` with overlapping UserPromptSubmit/Stop/PostToolUse
entries. A Helm-only advisory lock cannot bind that external writer, so locking
is not the correctness mechanism; exact-revision CAS is. A later intentional
Orca edit can still change the estate after Helm returns, so a green coverage
number is a read of the current file, not a perpetual ownership claim. And
`helm hooks status` adds per-home `deliver`/`join`/`stop`/`handoff`/`resume` columns
plus the seat block.

**Scope de-duplication is cross-file.** Claude loads user and project settings
together, so an owned hook in both `<CLAUDE_CONFIG_DIR>/settings.json` and
`<project>/.claude/settings.json` fires twice even though each file is internally
clean. `hooks status`, `hooks install`, and `doctor` scan the current checkout and
every identity-verified live seat's checkout for this pair. A confirmed pair is
printed as `cross-scope DUPLICATE`; unreadable roster/settings evidence is
`UNKNOWN`, never clean. Helm reports but does not delete either entry: a project
hook may be deliberate, and choosing which authored scope to remove is a judgment
rather than an installer merge. The scan is bounded by current/live projects; it
does not walk the historical project registry.

The generated command pipes the hook's FULL JSON to `helm inject --hook-json`,
which extracts the prompt, derives `--project` from the hook's `cwd` (longest
registry-path prefix; global-only when no project claims it), and stamps the
`session_id` onto the fire-ledger row. It is fail-open by construction:
`timeout` so a wedged helm can never hold a turn, `|| true` so a missing or
failing helm injects nothing instead of blocking.

`helm hooks install --harness codex` reports honestly: the codex notify-hook
recipe below is not yet mechanical, so codex stays hand-wired for now.

### Project scope: `helm hooks install --project DIR`

Not every operator wants the physics machine-wide. `--project` installs the
same full spec set into `DIR/.claude/settings.local.json` instead of any
claude home: sessions launched in that project get inject, the delivery lane,
the stop-guard and the handoff contract; every other session on the machine
stays hook-free. Same CAS pipeline, same merge-preservation laws, same beacon
permits — with one deliberate delta: the scalar estate defaults are **not**
seeded, because a project file is neither a home nor a seat and helm does not
own a project's other settings.

`settings.local.json`, deliberately: the generated commands carry this
machine's absolute helm path, so the file is per-machine and must never ride
a commit into someone else's checkout. Add `.claude/settings.local.json` to
the project's `.gitignore` — the installer reminds you, and never edits a
repo's ignore file itself. Only *new* sessions pick the hooks up (the harness
snapshots hook config at session start). `helm hooks status` reports
project-scoped wiring in its project-scopes lines, and flags a spec loaded at
both project and active-home scope as a cross-scope duplicate.

## The contract

```console
$ echo "<the user's prompt text>" | helm inject [--project <name>]
PREMISE naming-extremes: metaphors live at the extremes only ...
TERM drain: routing raw memory intake to typed homes ...
REFLEX: checkpoint the green slice
```

stdout is the injection payload (may be empty). `--json` returns
`{"pinned": [...], "jit": [...], "reflex": [...]}` for hooks that want
structure. `--hook-json` reads the harness hook's full JSON on stdin instead
(`prompt` / `cwd` / `session_id`, unknown keys tolerated; malformed JSON
injects nothing, rc 0). Without `--hook-json`, resolve `--project` from your
registry name (`helm projects`) yourself.

## Manual recipes (the fallback appendix)

### Claude Code

What `helm hooks install` writes, if you'd rather wire it by hand in one scope
(user **or** project `settings.json`, never both for the same event):

```json
{
  "hooks": {
    "UserPromptSubmit": [{
      "hooks": [{
        "type": "command",
        "command": "timeout 10 /path/to/helm/bin/helm inject --hook-json || true"
      }]
    }]
  }
}
```

Hook stdout becomes `additionalContext` automatically. The prompt-only
variant (`jq -r .prompt | helm inject --project myproject`) still works but
loses the cwd-derived project scope and the session on the ledger.

### Codex

`UserPromptSubmit`-equivalent notify hook: run `helm inject` with the prompt
text on stdin; return the output via `hookSpecificOutput.additionalContext`.
Keep it sparse — codex renders injected context as a visible developer
message.

### OpenCode

An `@opencode-ai/plugin` with `experimental.chat.system.transform`: shell out
to `helm inject`, append the lines to `output.system`. Feature-check the
experimental namespace and fail open (empty) if it moved.

### Hermes

A `pre_llm_call` plugin hook returning `{'context': <helm inject output>}` —
appended to the current turn, preserving the cached system-prefix.

## Rules of the road

- **Fail open.** A hook that cannot run helm must inject nothing, never block
  the turn. Wrap in `|| true` (the installer also adds a `timeout` guard).
- **Budget is helm's job.** The pinned lane is byte-capped and JIT is capped
  at 4 entries; hooks should not add their own truncation.
- **Per-project scoping.** `--hook-json` derives it from the turn's cwd; in
  manual wiring pass `--project` when the session's cwd maps to a registry
  project. Global entries fire everywhere, project entries only where they
  belong.
