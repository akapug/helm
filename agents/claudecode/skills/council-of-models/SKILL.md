---
name: council-of-models
description: Run /com, the council-of-models primitive for intra-agent, intra-turn multi-model synthesis. Use when the user invokes /com, asks for a panel of models beneath one acting agent, or requests a Hermes MoA-style proposer set where Claude/Codex/OpenCode proposers produce independent drafts and the current agent synthesizes the final answer.
---

# /com - Council Of Models

`/com` is a stored primitive for a panel of models inside one agent turn. It is
not an A2A council or huddle. Those coordinate multiple agent sessions through
shared comms (helm chat / the cell substrate). `/com` runs proposer models over
one focused brief, then the acting agent synthesizes the answer at the end of
the same turn.

## Invariants

- Use the built-in preset table (see Presets below). If a requested preset is
  missing or malformed, fail open to the built-in `duo` preset: one Claude
  proposer and one Codex proposer.
- Give proposers a focused brief: the user question, the minimal local facts they
  need, and the requested output shape. Do not replay the full conversation,
  tool logs, or hidden/system context.
- Proposers are tool-less. Claude proposers run as Task subagents with the model
  pinned and `tools: []`. Codex/OpenCode proposers run headless through their
  CLIs' one-shot exec mode (see Run Loop step 4).
- The acting agent is always the aggregator. Do not spawn a separate aggregator
  model, do not nest `/com`, and do not call raw provider APIs.
- Synthesize from proposer blocks at the tail of the current turn. Keep proposer
  output ephemeral unless the preset or invocation requests `--record`.
- Fail soft and loud: a failed proposer becomes a labeled unavailable block. Keep
  synthesizing from the remaining proposers; never silently drop it.

## Run Loop

1. Parse invocation options:
   - `--preset NAME`: select the named preset from the Presets section;
     otherwise use the default (`duo`).
   - `--record`: record the panel digest and synthesis even when the preset has
     `record = false`.
   - `--no-record`: keep the panel ephemeral even when the preset records.
2. Load the preset. If the named preset is missing, say
   `COM DEGRADED: preset unavailable` and use `duo`.
3. Build one focused brief for all proposers:
   - task/question;
   - relevant files, commits, or helm store/chat anchors already read;
   - explicit constraints and output contract;
   - instruction to answer independently and avoid deference to other models.
4. Run proposers independently:
   - `driver = "claude"`: start a Task subagent with `model` from the preset,
     `tools: []`, and the focused brief. If model pinning or tool restriction is
     unavailable, return a labeled unavailable/degraded block.
   - `driver = "codex"`: write the focused brief to a temp file and run the
     codex CLI headless (verified path, re-probed 2026-07-22, codex-cli 0.144.5):

```bash
codex exec --cd "$PWD" -s read-only --skip-git-repo-check --ephemeral \
  --model <model> -o "$answer_file" "$(cat "$brief_file")" </dev/null
```

     Read the answer from the `-o` file only; stdout is a noisy event log,
     never parse it.
   - `driver = "opencode"`: unverified best-effort,
     `opencode run -m <provider/model>` over the same brief. On any failure
     (tool missing, nonzero exit, timeout, empty answer) return a labeled
     unavailable block rather than aborting the panel.

5. Normalize each result as:

```text
[proposer <index>: <driver>/<model> status=<ok|unavailable>]
<answer or unavailable marker>
```

6. Synthesize only after all proposer blocks return or fail. State the answer,
   incorporate dissent explicitly, and name any degraded proposer in a short
   receipt.
7. If recording is enabled, append a compact `COM_RECORD` to the same durable
   surface that asked for it when one is obvious. Record the preset, proposers,
   unavailable blocks, and final synthesis. Do not dump full proposer transcripts
   unless the caller requested them.

## Presets

Built-in presets:

- `duo` (default): cross-family panel, one Claude proposer (`opus` as the Task
  model) plus one Codex proposer (`gpt-5.5`).
- `claude-trio`: same-family fallback when Codex is unavailable or not worth the
  spend — `opus`, `sonnet`, `opus` as three independent Task subagents.

Treat unknown drivers as unavailable rather than inventing a path.

## Output Shape

For ordinary `/com` use, return the synthesized answer first and keep the receipt
short:

```text
COM receipt: preset=<name>; proposers=<driver/model,...>; degraded=<none|...>; record=<on|off>
```

For review or decision work, preserve the caller's requested verdict shape. The
panel is an input to the acting agent's judgment, not a substitute for the final
answer.
