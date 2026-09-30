#!/usr/bin/env python3
"""THE PRECONDITION OF THE HOOK CHILD'S `-S`: helm needs nothing the site
stage provides.

MOVED OUT OF `tests/test_hook_wrapper.py`, where it was the one class that
walks the tree: `helm gate audits` runs every test module that reads the
tree, and carrying the walk there meant running the wrapper's process-driving
arms with it. The class and `_NEWER_STDLIB` are the text they had there. The
arm that helm ships no `.pth` is new: docs/HOOKS.md names three facts and
only two had a pin.
"""
import os
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# STDLIB ON A NEWER PYTHON than some interpreter that reads this tree, and so
# absent from that interpreter's `sys.stdlib_module_names`. Each is imported
# under an ImportError guard for the 3.9 floor. Held stale-free below: a name
# nothing imports any more leaves this list.
_NEWER_STDLIB = {
    "compression": "stdlib from 3.14 (PEP 784); helm/upstream_surface.py "
                   "reads zstd assets with it and reports them unreadable "
                   "below 3.14",
}


class HelmNeedsNothingTheSiteStageProvidesTest(unittest.TestCase):
    """THE PRECONDITION OF `-S`, pinned. The site stage adds site-packages
    and every .pth in them to sys.path, runs sitecustomize/usercustomize, and
    defines the exit/quit/help/copyright/credits/license builtins. helm uses
    none of those: every import anywhere in helm/ or bin/helm, guarded or
    nested, is the standard library or helm itself, and no helm source calls a
    site builtin. Green on the base by nature: this is the fact the floor
    stands on, and it must stay true for as long as the floor does."""

    def _sources(self):
        yield os.path.join(ROOT, "bin", "helm")
        for dirpath, _dirs, files in os.walk(os.path.join(ROOT, "helm")):
            for fn in files:
                if fn.endswith(".py"):
                    yield os.path.join(dirpath, fn)

    def test_every_import_is_stdlib_or_helm_and_no_site_builtin_is_called(self):  # noqa: VACUOUS_ASSERTION — the empty lists are the finding; the walk's own positive is the >1000 imports it read and the exact _NEWER_STDLIB set it found, both asserted unconditionally
        import ast
        if not hasattr(sys, "stdlib_module_names"):
            self.skipTest("python < 3.10 has no stdlib_module_names")
        std = set(sys.stdlib_module_names)
        own = {"helm"} | {fn[:-3] for fn in os.listdir(os.path.join(ROOT, "helm"))
                          if fn.endswith(".py")}
        builtins_of_site = {"exit", "quit", "help", "copyright", "credits",
                            "license"}
        foreign, calls, seen, newer = [], [], 0, set()
        for path in self._sources():
            with open(path, encoding="utf-8") as f:
                tree = ast.parse(f.read(), path)
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom) and not node.level:
                    names = [node.module or ""]
                for name in names:
                    seen += 1
                    top = name.split(".")[0]
                    if top in _NEWER_STDLIB:
                        newer.add(top)
                    elif top not in std | own:
                        foreign.append("%s:%d %s" % (path, node.lineno, name))
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                        and node.func.id in builtins_of_site:
                    calls.append("%s:%d %s()" % (path, node.lineno, node.func.id))
        self.assertGreater(seen, 1000, "MUST-HIT: the walk read no imports")
        self.assertEqual(foreign, [], "a module the -S launch cannot find")
        self.assertEqual(newer, set(_NEWER_STDLIB),
                         "a stale _NEWER_STDLIB entry: nothing imports it")
        self.assertEqual(calls, [], "a builtin only the site stage defines")

    def test_helm_ships_no_pth_file(self):
        """The third fact: the site stage would put a shipped `.pth` on
        sys.path, and `-S` skips it. CONTROL: the listing is the tree's own
        and it names this file."""
        shipped = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT,
                                 capture_output=True, text=True, check=True)
        names = shipped.stdout.split("\0")
        self.assertIn("tests/test_site_stage_precondition.py", names)
        self.assertEqual([n for n in names if n.endswith(".pth")], [])


if __name__ == "__main__":
    unittest.main()
