#!/usr/bin/env python3
"""helm work — in-cave git coordination: worktree lifecycle on the claims
lane (design: prd/2026-07-21-in-cave-git-coordination.md — the maintainer's
tree + the front desk). The shared checkout is the INTEGRATOR's tree; every
other seat works in a private room `<repo>-wt/<lane>` on branch
`lane/<lane>`, checked in and out at the desk.

Every leg is a REUSE — the anti-bureaucracy bar is what this module does NOT
add:
  * the desk     — seats.claim/release/claims_list, untouched: the lease
                   nonce is the room key, TTL the checkout deadline, expiry
                   monotonic; the stop-guard already refuses a session stop
                   with the key still in pocket.
  * the registry — `git worktree list --porcelain` ⋈ `.claims.json`, joined
                   at read time. ZERO new state files: registry drift is
                   unrepresentable.
  * do-not-disturb — `git worktree lock --reason lease:<id8>` (git-native:
                   even raw prune/remove refuses while locked).
  * housekeeping — `work gc`, dry-run default (gc.py culture). LOCKED or
                   OCCUPIED (any live process cwd) rooms are immune. The ONLY
                   write to authored bytes anywhere here is a RESCUE COMMIT
                   onto the lane's own branch — lost-and-found, never the
                   dumpster; no code path discards uncommitted work.
  * the rail     — composed reference-transaction + post-checkout hooks for
                   the ONE resource that needs determinism (the shared
                   checkout). The first refuses branch/HEAD mutation before
                   it happens and protects occupied worktree branches; the
                   second is a pointer-only safety net. Existing hooks remain
                   first-class participants, preserved byte-for-byte.

Tripwires (design §5): per-file claims, approval steps, a second registry,
queues/priorities on lanes, a daemon — any of these is the over-engineering slide; stop.
"""
# The top-level imports the pre-split module exposed as public attributes.
import fcntl
import json
import os
import re
import shlex
import stat
import subprocess
import sys
import tempfile
import time

from .. import automap, pk, seats

# --- re-exports: every top-level name the pre-split work.py defined --------
from ._common import (
    DEFAULT_TTL, LANE_RE, RECENT_WRITE_SECONDS,
    _AGENT_ROOM_RE, _VALUE_FLAGS, _WORKFLOW_ROOM_RE,
    _claude_homes, _load_json_nofollow, _read_small_nofollow,
)
from ._lanes import (
    find_root, lane_branch, lane_path, lane_rows, resource,
    unguarded_inventory, unguarded_rows, worktrees,
    _checkout_issue, _git, _git_bytes, _harness_state,
    _last_agent_terminal, _live_claude_sessions, _occupants,
    _occupants_many, _room_status, _status_entries, _worktree_records,
    _wrote_ago,
)
from ._gc import (
    gc_enact, gc_orphans, gc_scan, list_rows,
    _base, _dirty, _has_branch, _live, _merged, _removal_blocker,
    _wip_commit,
)
from ._claims import claim, release_lane, _infer_lane, _positional
from ._guard import (
    GUARD_HOOK, GUARD_HOOKS, LEGACY_HOOK_MARKERS, MANAGED_HOOK_MARKER,
    REF_GUARD_HOOK, hook_path, install_guard,
    _guard_plan, _hook_scope, _owned_hook, _path_snapshot, _put_snapshot,
)
from ._cli import USAGE, cmd_work
