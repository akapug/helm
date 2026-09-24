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
from unittest import mock

BIN = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "bin", "helm")


def _fixture_guard(home):
    """A real executable for the one REQUIRED external spec helm does not ship.

    Pinned per-run rather than left to the host's PATH: unpinned, these
    subprocesses resolve the guard on the owner's box and not in CI, so the
    contract under test — and now the exit code, since a shortened write is
    nonzero — would differ between the two."""
    p = os.path.join(home, "fab-suite-pretooluse")
    with open(p, "w") as f:
        f.write("#!/bin/sh\nexit 0\n")
    os.chmod(p, 0o755)
    return p


def run(args, root, home, **extra_env):
    env = dict(os.environ, HELM_CONFIG_ROOTS=root, HOME=home,
               HELM_HOME=os.path.join(home, ".helm"),
               HELM_SUITE_GUARD=_fixture_guard(home))
    env.update(extra_env)
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

    def _install(self, extra=(), **extra_env):
        return run(["hooks", "install", "--project", self.proj] + list(extra),
                   self.tmp, self.home, **extra_env)

    def _fake_external(self):
        """An executable file the EXTERNAL spec's env pin can resolve to, so
        an arm can exercise the written-guard path on a host that does not
        carry the real binary (the fab hosts do not)."""
        path = os.path.join(self.tmp, "fake-suite-guard")
        with open(path, "w") as f:
            f.write("#!/bin/sh\nexit 0\n")
        os.chmod(path, 0o755)
        return path

    def test_fresh_install_writes_every_spec_and_permits_no_estate_defaults(self):
        r = self._install()
        self.assertEqual(r.returncode, 0, r.stderr)
        s = json.load(open(self.sp))
        from helm.hooks import (SPECS, ESTATE_DEFAULTS, _lane_live,
                                _permits_live, resolved_specs)
        # RESOLVED, not raw: an EXTERNAL spec whose binary this host lacks is
        # deliberately NOT written (a hook pointing at nothing would arm the
        # MISSING alarm on every Bash call) and is reported by
        # `unresolved_externals` instead. The sibling arm below pins the
        # other half — a resolvable external IS written — so narrowing here
        # cannot hide a spec that simply stopped installing.
        live = resolved_specs(SPECS)
        self.assertTrue(live, "resolved_specs filtered every spec away")
        for spec in live:
            self.assertTrue(_lane_live(s, spec),
                            "spec not live after install: " + spec["name"])
        self.assertTrue(_permits_live(s), "beacon permits missing")
        # A project file is neither a home nor a seat — helm does not own a
        # project's scalar settings, so no ESTATE_DEFAULT may be seeded.
        for k in ESTATE_DEFAULTS:
            self.assertNotIn(k, s, "estate default leaked into project scope")

    def test_a_resolvable_external_guard_is_written_into_the_project(self):
        """The other half of the narrowing above: when the external DOES
        resolve, install writes it like any other spec. MUTATION: drop the
        external branch from the install path and this goes red while the
        arm above stays green, which is exactly why both exist."""
        from helm.hooks import SPECS, _lane_live
        ext = [s for s in SPECS if s.get("external")]
        self.assertTrue(ext, "no EXTERNAL spec to exercise")
        fake = self._fake_external()
        # HELM_-prefixed: `external_bin` reads the pin through `home.env`,
        # which prepends the namespace — the bare name resolves nothing
        env = {"HELM_" + s["name"].upper().replace("-", "_"): fake
               for s in ext}
        r = self._install(**env)
        self.assertEqual(r.returncode, 0, r.stderr)
        s = json.load(open(self.sp))
        self.assertIn(fake, json.dumps(s), "the resolved path is what landed")
        # THE PIN MUST BE SET IN THIS PROCESS TOO. `_lane_live` recomputes the
        # expected command from `external_bin`, so a parent without the pin
        # resolves a DIFFERENT command than the child wrote and reports a
        # correctly-written guard as missing — measured, and the reason this
        # arm is worth having at all.
        with mock.patch.dict(os.environ, env):
            for spec in ext:
                self.assertTrue(_lane_live(s, spec),
                                "resolvable external not written: "
                                + spec["name"])

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
