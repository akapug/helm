"""EVERY SYSTEMCTL CALL IS BOUNDED AND ITS TIMEOUT HANDLED (task/3254).

A `systemctl --user` that never returns (a wedged user manager, a stopped bus) used to hang the timer installers
with no deadline. This walks every helm/*.py file, not a list of modules: in each function that names systemctl, every
subprocess.run / call / check_call / check_output must pass timeout=, and must sit in the body of a try whose
handler catches subprocess.TimeoutExpired (or a base of it), so a timeout reports a failure instead of a traceback.
Red at 008e1fed7d8 with 14 findings: 13 calls with no timeout, and releasenightly's timeout=60 with no handler.

Since task/3307 the timer installers run systemctl through ONE function, timerhealth.install_user_timer, and write
under ONE directory, timerhealth.user_unit_dir(). The second arm refuses a module that joins that directory for
itself again, which is how nineteen copies of it came to exist.
"""
import ast
import os
import pathlib
import shutil
import tempfile
import unittest

ROOT = pathlib.Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CALLS = ("run", "call", "check_call", "check_output")
CATCHES = ("TimeoutExpired", "SubprocessError", "Exception", "BaseException")


def _names_systemctl(node):
    return any(isinstance(n, ast.Constant) and isinstance(n.value, str) and "systemctl" in n.value
               or isinstance(n, ast.Name) and "systemctl" in n.id for n in ast.walk(node))


def _catches_timeout(handler):
    types = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    return handler.type is None or any(getattr(t, "attr", getattr(t, "id", None)) in CATCHES for t in types)


def _handled(call, parents):
    node = call
    while node in parents:
        child, node = node, parents[node]
        if isinstance(node, ast.Try) and child in node.body and any(map(_catches_timeout, node.handlers)):
            return True
    return False


def census(root=ROOT):
    """-> (["helm/x.py function"] for each systemctl call checked, ["helm/x.py:LINE function: why"] for each
    unbounded or unhandled one)."""
    checked, out = [], []
    for path in sorted((root / "helm").glob("*.py")):
        source = path.read_text(encoding="utf-8")
        if "systemctl" not in source:  # every name or string the walk matches is in the text
            continue
        tree = ast.parse(source, filename=str(path))
        parents = {c: n for n in ast.walk(tree) for c in ast.iter_child_nodes(n)}
        calls = {}  # a nested function is walked inside its parent too: count each call once
        for fn in ast.walk(tree):
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) and _names_systemctl(fn):
                for c in ast.walk(fn):
                    if (isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute) and c.func.attr in CALLS
                            and isinstance(c.func.value, ast.Name) and c.func.value.id == "subprocess"):
                        calls.setdefault(c, fn.name)
        checked += ["helm/%s %s" % (path.name, name) for name in calls.values()]
        for c, name in sorted(calls.items(), key=lambda kv: kv[0].lineno):
            why = ("no timeout=" if not any(k.arg == "timeout" for k in c.keywords)
                   else None if _handled(c, parents) else "TimeoutExpired not caught")
            if why:
                out.append("helm/%s:%d %s: %s" % (path.name, c.lineno, name, why))
    return checked, out


def unit_dir_joins(root=ROOT):
    """-> ["helm/x.py:LINE"] for every call outside timerhealth.py whose literal arguments name "systemd" then
    "user": the os.path.join every installer spelled its unit directory with before task/3307, and the one a new
    installer would copy. Prose that names the directory is not a call and is not counted."""
    out = []
    for path in sorted((root / "helm").glob("*.py")):
        source = path.read_text(encoding="utf-8")
        if path.name == "timerhealth.py" or "systemd" not in source:  # a matching call spells it in the text
            continue
        for node in ast.walk(ast.parse(source, filename=str(path))):
            if isinstance(node, ast.Call):
                words = [a.value for a in node.args if isinstance(a, ast.Constant)]
                if any(words[i:i + 2] == ["systemd", "user"] for i in range(len(words))):
                    out.append("helm/%s:%d" % (path.name, node.lineno))
    return out


class SystemctlTimeoutsTest(unittest.TestCase):

    def test_every_systemctl_call_is_bounded_and_handled(self):
        checked, bad = census()
        self.assertEqual(bad, [], "systemctl calls that can hang or crash (%d):\n  %s"
                         % (len(bad), "\n  ".join(bad)))
        # THE SHARED INSTALLER IS AMONG THEM, and with it every timer installer's calls: a census that found
        # nothing, or everything but the one function the installers share, would pass vacuously.
        self.assertIn("helm/timerhealth.py install_user_timer", checked)
        self.assertGreaterEqual(len(checked), 6)

    def test_only_timerhealth_joins_the_user_unit_directory(self):
        """A twentieth copy of ~/.config/systemd/user writes where HELM_USER_UNIT_DIR, the operator's setting
        and the suite's plant, does not reach. POSITIVE CONTROL: the same scan finds the join in a planted tree,
        and passes over the docstring and the comment that name the directory."""
        self.assertEqual(unit_dir_joins(), [], "ask timerhealth.user_unit_dir() for the directory")
        planted = pathlib.Path(tempfile.mkdtemp(prefix="helm-test-unitdir-"))
        self.addCleanup(shutil.rmtree, str(planted), True)
        (planted / "helm").mkdir()
        (planted / "helm" / "x.py").write_text(
            '"""Units go to ~/.config/systemd/user."""\n'
            "# and so says this comment: ~/.config/systemd/user\n"
            'UDIR = os.path.join(HOME, ".config", "systemd", "user")\n', encoding="utf-8")
        self.assertEqual(unit_dir_joins(planted), ["helm/x.py:3"])


if __name__ == "__main__":
    unittest.main()
