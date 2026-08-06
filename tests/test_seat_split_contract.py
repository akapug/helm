"""Compatibility contracts for the decomposition of :mod:`helm.seat`."""
import ast
import inspect
import os
import unittest

from helm import (
    codexhomes,
    seat,
    seat_catalog,
    seat_credentials,
    seat_env,
    seat_health,
    seat_lifecycle,
    seat_paths,
    seat_ports,
    seat_provision,
    seat_proxy,
)


# Non-dunder names observed by reflection at the exact split-base commit. The
# smaller consumed-name inventory is useful for ownership, but this superset is
# the stronger motion gate: no attribute that existed on the monolith may
# disappear merely because its implementation moves to another module.
_BASELINE_SURFACE = frozenset({
    "AUTOCOMPACT_PCT_OVERRIDE",
    "CHILD_STAMP_VARS",
    "CODEX_HOMES",
    "CRED_ABSENT",
    "CRED_EXPIRED",
    "CRED_UNKNOWN",
    "CRED_VALID",
    "DREGG_SIGNER_DEFAULT",
    "FAMILIES",
    "HERMES_AUTH",
    "OBSERVED_FLOOR_HEADROOM",
    "OPENCODE_AUTHSTORE",
    "PASTE_UNSET_VARS",
    "PLAN_ENTRY_TOOL",
    "PROXY_BIN_DEFAULT",
    "PROXY_CURRENT",
    "PROXY_NO_RECORD",
    "PROXY_PID_DEAD",
    "PROXY_PID_REUSED",
    "PROXY_STALE",
    "PROXY_UNKNOWN",
    "PROXY_UNVERIFIABLE",
    "REBIND_INTERVAL_S",
    "SCRUB_VARS",
    "SPAWN_SEND_DELAY_S",
    "WINDOW_BACKINGS",
    "_BINARY_IDENTITY_TYPES",
    "_CC_ASSUMED_WINDOW_MIRROR",
    "_CONFIG_INVARIANTS",
    "_CPU_SAMPLE_MAX_AGE_S",
    "_ENSURE_STARTUP_GRACE_S",
    "_FEATURE_CACHE_FUTURE_SKEW_MS",
    "_FEATURE_CACHE_GATE",
    "_FEATURE_CACHE_KEYS",
    "_FEATURE_CACHE_MAX_AGE_MS",
    "_FEATURE_CACHE_MIN_FEATURES",
    "_LIVENESS_STATES",
    "_ONBOARD_KEYS",
    "_PANE_ANSI",
    "_PANE_CHROME",
    "_PANE_PROMPT",
    "_PLAN_PATH_RE",
    "_PROBE_AGENT_MD",
    "_PROXY_LAUNCH_RECORD_V",
    "_PROXY_RECORD_UNSET",
    "_PRUNE_HEAD_BYTES",
    "_QUOTED_COUNT",
    "_QUOTED_WORD",
    "_REBIND_SERVICE",
    "_REBIND_TIMER",
    "_SEAT_SURFACE_REFUSED",
    "_SEND_ONLY_DETAIL",
    "_SESSION_JSONL",
    "_STATE_NAMES",
    "_STUB_MAX_BYTES",
    "_TCP_LISTEN",
    "_UNBLOCK",
    "_USAGE",
    "_add",
    "_add_proxy_key",
    "_add_proxy_oauth",
    "_adopt",
    "_adopt_or_refuse_port",
    "_adoptable_cred",
    "_argv_config",
    "_backfill_spawn_session",
    "_binary_identity",
    "_bind_spawn_session",
    "_blocked_detail",
    "_canary_text",
    "_classify_pane_tail",
    "_config_digest",
    "_config_drift_lines",
    "_config_values",
    "_config_yaml",
    "_config_yaml_key",
    "_contextlib",
    "_cpu_canary",
    "_cpu_sample_path",
    "_cred_exp",
    "_cred_expiry",
    "_cred_remaining",
    "_cred_rolling",
    "_current_prompt_line",
    "_decode_launch_inputs",
    "_doctor",
    "_down",
    "_dq_escape",
    "_encode_launch_inputs",
    "_ensure",
    "_ensure_autocompact_timer",
    "_ensure_row",
    "_env_file_value",
    "_env_float",
    "_expired_field_epoch",
    "_family_owner_aliases",
    "_family_owner_aliases_are_unique",
    "_family_port_bases_are_unique",
    "_feature_cache_complete",
    "_feature_cache_seed",
    "_git_toplevel",
    "_glob_auth",
    "_has_real_turn",
    "_headless_spawn",
    "_hermes_pool_key",
    "_homing_from_launch",
    "_instance_dir",
    "_instance_gate",
    "_instance_port",
    "_jwt_claims",
    "_key_base_url",
    "_link_skills",
    "_listen_inodes",
    "_live_seat_orca_identity",
    "_live_session_orca_identity",
    "_liveness_from_orcaadopt",
    "_mint_instance_proxy",
    "_mint_probe_agents",
    "_minted_instances",
    "_minted_seats",
    "_multi_from_launch",
    "_nested_surface_error",
    "_newest_seat_session",
    "_newest_unpooled_codex_auth",
    "_oauth_cred_reason",
    "_onboarded_refs",
    "_opencode_authstore_key",
    "_owner_statement_reason",
    "_pane_live",
    "_pane_sendable",
    "_panes",
    "_pid_alive",
    "_pid_holding_socket",
    "_pid_identity",
    "_pids_holding_socket",
    "_port_listener",
    "_port_listeners",
    "_port_open",
    "_proc_argv",
    "_proc_cpu_sample",
    "_proc_root",
    "_prove_orca_rebind",
    "_prove_orca_replacement",
    "_prove_spawned_pane",
    "_proxy_age_s",
    "_proxy_bin",
    "_proxy_home",
    "_proxy_launch_inputs",
    "_proxy_live_text",
    "_proxy_lock",
    "_proxy_pid_record",
    "_proxy_pid_verdict",
    "_prune_source",
    "_quote_names_family",
    "_quoted_token_counts",
    "_read_token",
    "_reap_stale",
    "_rebind",
    "_recorded_pid_alive",
    "_register_spawn",
    "_repair_orca_handle",
    "_report_rehome",
    "_require_seat",
    "_resolve_homing",
    "_resolve_registered_pane",
    "_resume",
    "_resume_cwd",
    "_rfc3339",
    "_room_from_launch",
    "_running_pid",
    "_running_pid_rec",
    "_same_config",
    "_seat_env",
    "_seat_family",
    "_seat_home_cwd",
    "_seat_lifecycle_lock",
    "_seat_row",
    "_seat_session_by_id",
    "_seat_session_path_by_id",
    "_seat_surface_error",
    "_seat_token",
    "_seat_token_per",
    "_seed_onboarding",
    "_seed_seat_settings",
    "_sessionstart_pane_fields",
    "_smoke",
    "_smoke_multi_leg",
    "_socket_holders",
    "_spawn",
    "_spawn_args",
    "_spawn_path",
    "_spawn_plan",
    "_spawn_record",
    "_split_seat",
    "_stale_resume_cwd",
    "_status",
    "_token_export",
    "_token_file",
    "_unbacked_window_reason",
    "_unknown_seat_reason",
    "_up",
    "_upstream_row",
    "_valid_binary_identity",
    "_warn_feature_cache",
    "_where",
    "_where_adopted",
    "_write_launch_assets",
    "_write_launch_sh",
    "_write_private",
    "base64",
    "child_stamp_unsets",
    "cmd_seat",
    "codex_cred_state",
    "config_drift",
    "cred_state",
    "ensure_rebind_timer",
    "family_for",
    "glob",
    "hashlib",
    "home",
    "json",
    "launch_line",
    "math",
    "newest_valid_codex_auth",
    "onboarding_prompt",
    "os",
    "paste_unset_prefix",
    "probe_agents",
    "proxy_debug",
    "proxy_drift",
    "re",
    "rearm_prompt",
    "rebind_seat",
    "rebind_timer_units",
    "registered_seats",
    "scrub_env",
    "scrub_prefix",
    "seat_dir",
    "seat_liveness",
    "seats_root",
    "shlex",
    "shutil",
    "signal",
    "socket",
    "stat",
    "subprocess",
    "sys",
    "time",
    "translate_codex_auth",
    "upstream_phrase",
})

_FACADE_BRIDGE_SURFACE = frozenset({
    "_SEAT_FANOUT_NAMES",
    "_SEAT_IMPL_MODULES",
    "_SeatModule",
    "_seat_catalog_impl",
    "_seat_credentials_impl",
    "_seat_env_impl",
    "_seat_health_impl",
    "_seat_lifecycle_impl",
    "_seat_paths_impl",
    "_seat_ports_impl",
    "_seat_provision_impl",
    "_seat_proxy_impl",
    "_seed_impl_modules",
})
_CURRENT_SURFACE = _BASELINE_SURFACE | _FACADE_BRIDGE_SURFACE
_IMPL_MODULE_NAMES = (
    "helm.seat_catalog",
    "helm.seat_credentials",
    "helm.seat_env",
    "helm.seat_health",
    "helm.seat_lifecycle",
    "helm.seat_lifecycle_sessions",
    "helm.seat_lifecycle_runtime",
    "helm.seat_paths",
    "helm.seat_ports",
    "helm.seat_provision",
    "helm.seat_launch_assets",
    "helm.seat_proxy",
)
_MODULE_BINDINGS = frozenset({
    "_contextlib",
    "_seat_catalog_impl",
    "_seat_credentials_impl",
    "_seat_env_impl",
    "_seat_health_impl",
    "_seat_lifecycle_impl",
    "_seat_paths_impl",
    "_seat_ports_impl",
    "_seat_provision_impl",
    "_seat_proxy_impl",
    "base64",
    "glob",
    "hashlib",
    "home",
    "json",
    "math",
    "os",
    "re",
    "shlex",
    "shutil",
    "signal",
    "socket",
    "stat",
    "subprocess",
    "sys",
    "time",
})
_PINNED_FUNCTIONS = (
    "_family_owner_aliases_are_unique",
    "_panes",
    "_resume",
    "_spawn",
    "_unbacked_window_reason",
    "cmd_seat",
)


class SeatSplitCompatibilityTest(unittest.TestCase):
    def test_the_monoliths_runtime_surface_remains_resolvable(self):  # noqa: VACUOUS_ASSERTION — cmd_seat is the positive resolver control; the exact set plus fabricated name prove both presence and absence
        surface = frozenset(name for name in dir(seat)
                            if not (name.startswith("__") and name.endswith("__")))
        self.assertIn("cmd_seat", surface)
        self.assertEqual(surface, _CURRENT_SURFACE)
        self.assertFalse(hasattr(seat, "definitely_not_a_seat_symbol_7f3c"))

    def test_the_frozen_fanout_inventory_remains_exact(self):  # noqa: VACUOUS_ASSERTION — equality to the non-empty 262-name inventory is the positive control
        self.assertEqual(seat._SEAT_FANOUT_NAMES,
                         _CURRENT_SURFACE - {"_SEAT_FANOUT_NAMES"})

    def test_module_bindings_are_shared_across_every_implementation(self):  # noqa: VACUOUS_ASSERTION — the unconditional implementation-name tuple proves the identity matrix executes
        modules = tuple(seat._SEAT_IMPL_MODULES)
        self.assertEqual(tuple(module.__name__ for module in modules),
                         _IMPL_MODULE_NAMES)
        for name in sorted(_MODULE_BINDINGS):
            expected = getattr(seat, name)
            with self.subTest(name=name):
                self.assertEqual(inspect.ismodule(expected), True)
                for module in modules:
                    self.assertIs(getattr(module, name), expected)

    def test_compatibility_bootstrap_controls_do_not_leak(self):  # noqa: VACUOUS_ASSERTION — cmd_seat is the unconditional positive control on the same facade namespace
        self.assertIs(seat.cmd_seat, getattr(seat, "cmd_seat"))
        for name in ("_seat_compat", "EXPORTS", "IMPL_MODULES"):
            with self.subTest(name=name):
                self.assertFalse(hasattr(seat, name))

    def test_authority_and_import_guards_remain_physically_in_the_facade(self):
        facade = os.path.realpath(seat.__file__)
        self.assertEqual(os.path.basename(facade), "seat.py")
        for name in _PINNED_FUNCTIONS:
            with self.subTest(name=name):
                self.assertEqual(os.path.realpath(inspect.getsourcefile(
                    getattr(seat, name))), facade)

    def test_the_original_namespace_has_no_function_level_global_writes(self):
        tree = ast.parse(inspect.getsource(seat))
        self.assertGreater(sum(isinstance(node, ast.FunctionDef)
                               for node in tree.body), 0)
        writes = [(node.lineno, tuple(node.names)) for node in ast.walk(tree)
                  if isinstance(node, ast.Global)]
        self.assertEqual(writes, [])

    def test_facade_rebinding_reaches_moved_function_globals(self):  # noqa: VACUOUS_ASSERTION — the synthetic KEEP key is the unconditional positive control; cleanup is deliberately in finally
        original = seat.SCRUB_VARS
        try:
            seat.SCRUB_VARS = ("SYNTHETIC_PROXY_VAR",)
            self.assertIs(seat_env.SCRUB_VARS, seat.SCRUB_VARS)
            self.assertEqual(seat_env.scrub_env({
                "SYNTHETIC_PROXY_VAR": "remove",
                "KEEP": "yes",
            }), {"KEEP": "yes"})
        finally:
            seat.SCRUB_VARS = original
        self.assertIs(seat_env.SCRUB_VARS, original)

    def test_facade_deletion_reaches_moved_function_globals(self):  # noqa: VACUOUS_ASSERTION — the shared binding exists before deletion and is restored afterward
        original = seat.SCRUB_VARS
        self.assertIs(seat_env.SCRUB_VARS, original)
        try:
            del seat.SCRUB_VARS
            self.assertFalse(hasattr(seat_env, "SCRUB_VARS"))
        finally:
            seat.SCRUB_VARS = original
        self.assertIs(seat_env.SCRUB_VARS, original)

    def test_external_consumers_read_facade_rebindings_at_call_time(self):  # noqa: VACUOUS_ASSERTION — pool_dir's synthetic path is the positive control and the AST check forbids the import-time binder that bypassed it
        tree = ast.parse(inspect.getsource(codexhomes))
        direct = [alias.name for node in ast.walk(tree)
                  if isinstance(node, ast.ImportFrom) and node.level == 1
                  and node.module == "seat" for alias in node.names]
        self.assertEqual(direct, [])
        original = seat.seat_dir
        try:
            seat.seat_dir = lambda family, *args, **kwargs: "/synthetic/%s" % family
            self.assertEqual(codexhomes.pool_dir(), "/synthetic/codex/auth")
        finally:
            seat.seat_dir = original

    def test_catalog_functions_read_facade_rebindings(self):  # noqa: VACUOUS_ASSERTION — identity with the synthetic duplicate-port table is the positive control on the same rebinding
        original = seat.FAMILIES
        duplicate_ports = {
            "one": {"port": 8000},
            "two": {"port": 8000},
        }
        try:
            seat.FAMILIES = duplicate_ports
            self.assertIs(seat_catalog.FAMILIES, duplicate_ports)
            self.assertFalse(seat_catalog._family_port_bases_are_unique())
        finally:
            seat.FAMILIES = original
        self.assertIs(seat_catalog.FAMILIES, original)

    def test_path_back_edges_read_later_facade_rebindings(self):  # noqa: VACUOUS_ASSERTION — synthetic path equality is the positive control on the rebound implementation global
        original = seat._instance_dir
        synthetic = lambda family, name: "/synthetic/%s/%s" % (family, name)
        try:
            seat._instance_dir = synthetic
            self.assertIs(seat_paths._instance_dir, synthetic)
            self.assertEqual(seat_paths._proxy_home("codex", "codex-2"),
                             "/synthetic/codex/codex-2")
        finally:
            seat._instance_dir = original
        self.assertIs(seat_paths._instance_dir, original)

    def test_port_helpers_read_facade_path_rebindings(self):  # noqa: VACUOUS_ASSERTION — the synthetic token path is the positive control on the rebound helper
        original = seat._proxy_home
        synthetic = lambda family, name=None: "/synthetic/%s" % (name or family)
        try:
            seat._proxy_home = synthetic
            self.assertIs(seat_ports._proxy_home, synthetic)
            self.assertEqual(seat_ports._token_file("codex", "codex-2"),
                             "/synthetic/codex-2/token")
        finally:
            seat._proxy_home = original
        self.assertIs(seat_ports._proxy_home, original)

    def test_credential_roots_follow_facade_rebindings(self):  # noqa: VACUOUS_ASSERTION — equality to the synthetic root is the positive control on the rebound credential namespace
        original = seat.CODEX_HOMES
        synthetic = "/synthetic/codex-homes"
        try:
            seat.CODEX_HOMES = synthetic
            self.assertEqual(seat_credentials.CODEX_HOMES, synthetic)
        finally:
            seat.CODEX_HOMES = original
        self.assertEqual(seat_credentials.CODEX_HOMES, original)

    def test_provision_helpers_read_facade_rebindings(self):  # noqa: VACUOUS_ASSERTION — the rendered synthetic debug value is the positive control on the rebound provisioning global
        original = seat.proxy_debug
        synthetic = lambda: "synthetic-debug"
        try:
            seat.proxy_debug = synthetic
            self.assertIs(seat_provision.proxy_debug, synthetic)
            self.assertIn("debug: synthetic-debug", seat_provision._config_yaml(
                8123, "/synthetic/auth", "synthetic-token"))
        finally:
            seat.proxy_debug = original
        self.assertIs(seat_provision.proxy_debug, original)

    def test_proxy_helpers_read_facade_rebindings(self):  # noqa: VACUOUS_ASSERTION — the synthetic requirement function is observed before _up returns through its negative branch
        original = seat._require_seat
        synthetic = lambda family: None
        try:
            seat._require_seat = synthetic
            self.assertIs(seat_proxy._require_seat, synthetic)
            self.assertEqual(seat_proxy._up("synthetic-family"), 1)
        finally:
            seat._require_seat = original
        self.assertIs(seat_proxy._require_seat, original)

    def test_health_helpers_read_facade_rebindings(self):  # noqa: VACUOUS_ASSERTION — the synthetic live pid and open port produce a concrete HEALTHY row through all three rebound seams
        originals = {name: getattr(seat, name) for name in (
            "_proxy_pid_record", "_running_pid", "_port_open")}
        replacements = {
            "_proxy_pid_record": lambda family, name: None,
            "_running_pid": lambda family, name: 4321,
            "_port_open": lambda port: True,
        }
        try:
            for name, value in replacements.items():
                setattr(seat, name, value)
                self.assertIs(getattr(seat_health, name), value)
            port = seat._instance_port("codex", "codex")
            self.assertEqual(seat_health._ensure_row("codex", "codex"),
                             ("codex", "healthy",
                              "pid 4321 port %d" % port))
        finally:
            for name, value in originals.items():
                setattr(seat, name, value)

    def test_lifecycle_helpers_read_facade_rebindings(self):  # noqa: VACUOUS_ASSERTION — family_for returns the concrete synthetic family/reason pair through the rebound fallback seam
        original = seat._seat_family
        synthetic = lambda name: ("synthetic-family", "synthetic-reason")
        try:
            seat._seat_family = synthetic
            self.assertIs(seat_lifecycle._seat_family, synthetic)
            self.assertEqual(seat_lifecycle.family_for("synthetic-seat"),
                             ("synthetic-family", "synthetic-reason"))
        finally:
            seat._seat_family = original
        self.assertIs(seat_lifecycle._seat_family, original)


if __name__ == "__main__":
    unittest.main()
