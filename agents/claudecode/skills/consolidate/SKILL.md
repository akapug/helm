---
name: consolidate
description: >
  Inventory and clean a project's documentation baseline. Use when docs are a
  mess, a freshness check flags drift, a plan has landed but still reads active,
  or a retro asks for doc cleanup. Classify docs by lifecycle, apply safe
  cleanups, and surface judgment calls.
license: MIT
metadata:
  author: mc
  version: "1.0.0"
---

# /consolidate - docs into a living baseline

Docs have different contracts. Public docs must match current behavior. Planning
docs preserve decision history. Logs are append-only. Scratch expires.
Consolidation makes that explicit and leaves a clean baseline.

## Steps

1. **Inventory.**

```bash
git ls-files '*.md' '*.mdx' ':!:vendor/**' ':!:**/node_modules/**'
```

Include root READMEs, website docs, planning dirs, changelogs, and per-folder
READMEs.

2. **Classify.**
   - **public**: README, website docs, API docs; must match current behavior.
   - **planning**: PRDs, design records, RFCs; active -> done/superseded ->
     archived.
   - **log/diary**: changelog, release notes, session logs; append-only.
   - **scratch**: notes, spikes, temporary docs; archive or remove when spent.
   - **per-folder README**: describes current contents of one directory.

3. **Detect staleness.**
   - Broken links.
   - Public docs describing behavior code no longer has.
   - Plans still marked active after work landed.
   - Folder README no longer matching the folder.
   - Scratch that served its purpose.

4. **Apply safe cleanups.**
   - Fix dead public links.
   - Mark landed plans done or superseded when evidence is clear.
   - Archive scratch you created or that is clearly spent.
   - Append a compact cleanup note where the project records maintenance.

5. **Surface judgment calls.**
   - Merge duplicate docs.
   - Drop someone else's document.
   - Demote stale public docs.
   - Rewrite historical planning records.

6. **Verify.** Re-run the relevant doc freshness checks, link checks, website
   build, or review grep. Name any intended residue.

## Output Shape

```text
DOCS CONSOLIDATION
- inventory: <count> docs
- public:
- planning:
- logs:
- scratch:
- per-folder:
- safe changes applied:
- judgment calls:
- verification:
```

## Rules

- Do not "fix" historical planning text just because names changed later.
- Do keep public docs accurate.
- Safe-auto is limited to reversible or clearly evidenced cleanup.
- Deleting or merging docs you did not create needs a surfaced decision.

## Success Criteria

- Every doc is classified or explicitly out of scope.
- Safe changes are applied with evidence.
- Judgment calls are separated from automatic cleanup.
- Verification ran or a precise blocker is recorded.

## Cross-Refs

- `/drift` for code/plan drift against an approved baseline.
- `/learn` when recurring doc drift needs a rule or hook.
- `/devops` when docs are part of release readiness.
