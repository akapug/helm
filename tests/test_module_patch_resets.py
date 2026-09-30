"""Every test module that holds a module-level patcher puts it back on teardown.

The sliced gate's leak audit fails a run when a unit leaves module data other
units can see, and a stopped patcher left bound in a module global is exactly
that (it reads as "module data mutated ... (rebound)"). The audit sees it only
when the slicer schedules a reader of that module after it, so the omission
reads as a flake: the run is red, and the failing tests pass alone. The reset
is the one tests.test_landreq carries (task/3039). This scan fails the omission
on every run, not one run in N.
"""
import ast
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TESTS = os.path.join(ROOT, "tests")


def _module_patchers(tree):
    """Globals started as a patcher in setUpModule (declared `global` there):
    the names a module must bind to None at import and put back on teardown."""
    setup = next((n for n in tree.body if isinstance(n, ast.FunctionDef)
                  and n.name == "setUpModule"), None)
    if setup is None:
        return set()
    declared = {g for n in ast.walk(setup) if isinstance(n, ast.Global)
                for g in n.names}
    started = {n.func.value.id for n in ast.walk(setup)
               if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
               and n.func.attr == "start" and isinstance(n.func.value, ast.Name)}
    return declared & started


def _bound_none_at_import(tree, name):
    return any(isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant)
               and node.value.value is None
               and any(isinstance(t, ast.Name) and t.id == name for t in node.targets)
               for node in tree.body)


def _reset_in_teardown(tree, name):
    teardown = next((n for n in tree.body if isinstance(n, ast.FunctionDef)
                     and n.name == "tearDownModule"), None)
    if teardown is None:
        return False
    return any(isinstance(n, ast.Assign) and isinstance(n.value, ast.Constant)
               and n.value.value is None
               and any(isinstance(t, ast.Name) and t.id == name for t in n.targets)
               for n in ast.walk(teardown))


class ModulePatchResetTest(unittest.TestCase):

    def test_every_started_module_patcher_is_reset_on_teardown(self):
        missing = []
        for name in sorted(os.listdir(TESTS)):
            if not (name.startswith("test") and name.endswith(".py")):
                continue
            with open(os.path.join(TESTS, name), encoding="utf-8") as f:
                tree = ast.parse(f.read(), name)
            for patcher in sorted(_module_patchers(tree)):
                if not (_bound_none_at_import(tree, patcher)
                        and _reset_in_teardown(tree, patcher)):
                    missing.append("%s: %s" % (name, patcher))
        self.assertEqual(missing, [], "these modules start a module-level "
                         "patcher in setUpModule and leave it bound after "
                         "tearDownModule, or never bind it to None at import; "
                         "bind it to None at module level and set it back to "
                         "None in tearDownModule, as tests/test_landreq.py does")

    def test_the_scan_sees_the_leak_it_guards_against(self):
        """Control: the unfixed shape is caught and the fixed one is not."""
        leak = ast.parse(
            "_P = None\n"
            "def setUpModule():\n"
            "    global _P\n"
            "    _P = mock.patch.object(x, 'y', 1)\n"
            "    _P.start()\n"
            "def tearDownModule():\n"
            "    if _P is not None:\n"
            "        _P.stop()\n")
        fixed = ast.parse(
            "_P = None\n"
            "def setUpModule():\n"
            "    global _P\n"
            "    _P = mock.patch.object(x, 'y', 1)\n"
            "    _P.start()\n"
            "def tearDownModule():\n"
            "    global _P\n"
            "    if _P is not None:\n"
            "        _P.stop()\n"
            "    _P = None\n")
        self.assertEqual(_module_patchers(leak), {"_P"})
        self.assertFalse(_reset_in_teardown(leak, "_P"))
        self.assertTrue(_reset_in_teardown(fixed, "_P"))
        # A patcher global that import never bound is the same leak: the scan
        # must see it even though no module-level `= None` names it.
        unbound = ast.parse(
            "def setUpModule():\n"
            "    global _Q\n"
            "    _Q = mock.patch.object(x, 'y', 1)\n"
            "    _Q.start()\n"
            "def tearDownModule():\n"
            "    _Q.stop()\n")
        self.assertEqual(_module_patchers(unbound), {"_Q"})
        self.assertFalse(_bound_none_at_import(unbound, "_Q"))
        self.assertTrue(_bound_none_at_import(fixed, "_P"))


if __name__ == "__main__":
    unittest.main()
