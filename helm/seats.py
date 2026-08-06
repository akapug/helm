#!/usr/bin/env python3
"""helm seats — the agent-facing lane, collapsed onto the chat
room (hardened by adversarial review). An earlier design carried a separate
tmpfs whisper channel
because it had no room; helm HAS the room, so every capability here is a
READ PATTERN over /dev/shm/helm-chat plus small RAM state files — zero new
transports, zero daemons, zero slot files.

The lane is called DELIVERY, never "whisper" (that word is taken twice:
inject's first-turn brief digest, and the v1 on-ledger attestation frames).

TRUST DOMAIN — SAY IT LOUDLY: seat names are DISPLAY LABELS. Everything in
this module is advisory coordination between cooperating same-uid processes
in a 0700 tmpfs dir — not a security boundary, and never claimed as one
(principal cryptography stays dregg's; do not rebuild it here). What the
bindings below DO defend against is the realistic failure: an agent — or a
prompt-injected one — impersonating the owner or another holder through
legit tooling. Hence: owner-rule delivery trusts only rows the server-side
owner rails stamped (origin web/tui); claim leases bind to {session, lease
nonce, fence}, never to a matching display string.

The legs:
  * join    — SessionStart hook: roster row (RAM presence, keyed on the seat's
              HELM_CHAT_NAME so a seat joins as its family name) + cursor
              INITIALIZED HERE (a message posted between session start and
              the first tool boundary must deliver) + the seat's
              identity/protocol line as session context. That line DIRECTS the
              agent to arm its idle-wake beacon (a persistent Monitor on
              `helm chat wait --follow`) as a MANDATORY first action — the only
              thing that wakes an idle PTY agent (native-wake-only-agent-armed).
  * deliver — PostToolUse hook: the tool-boundary nudge plus a silent producer
              of claim-bound interval evidence when the documented payload has
              a subagent `agent_id`. At most ONE chat row per boundary, 200-byte
              clip, control-char scrub, information-not-instruction label.
              AT-LEAST-ONCE, NEVER AT-MOST-ONCE: the hook response is
              emitted in ONE unbuffered write and the cursor commits only
              AFTER — a kill in between produces a duplicate next boundary,
              which beats silence. Every unpaused fire touches the seat's own
              `.seen` file; a proxywatch credential wall pauses before presence
              or cursor mutation. The unchanged-room fast path never rewrites
              shared state.
  * delegation-stop — SubagentStop hook: removes that exact agent's activity
              entry so the parent Claude pid cannot preserve completed work.
  * wait    — the beacon: block until a row addressed to the seat lands
              (Monitor arms it). --follow keeps the room open and streams EACH
              new matching row as one line (one line = one agent wake), never
              returning on a match — MENTION-ONLY by default (mentions/
              replies/DMs/@all; ambient home-room rows wake nobody — owner
              directive, --ambient opts back in). NOTE: this is busy-turn parity
              plus an idle beacon the join context line makes MANDATORY to arm —
              nothing external can wake an idle PTY agent, so the self-armed
              Monitor is the only path.
  * claims  — advisory TTL lease with session+nonce+fence binding and
              monotonic expiry (the worktree-collision class).
  * stop-guard — Stop hook: the IDLE GATE (the work-arbiter capability,
              helm-native). BLOCKS a stop while undelivered mentions/owner
              rows sit past the seat's cursor (once per pending-fingerprint —
              never an infinite loop) or while THIS session holds a live
              claim lease; WARNs (never blocks) on a clean stop to arm the
              beacon; silently runs `helm index cap --apply`. Fail-open
              total; HELM_STOP_GUARD=0 kills it.

Council (embargoed verdicts) is DEFERRED to 0.3: a correct embargo needs an
expected-set freeze, a reveal state machine, salted commitments and batch-row
reveal. No live consumer today, so: record, don't build.

Cursor law: `<room>.cursor.<seat>[.<sid8>]` holds {dev, ino, off,
rid, active} — the room file's identity, the byte offset of the first
unprocessed row, the last processed row's stable id, and whether the seat
actually consumed room traffic (an EOF join baseline is not presence). The cursor is PER (seat,
session): two live sessions sharing one HELM_CHAT_NAME each hold their own
cursor, so an @mention FANS OUT to all of them instead of being race-consumed
by whichever boundary fires first (the live @mention-loss class). A caller
with no session (bare CLI) rides the seat-level cursor; a fresh session
cursor seeds from the seat-level one when it exists (upgrade continuity —
rows tracked before the split are not skipped). Fast path = one stat (same inode, size
== off ⇒ nothing new; a same-size REPLACEMENT changes the inode and is
caught). Inode change or shrink ⇒ rotation/replacement: reset to 0 and use
rid to suppress the retained overlap (duplicates acceptable, loss is not).
All cursor transitions serialize on `<room>.cursor.<seat>.lock`; rows are
selected/committed by byte offset from ONE fstat'd fd, never by line count.
Initialized at JOIN. Every hook-facing path is FAIL-OPEN TOTAL.

MULTI-ROOM (an owner post in a non-home room woke nothing): the lane is not
main-scoped. deliver_any (the
PostToolUse hook) and the wait --follow beacon consider EVERY live room —
the seat's private DM lane first, then primary, then newest-activity rooms,
ROOM_SCAN_CAP-bounded — with the same per (seat, room, session) cursor
mechanics per room. A TRACKED seat meeting a cursor-less room BACKFILLS from
offset 0 (a room born after its join is all post-join news — the mention
that created the channel must deliver); an untracked seat keeps the EOF
self-heal everywhere (pre-join backlog never floods). join baselines every
existing room; stop-guard and the roster report read pending across the same
bounded scan.

BEACON SCOPE (the live bug: a seat homed to #main never saw its own @seat
mention posted in a non-home room because homing ALLOWLISTED the scan): the scan
covers every live room; deliverable() applies the scope per row —
  (a) a @seat mention (or a {dm} row naming the seat, or a reply to the
      seat's row) surfaces from ANY room,
      always — a direct address is never filtered;
  (b) ANYTHING in the seat's HOME room (roster home_room) surfaces at the
      TOOL BOUNDARY and in the pending gate — the team channel is
      full-surface for its own BUSY team. The idle beacon (wait --follow)
      drops this tier BY DEFAULT: an ambient wake burns a full turn per row
      (premise mute-busy-home-room-trust-mentions, owner-confirmed) —
      --ambient opts a quiet-room seat back in;
  (c) @all broadcasts surface in {home, main}; owner-rail posts NO LONGER
      auto-wake (by owner directive: mentions + home room are enough) —
      never fleet-wide across every side room;
  (d) a MUTED room (helm chat seat mute <room> — roster row "mute") stops
      (b)/(c) noise at this seat; (a) still surfaces (mute tunes noise,
      never direct address).

DM: seats.dm() writes ONE row into the
recipient's private lane (chat.dm_room — the `dm-` reserved namespace, a
dm/ subdir file no room list ever shows). The recipient is the EXACT seat
token (a casefold roster snap only — never a substring, never a slug fold:
team.a and team-a are different lanes by key). Delivery/beacon/stop-guard
pick the lane up first in the room scan; nobody else ever scans it.
"""
import contextlib
import sys as _sys
import types as _types
import functools
import getpass
import glob
import errno
import hashlib
import json
import math
import os
import re
import signal
import sys
import time
import unicodedata

from . import chat, home, pk, vcs

# THE SHARED FLOOR (see seats_common's docstring for why these and not
# the most-used names). Re-exported so every existing `seats.<name>`
# caller outside this package keeps working unchanged.
from .seats_common import (  # noqa: F401
    MAX_BYTES, PREVIEW_CHARS, SEAT_BYTES, FRESH_S,
    QUIET_S, DEFAULT_TTL, STRICT_CLAIM_LOCK_WAIT_S, STRICT_CLAIM_LOCK_POLL_S,
    SCAN_CAP, ROOM_SCAN_CAP, BEACON_DRAIN_CAP, GUIDE_PATH,
    _BROADCAST, own_name, _scrub, _clip,
    _flocked, roster_path, _seat_key, roster,
    _GONE, process_sid_scan, _sessions_with_a_process, dm_lane,
    _SEAT_TOKEN, _CanonicalRecipient, _canonical_recipient, recipient_matches,
    _proc_stat_link, _get_pid_starttime, _get_live_pid_starttime, claims_path,
    _now_mono, _sweep, UNVERIFIED, STATUS_BYTES,
    _seat_label,
)

# THE GATE QUEUE — re-exported because helm/gate.py drives every public verb
# through `seats.gate_queue_*`, and a split must not move a caller's door.
from .seats_gate_queue import (  # noqa: F401
    GATE_QUEUE_CAPACITY, gate_queue_path, gate_queue_enqueue,
    gate_queue_try_start, gate_queue_bind_child, gate_queue_renew,
    gate_queue_validate_binding, gate_queue_prepare_finish,
    gate_queue_finish, gate_queue_snapshot,
    # AND TWO UNDERSCORED NAMES, because helm/gate.py calls them. A leading
    # underscore is a CONVENTION, not a measurement: gate.py reaches for
    # seats._gate_repo_id and seats._gate_pid_state directly, and omitting
    # them from this list broke `gate.run` while every gate_queue_* verb
    # resolved perfectly. The facade owes callers what they ACTUALLY use.
    _gate_repo_id, _gate_pid_state,
)

# IDENTITY + ADDRESSING. Every name is re-exported, including the
# underscored ones — the facade owes callers what they USE, and a leading
# underscore proved to be a convention rather than a measurement when
# helm/gate.py turned out to call seats._gate_repo_id.
from .seats_identity import (  # noqa: F401
    _FAMILIES, _family, auto_name, derive_seat,
    foreign_seat, acting_seat, identity_disagreement, _dispute_sentence,
    _live_session_of, _warn_disagreement, _assert_own_seat, _FOREIGN_WARNED,
    _warn_once, _warn_foreign, _git_root, _fingerprinted_project,
    _path_project, _git_project, derive_home_room, safe_cwd,
    resolve_homing, _OWNER_NAME, _HANDLE_RE, _git_owner_handle,
    owner_name, owner_names, _mention_re, mentions,
    seat_scope, _delivery_pause, _delivery_guard, deliverable,
    _same_reaction_target, _reaction_wake_body,
)

# THE ROSTER — RAM presence plus the identity-admin verbs.
from .seats_roster import (  # noqa: F401
    seen_path, roster_checked, runtime_for_session, touch_seen,
    last_seen, SESSIONS_KEPT, TEMP_ROOTS, _is_temp_cwd,
    nonpane_session, _keep_sessions, _evict_session, _RUNTIME_ENV,
    _RUNTIME_TOKEN, _runtime_metadata, _runtime_environment, write_roster,
    disown_session, seats_for_session, seat_for_session, _resolve_seat,
    _STATE_MARKERS, _key_bounded, _bounded_sub, _move_seat_state,
    rename_seat, set_mute, mutes, _allowed_rooms,
    room_in_scope, _rooms_to_baseline, _baseline_rooms, rehome_seat,
)

# DELIVERY — the cursor, the bounded tail scan, and recipient resolution.
from .seats_delivery import (  # noqa: F401
    _sid8, cursor_path, _cursor, rotation_hold_offset,
    _write_cursor, _baseline_state, _write_cursor_path, _cursor_paths,
    room_active, _backfill_missing_room_cursors, _baseline_room_cursors, _init_cursor,
    _tail, scan_path, _fair_room_slice, _scan_rooms,
    _room_dirty, deliver, _deliver_unpaused, deliver_any,
    _resolve_against, resolve_recipient, recipient_capability, _dm_canonical,
    dm,
)

# JOIN + WAIT — a seat becomes addressable, and the beacon that wakes it.
from .seats_join import join, wait, _emit_line, _beacon_orphaned  # noqa: F401

# STOP SIGNALS — the two hard blocks plus the ladder's cheap probes.
from .seats_stop_signals import (  # noqa: F401
    _stop_fp_path, _rows_fp, _pending_rows, _pending_all,
    _off, BEACON_LATCH, beacon_procs, _strict_live,
    owes_beacon, _beacon_block, SPIRAL_LATCH, _SPIRAL_LANE,
    _spiral_meld, _melded_with, _spiral_gate, STOP_WHISPER_CAP,
    _WHISPER_FIRED_CAP, STUCK_AT, DIRTY_AT, SOLO_LOAD_AT,
    PENDING_STALE_S, RUNNER_TAIL_ROWS, _CODE_EDIT_RE, _ask_candidate,
    _dispatch_candidate, _runner_latest, _edited_code, _gate_candidate,
    STAGED_DEL_NAMED, _STAGED_DEL_PATH_CLIP, _STAGED_DEL_TOK_CLIP, _staged_deletions,
    _deletions_named, _deletions_fp, _unverified_candidate, _unbanked_candidate,
)

# ACK / CONSUME — SENT is not SEEN is not ACTED.
from .seats_ack import (  # noqa: F401
    _MENTION_TOKEN, _ts_epoch, _dm_lanes, _all_lanes,
    _is_seat, _recipients, _row_offsets, _recipient_cursor,
    consume_state, _locate_row, ack, pending,
)

# DELEGATION PROOF — positive evidence a held lease is being worked.
from .seats_delegation import (  # noqa: F401
    _lease_worktree, DELEGATION_WINDOW_S, _ACTIVITY_PREFIX, _STOPPED_PREFIX,
    _delegation_activity_path, _delegation_stop_path, _mark_agent_stopped, _agent_stopped,
    _get_claim_lease, _enclosing_claude_holder, _AGENT_KEY_PREFIX, _activity_record_key,
    _activity_record_valid, _record_delegation_activity, _unlink_delegation_activity, _get_delegation_activity,
    _is_pid_alive, _record_posttool_delegation, _clear_posttool_delegation, _delegated_build,
    _lane_stem, _gate_pending, release_hint, _claim_evidence_warning,
)

# CLAIMS — the advisory TTL lease and its liveness census.
from .seats_claims import (  # noqa: F401
    _flock_holder, _lock_unavailable, _claim_flocked, _unique_json_object,
    _claims_read, _binding_ok, claim, refresh_claim,
    claim_guard, rebind_claim_sessions, rollback_claim_sessions, release,
    _CENSUS_KEYS, _ROW_UNCERTAIN, _IDENTITY_ATTRIBUTED, _IDENTITY_ENUM,
    row_attributed, liveness_snapshot, claim_liveness_mark, claim_holder_liveness,
    release_stale, _pub_res, own_leases, claims_list,
)

# THE ROSTER REPORT — the one seats surface a human reads directly.
from .seats_report import (  # noqa: F401
    presence_of, session_owners, unverified_seats, presence_with_identity,
    identity_warning, PRESENCE_DOTS, presence_dot, set_status,
    _fmt_left, _WORKTREE_RES, STATUS_FRESH_S, STATUS_SKEW_S,
    _status_age, _status_by, _fmt_age, status_line,
    _claims_by_holder, _is_ephemeral_sa, presence_report, REAP_S,
    _unlink_seat_state, _transcript_exists, _transcript_hit, _COULD_HOST_SEAT,
    _live_process_evidence, _gc_keep_reason, gc_roster, _ROW_CAPS,
    _pub_row, runtime_label, roster_report,
)

# THE WORK OFFER — the one rung that can CHANGE the world, plus the
# ladder that decides whether a seat hears it.
from .seats_work_offer import (  # noqa: F401
    _live_seats, _live_claims, _session_holds_claim, _row_project,
    _offer_rows, _offer_landing_state, _finalize_work_offer, _AUTOCLAIM_KINDS,
    _work_offer_candidate, _session_delegated, _solo_load_candidate, _whisper_candidates,
    _stop_whisper,
)

# THE STOP GUARD — one function, one answer: may this seat idle?
# The ROOM READ helpers live in their own module: folding them into their
# only consumer pushed seats_stop_guard past the split's own 1000-line
# finish line, and leaving them in THIS file was never an option —
# seats_stop_guard calls them, and importing from the facade that imports
# it is a cycle.
from .seats_room_advice import (  # noqa: F401
    _ROOM_READS, _ROOM_ROWS_SHOWN,
    _ledger_snapshot, _missed, _room_advice, _room_unfinished,
)
from .seats_stop_guard import (  # noqa: F401
    LEASE_LATCH, LEASE_TTL_ALARM_S, NDP_LATCH, _ndp_gate, stop_guard,
)

# THE CLI VERBS AND HOOK LEGS — the top of the layering.
from .seats_cli import (  # noqa: F401
    HOOK_STDIN_DEADLINE_S, _HOOK_STDIN_STALL, _hook_stdin, _hook_stdin_plain,
    _hook_stall_note, _flag, _COUNCIL_FLAGS, _council_positionals,
    _cmd_claims, _cmd_council, _env_session, _hook_emit,
    _payload_homing, _addressed, catchup, cmd,
)

                         # arming to a large backlog must NOT replay it as one
                         # burst: each emit is a Monitor event, and >~10 events
                         # in a burst trips Monitor's firehose auto-stop → SIGTERM
                         # (exit 143), and the seat goes DEAF (a seat with a
                         # large backlog can die within seconds on every arm). Cap
                         # the pass; the remainder delivers next poll, and the
                         # agent's own catch-up `helm chat read` advances the
                         # cursor to drain the rest. Single-shot mode is unbounded
                         # (it returns on the first match — no burst possible).
OWNER_RAILS = ("web", "tui")  # server-side owner surfaces stamp these origins
# the join banner's onboarding pointer — resolved against THIS checkout so a
# seat in any cwd can open it; tests pin banner ↔ file together (moving the
# guide without repointing this breaks the suite, not the fleet)


# ---------------------------------------------------------------------------
# identity + addressing
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# small flock helper (premise.py's pattern; every shared-state RMW uses it)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# roster (RAM presence) — full row at join; per-seat `.seen` touch on the
# hot path (freeze bar 6: deliver never rewrites shared state)
# ---------------------------------------------------------------------------


# never re-homes into one, but a fan-out subagent is born there constantly


# ENOENT/ESRCH mid-scan is a process that LEFT — genuine absence. Anything
# else is a process we could not READ, and its silence is not evidence.


# ---------------------------------------------------------------------------
# the cursor + the tail scan both deliver and the report use
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# join (SessionStart) + wait (the beacon)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# stop-guard (Stop hook) — the idle gate. A work-arbiter capability, helm-native:
# an agent must not idle past its inbox or walk away holding a lease. Arbiter
# shape (the stop-arbiter law): resolve posture ONCE, inline checks against
# it, surface ALL blocking messages in ONE exit-2 (fix everything in one
# shot); WARN lines ride along without changing the exit. Block-once-per-
# pending-fingerprint (guard-stop-inbox-beacon law): the FIRST stop on a given
# pending-row set blocks and points; a re-stop on the SAME rows passes —
# never an infinite block loop — and any new row re-arms the block. The hook
# JSON's stop_hook_active flag (the harness's own already-continuing signal)
# is honored the same way. FAIL-OPEN TOTAL: a broken guard must never wedge
# the fleet (cmd wraps everything; kill-switch HELM_STOP_GUARD=0, per-check
# HELM_STOP_GUARD_INBOX/CLAIMS/INDEX/WHISPER/BEACON=0). Bounded reads (the
# cursor tail's SCAN_CAP, one /proc pass for the beacon gate), no network.
# ---------------------------------------------------------------------------


# ── the ARMED-BEACON gate ──────────────────────────────────────────────────
# THE INCIDENT: a seat was restarted onto a new model. Its inbox beacon (the
# mandated `helm chat wait --seat <seat> --follow` Monitor) died with the OLD
# process and was never re-armed. The seat then ran BLIND for ~1.5h: 98
# addressed rows accumulated undelivered, and it missed a cross-family gate
# verdict it was actively waiting on — each side believed the other was not
# blocked. EVERY other seat had a beacon; only the restarted one did not. An
# idle seat with no beacon cannot be woken by anything — so a turn that ends
# without one ends the seat's ability to be reached at all.
#
# The Stop hook already WARNED about this on a clean stop. Per helm's own canon
# (premise enforce-not-advise-for-repeated-behavior: a behavior the human must
# repeat becomes a DETERMINISTIC HARD HOOK, exit-2, never another advisory
# nudge) the warn becomes a BLOCK. Owner's words: "we need a helm stophook that
# fires contextually if no monitor is set to force that to happen before a
# first (or any) turn can end."
#
# PRECISION IS THE WHOLE GAME — a guard that blocks on a bad signal is worse
# than no guard. So the block is gated THREE ways and fails open on all of them:
#   1. only a LAUNCHED FLEET SEAT owes a beacon: HELM_CHAT_NAME must be set in
#      this process's env (the launch seam exports it, launch.build_env /
#      seat.launch_line) AND name THIS seat. An ad-hoc claude session in a helm
#      project auto-names itself and is never blocked — it keeps today's warn.
#   2. the seat must be roster-registered (its SessionStart join actually ran,
#      so a delivery lane really exists to go dark).
#   3. absence must be PROVEN: the process table is read directly, and probe
#      trouble (an unlistable /proc, a cmdline that fails to read for any reason
#      other than not-dumpable) means liveness is UNKNOWN, not absent — no
#      block. A not-dumpable same-uid process (systemd --user) is OPAQUE by
#      kernel design and skipped: it can never be a plain `helm chat wait`
#      python process, and failing closed on it disabled the guard on every
#      real host (caught on the first live run).
# Non-wedging by construction: the latch stores the last OBSERVED state, so the
# transition into `missing` blocks exactly ONCE (a re-stop passes) and a later
# loss re-arms it. Kill-switch HELM_STOP_GUARD_BEACON=0.


# ── the REVIEW-SPIRAL gate: at round three, open a meld ────────────────────
# THE INCIDENT: six serialized review rounds on one small lane.
# The rule that forbids it — the store heuristic `review-begins-with-cat-file`,
# "at TWO rounds the cure is a MELD, never round three" — was in the
# integrator's injected context on EVERY turn of that session. The owner ended
# it by hand ("codex round 6? couldn't have been fixed with a meld?") and one
# meld exchange then closed all three remaining questions.
#
# A RULE THAT FIRES AND IS NOT FOLLOWED NEEDS A GATE, NOT A LOUDER RULE. Owner,
# same night: "we still fail to reach for them automatically. maybe stophooks
# that recognize situations where they would be handy?" — the same escalation
# premise (enforce-not-advise-for-repeated-behavior) that turned the beacon
# warn above into a block.
#
# The detection lives in dispatches.review_spiral (distinct reviewed TIPS per
# lane, not dispatch count — a same-tip fan-out to two families is healthy
# cross-family review, and the thresholds are derived there from the live
# ledger). This half owns the LATCH and the message.
#
# NON-WEDGING BY CONSTRUCTION, three ways:
#   1. the latch stores blake2b(lane|rounds), so the SAME spiral state blocks
#      exactly once — a re-stop passes, and only a NEW round (rounds+1) re-arms
#      it, which is the one event that deserves another block;
#   2. an unwritable latch DEGRADES TO A WARN rather than blocking, because a
#      gate that cannot remember is a gate that blocks every stop forever;
#   3. every probe failure — unreadable ledger, raising detector, missing
#      lane/peer — yields no finding at all. Absence unproven is never absence.
# Kill-switch HELM_STOP_GUARD_SPIRAL=0.


# ── stop-whisper: the CONTEXTUAL continuation lane ─────────────────────────
# Lineage: per-toolcall-whispers-are-the-goal (contextual injection is the END
# GOAL; the cure for slop is BUDGETS — bytes caps, contextual gating,
# fail-closed-to-nothing — never removal) + the work-arbiter's hold-once-
# per-fingerprint-then-release + reflex.py's counter thresholds (field-tested
# 3/8) and salience law. A Stop hook's only agent-visible channel is the
# block reason (exit 2 stderr), so a whisper IS a soft hold: it fires ONCE
# per (signal, level) fingerprint with the right continuation, and the very
# next stop on the same state passes — never an infinite hold, never
# wallpaper. ONE budgeted line per stop (STOP_WHISPER_CAP), highest-salience
# unlatched signal wins, each line ends in a pull-depth pointer (tiny nudge,
# depth on demand — contextual-routing-preserves-lightness).


# ── work-offer: the fleet self-saturation rung (AX primitive #1) ───────────
# The dispatched-review gap: an idle seat let a dispatched review sit — the fleet
# does not self-saturate (an idle seat never picked up a canary review). This
# rung is the BOTTOM of the ladder (lowest salience): OWN work first (the ask /
# dispatch / pending-inbox / claim signals all outrank it), then, only when the
# seat is genuinely idle, ONE terse offer of the top UNOWNED backlog row. It
# soft-holds once per backlog HEAD (fp = the row id) — never every stop — and
# fails closed to silence.
#
# THE ACTUATOR on top (a unanimous council verdict): sensing alone converted
# NOTHING — 19 offers in 48h, 0 self-claims. So the one UNAMBIGUOUS case
# (head row dispatched TO this idle seat, kind self-assignable) is now
# CLAIMED by the rung itself and whispered as here-is-your-task
# (fp autoclaim:<id8>); every ambiguous case keeps the offer, and every
# uncertainty fails toward the offer — see _work_offer_candidate.
#
# UNOWNED = not owned by a DIFFERENT live seat and not already claimed. A
# dispatch is owned by its recipient: a row assigned to THIS idle seat, or
# stranded to an absent/gone recipient, is offerable; a row in-flight to
# another LIVE seat is theirs — never poach it (the litmus's "offer only
# unowned work"). Two DELIBERATE non-sources: owner-asks — the ask rung above
# already surfaces every unreported ask (own work first) and the idle gate
# requires none, so an idle seat's realized offer never draws from them; and
# the todo mirror — a pull surface with no claim-handoff verb (todos.py) whose
# fleet read is O(sessions), off the stop hot path by design.


# ---------------------------------------------------------------------------
# claims — the advisory TTL lease
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# the roster report (CLI table + GET /api/chat/roster + the seats panel)
# ---------------------------------------------------------------------------


# past this age it yields to a live claim — a holding lease is fresher
# evidence of what the seat is on than an hours-old announcement. A fresh
# status still beats a claim; a stale status with NO claim still shows (with
# its age on every surface). A missing/junk status_ts counts as stale:
# unknown age must never outrank a live lease.

# the future (NTP drift between writers) still reads age 0; FURTHER in the
# future is a plant — clamping it to 0 forever would invert the decay law
# (perpetually 'fresh', outranking every live lease), so it counts as junk,
# same bucket as a missing ts.


                # own (gc's first keep tier; the CLI hides older rows behind
                # --all). Presence ALONE never deletes anything anymore.


# ---------------------------------------------------------------------------
# ACK / CONSUME LADDER (AX primitive #3): SENT != SEEN != ACTED.
# An addressed word has a per-(recipient, row) state the SENDER can OBSERVE,
# derived READ-ONLY off the EXISTING delivery cursor + touch_seen — never a
# second ledger. SENT (row written) -> SEEN (the recipient's cursor passed the
# row, or it was active in a later second) -> ACTED (an explicit ack row on
# the same one-writer chat path). `pending` is the sender's view of everything
# not yet ACTED, so a message sent to a dead / wedged / away recipient is a
# VISIBLE object, not silent loss (the notify-me gap).
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# CLI (dispatched from chat.cmd_chat) + the hook legs
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# THE FACADE OWNS WHAT ITS ATTRIBUTES MEAN, even after the split moved them
# ---------------------------------------------------------------------------
# A caller that writes `mock.patch.object(seats, "roster", ...)` is not asking
# for an attribute to change on THIS object — it is asking the roster to
# behave differently for the code under test. That request was honoured for
# free while every reader lived in this file and read this file's globals.
# The split broke it silently: a reader now in seats_identity resolves the
# bare name `roster` against ITS OWN module globals, so the patch sets an
# attribute nothing in the call path reads, and the test passes while testing
# nothing. Measured twice on one gate — test_gate_fifo's _flocked patch and
# test_landgate's `home` MODULE patch, the second one a layer deeper.
#
# THE PER-CASE FIX DOES NOT TERMINATE. 52 patch sites across 18 test files
# name a seats attribute; most still work, and they break ONE AT A TIME as
# each reader moves, so "fix the failing one" is a job that is never finished
# and is never visibly unfinished either. So the facade takes the promise
# back: setting an attribute here sets it in every sibling that HOLDS that
# name, and deleting it deletes it there too. mock's save/restore then works
# unchanged, because restore is just another setattr through this same door.
#
# `hasattr` is the membership test on purpose — fanning out to a sibling that
# never imported the name would CREATE a global that module does not expect,
# which is a different bug wearing this fix's clothes. And `home`, an
# imported MODULE rather than a moved function, is covered by exactly the
# same rule: the siblings hold it, so a patch of it reaches them.
_IMPL_MODULES = ("seats_common", "seats_gate_queue", "seats_identity",
                 "seats_roster", "seats_delivery",
                 "seats_join", "seats_stop_signals",
                 "seats_ack", "seats_delegation",
                 "seats_claims", "seats_report",
                 "seats_work_offer", "seats_room_advice",
                 "seats_stop_guard",
                 "seats_cli")


def _impl_modules(_stems=_IMPL_MODULES, _pkg=__name__.rsplit(".", 1)[0]):
    """Live sibling modules, resolved late. Import order is not ours to
    assume: a sibling may not be in sys.modules yet when this file finishes
    executing, and pinning the objects here would freeze that accident.

    THE MACHINERY'S OWN INPUTS ARE CAPTURED AT DEF TIME (default args), here
    and in everything below down to the facade methods. Every one of these
    names is also an ATTRIBUTE OF THE FACADE, which means every one is
    patchable through the very door this machinery implements — and a
    mock.patch of `seats._types` broke the RESTORE path of every other patch
    on the stack (reproduced by an adversarial probe: the exit's setattr
    routed through the Mock and raised). A save/restore mechanism must not
    read its own moving
    parts out of the namespace it is mutating."""
    import sys as _sys
    out = []
    for stem in _stems:
        mod = _sys.modules.get("%s.%s" % (_pkg, stem))
        if mod is not None:
            out.append(mod)
    return out


# OWNERSHIP IS REMEMBERED, AND IT IS __dict__ MEMBERSHIP, NOT hasattr.
# Three defects made this necessary, all reproduced in cross-family review:
#
#   DELETE-THEN-RESTORE LEFT SIBLINGS WITHOUT THE NAME. mock's exit path can
#   be `del` followed by a set. __delattr__ removed the name from every
#   sibling, and __setattr__ then asked hasattr() — which was now FALSE
#   everywhere, because the delete had just destroyed the very evidence the
#   restore needed. The name came back on the facade alone.
#
#   hasattr IS A LOOKUP, NOT OWNERSHIP. A module __getattr__ answers True for
#   names the module does not own, so fanning out on hasattr CREATES a global
#   in a module that never had one — a different bug wearing this fix's
#   clothes.
#
#   A RESTORE MUST NOT CLOBBER A LIVE SIBLING PATCH. Patch a sibling
#   directly, then patch the facade, then let the facade patch exit: its
#   restore fanned out and overwrote the sibling patch that was still active.
#   So a sibling is only overwritten when it still holds the value WE last
#   wrote there; a diverged sibling is left alone, because something else
#   owns it now.
_FANOUT_OWNERS = {}
_MISSING = object()


def _fanout_owners(name, facade_owned,
                   _cache=_FANOUT_OWNERS, _mods=_impl_modules):
    """Sibling modules that SHARE this facade global, remembered across
    deletes — and computed ONLY for a name the facade itself held.

    `facade_owned` is the cure for the create-case (reproduced in
    cross-family review): a
    `mock.patch.object(seats, "_GATE_STATES", create=True)` names something
    the facade NEVER exported but a sibling privately owns. The old owners
    scan asked only "which sibling holds this name?", so the patch clobbered
    the sibling's private global and the create-case restore — a delattr —
    then DELETED it there outright. A name the facade never held is
    facade-only BY DEFINITION: no reader resolves it through this module, so
    there is nothing to keep in sync and the fanout must keep its hands off.
    The empty answer is cached too, so the later delattr of that created
    name cannot re-ask the question and get the wrong answer."""
    known = _cache.get(name)
    if known is None:
        known = (frozenset(m.__name__ for m in _mods()
                           if name in m.__dict__)
                 if facade_owned else frozenset())
        _cache[name] = known
    return known


class _SeatsFacade(_types.ModuleType):
    def __setattr__(self, name, value,
                    _set=_types.ModuleType.__setattr__,
                    _owners=_fanout_owners, _mods=_impl_modules,
                    _missing=_MISSING, _cache=_FANOUT_OWNERS):
        # THE PREDICATE IS "IN SYNC WITH THE FACADE", not "equals what I last
        # wrote". My first cure compared against the facade's own last write,
        # which is _MISSING on the FIRST facade set — so a sibling already
        # holding its own direct patch was clobbered by the very first
        # facade patch, before any restore was involved. Comparing against
        # what the FACADE HELD A MOMENT AGO is the right question: if the
        # sibling still agrees with it, the two are in sync and the write
        # belongs to both; if it has diverged, something else owns it and we
        # keep our hands off.
        was = self.__dict__.get(name, _missing)
        _set(self, name, value)
        if name.startswith("__"):
            return
        # facade_owned: held a moment ago, or held once (owners remembered
        # across the delete half of mock's delete-then-restore exit).
        owners = _owners(name, was is not _missing or name in _cache)
        for mod in _mods():
            if mod.__name__ not in owners:
                continue
            cur = mod.__dict__.get(name, _missing)
            if cur is not _missing and was is not _missing and cur is not was:
                continue                   # diverged: a live direct patch
            _set(mod, name, value)

    def __delattr__(self, name,
                    _del=_types.ModuleType.__delattr__,
                    _set=_types.ModuleType.__setattr__,
                    _owners=_fanout_owners, _mods=_impl_modules):
        _del(self, name)
        if name.startswith("__"):
            return
        owners = _owners(name, True)       # resolved BEFORE the siblings lose it
        for mod in _mods():
            if mod.__name__ not in owners:
                continue
            try:
                _del(mod, name)
            except AttributeError:
                pass



_sys_modules_self = _sys.modules[__name__]
_sys_modules_self.__class__ = _SeatsFacade
