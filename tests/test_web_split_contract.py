#!/usr/bin/env python3
"""Compatibility contract for splitting :mod:`helm.web` without shrinking it."""
import ast
import glob
import inspect
import json
import os
import pickle
import subprocess
import sys
import tempfile
import textwrap
from types import ModuleType
import unittest
from unittest import mock

# -P (safe path) exists only from 3.11; older interpreters get no flag and
# the assertions below still hold — cwd never shadows the package imports here
_SAFE_PATH = ("-P",) if sys.version_info >= (3, 11) else ()

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import web  # noqa: E402


_BASELINE_SURFACE = frozenset("""
API BIND BaseHTTPRequestHandler CHAT_WIN_DEFAULT DECISION_CTX_CAP
DECISION_LOSS_WINDOW_S DEFAULT_PORT DISABLED_SUFFIX ENTRY_CAP ENTRY_KEYS Handler
HISTORY_GAUGE_WIRE_KEYS HISTORY_WIRE_KEYS
LEDGER_STATUS_KEYS LEDGER_TURNS MP_OWNER_CONNECTION MUTATION_TOKEN NATIVE_REC_KEYS
NATIVE_ROWS OWNER_PRESENCE_TTL PACKAGE_DIR POST_API QUERY_API REGISTRY_UNREAD_KEYS REVIEW_STMT_CAP
SESSION_CAP SESSION_KEYS TRASH_DIR ThreadingHTTPServer _CHAT_OLDER_CACHE
_CHAT_OLDER_LOCK _CHAT_OLDER_TTL _COCKPIT_BEAT _COLD_BODY_MAX_S _DEFAULT_ROOM
_LEDGER_WALK_TTL_S
_LOADED_STAMP _LR_CLOSED_CAP _LR_CLOSED_WINDOW_S _LR_COLD_WAIT_S _LR_FLOOR_S
_LR_HARD_TTL_S _LR_LANDS_CAP _LR_LAND_TASK _PROVIDER
_READY_TTL_S _ROOMS_SUM_CACHE _ROOMS_SUM_LOCK _ROOMS_SUM_TTL _ROOM_ROWS_LOCK
_ROOM_ROWS_MEMO _ROSTER_GIT_CACHE _ROSTER_GIT_TTL _ROSTER_REP_CACHE
_ROSTER_REP_LOCK _ROSTER_REP_TTL _SSE_DEAD_S _SSE_IDLE_WATCH_S _SSE_WATCH_S _Server _TURN_HASH
_UPSTREAM_STALE_S _alloc_models _annotate_upstream _api_allocate _api_burn
_api_catalog _api_chat _api_chat_dm _api_chat_ids _api_chat_older _api_chat_post
_api_chat_react _api_chat_read_post _api_chat_roster _api_chat_seat _api_cmd
_api_config_injection _api_configs _api_configs_backups _api_configs_cascade _api_configs_entry_post
_api_configs_file _api_configs_file_post _api_configs_homes _api_configs_resolve
_api_configs_restore_post _api_configs_tree _api_creds _api_flags _api_cwd_post
_api_decisions _api_decisions_comment _api_decisions_deliver
_api_friction _api_friction_dial _POSTURE_SAID _api_posture _api_posture_post
_api_decisions_verdict _api_history _api_homes _api_homes_post
_api_inject_act _api_inject_pack _api_ledger
_api_ledger_native _api_ledger_turn _api_lr _api_mp_presence _api_mp_publish
_api_mp_state _api_notes _api_physics _api_physics_diff _api_prune_post
_api_board _api_owed _api_projects_state _api_quota_status _api_ready
_api_registry
_api_roster_git _api_search
_api_session _api_sessions _api_skills _api_skills_delete _api_skills_toggle
_api_storage_matrix _api_store _api_store_confirm _api_store_reject
_api_store_review _api_task_notes _api_tasks _api_tasks_comment _api_todos
_api_whoami _api_accounts _api_accounts_post _measured_accounts _cached
_cached_swr
_CHAT_NAMES _NAMES_MAX_AGE_S _log_names
_catalog_opensession _catalog_rows _cell_seat_names _chat_fingerprint _chat_gen
_chat_profile _chat_transport _chat_win _claude_home_identity _cockpit_beat
_history_on_the_wire
_decision_recent _dm_lanes _dm_seat_map _ledger_node _lr_build _lr_epoch _lr_ledger_paths
_lr_land_proof _lr_land_task
_lr_snapshot _lr_reads _lr_native_chain _lr_newest_mtime _lr_project _lr_recent_lands _lr_repo
_lr_window _mp_actor _mp_caves _mp_pub _native_chat_pulse _norm
_owner_cursor _owner_read_path _owner_row _owner_signal _port _probe_epoch
_project_on_the_wire _project_detail_on_the_wire _api_project_detail
REGISTRY_CARD_KEYS REGISTRY_DETAIL_ROUTE
_provider _q1 _qcold _qinflight _qlock _qstate _resolve_home_path
_room_rows_memo _room_seats _room_stat _rooms_summary _rooms_summary_cached
_rooms_summary_invalidate _roster_cached _roster_git_one _seat_ephemeral
_source_stamp _sse_ensure_watcher _sse_state _sse_stop _sse_tick _sse_watcher
_sse_watcher_dead _swr_rebuild _transcripts _turn_about _upstream_by_family
_valid_skill_dir calendar cmd_web code_drift default_room get_allocations get_burn
get_creds get_flags get_history hashlib json make_server math os re registry secrets
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
    "helm.web_owed",
    "helm.web_board",
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


def _inherited_stdlib_names(path):
    """Stdlib module names a `helm/web_*.py` file USES but never imports.

    Read as syntax, not at runtime: `web_compat` legitimately `del sys` once
    its import-time work is done, so a vars() check would accuse the one
    module that got it right. What matters is whether the file NAMES its own
    binding, and only the source can say that.
    """
    with open(path, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    bound = set()
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            bound |= {(a.asname or a.name).split(".")[0] for a in node.names}
        elif isinstance(node, ast.Assign):
            bound |= {t.id for t in ast.walk(node)
                      if isinstance(t, ast.Name) and isinstance(t.ctx,
                                                                ast.Store)}
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                               ast.ClassDef)):
            bound.add(node.name)
    used = {n.value.id for n in ast.walk(tree)
            if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
            and n.value.id in sys.stdlib_module_names}
    return used, sorted(used - bound)


class WebImplementationBindingsTest(unittest.TestCase):
    """AN IMPLEMENTATION MODULE MAY INHERIT helm.web's HELPERS. IT MAY NOT
    INHERIT A STDLIB MODULE.

    The splice at the top of each impl module copies `helm.web`'s globals in
    when web.py is already loaded — which it always is in production, and
    never is on a direct `from helm import web_sse`. So a stdlib name the
    file used but did not import was bound by accident of import order: the
    module raised NameError standing alone, and nothing noticed, because the
    untested rung's whole point is that nothing imports these modules.

    MEASURED: `web_common.code_drift` formats every field of its report
    through `time`, `time` was never imported there, and the function's own
    fail-open caught the NameError and answered "no drift". The one detector
    whose job is to stop the console lying about which code it is running was
    itself permanently silent. Nine files carried the same shape.

    THE LINE IS DRAWN AT STDLIB ON PURPOSE. Inheriting `_q1` or `registry`
    from the facade is the design and this arm must not touch it; inheriting
    `time` is free to fix, costs nothing (web.py imports the same module
    object, and the fanout rebinds it over this one, so monkeypatching the
    facade still reaches here) and removes a trap from every future direct
    importer. `web_core` already wrote this rule for a single name; this is
    the same rule with a detector under it.
    """

    def test_no_implementation_module_inherits_a_stdlib_binding(self):  # noqa: VACUOUS_ASSERTION — `used` and `missing` come from ONE call per file, and the unconditional assertIn("time", total_used) plus the >=16-path floor are the controls on that same scan; the rung cannot link them because the accumulation happens inside the loop
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        paths = sorted(glob.glob(os.path.join(here, "helm", "web_*.py")))
        # POSITIVE CONTROL, unconditional and on the same observable: the
        # scan must FIND stdlib usage across this family before its empty
        # verdict below can mean anything. An empty `used` would make the
        # whole arm vacuous — a glob that matched nothing reads identical to
        # a family that is clean.
        self.assertGreaterEqual(len(paths), 16)
        total_used, offenders = set(), {}
        for path in paths:
            used, missing = _inherited_stdlib_names(path)
            total_used |= used
            if missing:
                offenders[os.path.basename(path)] = missing
        self.assertIn("time", total_used)
        self.assertGreaterEqual(len(total_used), 5)
        self.assertEqual(offenders, {})

    def test_the_detector_SEES_an_inherited_binding_when_one_exists(self):  # noqa: VACUOUS_ASSERTION — the must-hit IS the control: the planted file comes back as ["time"] before the cured rewrite of the SAME path is asserted empty, and each _inherited_stdlib_names call mints a fresh producer identity the rung cannot link
        """MUST-HIT through the same door. The arm above asserts an empty
        dict; planted source that uses `time` without importing it has to
        come back as a finding, or the green above is a scan that stopped
        looking rather than a family that is clean."""
        with tempfile.TemporaryDirectory() as tmp:
            planted = os.path.join(tmp, "web_planted.py")
            with open(planted, "w", encoding="utf-8") as fh:
                fh.write('"""A web impl module that inherits its clock."""\n'
                         "import sys\n"
                         "_web = sys.modules.get('helm.web')\n"
                         "def stamp():\n"
                         "    return time.time()\n")
            used, missing = _inherited_stdlib_names(planted)
            self.assertEqual(missing, ["time"])
            self.assertIn("time", used)
            # and the CURED form of the same file comes back clean, so the
            # finding above is the missing import and not the shape.
            with open(planted, "w", encoding="utf-8") as fh:
                fh.write('"""Cured."""\nimport sys\nimport time\n'
                         "_web = sys.modules.get('helm.web')\n"
                         "def stamp():\n"
                         "    return time.time()\n")
            self.assertEqual(_inherited_stdlib_names(planted)[1], [])

    def test_web_quota_serves_the_world_as_the_first_import(self):
        """task/2914 (c). `web_quota`'s handlers call `_provider`, `_cached`,
        `_transcripts` and `_q1`, and read `_qlock` and `_qstate`, and it bound
        none of them: they arrived only through the splice. So a direct import
        succeeded and the first call raised NameError. The same rule as the
        stdlib arm above, for the helpers its own world read cannot run
        without."""
        root, env = _fresh_process()
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(env, HOME=tmp, HELM_HOME=os.path.join(tmp, "helm"))
            code = (
                "import sys\n"
                "from helm import web_quota as q, web_cache\n"
                "assert 'helm.web' not in sys.modules\n"
                "class P:\n"
                "    def accounts(self):\n"
                "        return [{'name': 'n', 'provider': 'codex',\n"
                "                 'home': '', 'email': 'a' + '@' + 'b.test'}]\n"
                "    def cred_state(self): return []\n"
                "    def windows(self): return []\n"
                "web_cache._PROVIDER = P()\n"
                "w = q._world()\n"
                "assert w.state == 'measured', (w.state, w.cause)\n"
                "assert [r['name'] for r in q.get_creds()] == ['n']\n"
                "assert q._transcripts().__name__ == 'helm.transcripts'\n"
                "assert q._q1({'k': ['v']}, 'k') == 'v'\n"
                "assert 'helm.web' not in sys.modules\n"
                "print('ok')\n")
            p = subprocess.run([sys.executable, *_SAFE_PATH, "-c", code],
                               cwd=root, env=env, capture_output=True,
                               text=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stdout.strip(), "ok")

    def test_a_facade_helper_is_still_inherited_and_not_accused(self):
        """The other half of the line. `_q1`, `registry` and the rest of the
        facade surface are SUPPOSED to arrive through the splice; an arm that
        accused them would force every impl module to re-import the facade
        and the split would be undone by its own test."""
        used, missing = _inherited_stdlib_names(
            os.path.join(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__))), "helm", "web_core.py"))
        self.assertEqual(missing, [])
        self.assertIn("time", used)
        # web_core genuinely CALLS names it never binds — `_cached` and
        # `code_drift` both arrive through the splice — and the detector
        # deliberately lets them pass. Asserted against the parsed file
        # rather than a substring, so a mention in a comment cannot stand in
        # for a call.
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(here, "helm", "web_core.py"),
                  encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        called = {n.func.id for n in ast.walk(tree)
                  if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        bound = set()
        for node in tree.body:
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                bound |= {(a.asname or a.name).split(".")[0]
                          for a in node.names}
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                   ast.ClassDef)):
                bound.add(node.name)
        self.assertIn("_cached", called - bound)
        self.assertIn("code_drift", called - bound)


class WebSplitContractTest(unittest.TestCase):

    def test_the_runtime_surface_remains_exact(self):  # noqa: VACUOUS_ASSERTION — exact non-empty surface equality and derived cardinality positively control the fabricated-name absence check
        current = {name for name in vars(web)
                   if not (name.startswith("__") and name.endswith("__"))}
        self.assertNotIn("_fabricated_split_bridge", current)
        self.assertEqual(current, _SURFACE)
        # THE COUNT IS DERIVED, AND THE ARITHMETIC IS THE ASSERTION. A third
        # spelling of a population the two literals above already pin adds no
        # independence. What matters here is DISJOINTNESS: |A| + |B| is only
        # |A ∪ B| when no bridge name shadows a baseline one.
        self.assertTrue(_BASELINE_SURFACE.isdisjoint(_BRIDGE_SURFACE),
                        "a bridge name shadows a baseline one, so the surface "
                        "is smaller than the two lists claim: %r"
                        % (sorted(_BASELINE_SURFACE & _BRIDGE_SURFACE),))
        self.assertEqual(len(current),
                         len(_BASELINE_SURFACE) + len(_BRIDGE_SURFACE))

    def test_the_implementation_inventory_and_fanout_are_exact(self):  # noqa: VACUOUS_ASSERTION — exact 17-module inventory and exact non-empty 242-name fanout positively control the bridge-absence checks
        self.assertEqual(tuple(m.__name__ for m in web._WEB_IMPL_MODULES),
                         _IMPL_MODULE_NAMES)
        self.assertEqual(web._WEB_FANOUT_NAMES, _BASELINE_SURFACE)
        self.assertEqual(len(web._WEB_FANOUT_NAMES), 242)
        for module in web._WEB_IMPL_MODULES:
            for name in _BRIDGE_SURFACE:
                self.assertNotIn(name, module.__dict__)

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

    def test_every_historical_binding_assigns_and_deletes_across_all_owners(self):  # noqa: VACUOUS_ASSERTION — every exact fanout name is assigned, observed on all owners, deleted, observed absent, and restored
        for name in sorted(web._WEB_FANOUT_NAMES):
            original = getattr(web, name)
            marker = object()
            try:
                setattr(web, name, marker)
                for module in web._WEB_IMPL_MODULES:
                    self.assertIs(module.__dict__[name], marker, name)
                delattr(web, name)
                for module in web._WEB_IMPL_MODULES:
                    self.assertNotIn(name, module.__dict__)
            finally:
                setattr(web, name, original)

    def test_split_bridge_controls_never_fan_out(self):  # noqa: VACUOUS_ASSERTION — each exact bridge name is positively assigned on the facade before all 17 owner dictionaries are checked absent
        modules = tuple(web._WEB_IMPL_MODULES)
        for name in sorted(_BRIDGE_SURFACE):
            original = getattr(web, name)
            marker = () if name in {"_WEB_FANOUT_NAMES", "_WEB_IMPL_MODULES"} \
                else object()
            try:
                setattr(web, name, marker)
                self.assertIs(getattr(web, name), marker)
                for module in modules:
                    self.assertNotIn(name, module.__dict__)
            finally:
                ModuleType.__setattr__(web, name, original)

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
                [sys.executable, *_SAFE_PATH, "-c", code, module_name, names[0]],
                cwd=root, env=env, capture_output=True, text=True)
            self.assertEqual(p.returncode, 0, (module_name, p.stderr))
        p = subprocess.run(
            [sys.executable, *_SAFE_PATH, "-c",
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
                    [sys.executable, *_SAFE_PATH, "-c", code, *pair],
                    cwd=root, env=env, capture_output=True, text=True)
                self.assertEqual(p.returncode, 0, (pair, p.stderr))

    def test_facade_callables_unpickle_in_a_fresh_process(self):  # noqa: VACUOUS_ASSERTION — 159 exports plus 14 class methods are counted, canonically named, and rebound by identity in the child
        from helm import web_compat

        root, env = _fresh_process()
        items = [(name, value) for name, value in web_compat.EXPORTS.items()
                 if inspect.isfunction(value) or inspect.isclass(value)]
        self.assertEqual(len(items), 159)
        self.assertEqual({value.__module__ for _name, value in items},
                         {"helm.web"})
        methods = [(cls.__name__ + "." + name, value)
                   for cls in (web.Handler, web._Server)
                   for name, value in vars(cls).items()
                   if inspect.isfunction(value)]
        self.assertEqual(len(methods), 14)
        self.assertEqual({value.__module__ for _name, value in methods},
                         {"helm.web"})
        payload = pickle.dumps(items + methods)
        # THE CHILD'S COUNT IS THE PARENT'S MEASUREMENT, NOT A FOURTH LITERAL.
        # The literal was written out a third time, and it lived INSIDE A
        # SUBPROCESS STRING — where no sweep of this file's assertions could see
        # it, and where a stale value fails as an opaque non-zero exit rather
        # than as a count mismatch. The check it is really making is that the
        # PICKLE carried everything the parent put in, so it asks the parent how
        # many that was. The two independent pins stay above, in view.
        expected = len(items) + len(methods)
        p = subprocess.run(
            [sys.executable, *_SAFE_PATH, "-c",
             "import pickle,sys; from helm import web; "
             "items=pickle.load(sys.stdin.buffer); "
             "resolve=lambda n: __import__('functools').reduce(getattr, "
             "n.split('.'), web); "
             "bad=[n for n,v in items if v is not resolve(n)]; "
             "assert len(items)==%d,(len(items),bad); assert not bad,bad"
             % expected],
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

    def test_reload_recreates_the_monolith_runtime_state(self):  # noqa: VACUOUS_ASSERTION — populated cache, captured identities, and seeded stale bridge controls are contrasted with exact fresh post-reload state; the child's returncode binds every assertion
        """A reload rebuilds every runtime object the facade owns.

        IN A FRESH INTERPRETER, because a reload rebinds `helm.web` and every
        implementation module it fans out to for the REST OF THE PROCESS:
        every later module that imported `web` or reads its classes by name
        would hold objects from a different generation than the ones it
        imported. That reach is the thing under test, so it cannot be undone
        from inside and the only clean scope for it is a process of its own.
        """
        root, env = _fresh_process()
        code = textwrap.dedent("""
            import importlib, os
            from helm import web, localnames
            BRIDGE = %r
            state = web._qstate
            endpoint = web._api_registry
            handler = web.Handler
            token = web.MUTATION_TOKEN
            stale_bridge = object()
            for module in web._WEB_IMPL_MODULES:
                for name in BRIDGE:
                    module.__dict__[name] = stale_bridge
            state["probe"] = "reload must discard this"
            web.DEFAULT_PORT = 8123
            importlib.reload(web)
            assert web._qstate is not state
            assert web._qstate == {}, web._qstate
            assert web._api_registry is not endpoint
            assert web.Handler is not handler
            for module in web._WEB_IMPL_MODULES:
                for name in BRIDGE:
                    assert name not in module.__dict__, (module, name)
            pinned = (os.environ.get("HELM_API_TOKEN")
                      or localnames.legacy_env("API_TOKEN"))
            if pinned:
                assert web.MUTATION_TOKEN == pinned
            else:
                assert web.MUTATION_TOKEN != token
            assert web.DEFAULT_PORT == 7433, web.DEFAULT_PORT
            assert web.make_server.__defaults__ == (7433,)
            print("RELOAD-CHECKED")
        """) % (sorted(_BRIDGE_SURFACE),)
        p = subprocess.run([sys.executable, *_SAFE_PATH, "-c", code],
                           cwd=root, env=env, capture_output=True, text=True,
                           timeout=120)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("RELOAD-CHECKED", p.stdout)

    def test_reload_preserves_documented_environment_token_pins(self):  # noqa: VACUOUS_ASSERTION — both subprocesses positively assert the exact winning token before and after reload; returncode binds those assertions
        root, base_env = _fresh_process()
        code = ("import importlib,os,sys; from helm import web; "
                "expected=os.environ[sys.argv[1]]; "
                "assert web.MUTATION_TOKEN==expected; "
                "importlib.reload(web); "
                "assert web.MUTATION_TOKEN==expected")
        # the predecessor's spelling is documented only where the helm home's
        # local names declare a predecessor, so the second pin runs under one
        declared = tempfile.mkdtemp(prefix="helm-test-declared-home-")
        self.addCleanup(__import__("shutil").rmtree, declared, True)
        os.makedirs(os.path.join(declared, "_global"))
        with open(os.path.join(declared, "_global", "local-names.json"),
                  "w") as f:
            json.dump({"predecessor": "oldtool"}, f)
        cases = (
            ({"HELM_API_TOKEN": "helm-pin", "OLDTOOL_API_TOKEN": "old-pin",
              "HELM_HOME": declared}, "HELM_API_TOKEN"),
            ({"OLDTOOL_API_TOKEN": "old-pin", "HELM_HOME": declared},
             "OLDTOOL_API_TOKEN"),
        )
        for overrides, winner in cases:
            env = base_env.copy()
            env.pop("HELM_API_TOKEN", None)
            env.pop("OLDTOOL_API_TOKEN", None)
            env.update(overrides)
            p = subprocess.run(
                [sys.executable, "-P", "-c", code, winner],
                cwd=root, env=env, capture_output=True, text=True)
            self.assertEqual(p.returncode, 0, (overrides, p.stderr))

    def test_module_execution_reuses_the_package_module_identity(self):
        root, env = _fresh_process()
        p = subprocess.run(
            [sys.executable, *_SAFE_PATH, "-m", "helm.web", "--help"],
            cwd=root, env=env, capture_output=True, text=True)
        self.assertEqual(p.returncode, 2, p)
        self.assertIn("usage: helm web", p.stderr)
        self.assertNotIn("Traceback", p.stderr)


if __name__ == "__main__":
    unittest.main()
