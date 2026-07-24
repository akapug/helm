---
name: repo-explorer
description: >
  Build a live map of the repo before touching unfamiliar code. Use when
  onboarding to a repo, re-grounding after a resume, checking branch/worktree
  state, or answering "what is in here?" Read the repo's own rules, manifests,
  layout, worktrees, status, and recent history, then cite what matters.
license: MIT
metadata:
  author: helm
  version: "1.0.0"
---

# /repo-explorer - orient before editing

This skill orients. It does not edit. The output is a compact map of the real
repo state plus recommended next reads.

## Fast Map

Run the cheap probes first:

```bash
git status -sb
git log --oneline -12
git worktree list
git rev-parse --show-toplevel
find . -maxdepth 2 \( -name 'Cargo.toml' -o -name 'package.json' -o -name 'pyproject.toml' -o -name 'go.mod' -o -name 'justfile' -o -name 'Justfile' -o -name 'Makefile' \) -not -path '*/target/*' -not -path '*/node_modules/*'
```

Then read repo instructions and public entry points:

- `AGENTS.md`, `CLAUDE.md`, or equivalent agent instructions;
- `README.md`;
- primary manifests;
- top-level docs that define build, test, or contribution rules.

## Deeper Trace

1. **Rules before code.** Repo instructions override generic habits.
2. **Manifest names the stack.** Use project scripts and `just` recipes before
   ad hoc commands.
3. **Trace the native surface.** Most work extends existing patterns; find the
   adjacent implementation before adding a parallel one.
4. **History explains shape.** Use `git log -p --follow`, `git blame`, and
   commit messages for why a file is this way.
5. **Check active work.** Claims, worktrees, dirty state, and chat messages tell you
   who owns what now.
6. **Cite before claiming.** File/line, command output, commit, or a chat seq beats
   inference.

## Coordination Context

When coordination matters, read `helm chat` for the relevant rooms. Do not
inspect a client view as source of truth.

## Output Shape

```text
Repo map:
- root:
- branch/head:
- dirty state:
- active worktrees/claims:
- stack/manifests:
- test/build entrypoints:
- repo rules:
- recent relevant commits:
- next reads:
```

## Success Criteria

- Current branch, head, dirty state, and worktrees are known.
- Repo-local instructions were read.
- Native build/test entrypoints are identified.
- Any claim about behavior has a locator.
- Editing is handed to `/build`, `/fix`, or `/refine` after orientation.

## Cross-Refs

- `/build` for forward work after the map.
- `/fix` for repair work after the map.
- `/xchk` for claim grounding across sources.
