"""Silent-dispatcher class closure — no dispatcher may be born silent.

Four layers:

1. SweepTest DISCOVERS every subverb dispatcher from the SOURCE (AST — a
   module-level cmd/cmd_* function whose body dispatches on the first argv
   token via comparisons against string literals/collections or dict lookups)
   and asserts each refuses an unknown subverb with exit 2. A future
   dispatcher with a silent default fall-through is discovered automatically
   and fails here before it ships. Positional-by-design entries live in
   EXEMPT with their pinned alternative behavior; an exemption that stops
   matching a discovered dispatcher fails (no rot).

2. EXEMPT is BRANCH-AWARE, never wholesale: a positional dispatcher may
   still own subverb branches (`sessions resume …`), and each discovered
   branch literal must be declared WITH a guarded-tail probe — codex-2's
   exact-SHA pass proved `sessions resume <id> --go --bogus --help`
   spawned a pane and exited 0 behind the wholesale exemption.

3. NoArgRootLeaves closes the class the dispatcher detector structurally
   cannot see: a VERBS handler that never READS its args (cmd_sync) lets
   the root pass any junk straight through — `helm sync --bogus --help`
   ran the MUTATING sync and exited 0. The set of no-arg leaves is derived
   from the source and must equal cli.NOARG_VERBS, whose tails the root
   guards.

4. Regression tests pin codex-2's FIX findings (helm-dogfood 2026-07-22):
   trailing junk after a KNOWN subverb must refuse BEFORE the side-effecting
   work runs — `seat down codex --bogus --help` stopped the seat and exited
   0 — and `--help` after junk refuses too (the existence probe stays
   honest), while a clean `--help` tail still helps. Plus the centralized
   nearest-match hint below root: a typo'd subverb/flag names what its
   author probably meant.
"""
import ast
import contextlib
import importlib
import io
import os
import pathlib
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("HELM_HOME", tempfile.mkdtemp(prefix="helm-test-home-"))

HELM_DIR = pathlib.Path(__file__).resolve().parent.parent / "helm"
UNKNOWN = "zz-no-such-subverb-zz"

# (module, function) -> {rc, why, branches}. Exemptions are for dispatchers
# whose first token is a documented free positional, not a closed subverb
# set. Every key must still be DISCOVERED, and the exemption is BRANCH-aware:
# "branches" declares every string literal the dispatcher matches its first
# token against, each with a probe proving that branch's tail is guarded
# (junk + --help refuses with exit 2 BEFORE the named side-effecting call).
# A branch discovered in the source but not declared here fails the sweep —
# a dispatcher can be positional in one branch and must be guarded in the
# others; the wholesale free-pass is what hid the resume hole.
EXEMPT = {
    ("sessions", "cmd_sessions"): {
        "rc": 0,
        "why": "args[0] is the documented positional <project> filter",
        "branches": {
            "resume": {"argv": ["resume", "abcdef", "--go", "--bogus", "--help"],
                       "never": "spawn_resume"},
        },
    },
    ("creds", "cmd_swap"): {
        "rc": 1,
        "why": "args[0] is a positional <home-or-account-email>; an unknown "
               "target refuses with rc 1 (not-found), never runs a default",
        "branches": {},
    },
}


# --------------------------------------------------------------- discovery

def _is_str_const(n):
    return isinstance(n, ast.Constant) and isinstance(n.value, str)


def _str_collection(node):
    """A literal collection of (or dict keyed by) constant strings."""
    if isinstance(node, ast.Dict):
        return bool(node.keys) and all(
            k is not None and _is_str_const(k) for k in node.keys)
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return bool(node.elts) and all(_is_str_const(e) for e in node.elts)
    return False


def _collect_strcolls(nodes, into):
    for n in nodes:
        if isinstance(n, ast.Assign) and _str_collection(n.value):
            for t in n.targets:
                if isinstance(t, ast.Name):
                    into.add(t.id)
    return into


def _token(n):
    """args[0] or args.pop(0) — the first-token expressions."""
    if isinstance(n, ast.Subscript) and isinstance(n.value, ast.Name) \
            and n.value.id == "args":
        return isinstance(n.slice, ast.Constant) and n.slice.value == 0
    return (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            and n.func.attr == "pop" and isinstance(n.func.value, ast.Name)
            and n.func.value.id == "args" and n.args
            and isinstance(n.args[0], ast.Constant) and n.args[0].value == 0)


def _has_token(node, bound):
    for n in ast.walk(node):
        if _token(n):
            return True
        if isinstance(n, ast.Name) and n.id in bound \
                and isinstance(n.ctx, ast.Load):
            return True
    return False


def _tok_or_bound(n, bound):
    return _token(n) or (isinstance(n, ast.Name) and n.id in bound)


def _is_dispatcher(fn, module_strcolls):
    """Does this cmd function dispatch on the first argv token?"""
    if not fn.args.args or fn.args.args[0].arg != "args":
        return False
    strcolls = _collect_strcolls(ast.walk(fn), set(module_strcolls))
    bound, grew = set(), True
    while grew:                      # fixpoint: verb = args[0]; v2 = verb …
        grew = False
        for n in ast.walk(fn):
            if not isinstance(n, ast.Assign):
                continue
            for t in n.targets:
                if isinstance(t, ast.Tuple) and isinstance(n.value, ast.Tuple):
                    pairs = zip(t.elts, n.value.elts)
                elif isinstance(t, ast.Tuple):
                    pairs = ((e, n.value) for e in t.elts)
                else:
                    pairs = ((t, n.value),)
                for elt, v in pairs:
                    if isinstance(elt, ast.Name) and elt.id not in bound \
                            and _has_token(v, bound):
                        bound.add(elt.id)
                        grew = True

    def const_side(c):
        return _is_str_const(c) or _str_collection(c) \
            or (isinstance(c, ast.Name) and c.id in strcolls)

    def strdictish(v):
        return (isinstance(v, ast.Dict) and _str_collection(v)) \
            or (isinstance(v, ast.Name) and v.id in strcolls)

    for n in ast.walk(fn):
        if isinstance(n, ast.Compare) and _tok_or_bound(n.left, bound) \
                and all(const_side(c) for c in n.comparators):
            return True
        if isinstance(n, ast.Subscript) and strdictish(n.value) \
                and _tok_or_bound(n.slice, bound):
            return True
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) \
                and n.func.attr == "get" and strdictish(n.func.value) \
                and any(_tok_or_bound(a, bound) for a in n.args):
            return True
    return False


def _mod_tree(mod):
    """AST of a top-level helm module. A decomposed package (helm/<mod>/) is
    viewed as the monolith it replaced: one Module whose body concatenates
    every submodule's body, so structural detectors see the same functions
    and module-level string collections they saw in the single file."""
    p = HELM_DIR / (mod + ".py")
    if p.exists():
        return ast.parse(p.read_text())
    body = []
    for f in sorted((HELM_DIR / mod).glob("*.py")):
        body.extend(ast.parse(f.read_text()).body)
    return ast.Module(body=body, type_ignores=[])


def _iter_mod_trees():
    """(modname, tree) for every top-level module or package under helm/."""
    for p in sorted(HELM_DIR.glob("*.py")):
        yield p.stem, ast.parse(p.read_text())
    for d in sorted(HELM_DIR.iterdir()):
        if d.is_dir() and (d / "__init__.py").exists():
            yield d.name, _mod_tree(d.name)


def discover():
    """Every subverb dispatcher under helm/, straight from the source."""
    out = []
    for stem, tree in _iter_mod_trees():
        mod_strcolls = _collect_strcolls(tree.body, set())
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) \
                    and (node.name == "cmd" or node.name.startswith("cmd_")) \
                    and _is_dispatcher(node, mod_strcolls):
                out.append((stem, node.name))
    return out


def _fn_node(mod, fn):
    tree = _mod_tree(mod)
    return next(n for n in tree.body
                if isinstance(n, ast.FunctionDef) and n.name == fn)


def branch_literals(fn_node):
    """String literals a dispatcher compares its FIRST token against — the
    branch-aware view of an EXEMPT positional dispatcher. Deliberately
    generation-1 only (args[0]/args.pop(0), or a name assigned DIRECTLY from
    one): derived values (provider = match.get(...)) are data comparisons,
    never argv branches."""
    bound = set()
    for n in ast.walk(fn_node):
        if isinstance(n, ast.Assign) and _token(n.value):
            bound.update(t.id for t in n.targets if isinstance(t, ast.Name))
    lits = set()
    for n in ast.walk(fn_node):
        if not (isinstance(n, ast.Compare) and _tok_or_bound(n.left, bound)):
            continue
        for c in n.comparators:
            if _is_str_const(c):
                lits.add(c.value)
            elif isinstance(c, (ast.Tuple, ast.List, ast.Set)) and _str_collection(c):
                lits.update(e.value for e in c.elts)
            elif isinstance(c, ast.Dict) and _str_collection(c):
                lits.update(k.value for k in c.keys)
    return lits


def verb_map():
    """cli.VERBS straight from the source: verb -> (module, function), both
    the direct entries and the _lazy(module, fn) ones."""
    tree = ast.parse((HELM_DIR / "cli.py").read_text())
    out = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "VERBS" for t in node.targets):
            for k, v in zip(node.value.keys, node.value.values):
                if isinstance(v, ast.Name):
                    out[k.value] = ("cli", v.id)
                elif isinstance(v, ast.Call):
                    out[k.value] = (v.args[0].value, v.args[1].value)
    return out


def reads_args(mod, fn):
    """Does the handler ever LOAD its args parameter? A handler that never
    does is a no-arg leaf: nothing below main() will look at the tail."""
    node = _fn_node(mod, fn)
    param = node.args.args[0].arg
    return any(isinstance(n, ast.Name) and n.id == param
               and isinstance(n.ctx, ast.Load) for n in ast.walk(node))


def _call(mod, fn, argv):
    f = getattr(importlib.import_module("helm." + mod), fn)
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = f(list(argv))
    return rc, out.getvalue(), err.getvalue()


# ------------------------------------------------------------------ sweep

class SweepTest(unittest.TestCase):
    def test_discovery_finds_the_fleet(self):
        # detector-health floor: the discoverer itself must not rot silent.
        self.assertGreaterEqual(len(discover()), 30, discover())

    def test_every_discovered_dispatcher_refuses_unknown_subverb(self):
        found = discover()
        for mod, fn in found:
            expected = EXEMPT.get((mod, fn), {"rc": 2})["rc"]
            rc, out, err = _call(mod, fn, [UNKNOWN])
            self.assertEqual(
                rc, expected,
                "%s.%s([%r]) -> rc %r (want %r)\nstdout: %s\nstderr: %s"
                % (mod, fn, UNKNOWN, rc, expected, out, err))
            if expected == 2:
                self.assertTrue(err, "%s.%s refusal must ride stderr" % (mod, fn))

    def test_exemptions_do_not_rot(self):
        found = set(discover())
        for key in EXEMPT:
            self.assertIn(key, found,
                          "EXEMPT entry %r no longer matches a discovered "
                          "dispatcher — delete or re-justify it" % (key,))

    def test_exempt_branches_declared_and_tail_guarded(self):
        # branch-aware exemption: every subverb literal an EXEMPT dispatcher
        # matches its first token against must be DECLARED with a probe, and
        # that probe (known branch + junk + --help) must refuse with exit 2
        # BEFORE the branch's side-effecting call. A new branch born inside
        # a positional dispatcher fails here until it is declared + guarded.
        for (mod, fn), spec in EXEMPT.items():
            self.assertEqual(
                branch_literals(_fn_node(mod, fn)), set(spec["branches"]),
                "%s.%s branch set drifted from EXEMPT — declare each subverb "
                "branch with a guarded-tail probe" % (mod, fn))
            for name, probe in spec["branches"].items():
                with mock.patch("helm.%s.%s" % (mod, probe["never"])) as p:
                    rc, out, err = _call(mod, fn, probe["argv"])
                self.assertEqual(rc, 2, (mod, fn, name, out, err))
                self.assertTrue(err, "%s.%s %s refusal must ride stderr" % (mod, fn, name))
                self.assertFalse(
                    p.called, "%s.%s ran %s despite junk %r"
                    % (mod, fn, probe["never"], probe["argv"]))


class NoArgRootLeaves(unittest.TestCase):
    """codex-2 HIGH: cli.main(['sync','--bogus','--help']) returned 0 AND ran
    registry.sync — a MUTATING verb ran on junk while --help falsely
    succeeded, and the dispatcher detector structurally cannot see it (the
    leaf never dispatches). Class closure: derive the no-arg leaves from the
    source, pin them to cli.NOARG_VERBS, and prove the ROOT guards their
    tails."""

    def _main(self, argv):
        from helm import cli
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.main(argv)
        return rc, out.getvalue(), err.getvalue()

    def test_noarg_leaves_equal_the_declared_set(self):
        from helm import cli
        noarg = {v for v, (m, f) in verb_map().items() if not reads_args(m, f)}
        self.assertTrue(noarg)  # detector-health floor
        self.assertEqual(
            noarg, set(cli.NOARG_VERBS),
            "no-arg VERBS leaves drifted from cli.NOARG_VERBS — a handler "
            "that never reads args gets NO guard below main(), so the root "
            "must guard its tail (or the handler must own its args)")

    def test_root_guards_every_noarg_leaf_tail(self):
        from helm import cli
        for verb in cli.NOARG_VERBS:
            for tail in (["--zz-bogus"], ["--zz-bogus", "--help"], ["zz-extra"]):
                rc, out, err = self._main([verb] + tail)
                self.assertEqual(rc, 2, (verb, tail, out, err))
                self.assertIn(tail[0], err, (verb, tail))

    def test_sync_junk_with_help_never_syncs(self):
        # the exact codex-2 probe, pinned: refuse BEFORE the mutating sync.
        with mock.patch("helm.registry.sync") as p:
            rc, out, err = self._main(["sync", "--bogus", "--help"])
        self.assertEqual(rc, 2, (out, err))
        self.assertFalse(p.called, "registry.sync ran on junk")
        self.assertIn("--bogus", err)

    def test_clean_help_on_noarg_leaf_still_helps(self):
        from helm import cli
        for verb in cli.NOARG_VERBS:
            rc, out, _ = self._main([verb, "--help"])
            self.assertEqual(rc, 0, verb)
            self.assertTrue(out.strip(), verb)


class SessionsResumeTailGuard(unittest.TestCase):
    """codex-2 HIGH, pinned exactly: the resume branch of the positional
    cmd_sessions dispatcher guards its tail BEFORE rows_for/spawn."""

    def _call(self, argv):
        with mock.patch("helm.sessions.rows_for") as rows, \
                mock.patch("helm.sessions.spawn_resume") as spawn:
            rc, out, err = _call("sessions", "cmd_sessions", argv)
        return rc, out, err, rows, spawn

    def test_junk_with_help_never_spawns(self):
        rc, out, err, rows, spawn = self._call(
            ["resume", "abcdef", "--go", "--bogus", "--help"])
        self.assertEqual(rc, 2, (out, err))
        self.assertIn("--bogus", err)
        self.assertFalse(rows.called, "rows_for ran despite junk")
        self.assertFalse(spawn.called, "spawn_resume ran despite junk")

    def test_missing_option_value_refuses_before_spawn(self):
        # --title/--note used to be parsed AFTER the pane was spawned; a
        # missing value crashed post-action. Now it refuses pre-action.
        for argv in (["resume", "abcdef", "--go", "--title"],
                     ["resume", "abcdef", "--go", "--note", "--title", "t"]):
            rc, out, err, rows, spawn = self._call(argv)
            self.assertEqual(rc, 2, (argv, out, err))
            self.assertIn("wants a value", err, argv)
            self.assertFalse(spawn.called, argv)

    def test_flag_shaped_prefix_refuses(self):
        rc, out, err, rows, spawn = self._call(["resume", "--go"])
        self.assertEqual(rc, 2, (out, err))
        self.assertFalse(rows.called)

    def test_clean_help_tail_helps_without_resolving(self):
        for argv in (["resume", "abcdef", "--help"], ["resume", "--help"]):
            rc, out, err, rows, spawn = self._call(argv)
            self.assertEqual(rc, 0, (argv, err))
            self.assertIn("resume <session-id-prefix>", out + err)
            self.assertFalse(rows.called, argv)
            self.assertFalse(spawn.called, argv)


class NearestMatchBelowRoot(unittest.TestCase):
    """codex-2 MED, pinned exactly: the root's did-you-mean is centralized
    (cli.suggest) and wired below root — guard_tail flags AND subdispatcher
    verbs. One typo probe per dispatcher, end to end through cli.main."""

    def test_typos_name_the_nearest_match(self):
        from helm import cli
        for argv, want in ((["skills", "syn"], "sync"),
                           (["creds", "crosschek"], "crosscheck"),
                           (["tidy", "--aply"], "--apply"),
                           (["watchdog", "--quite"], "--quiet")):
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = cli.main(argv)
            self.assertEqual(rc, 2, (argv, err.getvalue()))
            self.assertIn("did you mean '%s'?" % want, err.getvalue(), argv)


# ---------------------------------------------------- codex-2 FIX findings

class TrailingJunkRefusesBeforeWork(unittest.TestCase):
    """Known subverb + trailing junk (with or without --help) must exit 2
    BEFORE the side-effecting call fires."""

    def _refuse(self, mod, fn, argv, patched):
        with mock.patch("helm.%s.%s" % (mod, patched)) as p:
            rc, _, err = _call(mod, fn, argv)
        self.assertEqual(rc, 2, (argv, err))
        self.assertFalse(p.called, "%s.%s ran despite junk %r" % (mod, patched, argv))
        return err

    def test_seat_down_junk_never_stops_the_seat(self):
        for argv in (["down", "codex", "--bogus"],
                     ["down", "codex", "--bogus", "--help"]):
            err = self._refuse("seat", "cmd_seat", argv, "_down")
            self.assertIn("--bogus", err)

    def test_seat_up_launch_smoke_junk(self):
        self._refuse("seat", "cmd_seat", ["up", "codex", "--bogus"], "_up")
        self._refuse("seat", "cmd_seat", ["launch", "codex", "--bogus"],
                     "launch_line")
        self._refuse("seat", "cmd_seat", ["smoke", "codex", "--bogus"], "_smoke")

    def test_seat_resume_junk_never_relaunches(self):
        self._refuse("seat", "cmd_seat", ["resume", "codex", "--bogus"],
                     "_resume")

    def test_router_down_junk_never_stops_the_router(self):
        for argv in (["down", "--bogus"], ["down", "--bogus", "--help"]):
            self._refuse("modelrouter", "cmd_router", argv, "_down")

    def test_router_valued_flag_wants_a_value(self):
        rc, _, err = _call("modelrouter", "cmd_router", ["probes", "--dir"])
        self.assertEqual(rc, 2)
        self.assertIn("wants a value", err)

    def test_hooks_install_junk_never_installs(self):
        self._refuse("hooks", "cmd_hooks", ["install", "--bogus"],
                     "install_home")

    def test_hooks_sync_junk_never_syncs(self):
        self._refuse("envtidy", "cmd_hooks_sync", ["--bogus"], "hooks_sync")
        self._refuse("hooks", "cmd_hooks", ["sync", "--bogus"], "install_home")

    def test_mcp_sync_junk_never_syncs(self):
        self._refuse("envtidy", "cmd_mcp", ["sync", "--bogus"], "mcp_sync")

    def test_skills_sync_junk_never_syncs(self):
        self._refuse("skillsync", "cmd_sync", ["--bogus", "--apply"], "sync")

    def test_skills_dupes_junk_never_scans(self):
        for argv in (["dupes", "--bogus"], ["dupes", "--bogus", "--help"]):
            self._refuse("skills", "cmd_skills", argv, "census")

    def test_lineage_seed_junk_never_mutates(self):
        self._refuse("lineage", "cmd_lineage", ["seed", "--bogus"],
                     "apply_seed")

    def test_work_gc_junk_never_sweeps(self):
        with mock.patch("helm.work.find_root", return_value="/zz-nowhere"):
            self._refuse("work", "cmd_work", ["gc", "--bogus", "--apply"],
                         "gc_scan")

    def test_creds_crosscheck_junk_never_scans(self):
        self._refuse("localscan", "cmd_crosscheck", ["--bogus"], "crosscheck")

    def test_index_cap_junk_never_applies(self):
        self._refuse("store", "cmd_index", ["cap", "--bogus", "--apply"],
                     "index_cap")

    def test_corpus_status_junk(self):
        self._refuse("corpus", "cmd_corpus", ["status", "--bogus"], "scan")

    def test_codex_list_junk(self):
        self._refuse("codexhomes", "cmd_codex", ["list", "--bogus"],
                     "_print_list")

    def test_autocompact_junk_never_fires_even_with_help(self):
        for argv in (["frobnicate"], ["frobnicate", "--help"]):
            self._refuse("autocompact", "cmd_autocompact", argv, "check")

    def test_watchdog_junk_with_help_refuses(self):
        # codex-2 MED: help used to be checked before junk, so
        # `watchdog frobnicate --help` exited 0 — a false existence probe.
        self._refuse("watchdog", "cmd_watchdog", ["frobnicate", "--help"],
                     "check")

    def test_interview_junk(self):
        self._refuse("whoami", "cmd_interview", [UNKNOWN], "merge_scaffold")

    def test_sessions_unknown_flag_refuses(self):
        rc, _, err = _call("sessions", "cmd_sessions", ["--bogus"])
        self.assertEqual(rc, 2)
        self.assertIn("--bogus", err)


class TidyParserTest(unittest.TestCase):
    """codex-2 HIGH: `tidy --repo --apply --apply` APPLIED with
    root='--apply'. Flag-shaped/missing values and duplicates refuse."""

    def _refuse(self, argv, needle):
        with mock.patch("helm.envtidy.tidy") as p:
            rc, _, err = _call("envtidy", "cmd_tidy", argv)
        self.assertEqual(rc, 2, (argv, err))
        self.assertFalse(p.called)
        self.assertIn(needle, err)

    def test_flag_shaped_repo_value(self):
        self._refuse(["--repo", "--apply", "--apply"], "wants a value")

    def test_missing_repo_value(self):
        self._refuse(["--repo"], "wants a value")

    def test_duplicate_repo(self):
        self._refuse(["--repo", "a", "--repo", "b"], "duplicate --repo")


class CleanHelpStillHelps(unittest.TestCase):
    """The guard refuses junk, but a CLEAN --help tail prints usage, exit 0
    (and never runs the work)."""

    def _help(self, mod, fn, argv, patched):
        with mock.patch("helm.%s.%s" % (mod, patched)) as p:
            rc, out, _ = _call(mod, fn, argv)
        self.assertEqual(rc, 0, argv)
        self.assertTrue(out.strip(), argv)
        self.assertFalse(p.called)

    def test_clean_help_tails(self):
        self._help("skillsync", "cmd_sync", ["--help"], "sync")
        self._help("envtidy", "cmd_tidy", ["--help"], "tidy")
        self._help("watchdog", "cmd_watchdog", ["--help"], "check")
        self._help("seat", "cmd_seat", ["down", "codex", "--help"], "_down")
        self._help("modelrouter", "cmd_router", ["down", "--help"], "_down")


# --------------------------------------- fable review findings (2026-07-22)

class MembershipReadersRefuseJunk(unittest.TestCase):
    """The adversarial review's HIGH: handlers that read flags only via
    membership tests (`'--apply' in args`) are invisible to BOTH AST
    detectors and include MUTATING verbs — `helm promote --bogus --apply`
    WROTE 43 intake files with rc 0, and `--help` after junk exited 0."""

    def _refuse(self, mod, fn, argv, patched):
        with mock.patch("helm.%s.%s" % (mod, patched)) as p:
            rc, _, err = _call(mod, fn, argv)
        self.assertEqual(rc, 2, (argv, err))
        self.assertFalse(p.called,
                         "%s.%s ran despite junk %r" % (mod, patched, argv))
        return err

    def test_promote_junk_never_writes(self):
        for argv in (["--bogus", "--apply"], ["--bogus", "--help"]):
            err = self._refuse("drain", "cmd_promote", argv, "promote")
            self.assertIn("--bogus", err)

    def test_drain_junk_never_routes(self):
        for argv in (["--bogus", "--apply"], ["--bogus", "--help"]):
            self._refuse("drain", "cmd_drain", argv, "classify")

    def test_sweep_junk_never_sweeps(self):
        for argv in (["--frobnicate", "--help"], ["--bogus", "--apply"]):
            self._refuse("sweep", "cmd_sweep", argv, "propose")

    def test_coach_junk_never_plans(self):
        for argv in (["lesson", "--bogus", "--apply"],
                     ["lesson", "--bogus", "--help"]):
            err = self._refuse("coach", "cmd_coach", argv, "plan")
            self.assertIn("--bogus", err)

    def test_coach_single_dash_stays_lesson_text(self):
        r = {"layer": "prior", "id": "x", "statement": "s",
             "confidence": 0.5, "why": "w", "landing": "l",
             "near": [], "subsumes": []}
        with mock.patch("helm.coach.plan", return_value=r) as p:
            rc, _, _ = _call("coach", "cmd_coach", ["use", "-j", "--json"])
        self.assertEqual(rc, 0)
        self.assertIn("use -j", p.call_args[0][0])

    def test_clean_help_still_helps(self):
        for mod, fn, patched in (("drain", "cmd_promote", "promote"),
                                 ("drain", "cmd_drain", "classify"),
                                 ("sweep", "cmd_sweep", "propose"),
                                 ("coach", "cmd_coach", "plan")):
            with mock.patch("helm.%s.%s" % (mod, patched)) as p:
                rc, out, _ = _call(mod, fn, ["--help"])
            self.assertEqual(rc, 0, (mod, fn))
            self.assertTrue(out.strip(), (mod, fn))
            self.assertFalse(p.called)


# The mutation switch is the tell: any module-level cmd_* handler whose
# source mentions '--apply' but never calls guard_tail is a membership
# reader that CAN run mutating work on a junk tail. Each must either grow
# the guard or be declared here with its alternative guard probed — a new
# handler born into the class fails the sweep before it ships.
APPLY_READER_EXEMPT = {
    ("coach", "cmd_coach"):
        "free-text lesson tail — guard_tail would junk the lesson words; "
        "custom '--' junk guard, probed in MembershipReadersRefuseJunk",
    ("gc", "cmd_gc"):
        "closed-set refusal of its own (every non --dry/--apply token "
        "refuses); probed below",
    ("rearm", "cmd_rearm"):
        "closed-set refusal of its own — every token outside {--apply,--json} "
        "refuses rc 2 (probed below); the usage-line message is separately "
        "pinned by test_rearm.test_bad_flag_rejected",
    ("seats", "cmd"):
        "the `helm chat <verb>` dispatcher, cmd(verb, args); '--apply' lives "
        "only in the `seat gc` branch, which refuses any non-(--apply) token "
        "rc 2 before gc_roster (probed below)",
}


class ApplyReadersAreGuarded(unittest.TestCase):
    """Class closure for the membership-reader hole the two structural
    detectors above cannot see (not dispatchers, not no-arg leaves)."""

    def _readers(self):
        found = set()
        for stem, tree in _iter_mod_trees():
            for node in tree.body:
                if not (isinstance(node, ast.FunctionDef)
                        and node.name.startswith("cmd")):
                    continue
                consts = {n.value for n in ast.walk(node)
                          if isinstance(n, ast.Constant)
                          and isinstance(n.value, str)}
                if "--apply" in consts \
                        and "guard_tail" not in ast.unparse(node):
                    found.add((stem, node.name))
        return found

    def test_every_apply_reader_is_guarded_or_declared(self):
        self.assertEqual(self._readers(), set(APPLY_READER_EXEMPT))

    def test_gc_junk_refuses_before_scan(self):
        with mock.patch("helm.gc.scan") as p:
            rc, _, err = _call("gc", "cmd_gc", ["--bogus", "--apply"])
        self.assertEqual(rc, 2, err)
        self.assertFalse(p.called)

    def test_rearm_junk_refuses_before_apply(self):
        # main grew rearm since the merge-base — its closed-set check refuses
        # every token outside {--apply,--json} BEFORE apply() signals anyone.
        for argv in (["--bogus", "--apply"], ["--bogus"]):
            with mock.patch("helm.rearm.apply") as p:
                rc, _, err = _call("rearm", "cmd_rearm", argv)
            self.assertEqual(rc, 2, (argv, err))
            self.assertIn("usage: helm rearm", err)
            self.assertFalse(p.called, argv)

    def test_seats_seat_gc_junk_refuses_before_prune(self):
        # seats.cmd(verb, args) is the `helm chat` dispatcher; '--apply' lives
        # only in the seat-gc branch, which refuses non-(--apply) tokens before
        # gc_roster. Called directly (its signature is not the _call shape).
        from helm import seats
        for argv in (["gc", "--bogus"], ["gc", "--bogus", "--apply"]):
            out, err = io.StringIO(), io.StringIO()
            with mock.patch("helm.seats.gc_roster") as p, \
                    contextlib.redirect_stdout(out), \
                    contextlib.redirect_stderr(err):
                rc = seats.cmd("seat", argv)
            self.assertEqual(rc, 2, (argv, err.getvalue()))
            self.assertFalse(p.called, argv)


# The membership-reader hole GENERALIZED past the mutation switch: the class
# now covers MEMBERSHIP-READERS. A root-dispatched (VERBS) handler that reads
# its flags ONLY via membership (`'--x' in args` / `args.index('--x')`) is
# invisible to BOTH structural detectors above — it is not a subverb
# dispatcher (SweepTest sees args[0] vs literals) and it DOES read args so it
# is not a no-arg leaf (NoArgRootLeaves). Nothing between the root and it
# guards the tail, so an unknown flag rides straight through: `drift --peekk`
# ran the snapshot-WRITING path and exited 0; `brief/projections/who/attribute
# /evolve --bogus` all ran their work with the bogus flag pretending to exist.
# This detector closes that class BY CONSTRUCTION — the next flags-only reader
# born without guard_tail (or a declared alternative refusal) fails HERE
# before it ships. Scope is deliberately tight: VERBS handlers only (the root
# dispatches straight to them); '--apply' readers belong to
# ApplyReadersAreGuarded above; subverb dispatchers to SweepTest; positional
# subhandlers reached THROUGH a dispatcher are that dispatcher's branch job.
FLAG_READER_EXEMPT = {
    ("premise", "cmd_premise"):
        "free-text `<id> | <statement>` capture — guard_tail would junk the "
        "statement words; a tail lacking the required '|' 2-part structure "
        "refuses rc 2 (probed below)",
    ("inject", "cmd_inject"):
        "accepts inline non-dash prompt text for quick tests (guard_tail "
        "would reject it); a custom guard refuses any unknown --flag rc 2 "
        "while passing inline text + the hook-json path (probed below)",
}


def _flag_membership(node):
    """Flag literals the fn reads via `'-x' in args`, `'-x' not in args`, or
    `args.index('-x')` — a flag consumed by membership rather than positional
    dispatch. Both ast.In and ast.NotIn count: the ORIGINAL drift bug shape was
    `snapshot = "--peek" not in args` (NotIn), so matching only In let a reader
    reborn in that exact founding shape slip past the detector silently."""
    flags = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Compare) and len(n.ops) == 1 \
                and isinstance(n.ops[0], (ast.In, ast.NotIn)) \
                and _is_str_const(n.left) and n.left.value.startswith("-") \
                and any(isinstance(c, ast.Name) and c.id == "args"
                        for c in n.comparators):
            flags.add(n.left.value)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) \
                and n.func.attr == "index" \
                and isinstance(n.func.value, ast.Name) \
                and n.func.value.id == "args" and n.args \
                and _is_str_const(n.args[0]) \
                and n.args[0].value.startswith("-"):
            flags.add(n.args[0].value)
    return flags


class MembershipFlagReadersGuarded(unittest.TestCase):
    """Third structural closure, companion to SweepTest + NoArgRootLeaves +
    ApplyReadersAreGuarded: every VERBS handler that reads its flags only via
    membership (and is neither a subverb dispatcher nor a no-arg leaf) must
    call guard_tail or be declared in FLAG_READER_EXEMPT with a probed
    alternative refusal. A new flags-only reader born unguarded fails here."""

    def _readers(self):
        vm = verb_map()
        dispatchers = set(discover())
        found = set()
        for verb, (mod, fn) in vm.items():
            if (mod, fn) in dispatchers:
                continue                      # SweepTest owns dispatchers
            node = _fn_node(mod, fn)
            if not (node.name == "cmd" or node.name.startswith("cmd")):
                continue
            if not node.args.args or node.args.args[0].arg != "args":
                continue                      # single-`args` handlers only
            if not _flag_membership(node):
                continue                      # not a flag-membership reader
            if not reads_args(mod, fn):
                continue                      # no-arg leaf: NoArgRootLeaves' job
            src = ast.unparse(node)
            if "guard_tail" in src or "--apply" in src:
                continue                      # guarded, or ApplyReaders' job
            found.add((mod, fn))
        return found

    def test_every_flag_reader_is_guarded_or_declared(self):
        self.assertEqual(self._readers(), set(FLAG_READER_EXEMPT))

    def test_premise_junk_refuses(self):
        for argv in (["--bogus"], ["--bogus", "--help"]):
            rc, _, err = _call("premise", "cmd_premise", argv)
            self.assertEqual(rc, 2, (argv, err))

    def test_inject_unknown_flag_refuses(self):
        for argv in (["--bogus"], ["--bogus", "--help"]):
            with mock.patch("helm.inject.gather") as g:
                rc, _, err = _call("inject", "cmd_inject", argv)
            self.assertEqual(rc, 2, (argv, err))
            self.assertIn("--bogus", err)
            self.assertFalse(g.called, argv)

    def test_inject_inline_text_still_works(self):
        with mock.patch("helm.inject.gather", return_value={}) as g, \
                mock.patch("helm.inject.render", return_value=""):
            rc, _, _ = _call("inject", "cmd_inject", ["hello world"])
        self.assertEqual(rc, 0)
        self.assertTrue(g.called)

    def test_detector_catches_not_in_founding_shape(self):
        # PIN: the ORIGINAL drift drift-bug shape was `snapshot = "--peek" not
        # in args` (ast.NotIn). A reader reborn in that exact founding shape
        # must NOT slip past — _flag_membership has to surface its flag, so the
        # guard would FAIL (flag its module unguarded) rather than pass silent.
        src = ("def cmd_planted(args):\n"
               "    snapshot = \"--x\" not in args\n"
               "    return snapshot\n")
        node = ast.parse(src).body[0]
        self.assertIn("--x", _flag_membership(node),
                      "founding `not in args` shape escaped the detector")

    def test_guarded_readers_refuse_unknown_flag(self):
        # end-to-end proof the five named + evolve now refuse their junk tail.
        for mod, fn, work, argv in (
                ("drift", "cmd_drift", "report", ["--peekk"]),
                ("brief", "cmd_brief", "compose", ["--bogus"]),
                ("registry", "cmd_projections", "projection_survey", ["--bogus"]),
                ("who", "cmd_who", "scan", ["--bogus"]),
                ("attribute", "cmd_attribute", "gather", ["--bogus"]),
                ("evolve", "cmd_evolve", "cycle", ["--bogus"])):
            with mock.patch("helm.%s.%s" % (mod, work)) as p:
                rc, _, err = _call(mod, fn, argv)
            self.assertEqual(rc, 2, (mod, argv, err))
            self.assertIn(argv[0], err, (mod, argv))
            self.assertFalse(p.called, (mod, argv))

    def test_guarded_readers_clean_help_helps(self):
        for mod, fn, work in (("drift", "cmd_drift", "report"),
                              ("brief", "cmd_brief", "compose"),
                              ("registry", "cmd_projections", "projection_survey"),
                              ("who", "cmd_who", "scan"),
                              ("attribute", "cmd_attribute", "gather"),
                              ("evolve", "cmd_evolve", "cycle")):
            with mock.patch("helm.%s.%s" % (mod, work)) as p:
                rc, out, _ = _call(mod, fn, ["--help"])
            self.assertEqual(rc, 0, mod)
            self.assertTrue(out.strip(), mod)
            self.assertFalse(p.called, mod)


class HelpFirstTailStillGuarded(unittest.TestCase):
    """`sessions resume --help --bogus` used to print usage and exit 0 — a
    soft false existence probe for --bogus. Junk beats help even when help
    comes first. (The ROOT's `helm <verb> --help --bogus` deliberately keeps
    the short-circuit: the root cannot know a verb's flag surface, so
    refusing there would lie about real flags; no work runs on that path.)"""

    def test_resume_help_then_junk_refuses(self):
        with mock.patch("helm.sessions.spawn_resume") as p:
            rc, _, err = _call("sessions", "cmd_sessions",
                               ["resume", "--help", "--bogus"])
        self.assertEqual(rc, 2, err)
        self.assertIn("--bogus", err)
        self.assertFalse(p.called)

    def test_resume_help_first_clean_still_helps(self):
        rc, out, _ = _call("sessions", "cmd_sessions", ["resume", "--help"])
        self.assertEqual(rc, 0)
        self.assertIn("resume", out)


class SessionsLimitBounds(unittest.TestCase):
    """--limit -5 was accepted and silently truncated the listing (rows_for's
    `len(out) >= limit` break fires immediately) — a nonsense value refuses
    rc 2 exactly like a non-integer one."""

    def test_negative_and_zero_limit_refuse(self):
        for bad in ("-5", "0"):
            with mock.patch("helm.sessions.rows_for") as p:
                rc, _, err = _call("sessions", "cmd_sessions",
                                   ["--limit", bad])
            self.assertEqual(rc, 2, (bad, err))
            self.assertFalse(p.called)


class CodexLaunchJunkBeforeGate(unittest.TestCase):
    """Junk on `codex launch` refuses rc 2 BEFORE launch_gate — a failing
    pool gate (rc 1) used to mask the unknown flag entirely, printing gate
    diagnostics as if the flag existed."""

    def test_launch_junk_refuses_before_gate(self):
        with mock.patch("helm.codexhomes.launch_gate") as g:
            rc, _, err = _call("codexhomes", "cmd_codex",
                               ["launch", "--bogus"])
        self.assertEqual(rc, 2, err)
        self.assertIn("--bogus", err)
        self.assertFalse(g.called)


class SessionsBareHelpTail(unittest.TestCase):
    """`sessions --all --help` used to refuse rc 2 lying that --help is an
    unknown arg (the bare-list junk sweep caught -h/--help when it was not
    argv[0]). -h/--help on an otherwise-clean tail prints usage, rc 0; real
    junk alongside --help still refuses (the existence probe stays honest)."""

    def _call(self, argv):
        with mock.patch("helm.sessions.rows_for") as rows:
            rc, out, err = _call("sessions", "cmd_sessions", argv)
        return rc, out, err, rows

    def test_help_after_flag_helps(self):
        for argv in (["--all", "--help"], ["--limit", "5", "--help"], ["-h"]):
            rc, out, err, rows = self._call(argv)
            self.assertEqual(rc, 0, (argv, err))
            self.assertIn("sessions", out + err)
            self.assertFalse(rows.called, argv)

    def test_junk_beside_help_still_refuses(self):
        rc, out, err, rows = self._call(["--all", "--bogus", "--help"])
        self.assertEqual(rc, 2, (out, err))
        self.assertIn("--bogus", err)
        self.assertFalse(rows.called)


if __name__ == "__main__":
    unittest.main()
