"""Silent-dispatcher class closure — no dispatcher may be born silent.

Two layers:

1. SweepTest DISCOVERS every subverb dispatcher from the SOURCE (AST — a
   module-level cmd/cmd_* function whose body dispatches on the first argv
   token via comparisons against string literals/collections or dict lookups)
   and asserts each refuses an unknown subverb with exit 2. A future
   dispatcher with a silent default fall-through is discovered automatically
   and fails here before it ships. Positional-by-design entries live in
   EXEMPT with their pinned alternative behavior; an exemption that stops
   matching a discovered dispatcher fails (no rot).

2. Regression tests pin codex-2's FIX findings (helm-dogfood 2026-07-22):
   trailing junk after a KNOWN subverb must refuse BEFORE the side-effecting
   work runs — `seat down codex --bogus --help` stopped the seat and exited
   0 — and `--help` after junk refuses too (the existence probe stays
   honest), while a clean `--help` tail still helps.
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

# (module, function) -> (pinned rc for an unknown first token, why exempt).
# Exemptions are for dispatchers whose first token is a documented free
# positional, not a closed subverb set. Every key must still be DISCOVERED.
EXEMPT = {
    ("sessions", "cmd_sessions"): (
        0, "args[0] is the documented positional <project> filter; "
           "'resume' is the one subverb"),
    ("creds", "cmd_swap"): (
        1, "args[0] is a positional <home-or-account-email>; an unknown "
           "target refuses with rc 1 (not-found), never runs a default"),
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


def discover():
    """Every subverb dispatcher under helm/, straight from the source."""
    out = []
    for p in sorted(HELM_DIR.glob("*.py")):
        tree = ast.parse(p.read_text())
        mod_strcolls = _collect_strcolls(tree.body, set())
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) \
                    and (node.name == "cmd" or node.name.startswith("cmd_")) \
                    and _is_dispatcher(node, mod_strcolls):
                out.append((p.stem, node.name))
    return out


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
            expected = EXEMPT.get((mod, fn), (2, None))[0]
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


if __name__ == "__main__":
    unittest.main()
