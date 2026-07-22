---
name: diary
description: >
  Write a compact end-of-session ambition and outcome entry. Use at the end of an
  autonomous or AFK run, when asked to log the session, or before settling into
  monitoring. It records baseline, what was driven, what was shipped, punts,
  lessons, and the ambition grade.
license: MIT
metadata:
  author: helm
  version: "1.0.0"
---

# /diary - make session outcome visible

The diary is not a turn-by-turn log. It is the outcome record that says whether
the session drove the largest tractable work, settled early, or descended into
monitoring while useful work remained.

## When To Run

- End of an autonomous, overnight, or AFK session.
- When the user says "write the diary" or "log the session".
- When you are about to park or monitor and need to prove that is justified.

## Procedure

1. **Reconstruct baseline.** Read the task list, active PRD, issue, helm chat, and
   recent commits. Name the largest tractable work available at session start.
2. **State drove.** What did you actually attempt or ship?
3. **State settled.** Did you park, monitor, or stop? If yes, was it justified by
   a named blocker or exhausted list?
4. **Assign one ambition grade.**
   - `drove-the-biggest`: attempted the largest tractable work.
   - `settled-early`: shipped real work but stopped short without a hard block.
   - `descended-to-monitoring`: watched/parked while tractable bigger work
     remained.
5. **Record evidence.** Commits, tests, helm chat/session refs, release
   artifacts, and dogfood results.
6. **Name punts and regressions.** If none, justify against the baseline.
7. **Route lessons.** Durable behavior goes through `/learn`; one-off facts go
   to memory or the relevant task.

## Entry Shape

```text
DIARY <date> <cell/session>
baseline:
drove:
shipped:
settled: <yes/no; justified because ...>
ambition_grade: <drove-the-biggest|settled-early|descended-to-monitoring>
evidence:
punts_or_regressions:
lessons:
next:
```

Write it to the project's chosen diary/log surface. If none exists, post a
compact message in helm chat and propose a durable location rather than
inventing hidden storage.

## Honesty Contract

Grade against the baseline, not effort. A busy session that avoided the largest
tractable slice is not `drove-the-biggest`. The diary is useful only if it makes
that visible.

## Success Criteria

- Baseline is reconstructed from real task state.
- Ambition grade is exactly one of the three allowed values.
- Evidence cites commits, commands, or helm chat messages.
- Punts and lessons are explicit.

## Cross-Refs

- `/dogfood` for real-use proof of shipped work.
- `/tlz` for cleaning the task list before final grade.
- `/learn` for durable lessons.
