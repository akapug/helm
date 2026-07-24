---
name: sessions
description: Use when an agent needs to MOVE, COPY, REHOME, or CROSS-HARNESS-PORT an existing agent session — "rehome this session to this project", "clone a session for a parallel lane", "port this to codex", "resurrect an expert session as a team member". Wraps clustervision (cv) session manipulation with enforced safety rails (dry-run first, original untouched, verify before resume).
license: MIT
metadata:
  author: helm
  version: "1.0.1"
---

# Sessions (RSH)

## Overview

Agent sessions are first-class, manipulable objects: you can rehome one to a new working directory,
clone it for a parallel lane, or port it to another harness — and cv (clustervision) is the mechanism
that makes it possible. This skill is the safe front door: `scripts/helm-session.sh` wraps cv's
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
scripts/helm-session.sh rehome <id> --to-dir "$PWD"
scripts/helm-session.sh rehome <id> --to-dir "$PWD" --apply

# clone a session for a parallel lane — an independent copy into the lane's room
# (lane rooms come from `helm work claim <lane>` → ../helm-wt/<lane>)
scripts/helm-session.sh clone <id> --to-dir ../helm-wt/<lane> --apply

# cross-harness port (claude -> codex, etc.)
scripts/helm-session.sh port-harness <id> --to codex --apply

# resurrect an expert into its OWN worktree (isolated .remember channel) — dry-run, then apply
scripts/helm-session.sh resurrect <id> --project-dir ../helm-wt/<role>-home
scripts/helm-session.sh resurrect <id> --project-dir ../helm-wt/<role>-home --apply

# read a session (no writes)
scripts/helm-session.sh export <id> --format md
```

Options: `--to <harness>` (target harness on rehome), `--no-context` (don't copy CLAUDE.md/MEMORY.md/
AGENTS.md to the new cwd), `--cwd <dir>` (rehome the converted session), `--project-dir <worktree>`
(resurrect: the expert's dedicated worktree = its isolated channel). Env: `HELM_SESSION_SCRATCH`
(dry-run scratch dir), `HELM_SESSION_CV_BIN`.

## Expert channel isolation (why `resurrect` needs a worktree)

The `remember` plugin's SessionStart hook injects `${PROJECT_DIR}/.remember/*` as the resumed agent's
first-person MEMORY. Spawning an expert in the **shared** checkout makes every expert read the *same*
`.remember` — so a resurrected expert wakes up believing it did the main session's work (identity poison).
Setting `REMEMBER_DIR` does **not** fix it: the plugin's shell resolve clobbers the env with
`${PROJECT_DIR}/.remember`. The only working lever is a per-expert `PROJECT_DIR`, cleanest as a dedicated
git **worktree** used as both `--cwd` and `CLAUDE_PROJECT_DIR` (real project checkout, project context intact,
`.remember` isolated by construction — and it satisfies the all-work-in-worktrees rule anyway).

`resurrect` enforces this so it can't be skipped:
- **Worktree mandatory (fail-loud).** `--project-dir` must be a *linked* worktree, never the shared/main
  checkout — refused with exit 8. The guard checks the git-dir (`git rev-parse --git-dir` of a linked
  worktree lives under `<main>/.git/worktrees/`), so any linked worktree passes regardless of where its
  directory sits — verified live against `../helm-wt/deck-fix` (accepted) and the main checkout (refused).
  Mint the room with `helm work claim <lane>` (helm's worktree lifecycle: lease + guard rails), not raw
  `git worktree add`.
- **Pre-create the isolated `.remember`** on `--apply` — defuses `bootstrap-dirs.sh`'s one-shot migration
  that would otherwise `mv`-STEAL the shared buffer into the new dir.
- **Channel-verify gate** (exit 9): a linked worktree's `.remember` is isolated by construction (exit 8),
  so a non-empty buffer there is the expert's OWN memory — the gate aborts only if an injected file is
  byte-identical to the shared/main-checkout buffer (the actual shared-buffer poison copied in), comparing
  the whole set the hook injects (`now`/`recent`/`archive`/`today-*`). This tests the *pipe*, not the person,
  and — unlike a raw emptiness check — survives re-resurrect into a permanent, reused home. After resume,
  have the expert quote its injected `=== MEMORY ===` block; it must be empty/its-own, never the shared buffer.
- **Native-memory re-key gate** (exit 10): Claude Code keys its *native* memory (`<projects>/<slug>/memory/`)
  off the slug dir the transcript lives in — NOT `CLAUDE_PROJECT_DIR`. `cv port --to-dir <worktree>` writes the
  ported transcript into the *worktree's* slug dir, so native memory follows the worktree BY CONSTRUCTION;
  `resurrect` asserts the ported transcript actually landed in the worktree slug and fails loud otherwise.
  Runs in both paths (dry-run checks the scratch slug dir cv wrote under `--out`; `--apply` checks the real
  projects root). Two channels, two levers: `.remember` isolates by `CLAUDE_PROJECT_DIR`; native `memory/`
  isolates by the transcript's slug dir.

## The flows

1. **Rehome to this project** — a session started elsewhere belongs here now: `rehome <id> --to-dir "$PWD"`.
2. **Clone for a parallel lane** — two lanes need independent copies of the same starting context: `clone`
   into each lane's cwd. Both resume the same history without stepping on each other.
3. **Cross-harness port** — move work between claude/codex/grok: `port-harness <id> --to <harness>`.
4. **Resurrect an expert** — bring a domain-expert session in as a live team member with context
   preserved: claim the room (`helm work claim <role>-home`), then
   `resurrect <id> --project-dir ../helm-wt/<role>-home`.
   Dry-run to confirm the carried context + resume line, then `--apply` and resume with the emitted
   `CLAUDE_PROJECT_DIR=<worktree>` so the expert gets its OWN isolated `.remember` channel (see "Expert
   channel isolation" above — history: a shared-checkout resurrect once poisoned an expert
   with the primary session's buffer; that is exactly what the exit-8/exit-9 rails now prevent).

## Scope: default harness roots ONLY (helm seat homes are NOT covered)

cv discovers sessions from the default harness roots (`~/.claude`, `~/.codex`, …) and does NOT read
`CLAUDE_CONFIG_DIR` — verified live 2026-07-22: with a seat's config dir exported, `cv ls` still lists
only default-root sessions, and `cv show`/`helm-session.sh export` on a session living under a helm
per-seat home (`~/.helm/_global/seats/<family>/instances/<n>/claude`) returns nothing. Until cv (or the
wrapper) grows source-root plumbing, this skill moves/copies/rehomes DEFAULT-ROOT sessions only — do not
claim it on helm seat-home sessions; that gap is real and open.

## Safety notes / when NOT to use

- `--apply` writes to real cv storage. Always dry-run first and read the preview.
- This copies sessions; it never deletes. To retire a source session, do that deliberately and separately.
- Not for reading/searching a session's CONTENT for recall — that's the `recall` skill.
- Cross-harness `convert` is best-effort on format fidelity; verify the ported session opens in the target
  harness before relying on it (the verify rail checks existence, not semantic fidelity).
