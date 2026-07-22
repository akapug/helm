---
name: sessions
description: Use when an agent needs to MOVE, COPY, REHOME, or CROSS-HARNESS-PORT an existing agent session — "rehome this session to this project", "clone a session for a parallel lane", "port this to codex", "resurrect an expert session as a team member". Wraps clustervision (cv) session manipulation with enforced safety rails (dry-run first, original untouched, verify before resume).
license: MIT
metadata:
  author: mc
  version: "1.0.0"
---

# Sessions (RSH)

## Overview

Agent sessions are first-class, manipulable objects: you can rehome one to a new working directory,
clone it for a parallel lane, or port it to another harness — and cv (clustervision) is the mechanism
that makes it possible. This skill is the safe front door: `scripts/mc-session.sh` wraps cv's
`port`/`convert`/`export` with the rails you want around anything that touches session storage.

**The three rails (enforced, not just advised):**
- **Dry-run first.** Every copy/convert defaults to a DRY-RUN — cv writes a preview under a scratch dir
  (`--out`), real storage untouched. You SEE the new session id, the resume command, and which context
  files carry, before committing. Pass `--apply` to write for real.
- **Original untouched.** `cv port`/`cv convert` are COPIES (they mint a NEW session id), never moves.
  After `--apply` the wrapper ASSERTS the original still exists with an unchanged cwd — a loud abort if not.
- **Verify before resume.** After `--apply` it confirms the new session resolves, then prints the resume
  command. You never resume a session that didn't actually land.

## Use it

```bash
# rehome a session into THIS project (dry-run, then apply)
scripts/mc-session.sh rehome <id> --to-dir "$PWD"
scripts/mc-session.sh rehome <id> --to-dir "$PWD" --apply

# clone a session for a parallel lane (e.g. a worktree) — an independent copy
scripts/mc-session.sh clone <id> --to-dir ../myrepo-worktrees/lane-b --apply

# cross-harness port (claude -> codex, etc.)
scripts/mc-session.sh port-harness <id> --to codex --apply

# resurrect an expert into its OWN worktree (isolated .remember channel) — dry-run, then apply
scripts/mc-session.sh resurrect <id> --project-dir ../mission-control-worktrees/<role>-home
scripts/mc-session.sh resurrect <id> --project-dir ../mission-control-worktrees/<role>-home --apply

# read a session (no writes)
scripts/mc-session.sh export <id> --format md
```

Options: `--to <harness>` (target harness on rehome), `--no-context` (don't copy CLAUDE.md/MEMORY.md/
AGENTS.md to the new cwd), `--cwd <dir>` (rehome the converted session), `--project-dir <worktree>`
(resurrect: the expert's dedicated worktree = its isolated channel). Env: `MC_SESSION_SCRATCH`
(dry-run scratch dir), `MC_SESSION_CV_BIN`.

## Expert channel isolation (why `resurrect` needs a worktree)

The `remember` plugin's SessionStart hook injects `${PROJECT_DIR}/.remember/*` as the resumed agent's
first-person MEMORY. Spawning an expert in the **shared** checkout makes every expert read the *same*
`.remember` — so a resurrected expert wakes up believing it did the captain's work (identity poison).
Setting `REMEMBER_DIR` does **not** fix it: the plugin's shell resolve clobbers the env with
`${PROJECT_DIR}/.remember`. The only working lever is a per-expert `PROJECT_DIR`, cleanest as a dedicated
git **worktree** used as both `--cwd` and `CLAUDE_PROJECT_DIR` (real MC checkout, project context intact,
`.remember` isolated by construction — and it satisfies the all-work-in-worktrees rule anyway).

`resurrect` enforces this so it can't be skipped:
- **Worktree mandatory (fail-loud).** `--project-dir` must be a *linked* worktree, never the shared/main
  checkout — refused with exit 8.
- **Pre-create the isolated `.remember`** on `--apply` — defuses `bootstrap-dirs.sh`'s one-shot migration
  that would otherwise `mv`-STEAL the shared buffer into the new dir.
- **Channel-verify gate** (exit 9): a linked worktree's `.remember` is isolated by construction (exit 8),
  so a non-empty buffer there is the expert's OWN memory — the gate aborts only if an injected file is
  byte-identical to the shared/main-checkout buffer (the actual captain poison copied in), comparing the
  whole set the hook injects (`now`/`recent`/`archive`/`today-*`). This tests the *pipe*, not the person,
  and — unlike a raw emptiness check — survives re-resurrect into a permanent, reused home. After resume,
  have the expert quote its injected `=== MEMORY ===` block; it must be empty/its-own, never the captain buffer.
- **Native-memory re-key gate** (exit 10): Claude Code keys its *native* memory (`<projects>/<slug>/memory/`)
  off the slug dir the transcript lives in — NOT `CLAUDE_PROJECT_DIR`. `cv port --to-dir <worktree>` writes the
  ported transcript into the *worktree's* slug dir, so native memory follows the worktree BY CONSTRUCTION;
  `resurrect` asserts the ported transcript actually landed in the worktree slug and fails loud otherwise.
  Runs in both paths (dry-run checks the scratch slug dir cv wrote under `--out`; `--apply` checks the real
  projects root). Two channels, two levers: `.remember` isolates by `CLAUDE_PROJECT_DIR`; native `memory/`
  isolates by the transcript's slug dir.

**Q47 invariant (captain ruling seq3211):** `resurrect` re-keys native memory to the worktree by construction,
so *future* experts isolate with no new mechanism. The identity **fence** is the backstop for any
*non-resurrected* expert (spawned via `mc agent start`, transcript still in the shared slug) — it cannot write
identity into the shared corpus regardless. Shared *non-identity* native memory is intentionally team-shared
(decisions/gotchas — a feature, not a leak). Blanket re-key of already-running experts is out of scope; an
individual expert is re-keyed on-demand only if it shows actual non-identity bleed.

Full analysis: `<MC_HOME>/prd/MC_EXPERT_REMEMBER_ISOLATION.md`.

## The flows

1. **Rehome to this project** — a session started elsewhere belongs here now: `rehome <id> --to-dir "$PWD"`.
2. **Clone for a parallel lane** — two lanes need independent copies of the same starting context: `clone`
   into each lane's cwd. Both resume the same history without stepping on each other.
3. **Cross-harness port** — move work between claude/codex/grok: `port-harness <id> --to <harness>`.
4. **Resurrect an expert** — bring a domain-expert session in as a live team member with context
   preserved (the QX-EXPERT-RESUME pattern): `resurrect <id> --project-dir ../mission-control-worktrees/<role>-home`.
   Dry-run to confirm the carried context + resume line, then `--apply` and resume with the emitted
   `CLAUDE_PROJECT_DIR=<worktree>` so the expert gets its OWN isolated `.remember` channel (see "Expert
   channel isolation" above — the shared-checkout resurrect that poisoned an expert with the captain's
   buffer is exactly what the exit-8/exit-9 rails now prevent).

## Safety notes / when NOT to use

- `--apply` writes to real cv storage. Always dry-run first and read the preview.
- This copies sessions; it never deletes. To retire a source session, do that deliberately and separately.
- Not for reading/searching a session's CONTENT for recall — that's the `recall` skill (`mc-recall.sh`).
- Cross-harness `convert` is best-effort on format fidelity; verify the ported session opens in the target
  harness before relying on it (the verify rail checks existence, not semantic fidelity).
