# Wiring `helm inject` into your harness

`helm inject` is the one active-fire surface: a hook calls it once per turn
with the prompt text; helm returns the context worth injecting — the pinned
lane (budget-capped), just-in-time typed-store matches, and live reflex
steers. No match, no output, no cost. The same call works from every harness,
which is what makes your knowledge fire wherever you work.

The contract:

```console
$ echo "<the user's prompt text>" | helm inject [--project <name>]
PREMISE naming-extremes: metaphors live at the extremes only ...
TERM drain: routing raw memory intake to typed homes ...
REFLEX: checkpoint the green slice
```

stdout is the injection payload (may be empty). `--json` returns
`{"pinned": [...], "jit": [...], "reflex": [...]}` for hooks that want
structure. Resolve `--project` from your registry name (`helm projects`).

## Claude Code

`UserPromptSubmit` hook (user or project `settings.json`):

```json
{
  "hooks": {
    "UserPromptSubmit": [{
      "hooks": [{
        "type": "command",
        "command": "jq -r .prompt | /path/to/helm/bin/helm inject --project myproject"
      }]
    }]
  }
}
```

Hook stdout becomes `additionalContext` automatically. (Without `jq`, any
one-liner that extracts `.prompt` from the hook's stdin JSON works.)

## Codex

`UserPromptSubmit`-equivalent notify hook: run the same command; return the
output via `hookSpecificOutput.additionalContext`. Keep it sparse — codex
renders injected context as a visible developer message.

## OpenCode

An `@opencode-ai/plugin` with `experimental.chat.system.transform`: shell out
to `helm inject`, append the lines to `output.system`. Feature-check the
experimental namespace and fail open (empty) if it moved.

## Hermes

A `pre_llm_call` plugin hook returning `{'context': <helm inject output>}` —
appended to the current turn, preserving the cached system-prefix.

## Rules of the road

- **Fail open.** A hook that cannot run helm must inject nothing, never block
  the turn. Wrap in `|| true`.
- **Budget is helm's job.** The pinned lane is byte-capped and JIT is capped
  at 4 entries; hooks should not add their own truncation.
- **Per-project scoping.** Pass `--project` when the session's cwd maps to a
  registry project; global entries fire everywhere, project entries only
  where they belong.
