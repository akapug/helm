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
  spawn <seat> [--room R] [--cwd DIR] [--replace] [--print]  SELF-ONBOARDING
                                      spawn: reap a stale same-name seat only
                                      with explicit --replace, launch via the
                                      detected metaharness (orca/herdr pane +
                                      onboarding injection) or DETACHED HEADLESS
                                      when none (onboarding = the boot first-
                                      prompt), register spawn.json; --print
                                      shows the exact per-harness calls.
                                      DEFAULT cwd = the seat's OWN home worktree
                                      <repo>-wt/seats/<seat> (create-or-reuse),
                                      never the shared checkout; --cwd overrides
  where <seat> [--json]               resolve a spawned seat: harness,
                                      handle/pid, worktree, room, liveness.
                                      Falls through to an ORCA-ADOPTED pane
                                      (one the metaharness launched, which has
                                      no spawn register) and labels which it is
  panes [--json]                      every metaharness pane GROUPED BY
                                      PROVENANCE: helm-spawned vs orca-adopted
                                      vs unowned — they support different verbs,
                                      so they are never one flat list
  composers [--json]                  READ-ONLY probe for HELD-BUT-UNSENT
                                      composer text: a seat whose next
                                      instruction was typed and never submitted
                                      reads IDLE-AND-HEALTHY to liveness,
                                      proxywatch and the beacons alike. Answers
                                      held / clear / CANNOT-TELL per pane and
                                      never folds the third into the second —
                                      an unreadable pane is not a clean one.
                                      Never sends a keystroke: held text is as
                                      often a human's unfinished draft as a
                                      stranded directive
  adopt <seat> [--repo DIR]           give an ORCA-LAUNCHED seat the same
        [--base REF]                  private home worktree a helm-spawned one
                                      gets (<repo>-wt/seats/<seat>) + make it
                                      visible to orca; never migrates a live seat
  resume <seat> [--cwd DIR] [--force] relaunch the seat's pane via the metaharness
                                      (freshest launch.sh + --resume/--continue),
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
  resume-turn --hook-json             the RESUME LEG of that compaction: the
              [--status]              SessionStart(source=compact) hook that
                                      restarts the turn loop, so a compacted
                                      seat does not sleep until a human types.
                                      --status prints what it last did
  list | status                       seats, proxy liveness, cred expiry
  doctor                              binary + cred + seat health, read-only
  doctor --ensure [--json]            supervise: respawn any dead/wedged proxy;
                                      CPU canary flags a THRASHING backend
                                      (rc 1 WARN) before it dies silent; rc 2
                                      if any row stays UNKNOWN (cron it)
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
    _instance_gate,
    _instance_port,
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
    _config_yaml,
    _config_yaml_key,
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
    _up,
    _down,
    _seat_env,
    _smoke_multi_leg,
    _smoke,
)

from . import seat_lifecycle as _seat_lifecycle_impl
from .seat_lifecycle import (
    _seat_family,
    family_for,
    _unknown_seat_reason,
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
    _spawn_record,
    _pane_live,
    _prove_spawned_pane,
    _pane_sendable,
    _SEND_ONLY_DETAIL,
    _live_session_orca_identity,
    _prove_orca_replacement,
    _live_seat_orca_identity,
    _prove_orca_rebind,
    rebind_seat,
    _repair_orca_handle,
    _resolve_registered_pane,
    _reap_stale,
    _headless_spawn,
    _register_spawn,
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
    _PANE_ANSI,
    _PANE_PROMPT,
    _PANE_CHROME,
    _current_prompt_line,
    _classify_pane_tail,
    _PLAN_PATH_RE,
    _blocked_detail,
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
