# Eval — corpus backup (2026-07-20)

Card: the owner's ask made active — "all session/subagent/workflow transcripts
should be canonically backed up safely for future training-data finetuning on
the homelab… just need to make sure we aren't discarding useful data."
`~/corpus-archive` existed but was hand-fed (dated dirs, last touched
2026-07-13); nothing ran on a schedule and the subagent estate was covered by
nothing at all.

## Ground truth first (the survey moved the design)

- Subagent + workflow transcripts do NOT live under `/tmp` — they live inside
  the projects tree: `~/.claude/projects/<slug>/<sid>/subagents/agent-*.jsonl`
  and `…/subagents/workflows/wf_*/agent-*.jsonl`. They are 4,092 of the 5,462
  claude jsonl files, and the session catalog's one-level glob never lists
  them — exactly the "useful data being discarded" class.
- All 8 `~/.claude-homes/*/projects` are symlinks to `~/.claude/projects` —
  inode dedup makes the cred-home estate free, and covers a future home that
  stops symlinking.
- `/tmp/claude-*` holds mostly scratch jsonl exhaust; real transcripts appear
  only under `projects/` / `subagents/`-shaped subtrees (sessions run with a
  relocated config dir). Pattern-scoping keeps the corpus signal-only while
  catching the reboot-ephemeral real ones.

## What shipped

- `helm/corpus.py` — `helm corpus backup [--dry] [--dest DIR] | corpus status`.
  Copy-only by law (no code path renames/rewrites/removes a source; the
  archive is append-only the other way too — a grown transcript re-copies into
  today's date dir, old snapshots stay). Incremental on the catalog-cache
  pattern: manifest keyed by source path, (size, mtime_ns) skip, sha256 on
  copy so mtime-only churn refreshes the cursor without a second body.
  Atomic dest writes (`.part` + `os.replace`), provenance mtime carried,
  fail-open per file (errors retry next run — only landed copies enter the
  manifest), fail-safe on space (statvfs preflight + 1GB margin aborts before
  the first byte with the point-HELM_CORPUS_DEST-elsewhere message).
- `scripts/helm-corpus.service` + `scripts/helm-corpus.timer` — daily
  `systemd --user` timer (Persistent=true back-fills slept-through days,
  IO-idle priority). NOT enabled here — the enable is a documented owner
  one-step (see VERBS.md).
- Docs: VERBS.md "corpus — the training-data archive" section; ENVIRONMENT.md
  `HELM_CORPUS_DEST` row (extra roots reuse `HELM_CLAUDE_ROOTS`/
  `HELM_CODEX_ROOTS` — one config surface with the catalog).

## Verification

- `tests/test_corpus.py` — 13 hermetic tests on planted obviously-synthetic
  estates: scan scope (subagents + workflows in; `.flat.`/scratch noise out),
  symlink-root inode dedup, cross-root relpath collision guard, dry copies
  nothing (byte-identical tree snapshot), dated layout + source-untouched law,
  no-op re-run, grown-source re-copy keeping the old snapshot, mtime-only
  churn deduped by hash, unreadable-source fail-open + retry, space-guard
  abort, `--dest` over env, status coverage/retired/last-run, usage.
- Suite: 849 tests, `-W error::ResourceWarning`, OK.
- Live read-only smoke (real estate, nothing copied): **6,031 transcripts,
  48.1GB — claude 5,173 (43.7GB, subagent/workflow files included), codex 857
  (4.4GB), tmp 1 (13.7KB)**; scan 3.2s; `status` shows archived 0 of 6,031,
  last run: never. Dest disk: 931GB free — first real run fits with margin.

## Measurement (owner-surface)

The heed signal: `helm corpus status` a week after the timer is enabled should
show archived ≈ estate with a fresh last-run receipt and 0 errors — and
`~/corpus-archive` gaining a dated dir per active day. If the owner instead
keeps hand-copying session dirs, the verb missed a need — find it and fold it
in. One-step left for the owner (surfaced, not silently parked): enable the
timer; optionally set `HELM_CORPUS_DEST` to the adata/dev-ext mount first.
