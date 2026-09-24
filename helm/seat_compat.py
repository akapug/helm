"""Compatibility exports for :mod:`helm.seat`.

This module owns no runtime behavior. It collects the monolith's import-time
bindings and immutable CLI data so ``helm.seat`` can remain the patchable
compatibility facade while its six physical authority functions stay there.
"""
import base64
import glob
import hashlib
import json
import math
import os
import re
import shlex
import shutil
import signal
import socket
import stat
import subprocess
import sys
import time

from . import home

from . import seat_catalog as _seat_catalog_impl
from .seat_catalog import (
    AUTOCOMPACT_PCT_OVERRIDE,
    CHILD_STAMP_VARS,
    CODEX_HOMES,
    DREGG_SIGNER_DEFAULT,
    FAMILIES,
    HERMES_AUTH,
    OBSERVED_FLOOR_HEADROOM,
    OPENCODE_AUTHSTORE,
    PLAN_ENTRY_TOOL,
    SCHEMA_UNSAFE_TOOLS,
    SPAWN_DENIED_TOOLS,
    PROXY_MODES,
    denied_tools,
    family_catalogued_models,
    family_route_aliases,
    FEEDBACK_ENV,
    FEEDBACK_DRAFTS_SETTING,
    FEEDBACK_RULE,
    feedback_env_words,
    PROXY_BIN_DEFAULT,
    SCRUB_VARS,
    WINDOW_BACKINGS,
    _CC_ASSUMED_WINDOW_MIRROR,
    _QUOTED_COUNT,
    _QUOTED_WORD,
    _family_owner_aliases,
    _family_port_bases_are_unique,
    _owner_statement_reason,
    _quote_names_family,
    _quoted_token_counts,
    proxy_route_family,
    provider_rung,
    proxy_route_response_model,
    proxy_routes,
    quota_group,
    quota_group_families,
    quota_group_phrase,
    QUOTA_GROUP_UNMEASURED,
)

_USAGE = """usage: helm seat <verb> [args]
  add <family> [--auth-from <path>]   mint the seat (translate cred read-only)
               [--key-from <path>]    proxy-key families: .env-style key file
               [--room R]             override the project-derived chat room
  up <family> | down <family>         start/stop the seat's local proxy
  launch <family> [--model M] [--room R] [--multi]  print the exact launch line (never runs it)
                                      --multi: mixed-model fleet — DROP the
                                      CLAUDE_CODE_SUBAGENT_MODEL pin (it blunt-pins
                                      over per-agent frontmatter) + mint probe agents
                                      EITHER WAY the seat's proxy config aliases
                                      CC's built-in frontmatter ids to a model the
                                      family serves: its launch model, or — where
                                      the family declares subagent_tiers — a model
                                      PER ID (codex: opus/fable -> gpt-6-astra,
                                      sonnet/haiku -> gpt-5.6-sol, so one pane
                                      bursts into sol workers + astra checkers)
                                      A proxy seat launches WITHOUT Skill and
                                      Agent(fork) (the deny set) and WITH
                                      Workflow, capped: the line exports
                                      CLAUDE_CODE_WORKFLOW_MAX_CONCURRENT_AGENTS=4
                                      and a workflow agent's model lands on
                                      the seat's own tier through that alias
                                      block (a sol seat never reaches astra)
  spawn <seat> [--room R] [--cwd DIR] [--model M]
               [--role worker|lead] [--replace] [--print]  SELF-ONBOARDING
                                      spawn. <seat> is <family>, <family>-N, or
                                      the PROJECT-CANONICAL <project>-<family>
                                      (acme-codex, acme-claude): the family
                                      is the last segment and the project form
                                      takes claude or codex ONLY — the families
                                      whose gate admits a non-family instance.
                                      <family>-N takes the OAuth-pool families
                                      only (a numbered seat IS a per-instance
                                      proxy, and the gate refuses a numbered
                                      proxy-key family).
                                      Native `claude` has no bare or numbered
                                      form; it rides `helm launch --seat` (no
                                      proxy, no port, no launch.sh). The
                                      project must be registered AND own the
                                      workspace: its checkout, a registered
                                      linked worktree, or its recorded cv
                                      scope. A project-codex seat gets its own
                                      allocated proxy endpoint, never the
                                      family base. The project is recorded on
                                      the seat and round-trips into where and
                                      the reboot sweep, and into
                                      resume/up/down as an ANSWER: a native
                                      seat has no launch.sh to replay and no
                                      proxy to start or stop, and each of those
                                      three says exactly that instead of
                                      calling its family unknown; a
                                      second same-family
                                      seat for one project prints a notice
                                      naming the existing one. An unregistered
                                      project, unknown family or wrong-project
                                      workspace is REFUSED, naming the registry
                                      and the admitted forms.
                                      An explicit lead starts Claude Code
                                      in ultracode mode; reap a stale same-name
                                      seat only
                                      with explicit --replace, launch via the
                                      detected metaharness (orca/herdr pane +
                                      onboarding injection) or DETACHED HEADLESS
                                      when none (onboarding = the boot first-
                                      prompt), register spawn.json; --print
                                      shows the exact per-harness calls.
                                      DEFAULT cwd = the seat's OWN home worktree
                                      <repo>-wt/seats/<seat> (create-or-reuse),
                                      never the shared checkout; --cwd overrides
  rehome <seat> --home H [--model M]  move a LIVE seat onto a named credhome
         [--apply]                    in one verb: prove the home's token is
                                      live or syncable from Orca (a home whose
                                      refresh chain has EXPIRED is refused with
                                      the login remedy, because `helm launch`
                                      would start the session on the stale
                                      token), resolve the seat's CURRENT roster
                                      session and its pane from process
                                      evidence, /exit the pane in ONE wake
                                      (never a signal — a killed session loses
                                      the transcript flush the relaunch
                                      resumes), wait for the process to be
                                      PROVEN gone, type `helm launch --seat S
                                      --home H -- --model M --resume SID` into
                                      that same pane, then verify the pane, the
                                      roster register and the inbox beacon and
                                      record one ledger row. Dry-run without
                                      --apply, and the dry run IS the plan: it
                                      prints the exact launch line
  where <seat> [--json]               resolve a spawned seat: harness,
                                      handle/pid, worktree, room, liveness.
                                      Falls through to an ORCA-ADOPTED pane
                                      (one the metaharness launched, which has
                                      no spawn register) and labels which it is
  reassign <seat-or-session> --to <seat>   move EVERY holding of a dead or
        [--reason R] [--force]        renamed seat in ONE verb and ONE ledger
        [--apply] [--json]            event: open/held dispatch rows (both the
                                      recipient half AND the issuer half, which
                                      a rename orphans just as thoroughly),
                                      task rows, and worktree leases. Dry-run
                                      by default; the manifest prints either
                                      way. SOURCE RESOLVES BY SESSION FIRST,
                                      because the case this exists for is the
                                      one where the name already changed under
                                      the holdings; a token with no roster row
                                      is the ORPHAN case, not an error. TARGET
                                      resolves an exact seat, else a FAMILY
                                      naming exactly one live seat (two is an
                                      ambiguity to report, never a tie to
                                      break). REFUSES only on a measured
                                      contradiction — a LIVE source without
                                      --force --reason, or a target nothing
                                      answers to — never on absence, because
                                      absence is the normal state of the
                                      evidence the morning after a reboot
  panes [--json]                      every metaharness pane GROUPED BY
                                      PROVENANCE: helm-spawned vs orca-adopted
                                      vs unowned — they support different verbs,
                                      so they are never one flat list
  retire-deny <tool> [--seat S|--all] [--apply]
                                      list (dry run) every proxy-seat
                                      settings.json carrying a deny of <tool>
                                      that neither helm.seeded_denies nor
                                      helm.operator_denies records, and what
                                      --apply would remove; --apply removes
                                      exactly that entry from exactly those
                                      files. A refresh never removes an
                                      unrecorded deny. Applying this
                                      fleet-wide is an OWNER decision;
                                      nothing in deploy or doctor runs it.
                                      Refuses a tool still in the deny set;
                                      a candidate that is REFUSED (a linked
                                      seat dir or child), UNREADABLE or
                                      MALFORMED (a record that is not a list
                                      of strings) is listed, blocks every
                                      write, and exits 1.
  retitle [--apply] [--json]          assert every namable pane's TAB TITLE as
                                      the SEAT NAME exactly as the fleet board
                                      shows it and nothing else, so a tab strip
                                      can be matched against the board by eye.
                                      Orca reverts these on every reboot.
                                      Dry-run by default, and the dry run IS
                                      the table: current title beside the one
                                      helm would write. Idempotent (a pane
                                      already correct is never re-written) and
                                      never fatal (a failed rename degrades to
                                      a row). A pane helm cannot NAME keeps its
                                      orca title untouched — a wrong
                                      helm-written title is worse than an
                                      honest host-written one. helm only ever
                                      WRITES a title; nothing reads one to
                                      decide who a seat is
  composers [--json] [--submit HANDLE]  probe for HELD-BUT-UNSENT composer
                                      text. The default scan is keystroke-free
                                      and answers held / helm-pending /
                                      helm-stranded / clear / cannot-tell;
                                      unknown is never folded into clear.
                                      Explicit --submit is a guarded actuator:
                                      it sends only bare Enter, and only for an
                                      exact recorded injection proven persistent
                                      across independent observations. Unmatched
                                      text remains a possible human draft
  adopt <seat> [--repo DIR]           give an ORCA-LAUNCHED seat the same
        [--base REF]                  private home worktree a helm-spawned one
                                      gets (<repo>-wt/seats/<seat>) + make it
                                      visible to orca; never migrates a live seat
  resume <seat> [--cwd DIR] [--role worker|lead] [--force]
                                      relaunch the seat's pane via the metaharness
                                      (freshest launch.sh + --resume/--continue),
                                      preserving its recorded role unless overridden,
                                      then re-arm its WAKE PATH (the inbox beacon
                                      is a per-session Monitor — a restart kills
                                      it, and a resumed session gets no turn to
                                      re-arm on its own). --cwd overrides the
                                      recorded/sniffed cwd (the shared checkout
                                      is the sane value); a recorded cwd that no
                                      longer exists or sits in a removed worktree
                                      REFUSES instead of spawning somewhere stale.
                                      An orca-adopted seat replays its TRANSCRIPT
                                      instead, and is REFUSED while anything still
                                      holds the seat open (--force overrides)
  smoke <family> [--multi]            the 4-leg acceptance gate (prompt/tool/subagent/whisper);
                                      --multi adds the mixed-model fan-out leg (conductor-log-verified)
  autocompact [--threshold N] [--once]  proxy-seat context watchdog: read each
                                      seat's context%%, inject /compact at the
                                      threshold BEFORE the 100%% hang (latched;
                                      --install-timer for the cadence)
  unblock [--seat S] [--dry-run]      answer only Claude Code's built-in
          [--quiet] [--json]          plan-execution prompt on an agent seat.
                                      Human permission dialogs, owner panes,
                                      unreadable or gated plans, and prompts
                                      that change before the locked send are
                                      surfaced without a keystroke
  boot-brief [--rearm]                expand the short exact-visible first turn
                                      into this seat's full onboarding/restart
                                      brief from its own process environment
  resume-turn --hook-json             the RESUME LEG of that compaction: the
              [--status] [--show DIGEST] SessionStart(source=compact) hook that
                                      restarts the turn loop, so a compacted
                                      seat does not sleep until a human types.
                                      --status prints what it last did; --show
                                      resolves a stored long directive token
  lifecycle show [<seat> --incarnation ID]
          | record <seat> renamed --successor S --incarnation ID --evidence REF
                                      inspect or append measured rename facts;
                                      disowned/decommissioned always refuse
  list | status                       seats, proxy liveness, cred expiry
  doctor                              binary + cred + seat health, read-only
  doctor --ensure [--json|--quiet]    supervise: respawn any dead/wedged proxy;
                                      CPU canary flags a THRASHING backend;
                                      --quiet prints only non-healthy rows plus
                                      a periodic heartbeat; rc 2 on UNKNOWN.
                                      Each pass also runs the cred-follow rung
                                      below, so the pool learns an orca account
                                      switch within one pass
  cred-follow [--apply] [--json]      make the codex proxy pool carry whichever
                                      account orca has ACTIVE, read daemon-free
                                      from orca's provenance file. Dry run by
                                      default and the dry run IS the table
                                      (PRESENT/MISSING/DISABLED/MISSING-AUTH/
                                      UNREADABLE/SKIPPED/UNKNOWN). Imports ONCE
                                      per account and never over an account
                                      already pooled: the proxy rotates its own
                                      refresh token away from orca's copy.
                                      Orca's files are never written
families: %s""" % ", ".join(sorted(FAMILIES))

from . import seat_env as _seat_env_impl
from .seat_env import (
    child_stamp_unsets,
    paste_unset_prefix,
    scrub_env,
    scrub_prefix,
)

# every var a pasteable command must clear, in ONE tuple so a site cannot
# carry half of them
PASTE_UNSET_VARS = CHILD_STAMP_VARS + SCRUB_VARS

from . import seat_paths as _seat_paths_impl
from .seat_paths import (
    CRED_ABSENT,
    CRED_EXPIRED,
    CRED_UNKNOWN,
    CRED_VALID,
    PROXY_CURRENT,
    PROXY_NO_RECORD,
    PROXY_PID_DEAD,
    PROXY_PID_REUSED,
    PROXY_STALE,
    PROXY_UNKNOWN,
    PROXY_UNVERIFIABLE,
    _BINARY_IDENTITY_TYPES,
    _PROXY_LAUNCH_RECORD_V,
    _PROXY_RECORD_UNSET,
    _binary_identity,
    _config_digest,
    _contextlib,
    _decode_launch_inputs,
    _encode_launch_inputs,
    PROJECT_PORT_BASE,
    PROJECT_PORT_SPAN,
    _instance_gate,
    _instance_port,
    _existing_instance_port,
    _instance_endpoint_error,
    _launch_endpoint,
    _maintenance_endpoint,
    _instance_ports_path,
    _project_port_block_is_clear,
    _numbered_port_collision,
    _numbered_port_reservations_are_disjoint,
    _seat_spawn_gate,
    allocate_instance_port,
    instance_port_ledger,
    _jwt_claims,
    _pid_alive,
    _pid_identity,
    _port_open,
    _proxy_bin,
    _proxy_home,
    _proxy_launch_inputs,
    _proxy_lock,
    _proxy_pid_record,
    _proxy_pid_verdict,
    _recorded_pid_alive,
    _rfc3339,
    _running_pid,
    _running_pid_rec,
    _valid_binary_identity,
    _write_private,
    proxy_drift,
    seat_dir,
    seats_root,
)

from . import seat_ports as _seat_ports_impl
from .seat_ports import (
    _TCP_LISTEN,
    _adopt_or_refuse_port,
    _argv_config,
    _listen_inodes,
    _pid_holding_socket,
    _pids_holding_socket,
    _port_listener,
    _port_listeners,
    _proc_argv,
    _proc_root,
    _read_token,
    _same_config,
    _socket_holders,
    _token_export,
    _token_file,
)

from . import seat_credentials as _seat_credentials_impl
from .seat_credentials import (
    _UNBLOCK,
    _cred_exp,
    _cred_expiry,
    _cred_remaining,
    _cred_rolling,
    _expired_field_epoch,
    _newest_unpooled_codex_auth,
    codex_cred_state,
    cred_state,
    newest_valid_codex_auth,
    translate_codex_auth,
)

from . import seat_provision as _seat_provision_impl
from .seat_provision import (
    proxy_debug,
    _auth_path_outside_the_seat,
    _seat_owning,
    _config_yaml,
    _config_yaml_key,
    proxy_config_plan,
    regenerate_proxy_config,
    proxy_alias_drift,
    _key_base_url,
    _instance_dir,
    _SEAT_SURFACE_REFUSED,
    _seat_surface_error,
    _nested_surface_error,
    _dq_escape,
    launch_line,
    _seat_token,
    _link_skills,
    _ONBOARD_KEYS,
    _FEATURE_CACHE_KEYS,
    _FEATURE_CACHE_GATE,
    _FEATURE_CACHE_MIN_FEATURES,
    _FEATURE_CACHE_MAX_AGE_MS,
    _FEATURE_CACHE_FUTURE_SKEW_MS,
    _feature_cache_complete,
    _feature_cache_seed,
    _warn_feature_cache,
    _onboarded_refs,
    _git_toplevel,
    _seed_onboarding,
    _seed_seat_settings,
    _seed_seat_rules,
    _PROBE_AGENT_MD,
    probe_agents,
    _mint_probe_agents,
    _mint_instance_proxy,
    _seat_token_per,
    _write_launch_assets,
    _write_launch_sh,
    _env_file_value,
    _hermes_pool_key,
    _opencode_authstore_key,
    _resolve_homing,
    _add_proxy_key,
    _adoptable_cred,
    _oauth_cred_reason,
    _add_proxy_oauth,
    _glob_auth,
    _add,
)

from . import seat_proxy as _seat_proxy_impl
from .seat_proxy import (
    _require_seat,
    _owned_process_state,
    _stop_owned_proxy,
    _up,
    _down,
    _seat_env,
    _smoke_multi_leg,
    _smoke,
)

from . import seat_lifecycle as _seat_lifecycle_impl
from .seat_lifecycle import (
    seat_model_phrase,
    ONBOARDING_ABSENT,
    ONBOARDING_INVALID,
    ONBOARDING_NONE,
    ONBOARDING_VALID,
    Onboarding,
    onboarding_invalid,
    parse_onboarding,
    _record_onboarding,
    _seat_family,
    family_for,
    _unknown_seat_reason,
    NATIVE_FAMILY,
    spawn_families,
    numbered_families,
    project_families,
    spawn_identity,
    _named_seat_family,
    registered_seat_family,
    _split_seat,
    _SESSION_JSONL,
    _STUB_MAX_BYTES,
    _has_real_turn,
    _PRUNE_HEAD_BYTES,
    _prune_source,
    _newest_seat_session,
    _seat_session_path_by_id,
    _seat_session_by_id,
    _homing_from_launch,
    _room_from_launch,
    _multi_from_launch,
    _ensure_autocompact_timer,
    SPAWN_SEND_DELAY_S,
    onboarding_prompt,
    rearm_prompt,
    _spawn_path,
    _seat_lifecycle_lock,
    _seat_lifecycle_lock_released,
    _spawn_record,
    _persisted_model,
    _pane_live,
    _prove_spawned_pane,
    _pane_sendable,
    _SEND_ONLY_DETAIL,
    _live_session_orca_identity,
    _prove_orca_replacement,
    _live_seat_orca_identity,
    _prove_orca_rebind,
    rebind_seat,
    rebind_refusal_means_dead,
    NO_EXACT_LIVE_SESSION,
    NO_DISTINCT_LIVE_PANE,
    _repair_orca_handle,
    _resolve_registered_pane,
    _reap_stale,
    _headless_spawn,
    _register_spawn,
    SPAWN_ATTEMPT_PENDING,
    SPAWN_ATTEMPT_COMPLETE,
    SPAWN_ATTEMPT_INCOMPLETE,
    SPAWN_CHILD_FIELDS,
    SESSION_BINDING_HARNESSES,
    _new_spawn_attempt,
    SPAWN_ATTEMPT_ENV,
    ATTEMPT_OWN,
    ATTEMPT_LIVE,
    ATTEMPT_GONE,
    ATTEMPT_UNVERIFIABLE,
    spawn_attempt_token,
    _publish_spawn_attempt,
    _attempt_process_state,
    _pending_attempt_conflict,
    _finalize_spawn_attempt,
    _backfill_spawn_session,
    _sessionstart_pane_fields,
    _bind_spawn_session,
    _spawn_plan,
    _seat_home_cwd,
    _resume_cwd,
    _stale_resume_cwd,
    _report_rehome,
    _spawn_args,
    _adopt,
    _where_adopted,
    REBIND_INTERVAL_S,
    _REBIND_SERVICE,
    _REBIND_TIMER,
    rebind_timer_units,
    ensure_rebind_timer,
    _rebind,
    registered_seats,
    _upstream_row,
    upstream_phrase,
    _where,
    _LIVENESS_STATES,
    _STATE_NAMES,
    RESTART_HELPFUL,
    RESTART_NOT_HELPFUL,
    RESTART_UNKNOWN,
    remediation_text,
    upstream_remediation,
    _PANE_ANSI,
    _PANE_PROMPT,
    _PANE_CHROME,
    _current_prompt_line,
    _classify_pane_tail,
    _wall_in_force,
    WALL_IN_FORCE,
    WALL_EXPIRED,
    WALL_UNDATED,
    WALL_UNANCHORED,
    _pool_expiry,
    _PLAN_PATH_RE,
    _blocked_detail,
    PLAN_EXECUTION_PROMPT,
    PERMISSION_PROMPT,
    _PLAN_EXECUTION_RE,
    _OPTION_RE,
    _AFFIRMATIVE_RE,
    _NEGATIVE_RE,
    _prompt_type,
    _prompt_options,
    affirmative_choice,
    vendor_escape_choice,
    ESCAPE_CONTINUE,
    ESCAPE_SPEND,
    ESCAPE_HUMAN,
    ESCAPE_ABSENT,
    _liveness_from_orcaadopt,
    seat_liveness,
)

from . import seat_health as _seat_health_impl
from .seat_health import (
    _minted_instances,
    _minted_seats,
    _proxy_live_text,
    _seat_row,
    _status,
    _CONFIG_INVARIANTS,
    _config_values,
    config_drift,
    _config_drift_lines,
    _alias_drift_lines,
    _doctor,
    _ENSURE_STARTUP_GRACE_S,
    _proxy_age_s,
    _CPU_SAMPLE_MAX_AGE_S,
    _env_float,
    _proc_cpu_sample,
    _cpu_sample_path,
    _cpu_canary,
    _canary_text,
    _ensure_row,
    _ensure,
)

IMPL_MODULES = (
    _seat_catalog_impl,
    _seat_credentials_impl,
    _seat_env_impl,
    _seat_health_impl,
    _seat_lifecycle_impl,
    *_seat_lifecycle_impl._IMPL_MODULES,
    _seat_paths_impl,
    _seat_ports_impl,
    _seat_provision_impl,
    *_seat_provision_impl._IMPL_MODULES,
    _seat_proxy_impl,
)

EXPORTS = {name: value for name, value in globals().items()
           if name != "IMPL_MODULES"
           and not (name.startswith("__") and name.endswith("__"))}
