---
name: recall
description: Use when a Herdr agent wants COLD recall — "have we solved this before / what did a prior session conclude / where's the prior art" — over past agent sessions, as opposed to HOT live state (the live chat). Wraps the isolated, project-scoped cv search façade (recall.sh).
license: MIT
metadata:
  author: helm
  version: "1.0.0"
---

# Recall

## Overview

`recall` is the **cold-recall tier** (Build Map 2b / Q26·CV6): the "solved before?" lookup over the
corpus of prior agent sessions. It is the cold half of the split — **hot** live state lives in
the live chat; **cold** "what did we conclude / where's the prior art" lives in cv
(clustervision). Reach for it before re-deriving something a past session already worked out.

It is a thin front door to `scripts/recall.sh`, which wraps `cv search` with three guarantees the
raw command doesn't give you:

- **Isolation, always.** Every cv invocation runs under a memory cgroup
  (`systemd-run --user --scope -p MemoryMax=…`) — the standing OOM caution, enforced not trusted.
- **Semantic when available.** Uses the cv embedding index when present (`~/.clustervision/embeddings.bin`),
  keyword full-text otherwise — same output either way.
- **Project-scoped by default.** Returns only sessions from the current repo (across its worktrees),
  not the whole ~24k-session corpus. `--scope global` opts out.

## Use it

```bash
scripts/recall.sh "<what you're trying to remember>"                    # project-scoped, cards
scripts/recall.sh "claim race" --limit 5                                # more hits
scripts/recall.sh "restore early exit" --scope global                   # whole corpus
scripts/recall.sh "wake convergence" --format json                      # machine-readable
```

Each result is a **recall card** — `date · harness · a one-line snippet · a `cv show <id>` pointer` —
the dense-summary-plus-pointer shape (injection-lazy-load): scan the cards, then pull the full session
with the pointer only for the one that matches. It never dumps a session into your context.

## When NOT to use it

- For HOT "what's happening right now / who's on what" — that's the live chat, not recall.
- To WRITE/distill a memory — that's the K-lane (distill/promotion), not this read-only façade.

## Notes / v1 limits

- Project scope is a post-filter over the top global hits (cv has no native project scope yet); if an
  in-project session ranks far down the global order it can fall outside the fetched window. Widen with
  `--limit` or use `--scope global`.
- `HELM_RECALL_MEMORY_MAX` (default 4G) tunes the cgroup cap; `CLUSTERVISION_HOME` (default
  `~/.clustervision`) points at the cv data home.
