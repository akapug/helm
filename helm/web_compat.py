"""Compatibility exports for :mod:`helm.web`.

This module owns no runtime behavior. It binds each monolith symbol to one
physical implementation owner while ``helm.web`` remains the patchable facade.
"""
import sys


_facade = sys.modules.get(__package__ + ".web")

_OWNER_NAMES = (
    ('web_common', (
        'BIND', 'DEFAULT_PORT', 'PACKAGE_DIR', 'MUTATION_TOKEN', '_source_stamp',
        '_LOADED_STAMP', 'code_drift', '_q1', '_DEFAULT_ROOM',
        'default_room', '_chat_profile', '_COCKPIT_BEAT', '_cockpit_beat',
        '_transcripts', 'MP_OWNER_CONNECTION',
    )),
    ('web_core', (
        'ENTRY_KEYS', 'ENTRY_CAP', 'REVIEW_STMT_CAP',
        'REGISTRY_UNREAD_KEYS', 'REGISTRY_CARD_KEYS',
        'REGISTRY_DETAIL_ROUTE', '_project_on_the_wire',
        '_project_detail_on_the_wire', '_api_project_detail',
        '_api_registry',
        '_api_projects_state', '_api_friction', '_api_friction_dial',
        '_POSTURE_SAID', '_api_posture', '_api_posture_post',
        '_api_store',
        '_api_store_review', '_api_store_confirm', '_api_store_reject',
        'DECISION_CTX_CAP', 'DECISION_LOSS_WINDOW_S', '_decision_recent',
        '_api_decisions', '_api_tasks', '_api_task_notes',
        '_api_tasks_comment',
        '_api_decisions_verdict', '_api_decisions_deliver',
        '_api_decisions_comment', '_api_whoami',
        'SESSION_KEYS', 'SESSION_CAP', '_api_sessions', '_READY_TTL_S',
        '_api_ready',
    )),
    ('web_configs', (
        '_api_configs', '_api_config_injection', '_api_inject_pack',
        '_api_inject_act', '_api_configs_cascade',
        '_api_configs_tree',
        '_api_configs_homes', '_api_configs_resolve', '_api_configs_file',
        '_api_configs_backups', '_api_configs_file_post',
        '_api_configs_entry_post', '_api_configs_restore_post', 'TRASH_DIR',
        'DISABLED_SUFFIX', '_api_skills', '_valid_skill_dir',
        '_api_skills_toggle', '_api_skills_delete', '_api_homes',
        '_api_homes_post', '_resolve_home_path', '_api_physics',
        '_api_physics_diff',
    )),
    ('web_cache', (
        '_qlock', '_qstate', '_qinflight', '_PROVIDER', '_provider', '_cached_swr',
        '_swr_rebuild', '_cached', '_COLD_BODY_MAX_S', '_qcold',
    )),
    ('web_quota', (
        '_catalog_rows', '_claude_home_identity', '_norm', 'get_creds',
        'get_history', '_probe_epoch', 'get_burn', '_alloc_models',
        'get_allocations', 'get_flags', '_api_flags',
        'HISTORY_WIRE_KEYS', 'HISTORY_GAUGE_WIRE_KEYS',
        '_history_on_the_wire',
        '_api_creds', '_api_history', '_api_burn', '_measured_accounts',
        '_api_allocate', '_api_quota_status', '_api_accounts',
        '_api_accounts_post',
    )),
    ('web_chat_rooms', (
        '_owner_read_path', '_owner_cursor', '_owner_signal', '_room_seats',
        '_ROOM_ROWS_MEMO', '_ROOM_ROWS_LOCK', '_room_rows_memo',
        '_dm_seat_map', '_dm_lanes',
        '_rooms_summary', '_ROOMS_SUM_CACHE', '_ROOMS_SUM_TTL',
        '_ROOMS_SUM_LOCK', '_rooms_summary_invalidate',
        '_rooms_summary_cached',
    )),
    ('web_chat', (
        'CHAT_WIN_DEFAULT', '_chat_gen', '_chat_win', '_CHAT_OLDER_CACHE',
        '_CHAT_OLDER_TTL', '_CHAT_OLDER_LOCK', '_room_stat',
        '_api_chat_older', '_api_chat_ids', '_api_chat',
        '_api_chat_read_post', '_api_chat_post', '_api_chat_dm',
    )),
    ('web_roster', (
        '_seat_ephemeral', '_ROSTER_REP_CACHE', '_ROSTER_REP_TTL',
        '_ROSTER_REP_LOCK', '_upstream_by_family', '_UPSTREAM_STALE_S',
        '_annotate_upstream', '_roster_cached', 'OWNER_PRESENCE_TTL',
        '_owner_row', '_api_chat_roster', '_api_notes',
        '_api_storage_matrix', '_api_todos', '_ROSTER_GIT_CACHE',
        '_ROSTER_GIT_TTL', '_roster_git_one', '_api_roster_git',
        '_api_chat_seat', '_api_chat_react',
    )),
    ('web_land_model', (
        '_LR_COLD_WAIT_S', '_LR_FLOOR_S', '_LR_HARD_TTL_S', '_LR_CLOSED_WINDOW_S',
        '_LR_CLOSED_CAP', '_LR_LANDS_CAP', '_LR_LAND_TASK',
        '_lr_ledger_paths', '_lr_newest_mtime', '_lr_snapshot', '_lr_reads',
        '_lr_epoch', '_lr_window',
        '_lr_repo', '_lr_land_task', '_lr_land_proof',
        '_lr_recent_lands', '_lr_native_chain',
    )),
    ('web_land', (
        '_lr_project', '_lr_build', '_api_lr',
    )),
    ('web_ledger', (
        'LEDGER_TURNS', 'LEDGER_STATUS_KEYS', '_LEDGER_WALK_TTL_S',
        '_TURN_HASH', '_turn_about',
        '_chat_transport', '_ledger_node', '_cell_seat_names',
        '_api_ledger', '_api_ledger_turn', 'NATIVE_ROWS', 'NATIVE_REC_KEYS',
        '_native_chat_pulse', '_api_ledger_native',
    )),
    ('web_sessions', (
        '_catalog_opensession', '_api_catalog', '_api_search', '_api_session',
        '_api_cmd', '_api_cwd_post', '_api_prune_post',
    )),
    ('web_multiplayer', (
        '_mp_actor', '_mp_caves', '_mp_pub', '_api_mp_state', '_api_mp_publish',
        '_api_mp_presence',
    )),
    ('web_sse', (
        '_SSE_WATCH_S', '_SSE_IDLE_WATCH_S', '_SSE_DEAD_S', '_sse_state',
        '_chat_fingerprint', '_log_names', '_CHAT_NAMES', '_NAMES_MAX_AGE_S',
        '_sse_tick', '_sse_watcher_dead', '_sse_watcher',
        '_sse_ensure_watcher', '_sse_stop',
    )),
    ('web_server', (
        'Handler', '_Server', 'make_server', 'cmd_web', '_port',
    )),
    ('web_owed', (
        '_api_owed',
    )),
    ('web_board', (
        '_api_board',
    )),
)
EXPORTS = {}


def _canonical_module(value, owner):
    canonical = __package__ + ".web"
    if getattr(value, "__module__", None) == owner:
        value.__module__ = canonical
    if not isinstance(value, type):
        return
    for member in vars(value).values():
        targets = (member.__func__,) if isinstance(
            member, (staticmethod, classmethod)) else (member,)
        if isinstance(member, property):
            targets = (member.fget, member.fset, member.fdel)
        for target in targets:
            if target is not None and getattr(
                    target, "__module__", None) == owner:
                target.__module__ = canonical


def _publish(module, names):
    owned = {name: module.__dict__[name] for name in names}
    for value in owned.values():
        _canonical_module(value, module.__name__)
    overlap = EXPORTS.keys() & owned.keys()
    if overlap:
        raise AssertionError("duplicate web compatibility owner: "
                             + min(overlap))
    EXPORTS.update(owned)
    if _facade is not None:
        _facade.__dict__.update(owned)


from . import web_common
_publish(web_common, _OWNER_NAMES[0][1])
from . import web_core
_publish(web_core, _OWNER_NAMES[1][1])
from . import web_configs
_publish(web_configs, _OWNER_NAMES[2][1])
from . import web_cache
_publish(web_cache, _OWNER_NAMES[3][1])
from . import web_quota
_publish(web_quota, _OWNER_NAMES[4][1])
from . import web_chat_rooms
_publish(web_chat_rooms, _OWNER_NAMES[5][1])
from . import web_chat
_publish(web_chat, _OWNER_NAMES[6][1])
from . import web_roster
_publish(web_roster, _OWNER_NAMES[7][1])
from . import web_land_model
_publish(web_land_model, _OWNER_NAMES[8][1])
from . import web_land
_publish(web_land, _OWNER_NAMES[9][1])
from . import web_ledger
_publish(web_ledger, _OWNER_NAMES[10][1])
from . import web_sessions
_publish(web_sessions, _OWNER_NAMES[11][1])
from . import web_multiplayer
_publish(web_multiplayer, _OWNER_NAMES[12][1])
from . import web_sse
_publish(web_sse, _OWNER_NAMES[13][1])
from . import web_server
_publish(web_server, _OWNER_NAMES[14][1])
from . import web_owed
_publish(web_owed, _OWNER_NAMES[15][1])
from . import web_board
_publish(web_board, _OWNER_NAMES[16][1])

OWNERS = tuple((globals()[module_name], names)
               for module_name, names in _OWNER_NAMES)
IMPL_MODULES = tuple(module for module, _names in OWNERS)
del _canonical_module, _facade, _publish, sys
