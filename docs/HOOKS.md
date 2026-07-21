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
helm hooks: seats (fleet delivery — deliver + join):
  codex                        add    backup: none — new file
helm hooks: 2 of 2 claude homes covered
helm hooks: 1 of 1 seats covered (fleet delivery)
$ helm hooks status             # per-home + per-seat coverage table, read-only
$ helm hooks install --dry      # the would-be diff per home/seat, nothing written
```

The installer merges the whole hook estate — four entries per home — into
each home's `settings.json`. Together they close the loop: turn start + tool
boundary + session start + idle gate.

| event | command | what it carries |
|---|---|---|
| `UserPromptSubmit` | `helm inject --hook-json` (timeout 10) | the per-turn context lane (pinned + JIT + reflexes) |
| `PostToolUse` (`*`) | `helm chat deliver --hook-json` (timeout 2) | the delivery lane: @mentions + owner posts nudge an agent BETWEEN tool calls (see VERBS.md, the delivery lane) |
| `SessionStart` (`*`) | `helm chat join --hook-json` (timeout 5) | the autojoin: roster presence row + the seat's identity as session context (incl. the mandatory beacon-arm directive) |
| `Stop` | `helm chat stop-guard --hook-json` (timeout 5) | the idle gate: BLOCKS a stop on undelivered mentions/owner rows (once per pending-fingerprint — never an infinite loop) or on claim leases held by the stopping session; WARNs to arm the beacon on a clean stop; silently runs `helm index cap --apply`. Kill: `HELM_STOP_GUARD=0`, per-check `HELM_STOP_GUARD_INBOX/CLAIMS/INDEX=0` |

**Seats are part of the estate.** A full `install` (no `--home` filter) also
wires the **delivery lane** (`PostToolUse` deliver + `SessionStart` join +
`Stop` stop-guard — **not** inject) into every multimodel seat's isolated
`CLAUDE_CONFIG_DIR` (`<helm_home>/_global/seats/<family>/claude`), so a
launched codex/kimi/… seat receives `@<family>` and owner posts under its
family name (`seat launch` exports `HELM_CHAT_NAME=<family>`; a seat's own
settings are never touched) and cannot idle past its inbox.
`helm hooks status` prints a `seats (fleet delivery)` block plus
`seat delivery: N of M seats`, and `helm hooks install` reports
`N of M seats covered (fleet delivery)`.

Same laws for every entry: MERGE-preserving (existing hooks — `helm record`'s
PostToolUse leg, a seat's foreign hooks — and settings keys are never
clobbered), idempotent (re-install reports `ok`), on configs.py's safety rails
(backup → validate → atomic write, re-parsed after the write, backup restored
on any failure). `--home NAME` narrows to one home (and skips seats); `helm
doctor` reports `inject coverage: N of M claude homes` so a gap can't hide, and
`helm hooks status` adds per-home `deliver`/`join`/`stop` columns plus the
seat block.

The generated command pipes the hook's FULL JSON to `helm inject --hook-json`,
which extracts the prompt, derives `--project` from the hook's `cwd` (longest
registry-path prefix; global-only when no project claims it), and stamps the
`session_id` onto the fire-ledger row. It is fail-open by construction:
`timeout` so a wedged helm can never hold a turn, `|| true` so a missing or
failing helm injects nothing instead of blocking.

`helm hooks install --harness codex` reports honestly: the codex notify-hook
recipe below is not yet mechanical, so codex stays hand-wired for now.

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

What `helm hooks install` writes, if you'd rather wire it by hand
(user or project `settings.json`):

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
