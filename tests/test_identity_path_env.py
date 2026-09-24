#!/usr/bin/env python3
"""The bounded identity-estate path declaration and suite isolation."""
import ast
import json
import os
import subprocess
import sys
import tempfile
import unittest

from helm import pathenv


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TARGETS = {
    ("helm/actors.py", "store_path"),
    ("helm/chat.py", "chat_dir"),
    ("helm/home.py", "helm_home"),
}


def _call_name(node):
    if isinstance(node, ast.Attribute):
        return node.attr
    return getattr(node, "id", None)


def identity_path_names():
    """({logical resolver names}, {target functions actually found}).

    Walk the source tree so moving one of the three authority functions does
    not make the arm silently stop seeing it, but inspect ONLY those named
    functions.  This is intentionally not a registry of every environment read
    in Helm.
    """
    names, found = set(), set()
    source = os.path.join(ROOT, "helm")
    for directory, _subdirs, files in os.walk(source):
        for filename in files:
            if not filename.endswith(".py"):
                continue
            path = os.path.join(directory, filename)
            rel = os.path.relpath(path, ROOT)
            with open(path, encoding="utf-8") as fh:
                tree = ast.parse(fh.read(), filename=rel)
            for node in tree.body:
                target = (rel, node.name) if isinstance(node, ast.FunctionDef) \
                    else None
                if target not in TARGETS:
                    continue
                found.add(target)
                for call in ast.walk(node):
                    if not isinstance(call, ast.Call) or not call.args:
                        continue
                    if _call_name(call.func) not in ("env", "surface_dir"):
                        continue
                    first = call.args[0]
                    if isinstance(first, ast.Constant) \
                            and isinstance(first.value, str):
                        names.add(first.value)
    return names, found


class IdentityPathDeclarationAuditTest(unittest.TestCase):
    def test_the_three_path_consumers_are_the_whole_shared_set(self):
        names, found = identity_path_names()
        self.assertEqual(found, TARGETS,
                         "control: the audit must find all three path consumers")
        self.assertEqual(names, {"HOME", "CHAT_DIR", "ACTORS"},
                         "control: the targeted readers must remain visible")
        expanded = {prefix + name for name in names
                    for prefix in ("HELM_", "MELD_")}
        self.assertEqual(expanded, set(pathenv.IDENTITY_PATH_ENV_KEYS))
        self.assertEqual(len(pathenv.IDENTITY_PATH_ENV_KEYS), 6,
                         "this must not grow into a general env registry")


class SuiteBootstrapActorIsolationTest(unittest.TestCase):
    CHILD = r"""
import json, os
import tests
from helm import actors, home
row, err = actors.bind("suite-bootstrap-actor")
if err:
    raise SystemExit(err)
print(json.dumps({
    "actor": row["canonical_name"],
    "home": home.helm_home(),
    "store": actors.store_path(),
    "store_exists": os.path.exists(actors.store_path()),
    "helm_actor": os.environ.get("HELM_ACTORS"),
    "meld_actor": os.environ.get("MELD_ACTORS"),
    "declared": list(tests.IDENTITY_PATH_ENV_KEYS),
}))
"""

    def test_parent_actor_override_and_no_override_both_write_only_under_test_home(self):  # noqa: VACUOUS_ASSERTION — the literal three-case loop always runs, and every case asserts store_exists after the child commits
        """The reproducer: inherited absolute target versus no target.

        Both HELM and legacy MELD spellings drive the inherited-target variant.
        The child really binds an Actor and reports the store while it exists;
        unchanged parent bytes therefore prove the write was redirected rather
        than merely refused.
        """
        for inherited in (None, "HELM_ACTORS", "MELD_ACTORS"):
            with self.subTest(inherited=inherited), \
                    tempfile.TemporaryDirectory() as tmp:
                parent = os.path.join(tmp, "parent-actors.json")
                original = json.dumps(
                    {"v": 1, "actors": {}, "by_name": {}},
                    sort_keys=True).encode("utf-8")
                with open(parent, "wb") as fh:
                    fh.write(original)

                env = dict(os.environ)
                for key in pathenv.IDENTITY_PATH_ENV_KEYS:
                    env.pop(key, None)
                env["TMPDIR"] = tmp
                if inherited:
                    env[inherited] = parent
                run = subprocess.run(
                    [sys.executable, "-c", self.CHILD], cwd=ROOT, env=env,
                    capture_output=True, text=True, timeout=30)
                self.assertEqual(run.returncode, 0, run.stderr)
                observed = json.loads(run.stdout)
                self.assertEqual(observed["actor"], "suite-bootstrap-actor")
                self.assertTrue(observed["store_exists"],
                                "control: the child must actually commit a store")
                self.assertEqual(observed["store"],
                                 os.path.join(observed["home"], "_global",
                                              ".state", "actors.json"))
                self.assertIsNone(observed["helm_actor"])
                self.assertIsNone(observed["meld_actor"])
                self.assertEqual(set(observed["declared"]),
                                 set(pathenv.IDENTITY_PATH_ENV_KEYS))
                self.assertNotEqual(observed["store"], parent)
                with open(parent, "rb") as fh:
                    self.assertEqual(
                        fh.read(), original,
                        "a test child mutated its parent Actor authority")


if __name__ == "__main__":
    unittest.main()
