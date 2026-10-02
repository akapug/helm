"""THE TREE-WIDE AUDITS, PRINTED AS A COMMAND INSTEAD OF TYPED FROM MEMORY.

A lane's focused run is chosen by what the change IMPORTS, by a planner or by
hand. The audits below import nothing they judge: each enumerates the package
or the tree and asserts a property of every module it finds, so no consumer
sweep over a change's symbols ever selects one, and a lane that adds a module,
a verb or a docstring reference learns about them at the whole-suite gate, the
slowest place in the tree to learn anything.

The cure for that is not a cleverer selector. It is to run the whole list
before every gate, and the list was being typed from memory, which is how an
audit gets left off it. `helm gate audits` prints the list as one command a
seat can paste, with the lane's own modules appended.

THE LIST IS THE ONE docs/MODULE_REGISTRIES.md ENUMERATES, plus `RUNG_ARMS`:
the arms that exercise the non-test rungs and the tree-wide audits no import
closure selects, plus `COMPOSITION_CHECKS`: the checks that refused another
lane's change at a train gate while no lane's audits ran them, plus
`TREE_READERS`: the tree readers `scan` finds that no other list names. A test
holds the enumerators and the page in step so neither can grow without the
other.

THE LIST AUDITS ITSELF. It is kept by hand, because `gate._tree_audits` reads
it from the tree it selects in by parsing literals and never running code. So
`tests/test_gateaudits_drift.py` holds it against `scan`, a static reading of
every test module that walks the tree from its own `__file__`: a reader no
list names, and no `EXEMPT` entry explains, is red; so is a listed module the
scan cannot see that `DECLARED` does not explain. The same module holds the
list's measured cost under `BUDGET_SHARE` of the whole suite's module-seconds
(`TIMINGS`, the file `helm gate slice-timings` prints), and every listed
module over `SLOW_SECONDS` to a reason in `SLOW`.

THE LINE RUNS AS SLICES WHEN THE SLICED PATH IS ENABLED (`mode`): the land
door admits a sliced receipt (`gate.sliced_land_refusal` answers None), the
tree ships the slice runner with its `--modules` scope, and the list is long
enough for slices to pay (`gate.focus_mode`). The land gate then runs as
slices too, so the audits run under the same runner and the same fail-mode
leak audit, on as many of a sliced gate's cores as the node's slots can
grant (`--slots fit`). Otherwise the line is the serial `python3 -m unittest`
it always was, and `--serial` asks for it by name. Either way stderr names
which runner the line starts and why.

`--wide` ADDS THE LANE'S OWN MODULES, READ FROM THE TREE (`lane_census`,
task/3674): the test modules the change touches, the tests of each helm
module it touches, the test modules that import a touched one, and every test
that names a touched path, each with the rule that selected it. They were
typed by hand before every gate, and auto-land's pre-gate audits run the same
list through the same function.
"""
import ast
import bisect
import os
import re
import sys

#: The test modules that enumerate the package (docs/MODULE_REGISTRIES.md,
#: "The twenty-two"), in that page's order.
ENUMERATORS = (
    "test_dispatch_honest", "test_display_launder_tripwire",
    "test_docstring_refs", "test_escape_hygiene", "test_foldcompose",
    "test_freetext", "test_handoff", "test_identity_layer",
    "test_instructions_are_runnable", "test_lease_recovery", "test_notify",
    "test_openflags", "test_orcaadopt", "test_readme_claims", "test_rearm",
    "test_reference_parity_pins", "test_roster_write_guard",
    "test_seat_facade_injection", "test_seats_split_contract",
    "test_session_start_closure", "test_surface_wiring", "test_vcs",
)
#: The arms on rungs whose enumeration lives outside tests/, the doc and
#: budget parities a new verb or a new hook line owes, and the scanner that
#: reads the PRODUCT tree for material that must never enter history: a lane
#: adding one fixture with an address-shaped string reddens it, and nothing
#: the lane imports would ever select it.
#: `test_env_hygiene` and `test_scratch` joined for task/3039: outside the
#: import closure and this list, they were the failing module in 5 and 4 of
#: the week's reds, and a focused plan for a change with no import graph (a
#: doc, a script) runs this whole list, so it must carry them.
#: `test_raw_module_reads` joined with the recorded-loads selection
#: (task/3039 lane 2): it refuses, anywhere in the tree, the one read the
#: load recorder cannot see, and like every audit here it imports nothing it
#: judges, so the static closure never selects it.
#: `test_elapsed_ceilings` refuses, anywhere under tests/, an assertion that
#: holds elapsed wall time under a ceiling and is not on its allowlist. A
#: change that writes such a ceiling edits a test module and imports nothing
#: of this audit, so no import closure selects it; only this list runs it
#: before a gate.
#: `test_gateaudits_drift` holds this list against `scan` and its budget: a
#: lane adding a test module that walks the tree imports nothing of it, so
#: only this list runs it before a gate.
RUNG_ARMS = ("test_wiring", "test_registry", "test_verb_sweep",
             "test_verbs_doc_parity", "test_hook_budgets",
             "test_seat_split_contract", "test_never_track",
             "test_env_hygiene", "test_scratch", "test_raw_module_reads",
             "test_elapsed_ceilings", "test_gateaudits_drift")
#: The checks a lane-on-lane composition made relevant but the standing audits
#: did not guarantee. Through train277, 11 of 23 red train gates were
#: composition-only. The first six below caused 6 of them; the other five were
#: test_scratch (train151-153), test_instructions_are_runnable (train157)
#: and test_env_hygiene (train196), which the list carries already. Train282
#: then exposed the seventh check below while a transient lane extra carried
#: it; its function-local import leaves the next family lane uncovered. Each
#: reads a surface other lanes write, not only its own fixtures:
#:   test_world_literals    every shipped file (git ls-files): train170
#:                          helm/repofacts.py, train257 helm/trailer_rung.py
#:                          (cures 32d70307fb9, 09a39acd7d7)
#:   test_assertion_hygiene every module under tests/: train173
#:                          tests/test_local_names.py
#:   test_no_private_names  every file under helm/ (git ls-files): train173
#:                          helm/actsteer.py, train181 helm/chatnode.py
#:                          (a3cf94232fa), train252's compose (9ccdf3655a9)
#:   test_delivery_truth    every post/dm sender label under helm/: train178
#:                          helm/chatnode.py (4842e2ed67d), train252's
#:                          compose (9ccdf3655a9)
#:   test_chat_reply        the doc contracts docs/VERBS.md and docs/WEB.md,
#:                          which verb lanes edit: train257 (09a39acd7d7)
#:   test_trailer_rung      every name the family catalog holds, which family
#:                          lanes edit: train261 (e1f27b09ede)
#:   test_burnflags         local certification reads the family catalog
#:                          through a function-local import the static closure
#:                          cannot see: train282 (1188398218c)
#:   test_change_notes      the lines under CHANGELOG.md's '## Unreleased',
#:                          which every lane wrote until change notes moved to
#:                          changes/: train512 and train514 each dropped two
#:                          cars that conflicted on that one hunk
#: Listing a check catches a lane whose own tree already carries it. When the
#: check and the violation arrive in different lanes, neither lane carries
#: both, and only the audits run on the COMPOSED tree catch it. The measured
#: examples include train276's elapsed ceiling and train282's burnflags clash.
COMPOSITION_CHECKS = ("test_world_literals", "test_assertion_hygiene",
                      "test_no_private_names", "test_delivery_truth",
                      "test_chat_reply", "test_trailer_rung",
                      "test_burnflags", "test_change_notes")
#: The tree readers `scan` finds that no list above names for a reason of its
#: own. A lane adding a test module that walks the tree adds it here, or to
#: `EXEMPT` with the reason no other lane's change can redden it. The last
#: three are the walking arms of test_gateshard (41 s), test_seats (33 s) and
#: test_hook_wrapper (10 s), each moved into a module of its own so the list
#: runs the walk without the rest of its module. test_timerhealth rides whole:
#: its walk finds every module that renders a [Timer], and its other unit-drift
#: arms render all of those modules' templates, so a lane that adds a timer or
#: an input to one reddens it without touching it (task/3405).
#: test_landing_protocol walks every module under helm/ for an instruction to
#: rebase reviewed work, so any lane adding such a sentence reddens it without
#: importing it (task/4050).
TREE_READERS = ("test_autocompact", "test_codexresets",
                "test_lock_fails_closed",
                "test_identity_path_env", "test_nonpane_session",
                "test_ownerdecisions", "test_seat_identity_cli",
                "test_socket_paths", "test_web_common",
                "test_web_split_contract", "test_stored_suite_runners",
                "test_seats_catchup_promise", "test_site_stage_precondition",
                "test_module_patch_resets",
                "test_goals", "test_no_seat_attribution",
                "test_systemctl_timeouts", "test_timerhealth",
                "test_verb_help_files", "test_landing_protocol")
AUDITS = ENUMERATORS + RUNG_ARMS + COMPOSITION_CHECKS + TREE_READERS

#: LISTED, AND INVISIBLE TO `scan`: each reads the tree through a helm table,
#: a file it names or a call the scan does not follow, never through a walk
#: rooted at its own `__file__`. The drift arm refuses a listed module the
#: scan cannot see unless it is here, so a module that stops reading the tree
#: leaves the list instead of costing every lane its runtime.
DECLARED = {
    "test_lease_recovery": "lists helm/seats*.py through a class attribute "
                           "(self.REPO) and reads docs/VERBS.md and "
                           "docs/ORCA_SEAM_AUDIT.md by name",
    "test_seat_facade_injection": "reads the seat family's sources through "
                                  "seat_compat.IMPL_MODULES and helm/seat.py "
                                  "and helm/web.py by name",
    "test_wiring": "drives helm/wiring.py, whose census walks the package",
    "test_verb_sweep": "runs every verb cli.VERBS registers",
    "test_verbs_doc_parity": "holds cli.VERBS against docs/VERBS.md",
    "test_hook_budgets": "holds every hook event's render to its budget and "
                         "reads the docs those renders cite by name",
    "test_seat_split_contract": "holds dir(seat) and the facade's re-exports "
                                "to their frozen inventories",
    "test_chat_reply": "reads docs/VERBS.md and docs/WEB.md by name",
    "test_trailer_rung": "reads every family name the catalogs hold",
    "test_burnflags": "reads the family catalog through a function-local "
                      "import",
    "test_gateaudits_drift": "walks tests/ through `scan` here, a call the "
                             "scan does not follow",
}
#: FOUND BY `scan` AND NOT LISTED, each with the reason no other lane's change
#: can redden it (it reads only its own fixtures, or re-derives both sides of
#: its comparison from the same walk).
EXEMPT = {}

#: THE BUDGET. The listed modules' measured seconds stay at or under this
#: share of the whole suite's module-seconds, and a listed module measured
#: over `SLOW_SECONDS` carries a reason in `SLOW`. `TIMINGS` is the verbatim
#: output of `helm gate slice-timings` (the newest sliced whole suite's
#: per-module seconds), committed so every host judges the same numbers.
#: After a sliced whole suite has run the modules the list gained, refresh it:
#:     helm gate slice-timings > tests/fixtures/slice-timings.json
BUDGET_SHARE = 0.20
SLOW_SECONDS = 15.0
TIMINGS = "tests/fixtures/slice-timings.json"
#: LISTED AND MEASURED OVER `SLOW_SECONDS`, each with why the whole module
#: rides the list. "The walk is the cost" means its tree-reading arms are most
#: of its time (per-test durations of a serial fab run), so moving them into a
#: module of their own would save almost nothing.
SLOW = {
    "test_orcaadopt": "the walk is the cost: its send-site and census-consumer "
                      "censuses are 48 of its 51 s of test time",
    "test_vcs": "the walk is the cost: its direct-spawn audit and call-site "
                "selection sweeps are 37 of its 51 s of test time",
    "test_surface_wiring": "the walk is the cost: every arm over 1 s derives "
                           "a dispatcher's accepted subverbs or flags from "
                           "the live tree's AST",
    "test_env_hygiene": "the walk is the cost: one census that parses every "
                        "tests/*.py is 28 s of its test time",
    "test_escape_hygiene": "the walk is the cost: two arms that read every "
                           "literal and top-level name under helm/, tests/ "
                           "and bin/ are 16 s",
    "test_identity_layer": "the walk is the cost: its uses-closure census is "
                           "22 of its 23 s of test time",
    "test_wiring": "the walk is the cost: one live actuator census of the "
                   "package is 25 of its 26 s of test time",
}

USAGE = ("usage: helm gate audits [--repo PATH] [--json] [--serial] "
         "[--wide [--base REF] [--tip REF]] [-- <test module> ...]\n"
         "  Prints ONE pasteable `fab test` command running every tree-wide "
         "audit, with any test modules named after `--` appended: as slices "
         "of helm/gateslice.py when the sliced path is enabled, else one "
         "serial unittest process (--serial forces it). Run it on the "
         "COMPOSED tree before a whole-suite gate. It runs nothing itself.\n"
         "  --wide adds the lane census of the change from the merge-base of "
         "--base (default: the trunk ref) to --tip (default: HEAD), read from "
         "the tip's committed tree: each touched test module, "
         "tests/test_<stem>*.py for each touched helm/<stem>.py or "
         "helm/<stem>/..., the test modules importing a touched one, and "
         "every test naming a touched path. stderr says how many; --json "
         "carries base, tip, modules and each module's reason.")
SLICED, SERIAL = "sliced", "serial"
# What the sliced line sets for its runner: the land gate's fail-mode leak
# audit, and a whole-node share, because the Fab grant (not a host cap) is
# what bounds its workers there.
SLICED_ENV = ("HELM_GATESLICE_LEAKS=fail", "HELM_GATE_SUITE_CAP=1")


def modules(extra=()):
    """The dotted module list: every audit, then `extra` in the caller's
    order, each named once. `extra` accepts `tests.test_x`, `test_x` or a
    path to the file."""
    out = ["tests." + name for name in AUDITS]
    for raw in extra:
        name = os.path.basename(str(raw))
        name = name[:-3] if name.endswith(".py") else name
        name = name.split(".")[-1] if name.startswith("tests.") else name
        dotted = "tests." + name
        if dotted not in out:
            out.append(dotted)
    return out


def missing(repo):
    """The listed audits with no file under `repo`/tests. A name this list
    carries that the tree does not is a list that has gone stale, and a
    command naming it would fail on import instead of auditing anything."""
    return [name for name in AUDITS
            if not os.path.isfile(os.path.join(repo, "tests", name + ".py"))]


def _runner_missing(repo):
    """Why `repo`'s tree cannot run a module list as slices, or None."""
    from . import gate, gateslice
    gone = [rel for rel in gate.SLICE_RUNNER_FILES
            if not os.path.isfile(os.path.join(repo, *rel.split("/")))]
    if gone:
        return "the tree does not ship the slice runner (%s missing)" \
            % ", ".join(gone)
    try:
        with open(os.path.join(repo, *gate.SLICE_RUNNER.split("/")),
                  encoding="utf-8") as fh:
            text = fh.read()
    except (OSError, UnicodeDecodeError) as exc:
        return "the tree's slice runner is unreadable (%s)" % exc
    if 'MODULES_FLAG = "%s"' % gateslice.MODULES_FLAG not in text:
        return ("the tree's slice runner predates its %s scope"
                % gateslice.MODULES_FLAG)
    return None


def mode(repo, extra=(), serial=False):
    """(SLICED or SERIAL, the one sentence that decided it)."""
    if serial:
        return SERIAL, "--serial asked for one unittest process"
    from . import gate
    why = gate.sliced_land_refusal()
    if why:
        return SERIAL, ("the sliced path is not enabled, so the land gate "
                        "runs serial and so do its audits: %s" % why)
    why = _runner_missing(repo)
    if why:
        return SERIAL, why
    names = modules(extra)
    decided, reason, _workers = gate.focus_mode({"selected": names},
                                                where="plan")
    if decided != gate.SLICED:
        return SERIAL, reason
    return SLICED, ("the sliced path is enabled: %d modules run as slices of "
                    "%s, with the land gate's fail-mode leak audit"
                    % (len(names), gate.SLICE_RUNNER))


def command(repo, extra=(), runner=SERIAL):
    names = " ".join(modules(extra))
    if runner == SLICED:
        from . import gate, gateslice
        return ("fab test --slots fit --cores %d --repo %s -- env %s python3 "
                "%s %s %s" % (gate.SLICE_SLOTS * gate._CORES_PER_SUITE, repo,
                              " ".join(SLICED_ENV), gate.SLICE_RUNNER,
                              gateslice.MODULES_FLAG, names))
    return "fab test --repo %s -- python3 -m unittest %s" % (repo, names)


# ----------------------------------------------------------- THE LANE CENSUS
#
# The audits import nothing they judge. A lane's own change is judged by the
# modules that DO reach it, and before every gate a seat chose those by hand:
# a room, a census typed from memory, then the fab line. `lane_census` reads
# them from the tip's committed tree with no room, in the two git calls
# `gate._tree_test_texts` makes, and says for each module which rule chose
# it. `helm gate audits --wide` prints what it answers, and auto-land's
# pre-gate audits run the same list (`autoland.Ops.audits`).

#: The reason every module carries. A lead module is an AUDIT or a PRE_GATE
#: audit; a census module carries the first of TOUCHED, STEM, IMPORTS and
#: NAMES that selected it, then the path or module that did.
AUDIT, PRE_GATE = "audit", "pre-gate audit"
TOUCHED, STEM, IMPORTS, NAMES = "touched", "stem of", "imports", "names"

#: An import statement, alone on its line or indented in a function body.
#: A parenthesised name list may span lines, and a backslash carries the
#: statement onto the next line wherever it stands: in the name list, before
#: `import` or after it (`_GAP` between words, `_REST` to the statement end).
_GAP, _REST = r"(?:[ \t]|\\\r?\n)", r"(?:[^\\\n]|\\\r?\n|\\)*"
_IMPORT = re.compile(
    r"^[ \t]*(?:from{g}+[.\w]+{g}+import{g}*(?:\([^)]*\)|{r})"
    r"|import{g}+{r})".format(g=_GAP, r=_REST), re.M)
#: A unittest failure heading, the serial runner's and the slice runner's.
_FAILED = re.compile(r"^(?:FAIL|ERROR): (.*)$", re.M)
_TEST_NAME = re.compile(r"\btests\.test\w*")


class _Scrubbed(object):
    """A git backend whose every call runs under the authority overlay
    (`vcs._authority_env`): an ambient GIT_DIR beats `git -C <repo>`, and
    the gate's tree readers take a backend and pass no env of their own."""

    def __init__(self, git):
        from . import vcs
        self.git, self.env = git, vcs._authority_env()

    def text(self, cwd, *args, **kw):
        kw.setdefault("env", self.env)
        return self.git.text(cwd, *args, **kw)

    def run(self, cwd, *args, **kw):
        kw.setdefault("env", self.env)
        return self.git.run(cwd, *args, **kw)


def _imports(text):
    """Every dotted name one test module's import statements name, the
    function-local ones too, a relative import resolved in the tests
    package: `from tests import test_x` and `from tests.test_x import Base`
    both name tests.test_x. A statement is read with every line a
    backslash continues (`_IMPORT`). One that does not parse alone (prose
    that starts with the word, a comment ending in a backslash) names every
    dotted word in it, which can only select more."""
    out = set()
    for found in _IMPORT.finditer(text):
        stmt, heads = found.group(0).strip(), []
        try:
            body = ast.parse(stmt).body
        except (SyntaxError, ValueError):
            body, heads = [], re.findall(r"[\w.]+", stmt)
        for node in body:
            if isinstance(node, ast.Import):
                heads += [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                base = (["tests"] if node.level == 1 else []) + (
                    node.module.split(".") if node.module else [])
                heads += [".".join(base + [a.name]) for a in node.names]
                heads += [".".join(base)] if base else []
        # A bare `from test_x import` runs with tests/ on the path.
        out.update(heads)
        out.update("tests." + h for h in heads)
    return out


def _importers(touched, tests, texts):
    """{test module: the module whose import put it on the list}: the test
    modules that import a touched module under tests/, then those that
    import one of them, until none is new. Only import statements count, so
    no module that consumes everything (`gate._scope_selection`) seeds it."""
    from . import gate
    seeds = sorted({gate._module_name(p) for p in touched
                    if p.startswith("tests/")} - {None})
    found, names, queue, seen = {}, {}, list(seeds), set(seeds)
    while queue:
        seed = queue.pop(0)
        leaf = seed.rsplit(".", 1)[-1]
        for m in tests:
            if m in seen or leaf not in texts[m]:
                continue
            if m not in names:
                names[m] = _imports(texts[m])
            if seed in names[m]:
                found[m] = seed
                seen.add(m)
                queue.append(m)
    return found


def lane_census(repo, base=None, tip="HEAD"):
    """THE LANE'S LIST, read from `tip`'s committed tree with no room:
    -> (census, None) or (None, why). The change is every path that differs
    from the one merge-base of `base` (default: the trunk ref) and `tip`, to
    `tip`.

    The list is every audit, then `autoland.PRE_GATE_AUDITS`, then each test
    module at the tip that a rule selects, in name order:
      touched  its own file is in the change;
      stem of  it is tests/test_<stem>*.py for a changed helm/<stem>.py or
               any changed file under helm/<stem>/ (helm/web_ui/ ships
               only parts, and a part change reddens its tests);
      imports  an import statement in it names a changed module under
               tests/, or a module this rule already selected (`_importers`);
      names    its source names a changed path (`gateloads.names_path`).
    census = {"base", "tip", "merge_base", "touched", "modules", "extra",
    "reasons"}: `modules` is the list to run, `extra` its census part and
    `reasons` one sentence per module ("names helm/harness.py").

    NEVER SHORTER THAN ITS RULES: a base or tip that does not resolve, an
    ambiguous merge-base, a module-shaped symlink or a tree git cannot read
    is refused with its reason, and nothing runs in its place."""
    from . import gate, gateloads, vcs
    from .autoland import PRE_GATE_AUDITS
    raw = vcs.backend(repo)
    git = _Scrubbed(raw)
    base = base or raw.trunk_ref(repo)
    shas = []
    for label, ref in (("base", base), ("tip", tip)):
        rc, out, err = git.text(repo, "rev-parse", "--verify", "-q",
                                "%s^{commit}" % ref)
        if rc != 0 or not out:
            return None, "the %s %s is not a commit in %s%s" % (
                label, ref, repo, (" (%s)" % err) if err else "")
        shas.append(out.lower())
    mb, why = gate._single_merge_base(git, repo, shas[0], shas[1], base)
    if why:
        return None, why
    changed, _structural, why = gate._changes_since(repo, git, mb, shas[1])
    if why:
        return None, why
    texts, why = gate._tree_test_texts(repo, git, shas[1])
    if why:
        return None, why
    touched = sorted(changed)
    tests = sorted(m for m in texts if gate._is_test_module(m))
    found = {}
    for p in touched:
        mod = gate._module_name(p)
        if mod in texts and gate._is_test_module(mod):
            found.setdefault(mod, "%s %s" % (TOUCHED, p))
    for p in touched:
        head, sub, _rest = p[len("helm/"):].partition("/") \
            if p.startswith("helm/") else ("", "", "")
        stem = head if sub else head[:-3] if head.endswith(".py") \
            else ""
        for m in tests if stem else ():
            if m.startswith("tests.test_" + stem):
                found.setdefault(m, "%s %s" % (STEM, p))
    for m, seed in _importers(touched, tests, texts).items():
        found.setdefault(m, "%s %s" % (IMPORTS, seed))
    leaves = {p: p.rsplit("/", 1)[-1] for p in touched}
    for m in tests:
        # A path's last component is in every text that names the path, so
        # one scan for it rules out the three `names_path` makes.
        hit = None if m in found else next(
            (p for p in touched if leaves[p] in texts[m]
             and gateloads.names_path(texts[m], p)), None)
        if hit:
            found[m] = "%s %s" % (NAMES, hit)
    lead = modules(PRE_GATE_AUDITS)
    extra = [m for m in sorted(found) if m not in lead]
    reasons = {m: AUDIT if m[len("tests."):] in AUDITS else PRE_GATE
               for m in lead}
    reasons.update((m, found[m]) for m in extra)
    return {"base": shas[0], "tip": shas[1], "merge_base": mb,
            "touched": touched, "modules": lead + extra, "extra": extra,
            "reasons": reasons}, None


def lane_modules(repo, base=None, tip="HEAD"):
    """(modules, reasons, err): `lane_census`'s list and one reason per
    module, or (None, None, why) when the census cannot be read."""
    census, why = lane_census(repo, base, tip)
    if why:
        return None, None, why
    return census["modules"], census["reasons"], None


def failing(text, names):
    """The modules of `names` a unittest run's FAIL and ERROR headings name,
    in the run's order: `FAIL: test_y (tests.test_x.T.test_y)`, a module
    that would not import (`ERROR: tests.test_x (unittest.loader.
    _FailedTest.tests.test_x)`) and a class fixture's `ERROR: setUpClass
    (tests.test_x.T)` all name tests.test_x."""
    listed, out = set(names), []
    for head in _FAILED.findall(text or ""):
        hit = next((n for n in _TEST_NAME.findall(head) if n in listed),
                   None)
        if hit and hit not in out:
            out.append(hit)
    return out


# ------------------------------------------------------------ THE DRIFT SCAN
#
# A TREE READER, statically: a test module that calls walk, listdir, scandir,
# glob, iglob, rglob or iterdir on a root derived from `__file__` (never
# `ast.walk`), or runs `git ls-files` / `git grep` with no cwd or a derived
# one and no single-file pathspec. A name is DERIVED in a scope when that
# scope binds it (an assignment, a loop or `with` target, a parameter
# default) from an expression naming `__file__` or a derived name; a function
# sees its enclosing scope's derived names unless it rebinds them. What it
# cannot see is a read through a helm API, a class attribute or a named file:
# `DECLARED` says so for each listed module that reads the tree that way.

_WALKS = frozenset(("walk", "listdir", "scandir", "glob", "iglob", "rglob",
                    "iterdir"))
_RECEIVER_WALKS = frozenset(("glob", "rglob", "iterdir"))
_GIT_ENUM = frozenset(("ls-files", "grep"))
_FUNCS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
#: THE PREFILTER, which only ever keeps more than the parse would find: a
#: walk needs a walk-shaped call and a `__file__` outside the line every test
#: module opens with, and a git read needs its subcommand as a whole string
#: literal. Parsing is the scan's whole cost, so a module failing both is
#: never parsed, and inside a parsed one a function whose lines hold no
#: walk-shaped call and no git word is never walked (`_hot`).
_WALK_CALL = re.compile(r"\b(?:%s)\s*\(" % "|".join(sorted(_WALKS)))
_GIT_WORD = re.compile(r"""['"](?:ls-files|grep)['"]""")
#: `sys.path.insert(0, <a call chain over __file__>)`, alone on its line. The
#: call returns None, so nothing derives from it: `_refs` never counts a
#: `__file__` inside any `<x>.path.insert(...)`, which is what lets the
#: prefilter skip a module whose only `__file__` is on such a line.
_PATH_IDIOM = re.compile(r"[ \t]*_?sys\.path\.insert\(0,\s*(?:[\w.]+\()+"
                         r"__file__\)+[ \t]*(?:#.*)?")


def _idiom(line):
    return bool(_PATH_IDIOM.fullmatch(line)) \
        and line.count("(") == line.count(")")


def _hot(text):
    """The line numbers a tree read could start on, or None when the text
    can hold none."""
    git = bool(_GIT_WORD.search(text)) and ("'git'" in text
                                            or '"git"' in text)
    walk = "__file__" in text and any(
        not _idiom(line) for line in text.splitlines() if "__file__" in line)
    lines = set()
    for pattern, on in ((_WALK_CALL, walk), (_GIT_WORD, git)):
        line, at = 1, 0
        for m in pattern.finditer(text) if on else ():
            line += text.count("\n", at, m.start())
            at = m.start()
            lines.add(line)
    return sorted(lines) or None


def _refs(node):
    """(names an expression reads, whether it names `__file__`), never
    looking inside a `<x>.path.insert(...)`, which returns None."""
    ids, rooted, stack = set(), False, [node]
    while stack:
        n = stack.pop()
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) \
                and n.func.attr == "insert" \
                and isinstance(n.func.value, ast.Attribute) \
                and n.func.value.attr == "path":
            continue
        if isinstance(n, ast.Name):
            ids.add(n.id)
            rooted = rooted or n.id == "__file__"
        elif isinstance(n, ast.Attribute) and n.attr == "__file__":
            rooted = True
        stack.extend(ast.iter_child_nodes(n))
    return ids, rooted


def _bound(targets):
    return {n.id for t in targets for n in ast.walk(t)
            if isinstance(n, ast.Name)}


def _defaults(args):
    """A function's parameter defaults as edges: a parameter defaulting to a
    derived expression is derived inside the function."""
    pos = args.posonlyargs + args.args
    tail = pos[len(pos) - len(args.defaults):]
    return [(d, {a.arg}) for a, d in zip(tail, args.defaults)] + [
        (d, {a.arg}) for a, d in zip(args.kwonlyargs, args.kw_defaults) if d]


def _elements(node):
    """An argv's literal pieces: a list or tuple, or a sum of them
    (`["git", "grep"] + args`)."""
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _elements(node.left) + _elements(node.right)
    if isinstance(node, (ast.Tuple, ast.List)):
        return list(node.elts)
    return [node]


def _call_reads(node):
    """How `node` might read the tree: [(label, parts, git)] where `parts`
    are the expressions that must be derived for it to count, and `git`
    says an ABSENT cwd counts too."""
    out = []
    fn = node.func
    name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
    if name in _WALKS and not (isinstance(fn, ast.Attribute)
                               and isinstance(fn.value, ast.Name)
                               and fn.value.id in ("ast", "_ast")):
        parts = list(node.args) + [k.value for k in node.keywords]
        if isinstance(fn, ast.Attribute) and name in _RECEIVER_WALKS:
            parts.append(fn.value)
        out.append(("%s:%d" % (name, node.lineno), parts, False))
    seq = [e.value for e in _elements(node.args[0] if node.args else None)
           if isinstance(e, ast.Constant) and isinstance(e.value, str)]
    words = set(seq) & _GIT_ENUM
    if "git" in seq and words and "--error-unmatch" not in seq:
        named = "ls-files" in seq and any(
            "." in x.rsplit("/", 1)[-1] and not x.startswith("-")
            for x in seq[seq.index("ls-files") + 1:])
        if not named:
            cwd = [k.value for k in node.keywords if k.arg == "cwd"]
            out.append(("git-%s:%d" % (sorted(words)[0], node.lineno), cwd,
                        not cwd))
    return out


def reads(text):
    """The tree reads `text` makes, as `call:line` labels, [] for none."""
    hot = _hot(text)
    if not hot:
        return []
    parent, edges, calls = [None], [[]], [[]]
    stack = [(child, 0) for child in ast.iter_child_nodes(ast.parse(text))]
    while stack:
        node, s = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            # A function holding no hot line holds no read, and its bindings
            # reach only its own body: nothing in it can matter.
            first = min([d.lineno for d in node.decorator_list]
                        + [node.lineno])
            at = bisect.bisect_left(hot, first)
            if at == len(hot) or hot[at] > node.end_lineno:
                continue
        if isinstance(node, _FUNCS):
            parent.append(s)
            edges.append(_defaults(node.args))
            calls.append([])
            s = len(parent) - 1
        elif isinstance(node, ast.Assign):
            edges[s].append((node.value, _bound(node.targets)))
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            edges[s].append((node.value, _bound([node.target])))
        elif isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)):
            edges[s].append((node.iter, _bound([node.target])))
        elif isinstance(node, ast.withitem) and node.optional_vars:
            edges[s].append((node.context_expr, _bound([node.optional_vars])))
        elif isinstance(node, ast.NamedExpr):
            edges[s].append((node.value, {node.target.id}))
        elif isinstance(node, ast.Call):
            calls[s].extend(_call_reads(node))
        stack.extend((child, s) for child in ast.iter_child_nodes(node))
    # Derive only along the scope chains that hold a candidate call: a
    # parent is always created before its children, so its set is ready.
    needed = set()
    for s, found in enumerate(calls):
        while found and s is not None and s not in needed:
            needed.add(s)
            s = parent[s]
    derived, out = {}, []
    for s in sorted(needed):
        es = [(_refs(expr), names) for expr, names in edges[s]]
        have = set(derived.get(parent[s], ())) - set().union(
            *(names for _r, names in es))
        grew = True
        while grew:
            grew = False
            for (ids, rooted), names in es:
                if (rooted or ids & have) and not names <= have:
                    have |= names
                    grew = True
        derived[s] = have
        for label, parts, bare in calls[s]:
            if bare or any(r or ids & have for ids, r in map(_refs, parts)):
                out.append(label)
    return sorted(out, key=lambda x: (int(x.rsplit(":", 1)[1]), x))


def scan(tests_dir):
    """{module: [call:line, ...]} for every top-level `test*.py` under
    `tests_dir` that reads the tree."""
    out = {}
    for name in sorted(os.listdir(tests_dir)):
        if name.startswith("test") and name.endswith(".py"):
            with open(os.path.join(tests_dir, name), encoding="utf-8") as fh:
                found = reads(fh.read())
            if found:
                out[name[:-3]] = found
    return out


def drift(found, audits=None, declared=None, exempt=None):
    """Every way the list and a `scan` disagree, one sentence each; [] when
    they agree."""
    audits = set(AUDITS if audits is None else audits)
    declared = DECLARED if declared is None else declared
    exempt = EXEMPT if exempt is None else exempt
    out = []
    for name in sorted(set(found) - audits - set(exempt)):
        out.append("UNLISTED %s reads the tree (%s) and no list names it: add "
                   "it to TREE_READERS, or to EXEMPT with the reason no other "
                   "lane's change can redden it"
                   % (name, ", ".join(found[name])))
    for name in sorted(audits - set(found) - set(declared)):
        out.append("UNSEEN %s is listed, the scan finds no tree read in it "
                   "and DECLARED does not say what it reads" % name)
    for name in sorted(set(declared) - audits):
        out.append("DECLARED names %s, which no list carries" % name)
    for name in sorted(set(declared) & set(found)):
        out.append("DECLARED names %s, which the scan now sees (%s): drop the "
                   "declaration" % (name, ", ".join(found[name])))
    for name in sorted(set(exempt) & audits):
        out.append("EXEMPT names %s, which a list carries" % name)
    for name in sorted(set(exempt) - set(found)):
        out.append("EXEMPT names %s, which the scan no longer sees reading "
                   "the tree: drop the exemption" % name)
    for table, rows in (("DECLARED", declared), ("EXEMPT", exempt)):
        for name, why in sorted(rows.items()):
            if not str(why).strip():
                out.append("%s gives %s no reason" % (table, name))
    return out


def timings(repo):
    """(per-module seconds, None) from `repo`'s committed `TIMINGS`, read
    through the schema `gate run --sliced --timings` applies, or (None,
    why)."""
    from .gate import read_slice_timings
    seconds, why = read_slice_timings(os.path.join(repo, TIMINGS))
    return seconds, (None if seconds else "%s: %s" % (TIMINGS, why))


def budget(seconds, audits=None, slow=None, share=BUDGET_SHARE,
           ceiling=SLOW_SECONDS):
    """The list's cost against measured per-module `seconds` ({"tests.x":
    s}, as `helm gate slice-timings` prints them). -> {"suite", "listed",
    "share", "unmeasured", "over"}: `over` holds one sentence per breach of
    the budget, judged on the measured modules only; `unmeasured` names the
    listed modules the timings do not hold, whose cost nobody knows."""
    audits = AUDITS if audits is None else audits
    slow = SLOW if slow is None else slow
    suite = float(sum(seconds.values()))
    cost = {n: seconds.get("tests." + n) for n in audits}
    listed = sum(s for s in cost.values() if s is not None)
    over = []
    if listed > share * suite:
        over.append("the %d measured audits cost %.1f s, %.1f%% of the "
                    "suite's %.1f s, over the %.0f%% budget"
                    % (sum(s is not None for s in cost.values()), listed,
                       100 * listed / suite, suite, 100 * share))
    for name in sorted(cost):
        if cost[name] is not None and cost[name] > ceiling \
                and not str(slow.get(name, "")).strip():
            over.append("%s is measured at %.1f s, over %.0f s, and SLOW "
                        "gives no reason it rides whole"
                        % (name, cost[name], ceiling))
    for name in sorted(set(slow) - set(audits)):
        over.append("SLOW names %s, which no list carries" % name)
    return {"suite": suite, "listed": listed,
            "share": listed / suite if suite else None,
            "unmeasured": sorted(n for n, s in cost.items() if s is None),
            "over": over}


def cmd(rest):
    from .cli import guard_tail
    rest = list(rest or ())
    extra = []
    if "--" in rest:
        cut = rest.index("--")
        rest, extra = rest[:cut], rest[cut + 1:]
    rc = guard_tail("helm gate audits", rest,
                    flags=("--json", "--serial", "--wide"),
                    valued=("--repo", "--base", "--tip"), usage=USAGE)
    if rc is not None:
        return rc
    value = {k: rest[rest.index(k) + 1] for k in ("--repo", "--base", "--tip")
             if k in rest}
    if "--wide" not in rest and ("--base" in value or "--tip" in value):
        print("helm gate audits: --base and --tip name the change --wide "
              "reads, and nothing reads them without it (%s)" % USAGE,
              file=sys.stderr)
        return 2
    repo = os.path.abspath(value.get("--repo") or os.getcwd())
    gone = missing(repo)
    if gone:
        print("helm gate audits: %s carries no tests/%s.py — this list names "
              "an audit the tree does not have; nothing printed"
              % (repo, ".py, tests/".join(gone)), file=sys.stderr)
        return 1
    census = None
    if "--wide" in rest:
        import time
        started = time.monotonic()
        census, why = lane_census(repo, value.get("--base"),
                                  value.get("--tip") or "HEAD")
        if why:
            print("helm gate audits: the lane census cannot be read (%s); "
                  "nothing printed, because a shorter list is not the lane's"
                  % why, file=sys.stderr)
            return 1
        census["seconds"] = round(time.monotonic() - started, 2)
        extra = census["modules"] + extra
    runner, reason = mode(repo, extra, serial="--serial" in rest)
    line = command(repo, extra, runner)
    if "--json" in rest:
        import json
        doc = {"repo": repo, "modules": modules(extra), "mode": runner,
               "mode_reason": reason, "command": line}
        for key in ("base", "tip", "merge_base", "touched", "reasons",
                    "seconds") if census else ():
            doc[key] = census[key]
        print(json.dumps(doc, indent=2))
        return 0
    print("helm gate audits: %s — %s" % (runner.upper(), reason),
          file=sys.stderr)
    if census:
        print("helm gate audits: census: %d modules from %d touched paths "
              "(merge-base %s, tip %s, %.1f s)"
              % (len(census["extra"]), len(census["touched"]),
                 census["merge_base"][:12], census["tip"][:12],
                 census["seconds"]), file=sys.stderr)
    print(line)
    return 0
