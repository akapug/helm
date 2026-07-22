# MC Comms Primitives Reference

## Canonical Operations

The current `mc comms --help` surface exposes:

```text
send --from <ep> --to <ep> [--to <ep> ...] [--priority low|normal|action-required] [--deadline-ms <n>] [--conv <c>] (--payload <text>|--payload-file <path>|--stdin)
poll [--conv <c>] <endpoint>
pending|inbox [--conv <c>] <endpoint>
wait [--conv <c>] [--timeout-ms <n>] <endpoint>
ack <endpoint> <seq>
show <seq>
recover <record_hash>
lifecycle <record_hash>
stranded
resolve <label>
sweep
```

Endpoint forms are `pane:<label>`, `human:<id>`, and `group:<name>`.

The design invariant from `<MC_HOME>/prd/COMMS_CORE_API.md` is durable-log-first delivery: `send` appends a recoverable record before any signal, `poll` is a projection, and `ack` is an idempotent consume receipt.

## Action-Required Delivery

Current code sends a durable record first, then attempts action-required wake dispatch only as a runtime leg. The retired native keystroke path is behind `experimental.legacy_injection_wake`, whose default is false.

Source anchors:

- `src/app/api/comms.rs`: `handle_comms_send` persists the record and calls `dispatch_action_required_wake`.
- `src/app/api/comms.rs`: `dispatch_action_required_wake` returns early when `state.legacy_injection_wake` is false.
- `src/config/model.rs`: `legacy_injection_wake` is documented as retired/default false.
- `agents/codex/home/.codex/mc-codex-hook.py`: Codex prompt-submit hook drains `mc comms pending`, inlines up to five records, and acks inlined seqs.

## Normal Agent Loop

1. Read pending records for your endpoint:

```bash
mc comms pending "pane:${HERDR_PANE_ID}"
```

2. Read full bodies when truncated:

```bash
mc comms show <seq>
```

3. Reply:

```bash
mc comms send --from "pane:${HERDR_PANE_ID}" --to pane:<target> --conv mc-night --payload '<message>'
```

4. Wake another agent only by durable action-required comms:

```bash
mc comms send --from "pane:${HERDR_PANE_ID}" --to pane:<target> --conv mc-night --priority action-required --payload '<specific ask>'
```

5. Ack after consuming:

```bash
mc comms ack "pane:${HERDR_PANE_ID}" <seq>
```

6. Wait when a queue turn is explicitly waiting for a reply:

```bash
mc comms wait --conv mc-night --timeout-ms 60000 "pane:${HERDR_PANE_ID}"
```

## Beacons

Use cockpit records for shared state:

```bash
mc cockpit status --sender "${HERDR_PANE_ID}" working
mc cockpit turn --sender "${HERDR_PANE_ID}" '<short progress summary>'

# Issue or branch-scope claim: current commit SHA.
mc cockpit claim --sender "${HERDR_PANE_ID}" --ttl 7200000 --sha "$(git rev-parse HEAD)" issue:Q16

# File-path claim: blob SHA for the file being fenced.
FILE=agents/claudecode/skills/comms-back/SKILL.md
mc cockpit claim --sender "${HERDR_PANE_ID}" --ttl 7200000 --sha "$(git hash-object "$FILE")" "$FILE"

mc cockpit release --sender "${HERDR_PANE_ID}" issue:Q16
mc cockpit release --sender "${HERDR_PANE_ID}" "$FILE"
```

## Emergency-Only Injection Boundary

Do not use `pane run`, send-text, send-keys, raw socket writes, or `legacy_injection_wake` for ordinary agent coordination. If the human owner explicitly orders an emergency native wake, write down:

- who ordered it
- which endpoint was targeted
- why durable comms plus beacons were insufficient
- what follow-up removes or prevents the emergency use

Then resume normal comms-only operation.

## Zero-Inject Evidence Model

`scripts/zero_inject_counter.py` scans the session's server/client/comms/receipt logs plus the global guard ledger. It reports:

- `action_required_records`: durable action-required traffic in `comms.log`
- `acks`: consume receipts in `comms.log`
- `native_inject_attempts`: evidence of retired Herdr native wake/inject use
- `native_inject_suppressed`: evidence that the retired path was suppressed
- `legacy_injection_enabled_configs`: config files enabling the retired path
- `guard_context_injections_ignored`: ordinary hook/context injections that are not native pane wakes

Use the counter output as a scoped measurement, not as an absolute global proof beyond the scanned files.
