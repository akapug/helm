# The registries a new module owes

A new file under `helm/` is not just a file. **Twenty-two test modules
enumerate the package** and assert a property of every module they find, and
**nine more registration points live outside `tests/`** — four rungs
(`helm/wiring.py`, `helm/docref_guard.py`, `helm/splitbudget.py`,
`helm/registry.py`), one dispatcher (`helm/cli.py`) and four docs
(`docs/VERBS.md`, `docs/ENVIRONMENT.md`, `docs/ARCHITECTURE.md`,
`docs/HOOKS.md`).

Nothing wrote that down, so the cost has been paid by discovery: a module lands,
the whole suite goes red, the author cures one arm, the next gate names the
next one. The whole suite is the slowest place in the tree to learn anything.

Read this the way you would read a checklist. **§1 fires no matter what you
write.** **§2 fires only if you do the thing in the TRIGGER column.** **§3 is
what your new test module owes.** **§4 applies only inside the `seats_*`
family.** **§5 will not catch you, which is the reason to read it.**

Each row names the enforcing arm and, more usefully, **the concrete edit** that
satisfies it. A list of test names alone is not actionable; the actionable half
is what you have to write.

This page is a projection of the tree and goes stale the way every projection
does — §7 is how to re-derive it.

---

## 1. Fires on any new module

These ask nothing about what your module does. Adding the file is enough.

### 1.1 Reachability — `helm/wiring.py` `ALLOWED`

**What refuses.** The Stop hook. `helm/seats_stop_guard.py` calls
`wiring.gate_lines()`, which calls `wiring.unwired_additions()`, which asks
`git diff --diff-filter=A` plus `git ls-files --others` for the modules **this
tree added** and reports any that no import path reaches from `cli`,
`__main__` or `__init__`. Your turn ends with:

    you added 1 module(s) that NOTHING can reach — built, not wired:
        helm/<name>.py

This is the only rung in the list that fires **before** a gate. It is latched
per distinct debt, so a standing debt stops one seat once rather than walling
every seat on the host.

**What you write.** Wire it — a `cli.VERBS` entry, or a caller in a module that
is already reachable — or add an entry to `ALLOWED` in `helm/wiring.py`:

```python
ALLOWED = {
    "<name>": "the reason static import cannot reach it",
}
```

An empty reason is not accepted: the list is the pressure, and "I forgot" is
not available as an outcome. If your module is a snapshot-and-run rung (the
`nevertrack` class — the hook copies the file beside itself and runs it as a
plain script where no helm package is importable), that is the reason, and
`tests/test_wiring.py::test_every_nonintrinsic_ALLOWED_entry_has_a_checkable_obligation`
pins the membership of that class, so a new member changes that arm's expected
list too.

**Related.** A module that declares an action meant to run unattended also owes
`ACTUATORS` and `ALLOWED_ACTUATORS` in the same file; the census then proves the
declaration against installed hooks, units and crontab rather than believing it.
`helm wiring` prints the whole ladder locally.

### 1.2 Nothing imports it — the UNTESTED rung, and the FACADE category beside it

**What refuses.** Nothing, and that is the point. `wiring.tested()` credits a
module only when some file under `tests/` names it in an import. A module no
test imports is reported by `helm wiring` and by the owner-facing census, never
by a refusal.

**What you write.** `tests/test_<name>.py`, with a real import. Importing is a
floor, not proof; the arms are the work — and that file owes §3.

**Any import form counts**, because the rung parses your test file rather than
pattern-matching its lines: `from helm import x`, `from helm import x as y`, a
parenthesised multi-line bundle, `from helm.pkg import sub` and
`import helm.pkg.sub` all credit the same nodes. It did not always — the
line-wise version could cross neither an alias nor an opening parenthesis, and
it published 27 modules as untested of which 16 had been imported by a test all
along. A module named only in a comment or a docstring still earns nothing:
prose about a module is not a test of it, which is the same distinction §1.1
draws for callers.

**A module behind a FACADE is reported separately, and is NOT tested.**
`helm/web_compat.py` copies a declared list of names out of each sibling
`web_*` implementation module into `helm.web`'s namespace. A test that drives
`web._api_chat` runs `web_chat._api_chat` and never spells `web_chat`, and
`tested()` is blind to it by construction — its submodule credit follows a
package's import closure, and these are siblings. Seven modules read as
untested while 46 test files ran their code.

Crediting them would have been the easy fix and the wrong one. The binding is
per NAME: a test that names `helm.web` names the whole namespace and touches a
handful of it, and the counterexample is on this tree — `web_common.code_drift()`
had never been called with all 46 of those files running, and answered "no
drift" through its own fail-open the whole time. Whole-module credit would have
published that module as verified.

So `wiring.facade_reached()` answers **per symbol**, and the census publishes a
third category, `facade_only`, that `helm wiring` prints as its own block:

    reached only through a FACADE — this is NOT tested: 5. ...
      web_chat: 2 of 13 re-exported names read off helm.web

`--verbose` adds the names no test reads. The fraction is the point; a module
here is credited by nothing. A module whose facade names **no** test reads gets
no row at all and stays in `untested` — which is where `web_multiplayer` and
`web_sessions` still are.

Two bounds, both under-crediting. A test that drives the HTTP surface issues
`GET /api/chat` and never names the handler, and a name reached through a patch
target string is not read either, because patching a symbol replaces it. The
owner table is read out of `web_compat.py`'s source rather than by importing
it; if it is renamed, the rung finds nothing and its modules go back to being
published as untested — a census may fail toward the accusation, never toward
the exemption, and
`tests/test_wiring.py::FacadeRungRealTreeTest::test_the_parsed_owner_table_matches_what_web_compat_ACTUALLY_binds`
is the arm that refuses to let that happen silently.

**Adding a facade.** Append `(compat module, facade module, owner-table name)`
to `wiring.FACADES`; the table must stay a literal `ast.literal_eval` can read.

### 1.3 Invalid escape sequences — `tests/test_escape_hygiene.py`

**What refuses.** `test_no_invalid_escape_sequences_in_tree` tokenizes every
`.py` under `helm/`, `tests/` and `bin/` and refuses a backslash followed by a
character Python does not recognise, inside any non-raw literal. Raw literals
are skipped, which is why this cannot be a text grep.

**What you write.** Double the backslash where a downstream shell, regex or
format string is meant to receive one. Never delete it — it is load-bearing for
the consumer. The defect is silent once a `.pyc` exists, which is why it is a
test and not a habit.

### 1.4 Third-party imports — `tests/test_readme_claims.py`

**What refuses.** `test_the_stdlib_only_promise_is_still_true` scans every
module directly under `helm/` for an import the standard library does not
provide.

**What you write.** A different design. The exemptions are narrow and
recognised BY NODE, never by name: an import inside a `try` whose handler
catches `ImportError` is the documented optional pattern, and a
`from . import X` / `import X` pathname-fallback **pair** is exempt as a pair —
an unguarded import of the same name elsewhere still counts.

### 1.5 Cited object ids — `helm/docref_guard.py` + `tests/test_docstring_refs.py`

**What refuses.** Two things, at two speeds.

*At commit:* `helm/docref_guard.py`, a pre-commit rung, reads the **staged
diff** for hex tokens your commit ADDS under `helm/` and refuses any this
repository cannot account for. It asks a fresh clone's question deliberately, so
a lane branch that resolves on your box does not count.

*At the gate:* `tests/test_docstring_refs.py::test_every_cited_sha_resolves`
walks every comment and docstring under `helm/` and asks the same question more
loosely.

**What you write.** If the token is a commit reachable from a remote ref,
nothing. Otherwise register it in `helm/docref_guard.py` under exactly one of
`REGISTRY_NAMES` — and the category is not a formality:

| Registry | The claim it makes |
|---|---|
| `LEDGER_CITED` | a live ledger row or receipt carries this id. Fails closed: a new key no live ledger vouches for is refused |
| `PATCH_IDS` | content identity, recomputed from the exact diff |
| `SKIP` | a reasoned non-object runtime token — a pid, a fixture id |

The registry file is exempt from itself; without that, the act of registering a
citation reads as making an unaccounted one and the guard blocks its own cure.
`docs/` is outside `POLICED` entirely, so prose in this file is not scanned.

---

## 2. Fires on what your module does

**TRIGGER** is the thing you have to do to attract the arm.

### 2.1 You add a verb

| Trigger | What refuses | What you write |
|---|---|---|
| a new `cmd_<name>` | nothing yet — an unregistered verb is simply invisible | `helm/cli.py` `VERBS`, via `_lazy` so one broken leg cannot take the CLI down |
| a registered verb | `helm <verb> --help` prints nothing useful | `helm/cli.py` `_VERB_HELP[<verb>]` — `cli.py` prefers it over `fn.__doc__`, so a docstring-only mention is **never printed** |
| a registered verb | the repo's own law: a verb without a doc entry is a bug | a `docs/VERBS.md` entry |
| your dispatcher compares `args[0]` against literal subverbs | `tests/test_surface_wiring.py::test_every_accepted_subverb_is_printed_or_reasoned` | put every accepted token in the root synopsis (`_VERB_HELP[<verb>]`), or add it to that file's `ALLOWED` with a reason **and its death condition** |
| your dispatcher takes an unknown subverb | `tests/test_dispatch_honest.py::test_every_discovered_dispatcher_refuses_unknown_subverb` — discovery is source-driven, so your dispatcher joins the fleet with no edit to the test | refuse an unknown head token BY NAME. A quietly-dropped `--gaet` reports a clean answer to a question nobody asked. Exemptions live in that file's `EXEMPT` and are checked for rot |
| your verb reads free text from argv | `tests/test_freetext.py::test_every_dispatcher_in_the_tree_routes_through_it_or_is_declared` | route through the free-text door, or declare the dispatcher in that file's `EXEMPT` |
| your handler reads a membership/`apply`-shaped argument | `tests/test_dispatch_honest.py::test_every_apply_reader_is_guarded_or_declared` | guard the read, or register in `APPLY_READER_EXEMPT` |
| your handler reads a flag | `tests/test_dispatch_honest.py::test_every_flag_reader_is_guarded_or_declared` | guard it, or register in `FLAG_READER_EXEMPT` |

### 2.2 You touch seat identity or the roster

The densest cluster, and where the most recently added verb actually got caught.

| Trigger | What refuses | What you write |
|---|---|---|
| you read the seat roster | `tests/test_display_launder_tripwire.py::test_every_roster_consumer_is_allowlisted` | an entry in `_ROSTER_CONSUMERS` keyed `<name>.py` — **and the entry is not the fix.** A roster key is unvalidated at the join seam, so every key that reaches an EMITTED value must pass through the one label door, `seats_common._seat_label`. Raw keys may still drive internal matching; only the emitted value is the hazard |
| the same | `test_roster_call_site_counts_are_pinned` | the matching count in `_ROSTER_CALL_COUNTS` |
| you delete a roster read | `test_allowlist_has_no_stale_entries` | remove both entries — an allowlist may not outlive its subject |
| you read the roster at all | nothing mechanical — but `seats.roster()` is **fail-open**: an unreadable, malformed or non-mapping register comes back `{}` and raises nothing, so an `except` around it can never fire | read through `roster_checked`, the tri-state producer, and render UNREADABLE. A fail-open read behind a dead `except` prints "nobody is available" for a register that merely would not parse |
| you resolve a **bare seat name** (`derive_seat`, `acting_seat`, `whoname`) | `tests/test_identity_layer.py::test_every_bare_resolver_call_is_CLASSIFIED` — the census is mechanical, so a new call site cannot appear unclassified | if the answer reaches a durable mutation, route it through `helm.actors.resolve_actor` instead. Otherwise add `(module, resolver, enclosing def) -> (RENDER\|ADDRESS\|BIND, why)` to `UsesClosureTest.REGISTRY`; the reason must be over 30 characters, because reviewing these is the whole point |
| you read the `HELM_CHAT_NAME` env var | `tests/test_display_launder_tripwire.py::test_only_the_accessor_reads_helm_chat_name` — source-driven, a new reader appears with no edit to the test | read it through the accessor module. There is no allowlist row for this one |
| you read a row's `from` field | `test_every_from_field_consumer_is_allowlisted` and `test_from_field_read_counts_are_pinned` | entries in `_FROM_FIELD_CONSUMERS` and `_FROM_FIELD_READ_COUNTS` |
| you read a nested transport projection | `test_every_nested_transport_projection_is_allowlisted` | an entry in `_TRANSPORT_PROJECTION_CONSUMERS` |
| you name the `seat_declared` key | `tests/test_rearm.py::test_seat_declared_is_named_only_where_it_is_allowed_to_be` — the walk is recursive and keys on the path under `helm/`, not the basename | an entry in `_SEAT_DECLARED_SITES` keyed `("<path under helm/>", "<innermost enclosing def>")` |
| you import a seat **impl** module (`seat_lifecycle`, `seat_paths`, …) | `tests/test_seat_facade_injection.py::test_nothing_imports_an_impl_module_without_the_facade` | prefer the facade: `from helm import seat`, then `seat.<name>` — it is the same function object. If you genuinely need the impl module, an accepted facade import must stand in the **same or an enclosing** scope; a sibling or nested scope does not qualify |

### 2.2b You add a symbol to a `helm/web_*.py` implementation module

`helm/web.py` is a facade: the impl modules are bound onto it by `web_compat`,
and the surface is pinned by count and by name in three places. Adding ONE
helper to `web_quota.py` costs four edits, and each is a separate red if you
find them one at a time. The client-side page has the same shape: the suite
lifts functions out of the assembled HTML **by name**, so a new one is simply
absent and its caller dies at a `ReferenceError` inside node.

| Trigger | What refuses | What you write |
|---|---|---|
| a new module-level name in `helm/web_*.py` that anything outside that module calls | `AttributeError: module 'helm.web' has no attribute '<name>'` at the call — the facade never bound it | the name in that module's tuple in `web_compat._OWNER_NAMES` |
| the same | `tests/test_web_split_contract.py::test_the_runtime_surface_remains_exact` | the name in `_BASELINE_SURFACE` |
| the same | `test_the_implementation_inventory_and_fanout_are_exact` | the `_WEB_FANOUT_NAMES` length pin in that arm |
| the same | `test_facade_callables_unpickle_in_a_fresh_process` | the export count in that arm, and the count named in its `# noqa` reason |
| a new top-level `function` in a `helm/web_ui/scripts/*.js.part` that an arm exercises | nothing mechanical — the harness builds a file without it and node raises `ReferenceError` at the first call, which surfaces as every arm in that class failing at once | the name in the harness's `EXTRACT`/`CONSTS` list. Lift the function; never re-implement it in the harness — a stub that is more forgiving than the shipped code is how a real defect rides through a green suite |

### 2.3 You spawn a subprocess

| Trigger | What refuses | What you write |
|---|---|---|
| your module spawns `git` directly | `tests/test_vcs.py::test_every_direct_git_spawn_is_declared_with_a_reason` | route through the `helm/vcs.py` seam. If you truly cannot — the snapshot-and-run rung class again — add `"<name>.py": "<a TRUE reason>"` to `_DIRECT_SPAWN_DEBT`. A wrong reason is worse than no allowlist, because the reason IS the audit trail |
| the same | `test_the_counts_are_pinned_and_match_the_docs` | the pinned count in that arm **and** the `"N direct git spawns"` sentence in `docs/ARCHITECTURE.md` — a registration point outside `tests/` |
| your module builds an argv dynamically and is **not** spawning git | the same census reports it UNRESOLVED | add it to `_DYNAMIC_ARGV_MODULES`. That list is for modules CONFIRMED not to be git; if it IS git, hoist the binary to a module constant so the census can see the spawn |
| your module `exec`s or attaches to a session | `tests/test_session_start_closure.py::test_every_exec_or_attach_site_is_classified` — AST, not grep, so a name in a comment cannot pad the closure | an entry in `_CLASSIFIED`. A module that `subprocess`es the seat's own `launch.sh` is covered BY CONSTRUCTION instead — the refusal lives inside the artifact it runs — and the second arm pins that the class is still enumerated |

### 2.4 You write state, read config, or notify

| Trigger | What refuses | What you write |
|---|---|---|
| your module reads or writes a store under the helm home | `helm doctor`'s projection-registry check, and `tests/test_registry.py::test_squatter_detected_in_both_roots` — an undeclared file in either root is a squatter | a `row(...)` in `helm/registry.py` `projections()`: the store's name, its kind (`projection`/`events`/`authored`/`backup`), its root, its globs, its `source=` and its `rebuild=`. `helm projections` is the read surface |
| your module reads a `HELM_*` variable **from the process environment** | nothing mechanical in the general case | a row in `docs/ENVIRONMENT.md`: variable, default, read by, legacy fallback. The env2 pattern — a working default, and a superseded spelling read as a fallback forever and written never |
| you spell a `HELM_*` key that is **not** read from the process environment: an internal protocol key on a dict helm builds and strips before any spawn | nothing | **no** `docs/ENVIRONMENT.md` row. A row there tells an operator they may set it, and they may not; that is a documentation lie, not a missing registration. Say what it is in the constant's own comment instead |
| you add a `HELM_STOP_GUARD_*` per-check kill switch | `tests/test_reference_parity_pins.py::test_every_swept_switch_has_an_environment_row` — it sweeps `_off("STOP_GUARD_X")` calls anywhere under `helm/` | its own row in `docs/ENVIRONMENT.md`, plus the switch count in `docs/HOOKS.md` (`test_hooks_md_states_the_switch_count`) |
| your module writes a value it CUT — a slice, a `*_CAP`/`*_MAX`/`*_LIMIT` bound — into something serialized, appended to a ledger, written to a file or published | `helm/silent_cap.py`, at pre-commit (WARN only) | mark the cut IN the written value (a `"%d of %d bytes"` notice beside it), or refuse over the bound. A deliberate cut names its reason on the slicing line: `# noqa: SILENT_CAP — <reason>`. An identifier prefix, a bound under four, and a `print` are not cuts and are never reported |
| your module notifies a phone | `tests/test_notify.py::test_there_is_exactly_ONE_phone_path_in_the_tree` and `test_only_notify_may_reach_the_telegram_transport` | reuse the existing channel. Compose, do not parallel — a second notification path adds failure modes without the benefit |
| your module reads the orca pane census | `tests/test_orcaadopt.py::test_every_census_consumer_declares_how_it_handles_BLINDNESS` | an entry in `_CENSUS_CONSUMERS`, and no consumer may emit its unsafe value while blind |
| your module sends to a pane | `tests/test_orcaadopt.py::test_every_send_site_is_declared_with_an_authority` — the walk is recursive and keys nested files on their path under `helm/` | an entry in `_SEND_AUTHORITIES` naming the authority (ADOPTED, REGISTERED, DELEGATED). Each class has its own arm proving the send really behaves that way |
| your module reaches the handoff seams | `tests/test_handoff.py::test_the_SEAMS_NEVER_LEAVE_THIS_MODULE` — matched by NAME, so an alias, an attribute form or a `getattr` constant all count | keep the seams inside `helm/handoff.py` |

### 2.5 Portability

| Trigger | What refuses | What you write |
|---|---|---|
| `os.path.realpath(p, strict=...)` | `tests/test_foldcompose.py::test_no_module_calls_realpath_with_the_3_10_strict_keyword` | `pathlib.Path(p).resolve(strict=True)`. The repo declares a 3.9 floor; the gate runs a much newer interpreter and cannot see this at all |
| `getattr(os, "O_...", 0)` | `tests/test_openflags.py::test_production_has_no_getattr_O_flag_defaulting_to_zero` | a default of 0 silently disables the protection the flag exists for — fail loudly instead |

### 2.6 You print an instruction

| Trigger | What refuses | What you write |
|---|---|---|
| your module (or a doc) prints a helm command as a remediation | `tests/test_instructions_are_runnable.py::test_every_advertised_root_and_subverb_resolves` — it scans every `.py`, `.md`, `.html` and `.js` under `helm/` and `docs/`, plus the README, for command-shaped text in code spans, console lines and CLI labels | a command whose root verb is in `cli.VERBS`, whose subverb is one the dispatcher accepts, and which carries every flag the verb REQUIRES. A printed cure that fails at argument parsing — or worse, succeeds as a dry run and changes nothing — teaches people to ignore the channel it came on |

Note the trap, because this page fell into it while being written: the scanner
strips `.,:;()[]{}` from the token after the verb, so a literal `helm` followed
by three dots as a placeholder reduces to an empty root verb and is refused.
Write `<verb>` when you mean a placeholder.

---

## 3. Your test module owes its own set

`tests/test_<name>.py` is enumerated too, by a second family of arms.

| Arm | Property | What you write |
|---|---|---|
| `helm/vacuous_assertion.py`, at pre-commit | an arm that asserts nothing — an `except: pass` with no observable, a memo arm where every branch agrees | assert the EFFECT. A deliberate exception carries a `# noqa: VACUOUS_ASSERTION` with the reason |
| `helm/orphaned_mock.py`, at pre-commit (WARN only) | a mock nothing drives | drive it, or drop it |
| `tests/test_env_hygiene.py::test_every_test_module_restores_the_env_vars_it_sets` | a test that sets an env var and leaks it into the next test | restore it; the detector accepts every restoration form already in use |
| `tests/test_env_hygiene.py::test_no_test_runs_its_own_setUp_over_a_live_fixture` | a test (or a helper it calls) that runs `self.setUp()` while the fixture is live, so tearDown restores the fixture's own temporary values and the next test in the process inherits them | a test of its own, or a per-case scope such as `mock.patch.dict(os.environ, ...)`. `self.tearDown()` as the statement immediately before is the one exempt shape |
| `tests/test_assertion_hygiene.py::test_no_assertion_in_tests_renders_the_ambient_environment` | an assertion whose expected value comes from the ambient environment | pin the value |
| `tests/test_scratch.py::test_no_test_module_unroutes_the_process` | a test that moves the process off the routed scratch estate | leave the routing alone |
| `tests/test_socket_paths.py::test_every_socket_fixture_file_is_represented` | a socket fixture outside the path budget | keep fixture socket paths inside the budget |
| `tests/test_suite_collection.py` | a test file `unittest discover` cannot collect | name and place it so discovery descends to it |

Tests must also point `HELM_HOME` and `HELM_ADOPTED_DIR` at temp dirs and never
touch the real stores — see [CONTRIBUTING.md](../CONTRIBUTING.md).

**This section is not claimed complete.** §1, §2 and §4 come from a deliberate
enumeration of everything that walks `helm/`; these eight arms were found as a
by-product of that sweep, and the `tests/` tree was never swept the same way.
Treat a red arm here as evidence the list is short, not as a surprise.

---

## 4. The `seats_*` family only

If your file is named `helm/seats.py` or `helm/seats_<something>.py` it joins a
second, stricter set. If it is not, none of these can see it.

| Arm | Property | What you write |
|---|---|---|
| `helm/splitbudget.py`, at pre-commit | a per-file line ceiling, asked against the **staged blob**. It refuses only what your commit makes WORSE: growth past the budget refuses, a shrinking over-budget file passes, a standing debt you did not touch is reported and never refuses | split the module, or shrink it |
| `tests/test_seats_split_contract.py::test_the_facade_only_ever_shrinks` | a ratchet on `seats.py`'s size | every extraction lowers `CEILING`; it may never rise |
| `::test_no_extracted_module_references_an_undefined_global` | a moved function may not call a name that stayed behind. Import does not catch this — the reference lives in a function body — and neither does a green suite | define it, import it, or let the facade export it |
| `::test_no_scanned_module_has_a_module_scope_annotation` | a module-scope annotated assignment confuses the undefined-global checker on 3.14 | drop the annotation |
| `::test_star_import_is_the_one_false_positive_and_is_unreachable` | no scanned module may `from x import *`, because the checker would lie there | do not star-import |
| `::test_no_sibling_rebinds_a_module_global` | a sibling may not rebind a facade global | pass the value instead |
| `tests/test_roster_write_guard.py::test_EVERY_whole_file_writer_reads_through_the_guard` | a function that writes `roster_path()` may not bind the fail-open reader | bind `roster_for_write`, never `roster` |
| `tests/test_lease_recovery.py::test_the_release_path_no_longer_calls_the_lease_a_capability` | the release path's wording, across every `seats*.py` — the invariant is about the SURFACE, never the filename it happens to live in | keep the wording |

---

## 5. The closed sets that will NOT catch you

These enumerate a **fixed list of modules** rather than the tree. A new module
is silently outside them. That is not a licence — it is the reason to check by
hand, because the arm cannot tell you.

- `tests/test_orcatitle.py` `_TITLE_MODULES` — the pane-identity modules whose
  `title` reads are censused. A new module that decides pane identity is
  invisible to it until somebody adds the name.
- `tests/test_session_start_closure.py`'s generated-asset consumers — the
  exec-site census walks, but that sibling classification is a closed list.
- `helm/nouncensus.py` `NOUNS` — the identity-bearing nouns. The census walks
  `wiring.modules()`, so your module IS scanned, but only for nouns already
  named.

The shape: **a set that is closed over the thing being added cannot report what
it admits.** When you add a module that belongs in one of these, adding the name
is part of the change, and no gate will say so.

---

## 6. Two worked examples

### A cache

`helm/gitfacts.py` owed three things and paid all three in its first commit: it
is reachable (`helm/vcs.py` imports it), `tests/test_gitfacts.py` imports it,
and it writes a cache under the helm home — so it carries a
`row("gitfacts", "projection", "cache", ...)` in `helm/registry.py`
`projections()` with its source and its rebuild. It owes nothing in
`_DIRECT_SPAWN_DEBT` precisely because it goes through the seam rather than
around it. And its `HELM_GITFACTS_UNCACHED` constant owes **no**
`docs/ENVIRONMENT.md` row: it is a reserved key of the seam's overlay dialect,
removed before a child environment is built and never read from `os.environ`.
That distinction is §2.4's third row, and it is the one a reader is most likely
to get backwards.

### A checkpoint

`helm/foldckpt.py` (task/2770) keeps the dispatch ledger's whole fold as a
checkpoint. What it is, what an operator should SEE, and what every stale
reason means are in `docs/FOLD_CHECKPOINT.md`; this entry is only about its
registrations, which it paid the same way: reachable because
`helm/dispatches.py` imports it, imported by `tests/test_foldckpt.py`, its git
reads routed through the `vcs` seam (so no `_DIRECT_SPAWN_DEBT` row), and no
new `HELM_*` variable. Its store is declared as
`row("dispatch-fold", "projection", "home", ...)` in `helm/registry.py`
`projections()`: the files live under `_global/.state/dispatch-fold/` in the
helm home, so HELM_HOME isolates them in every test — a cache outside the home
would be a cross-test channel — and its rebuild is any read of the ledger. The
glob names `pk.atomic_write`'s `*.tmp` too, because a crash between write and
rename leaves one, and an undeclared leftover is a squatter.

### A verb

`helm/reviewer_eligibility.py` landed reachable from `cli.VERBS`, with a
`_VERB_HELP` line and a `docs/VERBS.md` row — §1.1 and §2.1 paid at once. The
whole-suite gate then named three more in a single run, all from §2.2:

1. it read the roster and laundered nothing — cured by `_ROSTER_CONSUMERS`,
   `_ROSTER_CALL_COUNTS`, and every emitted key routed through the one label
   door;
2. reading that guard revealed the accessor was also wrong. `seats.roster()` is
   fail-open, so the `except` around the read could never fire and an
   unparseable register would have printed "nobody can review this row" — the
   exact inversion the verb exists to end. Cured by `roster_checked`;
3. an impl-module import with no facade import in scope — cured by asking
   `seat.seat_liveness`, which is the same function object.

Working §1 then §2 in order would have caught all four before the gate.

---

## 7. Re-deriving this list

**To USE the list, do not type it:** `helm gate audits [-- <your test modules>]`
prints it as one `fab test` command. `helm/gateaudits.py` carries the same
twenty-two names as the list at the end of this section, and
`tests/test_gateaudits.py` reddens when either changes without the other.

Start here, but do not stop here:

```console
$ helm wiring
$ git grep -n 'os.walk\|listdir\|\.glob(' -- tests/
```

A grep for one idiom finds one idiom. The list above is the union of three
passes, and the third is the one that finds what the first two miss:

1. an AST sweep of `tests/` for `glob`/`rglob`/`iterdir`/`listdir`/`walk`
   calls, resolving each call's root expression back through its assignment
   chain and keeping the ones rooted at the package or the repo;
2. an AST sweep for dict/set/list literals whose keys are helm module names —
   this finds the hand-maintained registries that no walk-shaped grep sees;
3. reading what the most recently added modules actually had to change:
   `git log --diff-filter=A -- helm/<name>.py`, then every commit after it on
   the same file. A registration the author had to make is recorded in history
   whether or not anybody wrote it down.

### The twenty-two

The test modules that enumerate the package, so the count above is checkable:

`test_dispatch_honest` · `test_display_launder_tripwire` · `test_docstring_refs`
· `test_escape_hygiene` · `test_foldcompose` · `test_freetext` · `test_handoff`
· `test_identity_layer` · `test_instructions_are_runnable` ·
`test_lease_recovery` · `test_notify` · `test_openflags` · `test_orcaadopt` ·
`test_readme_claims` · `test_rearm` · `test_reference_parity_pins` ·
`test_roster_write_guard` · `test_seat_facade_injection` ·
`test_seats_split_contract` · `test_session_start_closure` ·
`test_surface_wiring` · `test_vcs`

`tests/test_wiring.py` and `tests/test_registry.py` are not in that list: they
are the arms on two of the non-test rungs, and the enumeration they exercise
lives in `helm/wiring.py` and in the helm home rather than in the test file.
