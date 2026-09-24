#!/usr/bin/env python3
"""An assertion renders KEY NAMES, never VALUES: the refusing scan for it.

THE LEAK (task/2370). `assertNotIn(key, os.environ)` fails by rendering its
CONTAINER, and that container is the process environment, so one red test on a
build node writes every ambient value into the unittest failure text -- which
fab copies verbatim into the run's artifacts, where anyone who can read the
job can read a live service credential. A suite that is green except for one
arm is enough; nothing else in the tree says no.

PROVEN WITH A SENTINEL, not reasoned about. With HELM_LEAK_PROBE_KEY set to a
synthetic LEAK_SENTINEL value, the failure text of each form was counted for
occurrences of that value:

    assertNotIn(k, os.environ)              value x1   12818 chars
    assertNotIn(k, os.environ.keys())       value x1   12800 chars
    assertNotIn(k, dict(os.environ))        value x1
    assertNotIn(k, tuple(os.environ))       value x0    3857 chars
    assertNotIn(k, sorted(os.environ))      value x0
    assertNotIn(k, set(os.environ))         value x0
    assertFalse(k in os.environ, "...")     value x0     872 chars

``os.environ.keys()`` IS NOT A CURE and reads like one: a KeysView's repr
wraps the mapping it views, so it renders every value. The cure is a KEY VIEW
THAT HAS LOST ITS MAPPING -- ``tuple()``, ``sorted()``, ``set()``, ``list()``
-- or a boolean with a message naming only the key.

WHAT COUNTS AS THE AMBIENT ENVIRONMENT. `os.environ` is one spelling of it;
`dict(os.environ)`, `{**os.environ, ...}`, `os.environ.copy()` and a dict
comprehension reading `os.environ` are others, and so is any producer that
copies it -- `cell.build_env()` is documented as "a copy of os.environ",
`gate._suite_env()` and `gateshard._fresh_env()` both open with
`env = dict(os.environ)`. The scan follows straight-line assignment inside
each function, so binding one of those to a local `env` first does not hide
it; that binding was how five of the converted sites were written.

WHY A TEST MODULE AND NOT A PRE-COMMIT RUNG. helm's staged-source advisories
(`helm/vacuous_assertion.py`, `helm/hardcode.py`) are WARN-only and read the
STAGED DIFF, so they can say nothing about a site nobody is touching and
cannot refuse the commit that writes the next one. This class has no
legitimate instance -- the key view carries the same discriminating power in
every case -- so it gets a REFUSING arm over the WHOLE tests tree, on every
gate, and deliberately NO `noqa` escape to be trained toward.

EMBEDDED SOURCE IS SCANNED TOO. Two of the converted sites live inside
triple-quoted programs this suite writes out and runs as subprocesses
(tests/test_gateequiv.py, tests/test_gateshard.py). A child unittest renders
the child's environment, which on a build node is the same environment; a scan
that reads only the outer module's own assertions misses them.
"""
import ast
import fnmatch
import os
import tempfile
import textwrap
import unittest


HERE = os.path.dirname(os.path.abspath(__file__))

# Producers whose body OPENS with `dict(os.environ)`, so what they hand back
# carries every ambient value. Named by their DOTTED SPELLING, verified at each
# definition rather than guessed from the name -- `helm/cell.py:483 build_env`,
# `helm/gate.py:286 _suite_env`, `helm/gateshard.py:567 _fresh_env`.
#
# THE BARE NAME IS THE WRONG KEY, and measuring proved it: matching any
# `build_env` flagged eleven sites in tests/test_launch.py, tests/test_scratch.py
# and tests/test_seat.py, and `helm/launch.py:58 build_env(base, seat, ...)`
# copies the BASE ITS CALLER PASSES -- every one of those calls passes a
# synthetic literal like {"PATH": "/bin"}, so no ambient value exists to
# render. A producer added to this tree must be added here; the scan's own
# authority is over the `os.environ` spellings, and this list extends it by
# measurement.
ENV_PRODUCERS = ("cell.build_env", "gate._suite_env", "gateshard._fresh_env")

# These take a mapping and hand back a container that has FORGOTTEN it, so the
# failure text carries key names alone. THE CURE IS THE FORGETTING, NOT THE
# CALL: what they are handed decides what they render, and `_materializes_values`
# is where that distinction lives.
KEY_VIEWS = ("tuple", "sorted", "set", "list", "frozenset")

# These hand back the VALUES, or the pairs carrying them, so materializing one
# renders every value the mapping holds.
VALUE_VIEWS = ("items", "values")

MEMBERSHIP = {"assertIn": 1, "assertNotIn": 1}
EQUALITY = ("assertEqual", "assertNotEqual", "assertDictEqual",
            "assertCountEqual")

# The recognized API's PARAMETER NAMES, by position, read off
# `inspect.signature(unittest.TestCase.<verb>)` rather than guessed. A KEYWORD
# call is the same forbidden shape -- `assertNotIn(member="K",
# container=os.environ)` renders the container exactly as the positional form
# does -- and inspecting `node.args` alone skipped it. The set is finite because
# the verbs are, so binding their keywords needs no signature introspection at
# scan time.
PARAMS = {"assertIn": ("member", "container"),
          "assertNotIn": ("member", "container"),
          "assertEqual": ("first", "second"),
          "assertNotEqual": ("first", "second"),
          "assertDictEqual": ("d1", "d2"),
          "assertCountEqual": ("first", "second")}


def _is_os_environ(node):
    return (isinstance(node, ast.Attribute) and node.attr == "environ"
            and isinstance(node.value, ast.Name) and node.value.id == "os")


def _mentions_os_environ(node):
    return any(_is_os_environ(n) for n in ast.walk(node))


def _callee_name(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _dotted(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        head = _dotted(node.value)
        return (head + "." + node.attr) if head else ""
    return ""


def _materializes_values(node, tainted):
    """True when materializing `node` STILL yields environment values.

    `tuple(os.environ)` iterates the KEYS and that is the whole cure, but
    `tuple(os.environ.items())` and `list(os.environ.values())` iterate the
    pairs and the values themselves: the container forgot the mapping, never
    the contents it was handed, so the failure text carries every value exactly
    as the bare mapping does. Declaring every `tuple`/`sorted`/`set`/`list`
    safe regardless of its input made those two forms invisible.

    Recurses through KEY_VIEWS so a re-wrapped materializer
    (`sorted(tuple(env.items()))`) is not laundered by the outer call.
    """
    if not isinstance(node, ast.Call):
        return False
    name = _callee_name(node.func)
    if name in VALUE_VIEWS:
        return (isinstance(node.func, ast.Attribute)
                and _is_env_mapping(node.func.value, tainted))
    if name in KEY_VIEWS:
        return any(_materializes_values(a, tainted) for a in node.args)
    return False


def _is_env_mapping(node, tainted):
    """True when a failure rendering `node` would print environment VALUES."""
    if _is_os_environ(node):
        return True
    if isinstance(node, ast.Name):
        return node.id in tainted
    if isinstance(node, ast.Attribute):
        # .keys()/.items()/.copy() are reached as Call; a bare attribute chain
        # off an env mapping (os.environ.keys without the call) renders too.
        return _is_env_mapping(node.value, tainted)
    if isinstance(node, ast.Dict):
        # {**os.environ, "K": "v"}
        return any(k is None and _is_env_mapping(v, tainted)
                   for k, v in zip(node.keys, node.values))
    if isinstance(node, ast.DictComp):
        # {k: os.environ.get(k) for k in KEYS} -- values are in the mapping
        return _mentions_os_environ(node.value)
    if isinstance(node, ast.Call):
        name = _callee_name(node.func)
        if name in KEY_VIEWS:
            # The cure over KEYS, and no cure at all over items/values.
            return any(_materializes_values(a, tainted) for a in node.args)
        if name in ("copy", "keys") or name in VALUE_VIEWS:
            # A KeysView/ItemsView repr WRAPS the mapping it views, so these
            # render values exactly as the mapping does -- measured, and the
            # reason `os.environ.keys()` is refused rather than accepted.
            return (isinstance(node.func, ast.Attribute)
                    and _is_env_mapping(node.func.value, tainted))
        if name == "dict":
            return (any(_is_env_mapping(a, tainted) for a in node.args)
                    or any(_is_env_mapping(k.value, tainted)
                           for k in node.keywords if k.arg is None))
        if _dotted(node.func) in ENV_PRODUCERS:
            return True
        return False
    return False


def _targets(node):
    for t in ast.walk(node):
        if isinstance(t, ast.Name):
            yield t.id


SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)


def _blocks(stmt):
    """The statement lists nested inside `stmt`, in source order."""
    for _field, value in ast.iter_fields(stmt):
        if not isinstance(value, list):
            continue
        if value and isinstance(value[0], ast.stmt):
            yield value
        elif value and isinstance(value[0], ast.excepthandler):
            for handler in value:
                yield handler.body


def _own_expressions(stmt):
    """Nodes of `stmt` that are NOT another statement and NOT inside a nested
    scope. Nested scopes are yielded themselves so the caller can recurse with
    their own taint rather than the enclosing one."""
    stack = list(ast.iter_child_nodes(stmt))
    while stack:
        node = stack.pop()
        if isinstance(node, ast.stmt):
            continue                      # a nested block; _blocks walks it
        yield node
        if isinstance(node, SCOPES):
            continue                      # its own scope
        stack.extend(ast.iter_child_nodes(node))


class _Scan:
    def __init__(self, path, embedded_at=None):
        self.path = path
        self.embedded_at = embedded_at
        self.findings = []

    def line(self, node):
        return self.embedded_at or node.lineno

    def argument(self, node, name, index):
        """The argument at `index`, passed POSITIONALLY OR BY KEYWORD."""
        if index < len(node.args):
            return node.args[index]
        params = PARAMS.get(name, ())
        if index < len(params):
            for kw in node.keywords:
                if kw.arg == params[index]:
                    return kw.value
        return None

    def call(self, node, tainted):
        name = _callee_name(node.func)
        if name in MEMBERSHIP:
            indexes = (MEMBERSHIP[name],)
        elif name in EQUALITY:
            indexes = (0, 1)
        else:
            return
        for i in indexes:
            arg = self.argument(node, name, i)
            if arg is not None and _is_env_mapping(arg, tainted):
                self.findings.append((self.path, self.line(node), name,
                                      self.embedded_at is not None))
                return

    def embedded(self, node, tainted):
        """PREFILTER ON EVERY SPELLING THE PARSE CAN RECOGNIZE, never on
        `os.environ` alone: a child program comparing `cell.build_env()` with a
        mapping renders the same ambient values on the same build node, and
        demanding the literal `os.environ` in the text skipped such a program
        entirely -- recognized in ordinary source, invisible inside a string."""
        text = node.value
        if "assert" not in text:
            return
        if not ("os.environ" in text
                or any(p in text for p in ENV_PRODUCERS)):
            return
        try:
            sub = ast.parse(textwrap.dedent(text))
        except SyntaxError:
            return
        inner = _Scan(self.path, embedded_at=self.line(node))
        inner.block(sub.body, set(tainted))
        self.findings.extend(inner.findings)

    def taint(self, stmt, tainted):
        if isinstance(stmt, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            # EVERY TARGET, not the first. `first = alias = os.environ` binds
            # the same mapping to both names, and reading `targets[0]` left
            # `alias` clean, so an assertion on it rendered every value.
            targets = (stmt.targets if isinstance(stmt, ast.Assign)
                       else [stmt.target])
            # ONLY A NAME REBINDS. `base[KEY] = "..."` and `env.attr = x` MUTATE
            # a mapping that is still the same mapping, and reading the Name out
            # of such a target cleared a real finding: tests/test_evalpin.py
            # writes `base = dict(os.environ)` then `base[OVERRIDE] = path`, and
            # treating that second line as a rebind made the assertEqual on
            # `base` two lines later invisible to this scan.
            names = []
            for target in targets:
                if isinstance(target, ast.Name):
                    names.append(target.id)
                elif isinstance(target, (ast.Tuple, ast.List)):
                    names.extend(e.id for e in target.elts
                                 if isinstance(e, ast.Name))
            if not names:
                return
            if stmt.value is not None and _is_env_mapping(stmt.value, tainted):
                tainted.update(names)
            elif not isinstance(stmt, ast.AugAssign):
                # REBINDING REPLACES PROVENANCE, it never adds to it.
                tainted.difference_update(names)
            # AN AUGMENTED ASSIGNMENT MUTATES IN PLACE AND CLEARS NOTHING:
            # after `env |= {"SYNTHETIC": "value"}` every ambient entry env
            # already held is still in it, so treating the synthetic right-hand
            # side as a rebind laundered the mapping it was added to.
        if isinstance(stmt, (ast.With, ast.AsyncWith)):
            for item in stmt.items:
                if item.optional_vars is None:
                    continue
                names = list(_targets(item.optional_vars))
                if _is_env_mapping(item.context_expr, tainted):
                    tainted.update(names)
                else:
                    tainted.difference_update(names)

    def scope(self, node, tainted):
        body = node.body if isinstance(node.body, list) else [
            ast.Expr(value=node.body, lineno=node.lineno, col_offset=0)]
        self.block(body, tainted)

    def block(self, body, tainted):
        """One statement list, in order, sharing one taint set. A taint set
        deliberately outlives its block: a value bound inside `with` and read
        after it is the shape five of the converted sites were written in."""
        for stmt in body:
            if isinstance(stmt, SCOPES):
                self.scope(stmt, set(tainted))
                continue
            # A STATEMENT'S OWN EXPRESSIONS EVALUATE BEFORE ITS BIND, so they
            # are read under the taint that held BEFORE it:
            # `env = self.assertEqual(env, {})` asserts on the env it still has
            # and only then rebinds the name, and clearing the taint first made
            # that assertion invisible. A right-hand side can never see the
            # binding its own statement performs.
            for node in _own_expressions(stmt):
                if isinstance(node, SCOPES):
                    self.scope(node, set(tainted))
                elif isinstance(node, ast.Call):
                    self.call(node, tainted)
                elif isinstance(node, ast.Constant) and isinstance(node.value,
                                                                   str):
                    self.embedded(node, tainted)
            self.taint(stmt, tainted)
            for block in _blocks(stmt):
                self.block(block, tainted)


def scan_source(source, path="<memory>"):
    """Findings for one module's text. The instrument both controls drive."""
    scan = _Scan(path)
    scan.block(ast.parse(source).body, set())
    return scan.findings


# THE POPULATION THE SUITE RUNS, verified at the two places that define it:
# `helm/gate.py:71` is `unittest discover -s tests -t .`, whose default pattern
# this is, and `gateshard.discover_suite` spells it out as
# `pattern="test*.py"`. `test_*.py` is NARROWER than both: `tests/testleak.py`
# holding the bare-mapping shape is suite-executable and was skipped, and so was
# every module below the top directory.
DISCOVERY_PATTERN = "test*.py"


def discoverable_modules(root=HERE):
    """Every module the suite's discovery can reach under `root`, in order.

    RECURSIVE AND A DELIBERATE SUPERSET. unittest additionally requires a
    subdirectory to be an importable package before it descends; this walk does
    not check for `__init__.py`, so it reads a few files discovery would pass
    over. That is the correct side to err on for a class with NO legitimate
    instance: the cost of the superset is scanning a file nobody runs, and the
    cost of the subset was a claim of authority over the WHOLE tests tree held
    over its top directory alone.
    """
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d != "__pycache__")
        for name in sorted(filenames):
            if fnmatch.fnmatch(name, DISCOVERY_PATTERN):
                yield os.path.join(dirpath, name)


def scan_tests_tree(root=HERE, skip=()):
    findings = []
    skipped = {os.path.realpath(s) for s in skip}
    for path in discoverable_modules(root):
        if os.path.realpath(path) in skipped:
            continue
        rel = os.path.relpath(path, root).replace(os.sep, "/")
        with open(path, encoding="utf-8") as fh:
            findings.extend(scan_source(fh.read(), "tests/" + rel))
    return findings


# ---- the controls. These strings ARE the forbidden shape, which is why this
# ---- module excludes itself from the tree scan below.
_LEAKY_FIXTURE = """\
import os, unittest
class Probe(unittest.TestCase):
    def test_absent(self):
        self.assertNotIn("HELM_ANY", os.environ)
"""
_LEAKY_VIA_LOCAL = """\
import os
from helm import cell
class Probe:
    def test_absent(self):
        env = cell.build_env()
        self.assertNotIn("DREGG_NODE_PASSPHRASE", env)
"""
_LEAKY_EMBEDDED = '''\
class Outer:
    def test_writes_a_child(self):
        self.write("child", """
            import os, unittest
            class Probe(unittest.TestCase):
                def test_absent(self):
                    self.assertNotIn("HELM_ANY", os.environ)
        """)
'''
_CURED_FIXTURE = """\
import os, unittest
class Probe(unittest.TestCase):
    def test_absent(self):
        self.assertNotIn("HELM_ANY", tuple(os.environ))
        self.assertNotIn("HELM_ANY", sorted(os.environ))
        self.assertFalse("HELM_ANY" in os.environ, "HELM_ANY must be absent")
        self.assertEqual(os.environ.get("HELM_ANY"), None)
        env = os.environ.copy()
        drift = [k for k in sorted(env) if env.get(k) != env.get(k)]
        self.assertEqual(drift, [])
"""
_KEYS_VIEW_IS_NOT_A_CURE = """\
import os, unittest
class Probe(unittest.TestCase):
    def test_absent(self):
        self.assertNotIn("HELM_ANY", os.environ.keys())
"""
# A KEY VIEW OVER ITEMS IS NOT A KEY VIEW. Each of these three materializes the
# values or the pairs carrying them.
_VALUES_MATERIALIZED_IS_NOT_A_CURE = """\
import os, unittest
class Probe(unittest.TestCase):
    def test_drift(self):
        self.assertEqual(tuple(os.environ.items()), ())
        self.assertEqual(list(os.environ.values()), [])
        env = os.environ.copy()
        self.assertCountEqual(sorted(env.items()), [])
"""
# THE PAIRED QUIET CONTROL for the arm above: the same four materializers over
# KEYS, including `frozenset(env.keys())` -- a KeysView that HAS lost its
# mapping, which is the distinction the refused bare `env.keys()` turns on.
_KEYS_MATERIALIZED_IS_THE_CURE = """\
import os, unittest
class Probe(unittest.TestCase):
    def test_drift(self):
        env = dict(os.environ)
        self.assertEqual(sorted(set(env) - set(env)), [])
        self.assertEqual(tuple(sorted(env)), tuple(sorted(env)))
        self.assertNotIn("HELM_ANY", frozenset(env.keys()))
        self.assertNotIn("HELM_ANY", list(env))
"""
_CHAINED_ASSIGNMENT = """\
import os, unittest
class Probe(unittest.TestCase):
    def test_drift(self):
        first = alias = %s
        self.assertNotIn("HELM_ANY", first)
        self.assertNotIn("HELM_ANY", alias)
"""
_AUGMENTED_ASSIGNMENT = """\
import os, unittest
class Probe(unittest.TestCase):
    def test_drift(self):
        env = %s
        env |= {"SYNTHETIC": "value"}
        self.assertEqual(env, {})
"""
_REBIND_FROM_ITS_OWN_ASSERTION = """\
import os, unittest
class Probe(unittest.TestCase):
    def test_drift(self):
        env = dict(os.environ)
        env = self.assertEqual(env, {})
"""
_LEAKY_EMBEDDED_PRODUCER_ONLY = '''\
class Outer:
    def test_writes_a_child(self):
        self.write("child", """
            import unittest
            from helm import cell
            class Probe(unittest.TestCase):
                def test_drift(self):
                    self.assertEqual(cell.build_env(), {})
        """)
'''
_LEAKY_KEYWORD_FORM = """\
import os, unittest
class Probe(unittest.TestCase):
    def test_absent(self):
        self.assertNotIn(member="HELM_ANY", container=os.environ)
        self.assertEqual(first=os.environ, second={})
"""
_CURED_KEYWORD_FORM = """\
import os, unittest
class Probe(unittest.TestCase):
    def test_absent(self):
        self.assertNotIn(member="HELM_ANY", container=tuple(os.environ))
        self.assertNotIn("HELM_ANY", msg="named", container=sorted(os.environ))
"""


class ScannerSeesTheShapeTest(unittest.TestCase):
    """POSITIVE CONTROLS FIRST: an empty finding list below is only a clean
    tree if this instrument can fire at all."""

    def test_the_bare_mapping_is_seen(self):
        found = scan_source(_LEAKY_FIXTURE)
        self.assertEqual([(f[1], f[2]) for f in found], [(4, "assertNotIn")])

    def test_a_producer_bound_to_a_local_is_seen(self):
        found = scan_source(_LEAKY_VIA_LOCAL)
        self.assertEqual([(f[1], f[2]) for f in found], [(6, "assertNotIn")])

    def test_an_embedded_child_program_is_seen(self):
        found = scan_source(_LEAKY_EMBEDDED)
        self.assertEqual(len(found), 1, found)
        self.assertTrue(found[0][3], "an embedded finding must say so")

    def test_a_keys_view_still_renders_values_and_is_refused(self):
        """MEASURED, not assumed: KeysView's repr carries the mapping."""
        found = scan_source(_KEYS_VIEW_IS_NOT_A_CURE)
        self.assertEqual([(f[1], f[2]) for f in found], [(4, "assertNotIn")])

    def test_the_cured_forms_are_clean(self):
        # THE MUST-HIT FIRST, through the same call: an empty finding list means
        # the fixture is clean only once this instrument is shown to fire.
        self.assertEqual(len(scan_source(_LEAKY_FIXTURE)), 1)
        self.assertEqual(scan_source(_CURED_FIXTURE), [])

    def test_a_materialized_items_or_values_view_is_refused(self):  # noqa: VACUOUS_ASSERTION — the unconditional three-finding must-hit is the FIRST statement of this arm, through the same scan_source door; the rung cannot link it because each scan_source call mints a fresh producer identity
        """tuple/sorted/set/list cure a KEY iteration, never an items one."""
        found = scan_source(_VALUES_MATERIALIZED_IS_NOT_A_CURE)
        self.assertEqual([(f[1], f[2]) for f in found],
                         [(4, "assertEqual"), (5, "assertEqual"),
                          (7, "assertCountEqual")], found)
        # MUST STAY QUIET: the same call names over keys, so the refusal above
        # is the items/values distinction and not the materializer itself.
        self.assertEqual(scan_source(_KEYS_MATERIALIZED_IS_THE_CURE), [])

    def test_every_target_of_a_chained_assignment_is_tainted(self):  # noqa: VACUOUS_ASSERTION — the unconditional two-finding must-hit is the FIRST statement of this arm; the quiet control's separate scan_source call is a fresh producer identity the rung cannot link
        found = scan_source(_CHAINED_ASSIGNMENT % "os.environ")
        self.assertEqual([(f[1], f[2]) for f in found],
                         [(5, "assertNotIn"), (6, "assertNotIn")], found)
        # The SECOND name is the one a `targets[0]` read left clean. Quiet
        # control: the same chained shape over a synthetic mapping, so what the
        # scan answers to is the right-hand side.
        self.assertEqual(scan_source(_CHAINED_ASSIGNMENT % "{'PATH': '/bin'}"),
                         [])

    def test_an_augmented_assignment_never_clears_a_taint(self):  # noqa: VACUOUS_ASSERTION — the unconditional one-finding must-hit is the FIRST statement of this arm; the quiet control's separate scan_source call is a fresh producer identity the rung cannot link
        found = scan_source(_AUGMENTED_ASSIGNMENT % "dict(os.environ)")
        self.assertEqual([(f[1], f[2]) for f in found],
                         [(6, "assertEqual")], found)
        # `|=` MUTATES: every ambient entry survives it. Quiet control: the same
        # augmented statement over an untainted mapping stays untainted, so the
        # rule added no taint of its own.
        self.assertEqual(
            scan_source(_AUGMENTED_ASSIGNMENT % "{'PATH': '/bin'}"), [])

    def test_a_rebinding_assignment_inspects_its_own_right_hand_side(self):
        """The assertion evaluates BEFORE the name it rebinds is replaced."""
        found = scan_source(_REBIND_FROM_ITS_OWN_ASSERTION)
        self.assertEqual([(f[1], f[2]) for f in found],
                         [(5, "assertEqual")], found)

    def test_an_embedded_producer_only_program_is_seen(self):  # noqa: VACUOUS_ASSERTION — the absence asserted is a property of this arm's own fixture TEXT, and the unconditional positive control on the scan is the one-finding assertEqual two statements below it
        """THE INTERSECTION of the embedded and producer paths: a child program
        that never spells `os.environ` and renders it through a producer."""
        text = _LEAKY_EMBEDDED_PRODUCER_ONLY
        self.assertNotIn("os.environ", text,
                         "this control is only about the intersection while it "
                         "carries no literal os.environ anywhere")
        found = scan_source(text)
        self.assertEqual(len(found), 1, found)
        self.assertEqual(found[0][2], "assertEqual")
        self.assertTrue(found[0][3], "an embedded finding must say so")

    def test_a_keyword_call_is_the_same_shape(self):  # noqa: VACUOUS_ASSERTION — the unconditional two-finding must-hit is the FIRST statement of this arm; the quiet control's separate scan_source call is a fresh producer identity the rung cannot link
        found = scan_source(_LEAKY_KEYWORD_FORM)
        self.assertEqual([(f[1], f[2]) for f in found],
                         [(4, "assertNotIn"), (5, "assertEqual")], found)
        # MUST STAY QUIET, including the mixed form (positional member, keyword
        # container): the keyword binding reads the argument, it does not assume
        # a keyword call is guilty.
        self.assertEqual(scan_source(_CURED_KEYWORD_FORM), [])

    def test_a_rebind_drops_the_taint(self):
        tainted = ("import os\n"
                   "def t(self):\n"
                   "    env = dict(os.environ)\n"
                   "%s"
                   "    self.assertNotIn('K', env)\n")
        # control: WITHOUT the rebind the same source is flagged, so the empty
        # result below is the rebind and not a scan that stopped looking.
        self.assertEqual(len(scan_source(tainted % "")), 1)
        self.assertEqual(scan_source(tainted % "    env = {'PATH': '/bin'}\n"),
                         [])


class TestsTreeRendersNoEnvironmentValuesTest(unittest.TestCase):
    def test_no_assertion_in_tests_renders_the_ambient_environment(self):  # noqa: VACUOUS_ASSERTION — the planted-tree must-hit two statements above IS the unconditional control on this door, and the rung cannot link it because each scan_tests_tree call mints a fresh producer identity
        # MUST-HIT THROUGH THE SAME DOOR. scan_tests_tree() walks a directory
        # and filters names; an empty result can mean a clean tree, a filter
        # that matched nothing, or an unreadable root. Plant the shape in a
        # temporary tree and require the door to find it before believing the
        # real tree's answer.
        # THREE PLANTS, ONE PER WAY THE SUITE REACHES A MODULE the old
        # immediate-`test_*.py` listing could not: a bare `test*.py` name and a
        # module inside a discoverable package are both run by
        # `unittest discover -s tests` and were both skipped.
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "pkg"))
            for rel in ("test_planted_leak.py", "testleak.py",
                        os.path.join("pkg", "__init__.py"),
                        os.path.join("pkg", "test_nested_leak.py"),
                        "not_a_test.py"):
                with open(os.path.join(tmp, rel), "w", encoding="utf-8") as fh:
                    fh.write("" if rel.endswith("__init__.py")
                             else _LEAKY_FIXTURE)
            planted = scan_tests_tree(root=tmp)
        self.assertEqual(sorted(f[0] for f in planted),
                         ["tests/pkg/test_nested_leak.py",
                          "tests/test_planted_leak.py", "tests/testleak.py"],
                         "the tree door must find a planted shape at every "
                         "depth and spelling the suite discovers, and must "
                         "read the " + DISCOVERY_PATTERN + " population alone")
        found = scan_tests_tree(skip=(__file__,))
        self.assertEqual(
            [], found,
            "an assertion failure here would print every environment VALUE "
            "into the unittest report and from there into the fab artifact. "
            "Assert against tuple(...) of the mapping, or compare one value "
            "with a message naming only the KEY. Sites: "
            + ", ".join("%s:%d %s" % (f[0], f[1], f[2]) for f in found))

    def test_exactly_this_module_is_exempt(self):
        """The exemption is one file wide, and it is this one."""
        mine = os.path.basename(__file__)
        population = [os.path.relpath(p, HERE).replace(os.sep, "/")
                      for p in discoverable_modules()]
        self.assertIn(mine, population)
        with open(__file__, encoding="utf-8") as fh:
            own = scan_source(fh.read(), "tests/" + mine)
        self.assertGreater(len(own), 0,
                           "the controls must live in this file, or the "
                           "exemption is hiding nothing and should go")
        # ONE FILE WIDE, MEASURED OVER THE WHOLE DISCOVERED POPULATION rather
        # than asserted: drop the skip and the only module that answers is this
        # one. A second module's name appearing here is either a new leak or an
        # exemption that grew.
        self.assertEqual({f[0] for f in scan_tests_tree()},
                         {"tests/" + mine})


if __name__ == "__main__":
    unittest.main()
