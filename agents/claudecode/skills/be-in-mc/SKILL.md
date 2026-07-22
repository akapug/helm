---
name: be-in-mc
description: >
  First-run orientation for a Mission Control operator or agent. Use after the MC
  marketplace is installed, when someone says "set up Mission Control", "onboard me
  to MC", "be in MC", or joins an unfamiliar MC cell. Grounds the live cell, keeps
  the operator profile separate from fleet configuration, and hands off to /build.
license: MIT
metadata:
  author: mc
  version: "1.0.0"
---

# /be-in-mc — orient the cell, then build

`/be-in-mc` is the Mission Control first-run front door. It reimagines product
setup as instance orientation: MC is already a durable coordination runtime, so
do not create a second team model, install arbitrary packs, or repeat a stored
operator interview. Ground the real cell and then hand the work loop to `/build`.

## 1. Calibrate the operator once

- Run `/know-me --show` first. If the profile is empty, run the short `/know-me`
  interview one question at a time; otherwise leave the existing profile intact.
- Keep this separate from runtime configuration. The profile describes how the
  operator wants agents to work; it is not a request to mutate credentials,
  harnesses, packs, or the fleet.

## 2. Ground the live MC cell

When running in a managed pane, establish the durable facts before suggesting or
taking work:

1. Run `mc pane current` and record the pane label, workspace, and cwd.
2. Run `mc comms inbox pane:<label>` and read each directed record in full before
   acting. A broadcast is ambient context, not an assignment.
3. Run `mc restore verify --json` after a reboot or when runtime health is in
   doubt. Exit code 3 means `NOT CLEAR`; report the actual warning/skip projection
   separately from the pane's seat identity.
4. Read `mc cockpit get --ambient` only for current coordination context. Do not
   use an uncapped cockpit read as routine orientation.

Outside a managed pane, confirm the installed entry point with `mc --version`.
Do not fabricate pane labels or send comms from a shell that is not a live cell.

## 3. Take work through the coordination wire

- If an addressed record assigns work, use its exact row as the controlling spec.
  Check the named path or issue with `mc cockpit check`, then claim it before
  editing.
- Use a dedicated worktree for dispatched branch work. Never register a linked
  worktree as a Claude marketplace root; run `./bootstrap.sh` only from the
  durable checkout when the marketplace is absent.
- For no assignment, do not invent one. Leave the inbox drained and wait for a
  durable task or a human request.

## 4. Hand off to the work loop

Once the cell and task are grounded, use `/build`: specify, plan, inspect,
implement, verify the real artifact, cross-review non-trivial changes, and close
the same comms/cockpit surface that opened the work.

## Cross-refs

- `/know-me` — the durable operator-profile interview.
- `/build` — the ongoing implementation and verification loop.
- `repo-explorer` — source/worktree grounding before an unfamiliar code change.
- `comms-back` — durable MC replies and handoffs.
