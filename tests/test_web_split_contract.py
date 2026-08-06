#!/usr/bin/env python3
"""Compatibility contract for splitting :mod:`helm.web` without shrinking it."""
import inspect
import os
import pickle
import subprocess
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import web  # noqa: E402


_BASELINE_SURFACE = frozenset("""
API BIND BaseHTTPRequestHandler CHAT_WIN_DEFAULT DECISION_CTX_CAP
DECISION_LOSS_WINDOW_S DEFAULT_PORT DISABLED_SUFFIX ENTRY_CAP ENTRY_KEYS Handler
LEDGER_STATUS_KEYS LEDGER_TURNS MP_OWNER_CONNECTION MUTATION_TOKEN NATIVE_REC_KEYS
NATIVE_ROWS OWNER_PRESENCE_TTL PACKAGE_DIR POST_API QUERY_API REVIEW_STMT_CAP
SESSION_CAP SESSION_KEYS TRASH_DIR ThreadingHTTPServer _CHAT_OLDER_CACHE
_CHAT_OLDER_LOCK _CHAT_OLDER_TTL _COCKPIT_BEAT _COLD_BODY_MAX_S _DEFAULT_ROOM
_LOADED_STAMP _LR_CLOSED_CAP _LR_CLOSED_WINDOW_S _LR_COLD_WAIT_S _LR_FLOOR_S
_LR_FOLD _LR_HARD_TTL_S _LR_LANDS_CAP _LR_LANDS_WINDOW_S _PROVIDER
_READY_TTL_S _ROOMS_SUM_CACHE _ROOMS_SUM_LOCK _ROOMS_SUM_TTL _ROOM_ROWS_LOCK
_ROOM_ROWS_MEMO _ROSTER_GIT_CACHE _ROSTER_GIT_TTL _ROSTER_REP_CACHE
_ROSTER_REP_LOCK _ROSTER_REP_TTL _SSE_DEAD_S _SSE_IDLE_WATCH_S _SSE_WATCH_S _Server _TURN_HASH
_UPSTREAM_STALE_S _alloc_models _annotate_upstream _api_allocate _api_burn
_api_catalog _api_chat _api_chat_dm _api_chat_ids _api_chat_older _api_chat_post
_api_chat_react _api_chat_read_post _api_chat_roster _api_chat_seat _api_cmd
_api_configs _api_configs_backups _api_configs_cascade _api_configs_entry_post
_api_configs_file _api_configs_file_post _api_configs_homes _api_configs_resolve
_api_configs_restore_post _api_configs_tree _api_creds _api_cwd_post
_api_decisions _api_decisions_comment _api_decisions_deliver
_api_decisions_verdict _api_history _api_homes _api_homes_post _api_ledger
_api_ledger_native _api_ledger_turn _api_lr _api_mp_presence _api_mp_publish
_api_mp_state _api_notes _api_physics _api_physics_diff _api_prune_post
_api_quota_status _api_ready _api_registry _api_roster_git _api_search
_api_session _api_sessions _api_skills _api_skills_delete _api_skills_toggle
_api_storage_matrix _api_store _api_store_confirm _api_store_reject
_api_store_review _api_task_notes _api_tasks _api_tasks_comment _api_todos
_api_whoami _cached
_cached_swr
_CHAT_NAMES _NAMES_MAX_AGE_S _log_names
_catalog_opensession _catalog_rows _cell_seat_names _chat_fingerprint _chat_gen
_chat_profile _chat_transport _chat_win _claude_home_identity _cockpit_beat
_decision_recent _dm_lanes _dm_seat_map _ledger_node _lr_build _lr_epoch _lr_ledger_paths
_lr_native_chain _lr_newest_mtime _lr_project _lr_recent_lands _lr_repo
_lr_window _lr_witnessed _mp_actor _mp_caves _mp_pub _native_chat_pulse _norm
_owner_cursor _owner_read_path _owner_row _owner_signal _port _probe_epoch
_provider _q1 _qcold _qinflight _qlock _qstate _resolve_home_path
_room_rows_memo _room_seats _room_stat _rooms_summary _rooms_summary_cached
_rooms_summary_invalidate _roster_cached _roster_git_one _seat_ephemeral
_source_stamp _sse_ensure_watcher _sse_state _sse_stop _sse_tick _sse_watcher
_sse_watcher_dead _swr_rebuild _transcripts _turn_about _upstream_by_family
_valid_skill_dir calendar cmd_web code_drift default_room get_allocations get_burn
get_creds get_history hashlib json make_server math os re registry secrets
subprocess sys threading time urllib web_ui_loader
""".split())

_BRIDGE_SURFACE = frozenset({
    "_WEB_FANOUT_NAMES",
    "_WEB_IMPL_MODULES",
    "_WebModule",
    "_seed_impl_modules",
})
_SURFACE = _BASELINE_SURFACE | _BRIDGE_SURFACE

_IMPL_MODULE_NAMES = (
    "helm.web_common",
    "helm.web_core",
    "helm.web_configs",
    "helm.web_cache",
    "helm.web_quota",
    "helm.web_chat_rooms",
    "helm.web_chat",
    "helm.web_roster",
    "helm.web_land_model",
    "helm.web_land",
    "helm.web_ledger",
    "helm.web_sessions",
    "helm.web_multiplayer",
    "helm.web_sse",
    "helm.web_server",
)

_MODULE_BINDINGS = frozenset("""
calendar hashlib json math os re registry secrets subprocess sys threading time
urllib web_ui_loader
""".split())


def _fresh_process():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env = os.environ.copy()
    env["PYTHONPATH"] = root
    return root, env


class WebSplitContractTest(unittest.TestCase):

    def test_the_runtime_surface_remains_exact(self):
        current = {name for name in vars(web)
                   if not (name.startswith("__") and name.endswith("__"))}
        self.assertNotIn("_fabricated_split_bridge", current)
        self.assertEqual(current, _SURFACE)
        self.assertEqual(len(current), 218)

    def test_the_implementation_inventory_and_fanout_are_exact(self):
        self.assertEqual(tuple(m.__name__ for m in web._WEB_IMPL_MODULES),
                         _IMPL_MODULE_NAMES)
        self.assertEqual(web._WEB_FANOUT_NAMES,
                         _SURFACE - {"_WEB_FANOUT_NAMES"})
        self.assertEqual(len(web._WEB_FANOUT_NAMES), 217)

    def test_bootstrap_names_do_not_leak(self):  # noqa: VACUOUS_ASSERTION — the exact non-empty implementation inventory is the positive control for the two deliberately hidden bootstrap names
        self.assertFalse(hasattr(web, "_web_compat"))
        self.assertFalse(hasattr(web, "IMPL_MODULES"))

    def test_module_valued_bindings_keep_their_identity(self):  # noqa: VACUOUS_ASSERTION — exact non-empty name equality plus per-name sys.modules identity are unconditional positive controls
        current = {name for name, value in vars(web).items()
                   if inspect.ismodule(value)}
        self.assertEqual(current, _MODULE_BINDINGS)
        for name in _MODULE_BINDINGS:
            value = getattr(web, name)
            self.assertIs(value, sys.modules[value.__name__])
            for module in web._WEB_IMPL_MODULES:
                self.assertIs(module.__dict__[name], value)

    def test_assignment_and_deletion_fan_out(self):  # noqa: VACUOUS_ASSERTION — assignment first proves every module contains the marker before deletion proves the same binding is absent
        original = web._READY_TTL_S
        marker = object()
        try:
            web._READY_TTL_S = marker
            for module in web._WEB_IMPL_MODULES:
                self.assertIs(module._READY_TTL_S, marker)
            del web._READY_TTL_S
            for module in web._WEB_IMPL_MODULES:
                self.assertNotIn("_READY_TTL_S", module.__dict__)
        finally:
            web._READY_TTL_S = original

    def test_each_physical_module_can_be_the_first_import(self):  # noqa: VACUOUS_ASSERTION — every exact owner proves one-way import before its first exported object is rebound to the facade
        from helm import web_compat

        root, env = _fresh_process()
        code = ("import importlib,sys; "
                "owner=importlib.import_module(sys.argv[1]); "
                "assert 'helm.web' not in sys.modules; "
                "facade=importlib.import_module('helm.web'); "
                "assert owner.__dict__[sys.argv[2]] is "
                "getattr(facade,sys.argv[2])")
        for (module, names), module_name in zip(web_compat.OWNERS,
                                                _IMPL_MODULE_NAMES):
            self.assertEqual(module.__name__, module_name)
            p = subprocess.run(
                [sys.executable, "-P", "-c", code, module_name, names[0]],
                cwd=root, env=env, capture_output=True, text=True)
            self.assertEqual(p.returncode, 0, (module_name, p.stderr))
        p = subprocess.run(
            [sys.executable, "-P", "-c",
             "import importlib,sys; "
             "compat=importlib.import_module('helm.web_compat'); "
             "assert 'helm.web' not in sys.modules; "
             "facade=importlib.import_module('helm.web'); "
             "assert compat.IMPL_MODULES == facade._WEB_IMPL_MODULES"],
            cwd=root, env=env, capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)

    def test_concurrent_first_imports_do_not_invert_module_locks(self):  # noqa: VACUOUS_ASSERTION — 40 fresh processes cross both former owner/compat and facade/compat lock inversions
        root, env = _fresh_process()
        code = """
import importlib
import sys
import threading
barrier = threading.Barrier(2)
errors = []
def load(name):
    barrier.wait()
    try:
        importlib.import_module(name)
    except BaseException as error:
        errors.append((type(error).__name__, str(error)))
a = threading.Thread(target=load, args=(sys.argv[1],))
b = threading.Thread(target=load, args=(sys.argv[2],))
a.start()
b.start()
a.join()
b.join()
assert not errors, errors
"""
        pairs = (("helm.web_compat", "helm.web_cache"),
                 ("helm.web", "helm.web_compat"))
        for pair in pairs:
            for _ in range(20):
                p = subprocess.run(
                    [sys.executable, "-P", "-c", code, *pair],
                    cwd=root, env=env, capture_output=True, text=True)
                self.assertEqual(p.returncode, 0, (pair, p.stderr))

    def test_facade_callables_unpickle_in_a_fresh_process(self):  # noqa: VACUOUS_ASSERTION — 137 exports plus 9 class methods are counted, canonically named, and rebound by identity in the child
        from helm import web_compat

        root, env = _fresh_process()
        items = [(name, value) for name, value in web_compat.EXPORTS.items()
                 if inspect.isfunction(value) or inspect.isclass(value)]
        self.assertEqual(len(items), 137)
        self.assertEqual({value.__module__ for _name, value in items},
                         {"helm.web"})
        methods = [(cls.__name__ + "." + name, value)
                   for cls in (web.Handler, web._Server)
                   for name, value in vars(cls).items()
                   if inspect.isfunction(value)]
        self.assertEqual(len(methods), 9)
        self.assertEqual({value.__module__ for _name, value in methods},
                         {"helm.web"})
        payload = pickle.dumps(items + methods)
        p = subprocess.run(
            [sys.executable, "-P", "-c",
             "import pickle,sys; from helm import web; "
             "items=pickle.load(sys.stdin.buffer); "
             "resolve=lambda n: __import__('functools').reduce(getattr, "
             "n.split('.'), web); "
             "bad=[n for n,v in items if v is not resolve(n)]; "
             "assert len(items)==146,(len(items),bad); assert not bad,bad"],
            cwd=root, env=env, input=payload, capture_output=True)
        self.assertEqual(p.returncode, 0, p.stderr.decode())

    def test_provider_publication_uses_the_facade(self):  # noqa: VACUOUS_ASSERTION — the provider marker is asserted on the facade and every implementation module before restoration
        from helm import providers

        original = web._PROVIDER
        marker = object()
        try:
            web._PROVIDER = None
            with mock.patch.object(providers, "default_provider",
                                   return_value=marker):
                self.assertIs(web._provider(), marker)
            self.assertIs(web._PROVIDER, marker)
            for module in web._WEB_IMPL_MODULES:
                self.assertIs(module._PROVIDER, marker)
        finally:
            web._PROVIDER = original

    def test_reload_recreates_the_monolith_runtime_state(self):  # noqa: VACUOUS_ASSERTION — a populated pre-reload cache and four captured identities are contrasted with exact fresh post-reload values
        import importlib

        state = web._qstate
        endpoint = web._api_registry
        handler = web.Handler
        token = web.MUTATION_TOKEN
        state["probe"] = "reload must discard this"
        web.DEFAULT_PORT = 8123
        importlib.reload(web)
        self.assertIsNot(web._qstate, state)
        self.assertEqual(web._qstate, {})
        self.assertIsNot(web._api_registry, endpoint)
        self.assertIsNot(web.Handler, handler)
        self.assertNotEqual(web.MUTATION_TOKEN, token)
        self.assertEqual(web.DEFAULT_PORT, 7433)
        self.assertEqual(web.make_server.__defaults__, (7433,))

    def test_module_execution_reuses_the_package_module_identity(self):
        root, env = _fresh_process()
        p = subprocess.run(
            [sys.executable, "-P", "-m", "helm.web", "--help"],
            cwd=root, env=env, capture_output=True, text=True)
        self.assertEqual(p.returncode, 2, p)
        self.assertIn("usage: helm web", p.stderr)
        self.assertNotIn("Traceback", p.stderr)


if __name__ == "__main__":
    unittest.main()
