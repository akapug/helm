"""Report a test double planted on a name the code under test cannot reach.

THE CLASS, and it is the QUIET half of one already measured twice. When a
change moves which function a caller invokes, every arm that patches the OLD
callee for that caller is now planting its double BESIDE the live path. The
halves are asymmetric and only one is lucky: an arm asserting a RAISE or a
SIDE EFFECT goes RED and gets fixed, while AN ARM ASSERTING AN ABSENCE GOES
GREEN AND VACUOUS FOREVER, because a double that never fires and a code path
that never runs produce the identical observable.

SIBLING OF THE VACUOUS-ASSERTION RUNG, and deliberately so: that one asks
whether an arm's assertions could pass without proving anything, this one asks
whether the arm's FIXTURE is even connected to the code it names. Same door,
same WARN-only posture, same planted control.

WHY A CALL GRAPH AND NOT A GREP. Grepping for a retired name over-reports
badly: a helper can lose ONE caller and keep three, so most arms patching it
are still correct. The question is per-arm — is the patched name REACHABLE
from the entry points this arm actually calls? — and that is what an
intra-module call graph answers.

WHAT IT REFUSES TO GUESS. An arm with no function entry point into the patched
module is UNKNOWN, never a finding: it may reach the code through a helper,
another module, or a class this walker does not follow. Dynamic dispatch,
getattr and callbacks are the same case.

AND THE LIMIT THAT MAKES THIS A CENSUS RATHER THAN A RUNG, stated plainly
because three of the first candidates read by hand were CORRECT ARMS: having a
function entry point does not make it the arm's DRIVER. An arm can call one
function of a module incidentally and drive the code under test through a
class helper into a different module entirely, and this walker cannot follow
that. So a finding here is a QUESTION FOR A READER — is this double connected
to what the arm actually exercises? — and never a verdict. It is WARN-only for
that reason, and it must earn the right to refuse by being read first.
"""
import ast
import os
import shutil
import subprocess
import sys
import tempfile

#: A PLANTED ARM THE ANALYZER MUST SEE, built from REAL names so it exercises
#: the real call graph rather than a fiction. It patches the builder
#: `_landed_index` STOPPED calling and enters through `_landed_index` — the
#: historical instance this scanner exists for. If this control goes
#: unreported the walker is broken and its silence is not a clean bill, the
#: same contract the vacuous-assertion rung holds itself to.
#:
#: IT IS ALSO A TRIPWIRE ON ITS OWN PREMISE: should `_landed_index` ever call
#: `_stored_patch_index` again, the control stops being an orphan and fails
#: LOUDLY, which is the right way for a control built on a fact to notice
#: that the fact moved.
#: THE CONTROLS' SUBJECT IS SOURCE THIS MODULE WRITES, NOT SOURCE IT SHARES A
#: REPOSITORY WITH. The first cut planted its arms against real helm functions
#: -- `landreq._landed_index` and its neighbours -- and reachability is
#: resolved by finding the imported module ON DISK under the scanned root. That
#: made the controls fail in two ways that have nothing to do with the walker.
#: OUTSIDE helm they cannot resolve at all, so every commit in another
#: repository with this rail armed printed CONTROL FAILED and the scanner's
#: silence became worthless (helm task/2248, measured across three roots: helm
#: checkout True, helm worktree True, a non-helm checkout FALSE). And INSIDE
#: helm an ordinary refactor of landreq could retire the control's premise
#: without anyone noticing they had disarmed the census.
#:
#: A control whose subject is live source is a control that can be refactored
#: away. These four are staged into a private temporary root instead, so the
#: only thing they can be broken by is a change to the walker -- which is
#: precisely what a control is for.
_CONTROL_PACKAGE = "ctl"
_CONTROL_MODULE = "mod"
#: The synthetic subject. `entry` reaches `_reached` and nothing reaches
#: `_never_reached`, which is the whole distinction the four arms below split
#: on -- stated in six lines that cannot drift, rather than borrowed from a
#: module somebody else maintains.
_CONTROL_SUBJECT = """
def entry(a, b):
    return _reached(a, b)


def _reached(a, b):
    return (a, b)


def _never_reached(a, b):
    return (b, a)
"""

_CONTROL = """
from unittest import mock
from ctl import mod


def test_planted_orphan_control():
    with mock.patch.object(mod, "_never_reached"):
        mod.entry("/g", "trunk")
"""

#: The must-STAY-QUIET half. Same shape, but the patched name IS reachable
#: from the entry point, so reporting it would mean the walker cannot tell a
#: live fixture from an orphaned one.
_QUIET_CONTROL = """
from unittest import mock
from ctl import mod


def test_planted_live_control():
    with mock.patch.object(mod, "_reached"):
        mod.entry("/g", "trunk")
"""

#: THE ABSENCE-ASSERTING HALF, and it is the half this census was filed for.
#: Same two polarities as above, but every arm asserts the double NEVER fired
#: -- the spelling an exemption must NOT key on. The orphan must still be
#: reported: `_never_reached` is not reachable from `entry`, so
#: `assert_not_called` there is guaranteed to pass and measures nothing.
_ABSENCE_ORPHAN_CONTROL = """
from unittest import mock
from ctl import mod


def test_planted_absence_orphan_control():
    with mock.patch.object(mod, "_never_reached") as idx:
        mod.entry("/g", "trunk")
    idx.assert_not_called()
"""

#: ...and the same spelling over a REACHABLE target must stay quiet, because
#: there `assert_not_called` is a real measurement: the double could have
#: fired and the arm's subject is that it did not.
_ABSENCE_LIVE_CONTROL = """
from unittest import mock
from ctl import mod


def test_planted_absence_live_control():
    with mock.patch.object(mod, "_reached") as ext:
        mod.entry("/g", "trunk")
    ext.assert_not_called()
"""


_DEPENDENCY_ERROR = ""
try:
    if __package__:
        from . import vacuous_assertion as _va
    else:
        # The installed script requires its SIBLING snapshot, not a package
        # found elsewhere on PYTHONPATH. Missing data means no scan, not an
        # optional helper whose absence silently disables exemptions.
        sibling = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "vacuous_assertion.py")
        if not os.path.isfile(sibling):
            raise ImportError("required sibling snapshot is missing")
        import vacuous_assertion as _va
    _spy_fired = _va.spy_assertion_proves_it_fired
except (ImportError, AttributeError) as exc:
    _spy_fired = None
    _DEPENDENCY_ERROR = str(exc)


def _git(root, *args):
    done = subprocess.run(("git",) + args, cwd=root,
                          capture_output=True, text=True)
    return done.returncode, done.stdout


def _module_alias_map(tree):
    """{local alias -> dotted module} for `from helm import X` / `import X`."""
    aliases = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            for a in node.names:
                aliases[a.asname or a.name] = "%s.%s" % (node.module, a.name)
        elif isinstance(node, ast.Import):
            for a in node.names:
                aliases[a.asname or a.name.split(".")[0]] = a.name
    return aliases


def _module_path(root, dotted):
    """A dotted module -> its file under `root`, or None when not ours."""
    rel = os.path.join(*dotted.split(".")) + ".py"
    path = os.path.join(root, rel)
    return path if os.path.exists(path) else None


def call_graph(source):
    """{function name -> set of names it calls}, intra-module, nested included.

    Methods are keyed by their BARE name. That is deliberately coarse: two
    classes with a same-named method merge, which can only make a name look
    MORE reachable and therefore can only SUPPRESS a finding. A census that
    errs toward silence is the one people can act on.
    """
    graph = {}
    tree = ast.parse(source)

    def calls_in(node):
        found = set()
        for sub in ast.walk(node):
            if not isinstance(sub, ast.Call):
                continue
            f = sub.func
            if isinstance(f, ast.Name):
                found.add(f.id)
            elif isinstance(f, ast.Attribute):
                found.add(f.attr)
        return found

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            graph.setdefault(node.name, set()).update(calls_in(node))
    return graph


def reachable(graph, entries):
    """Every name reachable from `entries` through `graph`."""
    seen, frontier = set(), list(entries)
    while frontier:
        name = frontier.pop()
        if name in seen:
            continue
        seen.add(name)
        frontier.extend(graph.get(name, ()))
    return seen


def _handle_map(fn):
    """{id(patch call) -> handle name} for `with patch.object(...) as NAME`.

    THE HANDLE IS PER-DOUBLE AND THE EXEMPTION HAS TO BE TOO. An arm commonly
    holds several doubles under one `with`, and a firing proof about ONE of
    them says nothing about the others. MEASURED on this tree: an arm that
    reads `git.call_args[0][1]` beside `idx.assert_not_called()` had its
    INERT `idx` double exempted by its LIVE `git` double, and the finding
    this census was filed to make vanished from the output.
    """
    handles = {}
    for node in ast.walk(fn):
        if not isinstance(node, (ast.With, ast.AsyncWith)):
            continue
        for item in node.items:
            var = item.optional_vars
            if isinstance(var, ast.Name):
                handles[id(item.context_expr)] = var.id
    return handles


def _patches(fn, aliases):
    """[(line, alias, target, handle)] for each mock.patch.object here."""
    handles = _handle_map(fn)
    out = []
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call):
            continue
        # `mock.patch.object(M, "name")` parses as Attribute(object) over
        # Attribute(patch), and `patch.object(...)` as Attribute(object) over
        # Name(patch). Both spellings are the same fixture and both count.
        f = node.func
        if not (isinstance(f, ast.Attribute) and f.attr == "object"):
            continue
        base = f.value
        if not ((isinstance(base, ast.Attribute) and base.attr == "patch")
                or (isinstance(base, ast.Name) and base.id == "patch")):
            continue
        if len(node.args) < 2:
            continue
        mod, target = node.args[0], node.args[1]
        if not (isinstance(mod, ast.Name) and mod.id in aliases):
            continue
        if not (isinstance(target, ast.Constant)
                and isinstance(target.value, str)):
            continue
        # A `side_effect=` IS AN INSTRUMENT, NOT A STUB, and an arm that
        # installs one is measuring INVOCATION — either a plant it expects to
        # fire, or a recorder it asserts stayed empty. Measured on this tree:
        # an arm can assert non-invocation through a LOCAL LIST the side
        # effect appends to, with no spy handle for a walker to find, so the
        # handle check below cannot see it. Skipping every side_effect
        # under-reports and never over-reports, which is the direction a
        # census people act on has to err in.
        if any(k.arg == "side_effect" for k in node.keywords):
            continue
        out.append((node.lineno, mod.id, target.value,
                    handles.get(id(node))))
    return out


def _entry_points(fn, alias):
    """Every name this arm REACHES FOR through `alias` — its way in.

    A REFERENCE COUNTS, NOT ONLY A CALL, and this was a false positive shape
    on the live tree: `_out(ready.cmd_ready, [])` hands the entry point to a
    helper that calls it, so a walker looking only for `alias.name(...)` sees
    no entry at all and calls every double in that arm orphaned. Passing a
    function is a call site one hop away.

    WIDENING THE ENTRY SET CUTS BOTH WAYS AND I FIRST WROTE THAT IT DID NOT.
    More entries make a patched name look MORE reachable, which suppresses
    findings — but they also move arms OUT of the no-entry-point bucket,
    where they were being skipped as UNKNOWN, and those become eligible to be
    reported. Measured on the live tree: 72 findings before this change and
    126 after. The net is not a suppression and the honest claim is that this
    makes the walker see MORE of what an arm actually touches, in both
    directions.
    """
    found = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) \
                and node.value.id == alias:
            found.add(node.attr)
    return found


def _spy_handles(fn):
    """{name bound by `with mock.patch.object(...) as NAME`} in this function."""
    names = set()
    for node in ast.walk(fn):
        if not isinstance(node, (ast.With, ast.AsyncWith)):
            continue
        for item in node.items:
            v = item.optional_vars
            if isinstance(v, ast.Name):
                names.add(v.id)
    return names


def _firing_statements(fn, handle):
    """The straight-line prefix, descending only into mock.patch.object.

    `_Assertions` tracks guarded positives for a different question. Its
    generic with-visitor permits suppressing contexts, so borrowing its nodes
    would not establish that a failure escapes this test. Stop at unfamiliar
    control flow instead; missing an exemption only leaves an advisory.

    A CLOSED BLOCK CANNOT SWALLOW WHAT FOLLOWS IT, which is why an
    uninterpretable `with` SKIPS rather than ends the walk. The reason never
    to DESCEND stands unchanged: a context manager we cannot read might
    suppress a failure raised inside its body. That reason says nothing about
    the statements AFTER it -- by then `__exit__` has run and cannot reach a
    later raise -- and ending the walk there cost the exemption to every arm
    whose fixture scope precedes its patch block, which is an ordinary shape:
    `with serial_process(ran=9):` or a tempdir or a chdir, then the patch.
    A block that MENTIONS the handle is still refused outright, because that
    is an alias or a capture and interpreting it is exactly what this walk
    declines to do.
    """
    stack = [iter(fn.body)]
    while stack:
        node = next(stack[-1], None)
        if node is None:
            stack.pop()
            continue
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            yield node                  # inspect captures, never credit the body
        elif isinstance(node, ast.With):
            if not all(isinstance(i.context_expr, ast.Call)
                       and _va._call_name(i.context_expr.func) in (
                           "mock.patch.object", "patch.object")
                       for i in node.items):
                if any(_uses_handle(i.context_expr, handle)
                       or (i.optional_vars is not None
                           and _uses_handle(i.optional_vars, handle))
                       for i in node.items):
                    return              # alias or capture: never interpreted
                continue                # closed scope: skip it, keep walking
            yield node                  # the handle binding precedes its body
            stack.append(iter(node.body))
        elif isinstance(node, (ast.Assert, ast.Expr, ast.Assign, ast.Pass,
                               ast.Import, ast.ImportFrom)):
            yield node                  # no walk into expression descendants
        else:
            return                      # if/loop/try/return/raise/etc: UNKNOWN


def _uses_handle(node, handle):
    return any(isinstance(n, ast.Name) and n.id == handle for n in ast.walk(node))


# Keywords that configure what the double DOES without touching what it
# RECORDS. `called`, `call_count`, `call_args` and `mock_calls` are written by
# the call machinery BEFORE either of these is consulted, so a double
# configured with them answers the fired-question exactly as a bare one does.
#
# `side_effect` is deliberately NOT here even though it records the same way.
# It can raise, and this walk's premise is that a failure ESCAPES the test --
# reasoning about which exception surfaces from the patched block is the
# interpretation this function exists to refuse. AND ITS ABSENCE HERE CHANGES
# NOTHING TODAY, which is worth saying so nobody reads it as a live refusal:
# a `side_effect=` patch is already exempted upstream, at the clause above
# that calls it an instrument rather than a stub, so it never reaches this
# predicate as a candidate at all. Measured both ways -- with a fired-proof
# and with none -- and the census is quiet either way.
_OBSERVABLE_PRESERVING_KWARGS = ("return_value", "wraps")


def _default_spy_binding(call):
    # Unknown constructor/configuration can replace the default observables.
    # Do not interpret new=, arbitrary factories, **kwargs or configure_mock.
    # A `**kwargs` entry has arg None and fails every arm below, as it must.
    return len(call.args) == 2 and all(
        k.arg in _OBSERVABLE_PRESERVING_KWARGS
        or (k.arg == "new_callable" and _va._call_name(k.value) in (
            "mock.Mock", "mock.MagicMock", "mock.AsyncMock"))
        for k in call.keywords)


def _asserts_the_spy_FIRED(fn, handle):
    """Credit a default, unique binding before its first unsupported use.

    The shared helper judges the never-fired valuation; this boundary selects
    context and preserves its premise. Except for direct invocations, an
    uncredited use ends the prefix, even when harmless: this is not an
    interpreter for mutators, aliases or helpers.
    """
    stores = [n for n in ast.walk(fn) if isinstance(n, ast.Name)
              and isinstance(n.ctx, ast.Store) and n.id == handle]
    if len(stores) != 1:
        return False                    # no binding-epoch analysis is promised
    for node in ast.walk(fn):
        if isinstance(node, (ast.Yield, ast.YieldFrom)):
            return False                # a generator body need not have run
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) \
                and node is not fn and node.name == handle:
            return False
        if isinstance(node, ast.alias) and (node.asname or node.name.split(".")[0]) == handle:
            return False
        if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Store) \
                and isinstance(node.value, ast.Name) and node.value.id == handle:
            return False                # the test overwrites a mock observable
    bound = False
    for node in _firing_statements(fn, handle):
        if isinstance(node, ast.With):
            for item in node.items:
                if _uses_handle(item.context_expr, handle):
                    return False        # passed into another context/binding
                if isinstance(item.optional_vars, ast.Name) \
                        and item.optional_vars.id == handle:
                    if not _default_spy_binding(item.context_expr):
                        return False
                    bound = True
            continue
        safe_args = True
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
            call = node.value
            # Argument evaluation precedes assertion execution. An explicit
            # escape here can change the readings before the assertion tests
            # them, so reject it BEFORE asking the shared helper for credit.
            args = call.args + [k.value for k in call.keywords]
            safe_args = all(not _uses_handle(a, handle)
                            or _va._never_fired_value(a, handle) is not _va._UNDECIDABLE
                            for a in args)
        if bound and safe_args and _spy_fired(node, handle):
            return True
        if _uses_handle(node, handle):
            if bound and isinstance(node, ast.Expr) \
                    and isinstance(node.value, ast.Call) \
                    and isinstance(node.value.func, ast.Name) \
                    and node.value.func.id == handle and safe_args:
                continue                # direct invocation of the default mock
            return False                # capture, alias, mutator or unknown use
    return False


def _exempt(lines, fn):
    """Is this test declared deliberately unreachable on its own def line?

    THE EXEMPTION IS BY IDENTITY AND CARRIES A REASON, the same shape the
    vacuous-assertion rung uses: a shape-based carve-out would exempt whatever
    else happens to wear that shape.
    """
    index = fn.lineno - 1
    if 0 <= index < len(lines):
        return "noqa: ORPHANED_MOCK" in lines[index]
    return False


def scan_source(root, rel, source):
    """[(line, test, alias, target, why)] — orphaned doubles in one test file."""
    if _spy_fired is None:
        return [], ["required vacuous_assertion sibling unavailable: "
                    + _DEPENDENCY_ERROR]
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return [], ["%s: %s" % (rel, exc)]
    lines = source.splitlines()
    aliases = _module_alias_map(tree)
    graphs, issues, findings = {}, [], []

    def graph_for(alias):
        if alias in graphs:
            return graphs[alias]
        path = _module_path(root, aliases.get(alias, ""))
        if not path:
            graphs[alias] = None
            return None
        try:
            with open(path, encoding="utf-8") as fh:
                graphs[alias] = call_graph(fh.read())
        except (OSError, UnicodeDecodeError, SyntaxError) as exc:
            issues.append("%s: %s" % (path, exc))
            graphs[alias] = None
        return graphs[alias]

    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not node.name.startswith("test"):
            continue
        if _exempt(lines, node):
            continue
        for line, alias, target, handle in _patches(node, aliases):
            graph = graph_for(alias)
            if graph is None:
                continue
            if target not in graph:
                # NOT A FUNCTION IN THAT MODULE, so reachability is not the
                # question. Patching a CONSTANT — a budget, a tuple of caps,
                # a state word — is ordinary and correct, and a call graph
                # over function names can never contain it. Reporting those
                # would be tripping a clause that does not apply, and it is
                # what the first run of this scanner did: three of its
                # eighteen findings were module-level data.
                continue
            # THIS DOUBLE'S OWN HANDLE, never the arm's whole set: a
            # sibling double firing proves nothing about this one.
            if handle and _asserts_the_spy_FIRED(node, handle):
                continue
            # ONLY FUNCTIONS ARE ENTRY POINTS. An arm that touches the
            # module solely to read a CONSTANT — a tuple of states, a budget
            # — has not called into it, and the closure from a constant name
            # is just that name, so every double in that arm would look
            # unreachable. Measured on the live tree: an arm whose only
            # module touch was a states tuple was reported twice.
            entries = {e for e in _entry_points(node, alias) if e in graph}
            if not entries:
                # NO ENTRY POINT IS UNKNOWN, NEVER A FINDING. This arm may
                # reach the code through a helper, another module, or a class
                # this walker does not follow; reporting it would be
                # asserting absence from a probe that cannot see.
                continue
            if target in reachable(graph, entries):
                continue
            findings.append((line, node.name, alias, target,
                             "NOT REACHED BY THE MODELED GRAPH from %s"
                             % ", ".join(sorted(entries)[:3])))
    return findings, issues


def _controls_hold(_root=None):
    """(True/False/None, why) — can this walker still see its own planted arms?

    TWO CONTROLS, NOT ONE, because a scanner can fail in two directions and
    only one of them is loud. A walker that reports NOTHING passes a
    findings-only check trivially; a walker that reports EVERYTHING passes a
    control that only plants an orphan. So one arm must be found and one must
    stay quiet, and a census that cannot do both says so instead of printing
    a number.

    THE SUBJECT IS STAGED, NOT BORROWED. Reachability resolves an imported
    module to a FILE UNDER THE SCANNED ROOT, so arms written against real helm
    functions answer a question about whatever repository the commit happens
    to be in. Outside helm they cannot resolve at all and this reported CONTROL
    FAILED on every commit in another checkout with the rail armed; inside
    helm, an ordinary refactor could retire the premise silently. The four arms
    now run against a synthetic package written into a private temporary
    directory, so the ONLY thing that can break them is a change to the walker.
    `_root` is accepted and ignored, kept so the call site reads unchanged.

    THREE ANSWERS, NOT TWO. None is UNKNOWN — the staging itself failed — and
    it must never be reported as a control FAILURE: a rung that cannot set up
    its own experiment has learned nothing about the walker, and saying
    otherwise would put an alarm where an admission belongs.
    """
    try:
        tmp = tempfile.mkdtemp(prefix="orphaned-mock-control-")
    except OSError as exc:
        return None, "could not stage the control package (%s)" % exc
    try:
        stage_controls(tmp)
        return _controls_hold_at(tmp)
    except OSError as exc:
        return None, "could not stage the control package (%s)" % exc
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def stage_controls(dest):
    """Write the controls' synthetic package under `dest` -> `dest`.

    ONE STAGING, TWO CALLERS. `_controls_hold` uses it at commit time and the
    suite uses it to scan the same arms, so a test cannot certify a subject
    the rung does not actually build. Splitting them is how a control and its
    arm drift into agreeing about different things.
    """
    pkg = os.path.join(dest, _CONTROL_PACKAGE)
    os.makedirs(pkg, exist_ok=True)
    with open(os.path.join(pkg, "__init__.py"), "w", encoding="utf-8"):
        pass
    with open(os.path.join(pkg, _CONTROL_MODULE + ".py"), "w",
              encoding="utf-8") as fh:
        fh.write(_CONTROL_SUBJECT)
    return dest


def _controls_hold_at(root):
    """The four arms, against a root whose contents this module wrote."""
    loud, _ = scan_source(root, "<control>", _CONTROL)
    if not loud:
        return False, ("the planted ORPHAN went unreported, so the walker "
                       "cannot see a double on a name nothing reaches — the "
                       "subject is written by this module, so the premise "
                       "cannot have moved and only the walker can be wrong")
    quiet, _ = scan_source(root, "<control>", _QUIET_CONTROL)
    if quiet:
        return False, ("the planted LIVE fixture was reported as orphaned, "
                       "so this walker cannot tell a connected double from a "
                       "disconnected one")
    # THE ABSENCE-ASSERTING PAIR, and it is not a duplicate of the pair above:
    # those two arms assert nothing at all, so they cannot detect an exemption
    # keyed on the word "assert". These two wear that exact spelling, and they
    # must still split by REACHABILITY.
    absent_loud, _ = scan_source(root, "<control>", _ABSENCE_ORPHAN_CONTROL)
    if not absent_loud:
        return False, ("an `assert_not_called` double planted where nothing "
                       "can reach it went unreported — the exemption is "
                       "keying on assertion spelling again, which hides the "
                       "very half this census counts")
    absent_quiet, _ = scan_source(root, "<control>", _ABSENCE_LIVE_CONTROL)
    if absent_quiet:
        return False, ("an `assert_not_called` double on a REACHABLE target "
                       "was reported as orphaned, so the walker now cries "
                       "wolf on a real absence measurement")
    return True, ""


def _test_files(root):
    tests = os.path.join(root, "tests")
    if not os.path.isdir(tests):
        return []
    return sorted(os.path.join("tests", n) for n in os.listdir(tests)
                  if n.startswith("test_") and n.endswith(".py"))


def main(argv):
    if _spy_fired is None:
        print("[helm orphaned-mock] scan UNKNOWN: required vacuous_assertion "
              "sibling unavailable (%s) — advisory SKIPPED; commit allowed; "
              "reinstall: helm work install-guard --apply" % _DEPENDENCY_ERROR,
              file=sys.stderr)
        return 0
    argv = list(argv or [])
    staged = "--staged" in argv
    root = os.getcwd()
    rc, out = _git(root, "rev-parse", "--show-toplevel")
    if rc == 0 and out.strip():
        root = out.strip()

    # THREE ANSWERS AND THEY ARE NOT INTERCHANGEABLE. None means the rung
    # could not stage its own experiment, which says nothing about the walker
    # and must not wear an alarm's words; False means the walker measurably
    # failed an arm whose subject this module wrote, and that IS an alarm.
    held, why = _controls_hold()
    if held is None:
        print("[helm orphaned-mock] CONTROLS UNMEASURED: %s — commit allowed, "
              "and this scanner has not been checked either way" % why,
              file=sys.stderr)
        return 0
    if not held:
        print("[helm orphaned-mock] CONTROL FAILED: %s — commit allowed, but "
              "this scanner's silence is not a clean bill" % why,
              file=sys.stderr)
        return 0

    findings, issues = [], []
    if staged:
        rc, out = _git(root, "diff", "--cached", "--name-only",
                       "--diff-filter=ACMR")
        # A FAILED ENUMERATION IS NOT AN EMPTY ONE. `git diff --cached` exits
        # non-zero when it cannot read the index, and its stdout is empty
        # either way — so taking the empty list would report ZERO candidates
        # for a scan that never happened, which is the exact collapse this
        # census exists to name in other people's tests. It is advisory
        # UNKNOWN and rc stays 0, because a warn rung must not block a commit
        # on its own blindness; it just may not call that blindness a clean
        # bill.
        if rc != 0:
            print("[helm orphaned-mock] scan UNKNOWN: the staged file list "
                  "could not be read (git diff --cached exited %d) — this run "
                  "examined NOTHING and its silence is not a clean bill"
                  % rc, file=sys.stderr)
            return 0
        rels = [r for r in out.splitlines()
                if r.startswith("tests/") and r.endswith(".py")]
        for rel in rels:
            rc, blob = _git(root, "show", ":" + rel)
            if rc != 0:
                issues.append("%s: unreadable staged blob" % rel)
                continue
            f, i = scan_source(root, rel, blob)
            findings.extend((rel,) + row for row in f)
            issues.extend(i)
    else:
        for rel in _test_files(root):
            try:
                with open(os.path.join(root, rel), encoding="utf-8") as fh:
                    f, i = scan_source(root, rel, fh.read())
            except (OSError, UnicodeDecodeError) as exc:
                issues.append("%s: %s" % (rel, exc))
                continue
            findings.extend((rel,) + row for row in f)
            issues.extend(i)

    for issue in issues:
        print("[helm orphaned-mock] scan UNKNOWN: %s" % issue, file=sys.stderr)
    for rel, line, test, alias, target, why in findings:
        print("[helm orphaned-mock] %s:%d %s — patches %s.%s, %s"
              % (rel, line, test, alias, target, why), file=sys.stderr)
    if findings:
        print("[helm orphaned-mock] %d CANDIDATE%s: doubles the modeled call "
              "graph did not reach from this arm's entry points. ADVISORY, "
              "WARN ONLY, and NOT a proof of unreachability — this walker does "
              "not follow test HELPER METHODS, CROSS-MODULE consumers or verb "
              "DISPATCH TABLES, and a hand-labelled sample of ten found NINE "
              "reached through exactly those. What a candidate means is worth "
              "a READ: where a double truly is off the path, an arm asserting "
              "an ABSENCE proves nothing, because a double that never fires "
              "and a path that never runs look identical. Add "
              "`# noqa: ORPHANED_MOCK — <reason>` on the test definition when "
              "the double is deliberately unreachable."
              % (len(findings), "S"[:len(findings) != 1]), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
