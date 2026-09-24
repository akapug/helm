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
    seat_lifecycle_runtime,
    seat_lifecycle_sessions,
    seat_paths,
    seat_ports,
    seat_provision,
    seat_proxy,
)


# Non-dunder names observed by reflection at exact split base d225f4c3. The
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
    "PERMISSION_PROMPT",
    "PLAN_ENTRY_TOOL",
    "PLAN_EXECUTION_PROMPT",
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
    "_AFFIRMATIVE_RE",
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
    "_NEGATIVE_RE",
    "_ONBOARD_KEYS",
    "_OPTION_RE",
    "_PANE_ANSI",
    "_PANE_CHROME",
    "_PANE_PROMPT",
    "_PLAN_EXECUTION_RE",
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
    "WALL_EXPIRED",
    "WALL_IN_FORCE",
    "WALL_UNDATED",
    "_adopt_or_refuse_port",
    "_auth_path_outside_the_seat",
    "_adoptable_cred",
    "_argv_config",
    "_backfill_spawn_session",
    "_binary_identity",
    "_bind_spawn_session",
    "_blocked_detail",
    "_canary_text",
    "_classify_pane_tail",
    "vendor_escape_choice",
    "ESCAPE_CONTINUE",
    "ESCAPE_SPEND",
    "ESCAPE_HUMAN",
    "ESCAPE_ABSENT",
    "_wall_in_force",
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
    "_persisted_model",
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
    "_prompt_options",
    "_prompt_type",
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
    "_seat_owning",
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
    "affirmative_choice",
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
    "provider_rung",
    "proxy_debug",
    "proxy_drift",
    "proxy_route_family",
    "proxy_route_response_model",
    "proxy_routes",
    "re",
    "rearm_prompt",
    "rebind_seat",
    "rebind_timer_units",
    "registered_seats",
    "scrub_env",
    "scrub_prefix",
    "seat_dir",
    "seat_liveness",
    "seat_model_phrase",
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
_POST_SPLIT_SURFACE = frozenset({
    "_boot_brief",
    "_boot_brief_wire",
    # 2026-08-22: `seat resume --all` reads rebind's two zero-count refusal
    # sentences BACK through the facade, so they and the matcher are surface.
    "NO_DISTINCT_LIVE_PANE",
    "NO_EXACT_LIVE_SESSION",
    "rebind_refusal_means_dead",
    # The homing re-derivation `_spawn` and `_resume` now SHARE, plus the
    # launch-script rollback both of them use on a failure that leaves no
    # registered pane. They live on the facade beside the two callers rather
    # than in an impl module because they exist to keep those two functions
    # from drifting apart, which is exactly what two copies of one law did.
    "_cwd_moved",
    "_launch_snapshot",
    "_restore_launch",
    "_room_for_cwd",
    # Remediation is a typed capability on the same liveness/upstream owner row;
    # consumers import these names through the compatibility facade rather than
    # retyping restart advice independently.
    "RESTART_HELPFUL",
    "RESTART_NOT_HELPFUL",
    "RESTART_UNKNOWN",
    "remediation_text",
    "upstream_remediation",
    # The per-family schema-unsafe tool deny (task/1941): the measured list and
    # the resolver every launch surface reads through the facade.
    "SCHEMA_UNSAFE_TOOLS",
    "SPAWN_DENIED_TOOLS",
    "PROXY_MODES",
    "denied_tools",
    # The metered QUOTA GROUP a family bills, and the phrase a seat surface
    # prints for it. Facade surface because `helm/seat_lifecycle.py` renders it
    # on `helm seat where`, the same way it reads `seat_model_phrase`: one
    # credential can meter two groups (the antigravity account meters Gemini
    # separately from Claude-and-GPT), so the group — never the credential — is
    # what a shared bar covers. `QUOTA_GROUP_UNMEASURED` rides with them
    # because the unmeasured rendering is the DEFAULT one until task/2800 reads
    # the vendor's quota endpoint, and a caller that retyped it would be free
    # to retype a number instead.
    "quota_group",
    "quota_group_families",
    "quota_group_phrase",
    "QUOTA_GROUP_UNMEASURED",
    # Task/2466's per-subagent tier table: the union of the models a family
    # catalogues. Facade surface because `helm/proxywatch.py` reads it as
    # `seatmod.family_catalogued_models` to decide whether an alias row's name
    # belongs to the channel's OWN family. Its two siblings in that table --
    # `subagent_tier_model` and `subagent_tier_error` -- are deliberately NOT
    # here: their only callers are seat_launch_assets and seat_catalog itself,
    # which import them from `helm.seat_catalog` at the call site, so the
    # facade owes them nothing.
    "family_catalogued_models",
    # The claude-side ALIASES a family's proxy serves as seat runtimes — the
    # sibling reading of the line above, and facade surface for the same kind
    # of reason: `helm/modelrouter.py` reads it as `seat.family_route_aliases`
    # to build the non-claude routing table, and a per-model family (openrouter)
    # answers several aliases where `model` answers one.
    "family_route_aliases",
    # Feedback about helm never leaves helm (task/2328): the two env switches,
    # the drafts-off setting, the seeded rule sentence and the rules seeder —
    # every launch door (proxy launch line, native build_env, the seat seed)
    # reads them through the facade, like the deny set above.
    "FEEDBACK_ENV",
    "FEEDBACK_DRAFTS_SETTING",
    "FEEDBACK_RULE",
    "feedback_env_words",
    "_seed_seat_rules",
    # Task/1967: schema-aware migration and exact listener-incarnation restart
    # are compatibility-facade primitives because doctor and lifecycle share them.
    "proxy_config_plan",
    "regenerate_proxy_config",
    "proxy_alias_drift",
    "_alias_drift_lines",
    "_owned_process_state",
    "_stop_owned_proxy",
    # The onboarding register's ONE writer and ONE reader door. They are facade surface because `seat._spawn`/`_resume` call the
    # writer from the facade's own body, and because every consumer outside
    # this family reaches the schema through `seat` rather than importing an
    # impl module — the import discipline this tree already keeps.
    "ONBOARDING_ABSENT",
    "ONBOARDING_INVALID",
    "ONBOARDING_NONE",
    "ONBOARDING_VALID",
    "Onboarding",
    "parse_onboarding",
    "onboarding_invalid",
    "_record_onboarding",
})


# Task/2440 — the project-canonical seat surface, kept as its OWN tier so the
# arm that resolves each name to its defining module has an exact subject.
# Every name here is reached by a consumer through `seat`, the import
# discipline this tree already keeps.
_PROJECT_SEAT_SURFACE = frozenset({
    # Task/2440, tier 1 — the PROJECT-CANONICAL NAME RESOLVER. `<project>-
    # <family>` is read by spawn, by `where`, by `resume` and by the reboot
    # sweep, so the resolver and its two family lists are facade surface for the
    # same reason every other shared predicate here is: the consumers reach the
    # schema through `seat`, never through an impl module.
    "NATIVE_FAMILY",
    "spawn_families",
    # round four — the BARE list and the NUMBERED list are different sets (only
    # an OAuth-pool family may be numbered), and a message that prints one while
    # meaning the other advertises a form the next gate refuses.
    "numbered_families",
    "project_families",
    "spawn_identity",
    "_named_seat_family",
    "registered_seat_family",
    # Task/2440, tier 2 — the PROJECT-INSTANCE ENDPOINT BLOCK and the
    # family-agnostic surface-ownership gate. `doctor`, the proxy launch line and
    # both spawn legs read the allocation and the gate through the facade; the
    # block constants are surface because the refusal that keeps base+N out of
    # the block is stated in terms of them.
    "PROJECT_PORT_BASE",
    "PROJECT_PORT_SPAN",
    "_project_port_block_is_clear",
    "_numbered_port_collision",
    "_numbered_port_reservations_are_disjoint",
    "_instance_ports_path",
    "instance_port_ledger",
    "allocate_instance_port",
    # round four — the READER's endpoint, distinct from the admission answer: a
    # seat minted before the block existed still holds its derived port, and a
    # status row handed None formats `port %d` of a None.
    "_existing_instance_port",
    "_seat_spawn_gate",
    # Task/2440, tier 3 — the WORKSPACE-BELONGS-TO-THE-PROJECT door. These live
    # on the facade beside `_spawn` for the `_cwd_moved`/`_room_for_cwd` reason
    # above: they exist to keep one law from being retyped per adapter, and the
    # law has to run before either adapter provisions anything.
    "_project_tla_notice",
    "_project_scope_prefixes",
    "_workspace_project",
    "_main_checkout",
    "_nearest_existing_dir",
    "_workspace_repo_candidates",
    "_workspace_candidates",
    "_spawn_workspace_ref",
    "_spawn_scope_error",
    # Task/2440, tier 4 — the NATIVE LEG and the two lifecycle rungs both legs
    # now share. `_spawn_reap` and `_spawn_submit` are facade surface precisely
    # because they are the rungs a second lifecycle skipped; naming them here is
    # what makes a later leg that quietly stops calling them visible.
    "_spawn_reap",
    "_spawn_submit",
    "_native_launch_argv",
    "_spawn_native_plan",
    "_spawn_native",
    # Task/2440, round four — the four rungs the native producer owes its own
    # record. The endpoint COMMIT is separated from the admission gate, the
    # selected claude home is pinned so the exact-session proof can find the
    # storage a native session really writes, the pre-spawn register is handed
    # back when the pane never starts, and a closed pane's state is the terminal
    # proof's word rather than the string "UP".
    "_ensure_instance_endpoint",
    "_native_config_home",
    "_restore_spawn_register",
    "_pane_state_after_close",
    # Task/2440, round five — the ONE door between `_instance_port` (which
    # answers None for three real states) and every `%d` that writes an
    # endpoint into a launch asset. It is facade surface because the launch
    # writers reach it through the fanout, so a seam that stopped supplying it
    # is a name gap rather than a runtime None.
    "_launch_endpoint",
    # ...and the PURE admission half beside it, because `seat launch` is a
    # SECOND door that mints assets carrying an endpoint and had no endpoint
    # admission at all: `launch codex -i 183` reached the renderer with a seat
    # whose derived port the allocation refuses.
    "_instance_endpoint_error",
    # Task/2440, round six — the MAINTENANCE half of that one door. Every
    # consumer that operates on an instance which ALREADY EXISTS (`seat up`, the
    # supervise reconciler, the drift reader, all of them through
    # `proxy_config_plan`) resolves its endpoint here, so none of them can reach
    # the new-admission refusal that was stopping a restart cold. Facade surface
    # for `_launch_endpoint`'s own reason: the asset writers reach it through the
    # fanout.
    "_maintenance_endpoint",
    # Task/2440, rounds six and seven — the SPAWN ATTEMPT. A spawn publishes its
    # register before the child runs (so the child's first SessionStart has a
    # family to resolve and a record to bind into) and RELEASES the lifecycle
    # lock while that child starts (so the child's own hook is not killed waiting
    # on it). These are the names that carry the serialization and the authority
    # across that window: the record's three states and the fields the child
    # writes, the lock's release door, the ATTEMPT TOKEN's one producer
    # (`_publish_spawn_attempt`) and the env name and reader that thread it into
    # the child and back out of its hook, the three-plus-one verdicts about the
    # publishing process, the in-flight refusal that replaces the flock for the
    # duration, the finalize that merges only the same attempt, the settlement
    # that restores authority only on PROVEN absence and otherwise KEEPS the
    # facts it holds, and the where-claim derived from what it returned.
    "SPAWN_ATTEMPT_PENDING",
    "SPAWN_ATTEMPT_COMPLETE",
    "SPAWN_ATTEMPT_INCOMPLETE",
    "SPAWN_ATTEMPT_ENV",
    "SPAWN_CHILD_FIELDS",
    "SESSION_BINDING_HARNESSES",
    "ATTEMPT_OWN",
    "ATTEMPT_LIVE",
    "ATTEMPT_GONE",
    "ATTEMPT_UNVERIFIABLE",
    "_seat_lifecycle_lock_released",
    "spawn_attempt_token",
    "_new_spawn_attempt",
    "_publish_spawn_attempt",
    "_attempt_process_state",
    "_pending_attempt_conflict",
    "_finalize_spawn_attempt",
    "SETTLED_RESTORED",
    "SETTLED_INCOMPLETE",
    "SETTLED_FOREIGN",
    "SETTLED_UNRECORDED",
    "_settle_spawn_attempt",
    "_settled_record_ref",
    "_settled_where_claim",
    "_refuse_in_flight_spawn",
    # the per-seat proxy-pool wall: the pane judge's UNANCHORED verdict and
    # the expiry it derives from an observation instant (helm/poolwall.py)
    "WALL_UNANCHORED",
    "_pool_expiry",
})

_CURRENT_SURFACE = (_BASELINE_SURFACE | _FACADE_BRIDGE_SURFACE |
                    _POST_SPLIT_SURFACE | _PROJECT_SEAT_SURFACE)
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

    def test_every_project_seat_name_frozen_above_is_a_REAL_re_export(self):  # noqa: VACUOUS_ASSERTION — the two membership asserts at the end ARE the unconditional positive control: one impl-owned name and one facade-local name must both have been resolved, so neither tier's loop can have been skipped
        """Task/2440's cure to the two frozen registries above was a list of
        names, and a list of names is the one kind of cure that can be wrong in a
        way equality cannot see: `seat.foo` could be a stale COPY left on the
        facade after the implementation moved, and both inventories would still
        match. This resolves every added name to the module that DEFINES it and
        requires the facade attribute to be that module's identical object.

        CONTROLS, one per direction, so the arm cannot pass by treating the two
        tiers alike: `allocate_instance_port` must resolve into seat_paths (an
        impl-owned re-export) and `_spawn_native` must resolve into seat.py
        itself (facade-local, beside the `_spawn` it exists to keep honest).
        Blast radius: reflection over already-imported modules; nothing runs.
        """
        impl = {"seat_lifecycle_sessions.py": seat_lifecycle_sessions,
                "seat_lifecycle.py": seat_lifecycle,
                "seat_lifecycle_runtime.py": seat_lifecycle_runtime,
                "seat_paths.py": seat_paths}
        facade = os.path.realpath(seat.__file__)
        checked = []
        for name in sorted(_PROJECT_SEAT_SURFACE):
            value = getattr(seat, name)
            if not inspect.isfunction(value):
                # the constants (NATIVE_FAMILY, the port block, the attempt
                # states and the two declared tuples) carry no source file;
                # resolvability is all there is to prove for them
                self.assertIn(type(value), (str, int, tuple), name)
                continue
            # UNWRAPPED, because a @contextmanager's own code object lives in
            # contextlib. The decorated object IS the impl module's attribute —
            # which the identity assert below is what actually proves — so
            # resolving its source through `__wrapped__` asks about the function
            # this tree wrote rather than about the decorator it wears.
            source = os.path.realpath(inspect.getsourcefile(inspect.unwrap(value)))
            with self.subTest(name=name):
                if source == facade:
                    checked.append(("facade", name))
                    continue
                owner = impl.get(os.path.basename(source))
                self.assertIsNotNone(
                    owner, "%s resolves into %s, which is not one of the "
                    "project-seat implementation modules" % (name, source))
                self.assertIs(value, getattr(owner, name),
                              "%s on the facade is not %s's object — a stale "
                              "copy survived the move"
                              % (name, owner.__name__))
                checked.append(("impl", name))
        self.assertIn(("impl", "allocate_instance_port"), checked)
        self.assertIn(("facade", "_spawn_native"), checked)

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

    def test_health_helpers_read_facade_rebindings(self):  # noqa: VACUOUS_ASSERTION — synthetic liveness plus current config/process state produce a concrete HEALTHY row through every rebound seam
        originals = {name: getattr(seat, name) for name in (
            "_proxy_pid_record", "_running_pid", "_port_open",
            "proxy_config_plan", "proxy_drift")}
        replacements = {
            "_proxy_pid_record": lambda family, name: None,
            "_running_pid": lambda family, name: 4321,
            "_port_open": lambda port: True,
            "proxy_config_plan": lambda path, family, name: {"changed": False},
            "proxy_drift": lambda family, name, record=None:
                (seat.PROXY_CURRENT, None),
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
