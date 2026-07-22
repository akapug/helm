---
name: comms-back
description: Use when an MC/Herdr agent needs to answer, wake, hand off, or close out action-required communication through durable Mission Control comms instead of pane keystroke injection.
license: MIT
metadata:
  author: mc
  version: "1.0.0"
---

# Comms Back

## Overview

Comms Back is the Mission Control reply-path discipline: every answer, wake, handoff, and closeout goes through durable `mc comms` records plus cockpit beacons. Native Herdr pane injection is emergency-only and is not part of normal operation.

Use this skill when a record says action is required, when you need to reply to another pane, when you are handing off work, or when you are asked to prove that a coordination turn used comms instead of inject.

## Core Rules

1. Treat the durable comms record as the source of truth. Use `pending`, `show`, `recover`, `lifecycle`, and `ack`; do not rely on pane scrollback for delivery state.
2. Send replies with `mc comms send`. Use `--priority action-required` only when the recipient must take a turn; otherwise use normal priority or a cockpit beacon.
3. Use cockpit records for coordination beacons: `status`, `turn`, `claim`, and `release`. These are records too, not typed pane messages.
4. Ack only after the record has been inlined by the hook or you have manually read enough of it to act. Ack is an idempotent consume receipt; it does not delete the record.
5. Never extend or use the retired native injection wake in normal operation. The `experimental.legacy_injection_wake` flag is an emergency transition escape hatch only.
6. Before saying "zero inject", run the counter in `scripts/zero_inject_counter.py` and report the counts, scope, and session directory.

## Reply Workflow

1. Identify yourself and your endpoint.

```bash
printf '%s\n' "${HERDR_PANE_ID:-unknown}"
mc comms pending "pane:${HERDR_PANE_ID}"
```

If the hook already delivered records into context, use those seq numbers. For long or truncated bodies, read the full record:

```bash
mc comms show <seq>
```

2. Triage by action. Act on assignments, requests, blockers, direct questions, and messages naming your pane. Treat FYI/status/ack/all-clear records as no-action unless they change your current work.

3. Reply through comms, not by typing into another pane.

```bash
mc comms send --from "pane:${HERDR_PANE_ID}" --to pane:w3:p1 --conv mc-night --payload 'ACK seq1440: stopped Q5, refreshed report, pulling Q16.'
```

Use action-required only for a real turn-taking ask:

```bash
mc comms send --from "pane:${HERDR_PANE_ID}" --to pane:w1:pK --conv mc-night --priority action-required --payload 'REVIEW_REQUEST Q16: skill and zero-inject counter ready at <skills-dir>/comms-back.'
```

4. Ack consumed records after you have responded or consciously decided no action is needed.

```bash
mc comms ack "pane:${HERDR_PANE_ID}" <seq>
```

5. Leave beacons for work state.

```bash
mc cockpit status --sender "${HERDR_PANE_ID}" working
mc cockpit turn --sender "${HERDR_PANE_ID}" 'Q16 in progress: skill scaffolded, counter under validation'

# Issue or branch-scope claims use the current commit.
mc cockpit claim --sender "${HERDR_PANE_ID}" --ttl 7200000 --sha "$(git rev-parse HEAD)" issue:Q16

# File-path claims use the blob hash for the file being fenced.
FILE=agents/claudecode/skills/comms-back/SKILL.md
mc cockpit claim --sender "${HERDR_PANE_ID}" --ttl 7200000 --sha "$(git hash-object "$FILE")" "$FILE"

mc cockpit release --sender "${HERDR_PANE_ID}" issue:Q16
```

## Handoff Shape

Keep handoffs compact and actionable:

```text
CODE_READY Q16: <skills-dir>/comms-back
change: added comms-only reply workflow and zero-inject counter
evidence: python3 .../scripts/zero_inject_counter.py --session-dir ~/.config/herdr/sessions/mc-headful
state: no repo source edits; skill file outside repo
next: review skill wording and decide whether to promote into shared plugin packaging
```

For status batches, group related facts and include evidence paths or SHAs. Do not send multiple one-line pings when one durable record can carry the state.

## Zero-Inject Proof

Run the counter before making a zero-inject claim:

```bash
python3 <skills-dir>/comms-back/scripts/zero_inject_counter.py --session-dir ~/.config/herdr/sessions/mc-headful
```

The PASS condition is:

- `native_inject_attempts = 0`
- `legacy_injection_enabled_configs = 0`
- action-required records are present in `comms.log` or the scope is explicitly stated as "no action-required traffic in this window"

The counter deliberately reports normal guard context injections separately as `guard_context_injections_ignored`; those are not Herdr native pane keystroke wakes.

## References

Read `references/mc-comms-primitives.md` when you need exact command semantics, source anchors, or the counter's evidence model.

## Verification

- [ ] Pending records read from `mc comms pending`, hook context, or `mc comms show`.
- [ ] Replies and handoffs sent with `mc comms send`, not pane keystrokes.
- [ ] Consumed records acked with `mc comms ack`.
- [ ] Cockpit claims/status/turn records updated for work state.
- [ ] Native injection not enabled or used.
- [ ] Zero-inject claim backed by `scripts/zero_inject_counter.py` output.
