"""helm hooks install --project — the project-scoped estate.

The scoped alternative to a home install: the full spec set lands in
<project>/.claude/settings.local.json, so sessions launched in the project
get the physics and every other session on the machine stays hook-free.

Subprocess-driven: configs' CWD_ROOTS is read at import time (HELM_CONFIG_ROOTS),
so each case runs bin/helm in a child process with the roots pointed at a
scratch tree — the same isolation a real operator's env override gets.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

BIN = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "bin", "helm")


def run(args, root, home):
    env = dict(os.environ, HELM_CONFIG_ROOTS=root, HOME=home,
               HELM_HOME=os.path.join(home, ".helm"))
    return subprocess.run([sys.executable, BIN] + list(args),
                          capture_output=True, text=True, env=env)


class ProjectInstallTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-proj-hooks-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(self.home)
        self.proj = os.path.join(self.tmp, "proj")
        os.makedirs(self.proj)
        self.sp = os.path.join(self.proj, ".claude", "settings.local.json")

    def _install(self, extra=()):
        return run(["hooks", "install", "--project", self.proj] + list(extra),
                   self.tmp, self.home)

    def test_fresh_install_writes_every_spec_and_permits_no_estate_defaults(self):
        r = self._install()
        self.assertEqual(r.returncode, 0, r.stderr)
        s = json.load(open(self.sp))
        from helm.hooks import (SPECS, ESTATE_DEFAULTS, _lane_live,
                                _permits_live)
        for spec in SPECS:
            self.assertTrue(_lane_live(s, spec),
                            "spec not live after install: " + spec["name"])
        self.assertTrue(_permits_live(s), "beacon permits missing")
        # A project file is neither a home nor a seat — helm does not own a
        # project's scalar settings, so no ESTATE_DEFAULT may be seeded.
        for k in ESTATE_DEFAULTS:
            self.assertNotIn(k, s, "estate default leaked into project scope")

    def test_reinstall_is_byte_idempotent_and_reports_ok(self):
        self._install()
        with open(self.sp, "rb") as f:
            before = f.read()
        r = self._install()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("hook up to date", r.stdout)
        with open(self.sp, "rb") as f:
            self.assertEqual(f.read(), before)

    def test_foreign_entries_and_scalar_settings_survive(self):
        os.makedirs(os.path.dirname(self.sp))
        with open(self.sp, "w") as f:
            json.dump({"hooks": {"PostToolUse": [
                {"matcher": "Write",
                 "hooks": [{"type": "command",
                            "command": "my-custom-formatter"}]}]},
                "model": "opus"}, f)
        r = self._install()
        self.assertEqual(r.returncode, 0, r.stderr)
        s = json.load(open(self.sp))
        cmds = [h["command"] for g in s["hooks"]["PostToolUse"]
                for h in g["hooks"]]
        self.assertIn("my-custom-formatter", cmds)
        self.assertEqual(s["model"], "opus")

    def test_project_and_home_scopes_refuse_to_mix(self):
        r = run(["hooks", "install", "--project", self.proj, "--home", "x"],
                self.tmp, self.home)
        self.assertEqual(r.returncode, 2)
        self.assertIn("different scopes", r.stderr)

    def test_missing_project_dir_fails_loud(self):
        r = run(["hooks", "install", "--project",
                 os.path.join(self.tmp, "nope")], self.tmp, self.home)
        self.assertEqual(r.returncode, 1)
        self.assertIn("no such project directory", r.stderr)

    def test_dry_run_writes_nothing(self):
        r = self._install(("--dry",))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(os.path.exists(self.sp),
                         "dry run must not create the settings file")


if __name__ == "__main__":
    unittest.main()
