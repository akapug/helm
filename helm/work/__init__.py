#!/usr/bin/env python3
"""helm work — in-cave git coordination: worktree lifecycle on the claims
lane (design: the maintainer's tree + the front desk). The shared checkout is
the INTEGRATOR's tree; every other seat works in a private room
`<repo>-wt/<lane>` on branch `lane/<lane>`, checked in and out at the desk.

Every leg is a REUSE — the anti-bureaucracy bar is what this module does NOT
add:
  * the desk     — seats.claim/release/claims_list + own_leases: the lease
                   nonce CONFIRMS a deliberate release (it is not a secret —
                   every seat here is the same uid and can read the ledger,
                   so `helm work list` reprints the holder's own), TTL is the
                   checkout deadline, expiry monotonic; the stop-guard already
                   refuses a session stop with a lease still held.
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
queues/priorities on lanes, a daemon — any of these is the control-plane slide; stop.
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
    find_root, lane_branch, lane_path, lane_rows, auto_rows, managed_room_kind,
    resource,
    unguarded_inventory, unguarded_rows, worktrees,
    _checkout_issue, _git, _git_bytes, _harness_state,
    _disposable_worktree_occupant, _last_agent_terminal, _live_claude_sessions,
    _occupants, _occupants_many, _room_status, _status_entries, _worktree_records,
    _wrote_ago,
)
from ._gc import (
    EnactResult, RETIRABLE, estate_phantom_records, format_gc_summary,
    gc_enact, gc_orphans, was_reclassified,
    LANE_GONE, LANE_LANDED, LANE_UNKNOWN, LANE_UNLANDED, LANE_UNSTARTED,
    landed_leases, lanes_landed, release_command, rooms_dirty,
    gc_scan, green_receipts, lane_overlaps, list_rows, phantom_records,
    phantom_scan, seam_candidates,
    post_gc_summary, prune_phantom_records, _base, _delete_lane_branch, _dirty,
    _has_branch, _live, _merge_state, _merged, _proof_word, _removal_blocker,
    _wip_commit,
)
from ._claims import claim, release_lane, release_stale_lane, _infer_lane, _positional
from ._common import PEEK_DIRNAME
from ._peek import peek, peek_area, peek_drop, peek_path, peek_rows, \
    resolve_commit
from ._guard import (
    GUARD_HOOK, GUARD_HOOKS, LEGACY_HOOK_MARKERS, MANAGED_HOOK_MARKER,
    REF_GUARD_HOOK, hook_path, install_guard,
    _guard_plan, _hook_scope, _owned_hook, _path_snapshot, _put_snapshot,
)
from ._cli import USAGE, cmd_work
