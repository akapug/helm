#!/usr/bin/env python3
"""NO TEST REACHES THIS HOST'S SYSTEMD USER MANAGER OR ITS UNIT FILES (task/3306).

A fab job runs with the host's own HOME, XDG_RUNTIME_DIR and session bus, and
the fab hosts are the gate hosts every land depends on. Test runs there wrote
and ENABLED helm timer units whose WorkingDirectory was a fab worktree, so a
nightly canary ran whole suites on a gate host from a recycled checkout. The
guard lives in tests/__init__.py, the one file every runner loads; this module
drives it one cell at a time:

  * an installer run through tests._tmphome.fake_user_systemd writes only
    under its temp HOME and runs only the recording fake, in this process and
    in a child it spawns;
  * a test that forgets the fake is refused LOUDLY, naming the seam, and the
    host's systemctl never runs: the refusal is a BaseException, so the
    installer's own `except OSError` cannot turn it into a quiet failure;
  * every other spelling of the call (an absolute path, a wrapper, a shell
    string, os.system, posix_spawnp, Popen's executable=) is refused the same
    way, because the refusal is an audit hook on the exec itself rather than a
    list of call sites; a command that only MENTIONS systemctl still runs;
  * a write under the host's systemd directories is refused, proved in a
    child whose host home is a temp dir, so a regression writes that temp dir
    and never the real one;
  * a child, which carries no audit hook, inherits a dead user bus, so the
    host's real systemctl cannot reach the manager from it;
  * outside a test process nothing changes: the installer writes its units
    and runs its systemctl. The guard exists only in a process that imported
    the tests package, and the production arm's child reports it never did;
  * HELM_USER_UNIT_DIR, which every installer asks before HOME (task/3307),
    is removed when the suite inherits it, so a value the parent set cannot
    carry an arm's units, or its child's, away from the arm's temp home.

Every stand-in for the HOST's systemctl here is a recording script outside the
suite root, so on a tree without the guard these arms fail by recording a run,
never by reaching the host.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import tests
from tests._tmphome import fake_user_systemd

from helm import gc

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_RELOAD = ["--user", "daemon-reload"]
_ENABLE = ["--user", "enable", "--now", "helm-gc.timer"]


def _host_stand_in(case):
    """A recording systemctl OUTSIDE this process's suite root: the guard must
    treat it as the host's binary, and it is harmless when the guard is
    missing. -> (bindir, path, ran), where `ran` exists once it has run."""
    root = tempfile.mkdtemp(prefix="helm-test-host-systemctl-",
                            dir=os.path.dirname(tests._testroot()))
    case.addCleanup(shutil.rmtree, root, True)
    bindir = os.path.join(root, "bin")
    os.makedirs(bindir)
    ran = os.path.join(root, "ran")
    path = os.path.join(bindir, "systemctl")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("#!/bin/sh\nprintf '%%s\\n' \"$*\" >> '%s'\n" % ran)
    os.chmod(path, 0o755)
    return bindir, path, ran


def _on_path(bindir):
    return mock.patch.dict(os.environ, {
        "PATH": bindir + os.pathsep + os.environ.get("PATH", os.defpath)})


def _refusal(call):
    """What `call` raised, as 'Type: text', or '' when it ran to the end."""
    try:
        call()
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException as exc:    # the refusal is a BaseException by design
        return "%s: %s" % (type(exc).__name__, exc)
    return ""


def _run_outside_the_suite(case, path):
    """Run `path` from a python with no audit hook: the control that the
    stand-in records when it runs, so its silence above is a refusal."""
    code = ("import json, subprocess\n"
            "print(json.dumps(subprocess.run([%r, '--version']).returncode))\n"
            % path)
    case.assertEqual(_child(case, code, dict(os.environ)), 0)


def _child(case, code, env):
    """Run `code` in a fresh python at the repo root. -> the JSON it printed."""
    p = subprocess.run([sys.executable, "-c", code], cwd=REPO, env=env,
                       capture_output=True, text=True, timeout=120)
    case.assertEqual(p.returncode, 0, p.stderr[-2000:])
    return json.loads(p.stdout.strip().splitlines()[-1])


def _tree(root):
    """Every path under `root` with what it is, to compare before and after."""
    out = []
    for base, dirs, files in os.walk(root):
        for name in sorted(dirs + files):
            path = os.path.join(base, name)
            out.append((os.path.relpath(path, root), os.path.islink(path),
                        os.path.isdir(path)))
    return sorted(out)


class FakeSeamTest(unittest.TestCase):
    """A test that installs a timer through the seam."""

    def test_an_installer_through_the_fake_writes_only_under_its_temp_home(self):
        fake = fake_user_systemd(self)
        ok, detail = gc.ensure_timer()
        self.assertTrue(ok, detail)
        spath, _service, tpath, _timer = gc.timer_units()
        self.assertEqual({os.path.dirname(spath), os.path.dirname(tpath)},
                         {fake.unit_dir})
        self.assertEqual(sorted(os.listdir(fake.unit_dir)),
                         ["helm-gc.service", "helm-gc.timer"])
        self.assertEqual(fake.calls(), [_RELOAD, _ENABLE])

    def test_a_child_outside_the_suite_installs_through_the_same_fake(self):
        """The production path, unchanged: a python that never imports the
        tests package runs the installer's real code, which writes its units
        and runs its systemctl, here the fake the child inherited."""
        fake = fake_user_systemd(self)
        code = ("import json, sys\n"
                "from helm import gc\n"
                "ok, detail = gc.ensure_timer()\n"
                "print(json.dumps([ok, detail, 'tests' in sys.modules]))\n")
        ok, detail, imported = _child(self, code, dict(os.environ))
        self.assertTrue(ok, detail)
        self.assertFalse(imported, "the child loaded the tests package")
        self.assertEqual(sorted(os.listdir(fake.unit_dir)),
                         ["helm-gc.service", "helm-gc.timer"])
        self.assertEqual(fake.calls(), [_RELOAD, _ENABLE])


class ForgottenFakeTest(unittest.TestCase):
    """A test that forgets the fake fails loudly, naming the seam."""

    def test_the_hosts_systemctl_is_refused_in_process(self):
        bindir, path, ran = _host_stand_in(self)
        with _on_path(bindir):
            refusal = _refusal(lambda: subprocess.run(
                ["systemctl"] + _RELOAD, capture_output=True, timeout=30))
        self.assertIn("SystemdRefused", refusal)
        self.assertIn("fake_user_systemd", refusal)
        self.assertIn(os.path.realpath(path), refusal)
        self.assertFalse(os.path.exists(ran), "the host's systemctl ran")
        _run_outside_the_suite(self, path)
        self.assertTrue(os.path.exists(ran), "the stand-in cannot record")

    def test_an_installer_that_forgets_the_fake_is_refused_not_failed(self):
        """HOME is moved, so a missing guard writes a temp dir; systemctl is
        not faked. The installer's own `except` must not fold the refusal into
        an (ok, detail) the arm might never read."""
        bindir, path, ran = _host_stand_in(self)
        home = tempfile.mkdtemp(prefix="helm-test-home-")
        self.addCleanup(shutil.rmtree, home, True)
        with _on_path(bindir), mock.patch.dict(os.environ, {"HOME": home}):
            refusal = _refusal(gc.ensure_timer)
        self.assertIn("SystemdRefused", refusal)
        self.assertIn("fake_user_systemd", refusal)
        self.assertFalse(os.path.exists(ran), "the host's systemctl ran")
        _run_outside_the_suite(self, path)
        self.assertTrue(os.path.exists(ran), "the stand-in cannot record")


class EverySpellingTest(unittest.TestCase):
    """A helper that shells out to systemctl directly, bypassing the seam, is
    caught by the audit hook on the exec: there is no call site to miss."""

    def test_every_spelling_of_the_call_is_refused(self):
        bindir, path, ran = _host_stand_in(self)
        spellings = {
            "argv through PATH": lambda: subprocess.run(
                ["systemctl"] + _RELOAD, timeout=30),
            "an absolute path": lambda: subprocess.call(
                [path] + _RELOAD, timeout=30),
            "a wrapper": lambda: subprocess.run(
                ["env", "SYSTEMD_PAGER=cat", "systemctl"] + _RELOAD,
                timeout=30),
            "a shell string": lambda: subprocess.run(
                "systemctl --user daemon-reload", shell=True, timeout=30),
            "sh -c after a separator": lambda: subprocess.run(
                ["sh", "-c", "cd / && systemctl --user daemon-reload"],
                timeout=30),
            "os.system": lambda: os.system("systemctl --user daemon-reload"),
            "posix_spawnp": lambda: os.waitpid(os.posix_spawnp(
                "systemctl", ["systemctl"] + _RELOAD, dict(os.environ)), 0),
            "Popen executable=": lambda: subprocess.Popen(
                ["renamed"] + _RELOAD, executable=path).wait(30),
        }
        with _on_path(bindir):
            for name, call in spellings.items():
                with self.subTest(name):
                    self.assertIn("SystemdRefused", _refusal(call))
        self.assertFalse(os.path.exists(ran), "the host's systemctl ran")
        _run_outside_the_suite(self, path)
        self.assertTrue(os.path.exists(ran), "the stand-in cannot record")

    def test_a_command_that_only_mentions_systemctl_runs(self):
        bindir, path, ran = _host_stand_in(self)
        note = os.path.join(os.path.dirname(bindir), "note")
        with open(note, "w", encoding="utf-8") as fh:
            fh.write("systemctl --user daemon-reload\n")
        with _on_path(bindir):
            grep = subprocess.run(["grep", "-c", "systemctl", note],
                                  capture_output=True, text=True, timeout=30)
            echo = subprocess.run("echo systemctl", shell=True,
                                  capture_output=True, text=True, timeout=30)
        self.assertEqual(grep.stdout.strip(), "1")
        self.assertEqual(echo.stdout.strip(), "systemctl")
        self.assertFalse(os.path.exists(ran))
        _run_outside_the_suite(self, path)
        self.assertTrue(os.path.exists(ran), "the stand-in cannot record")


class HostUnitDirTest(unittest.TestCase):
    """A write under the host's systemd directories is refused.

    Proved in a CHILD test process whose host home is also a temp dir
    (HELM_SUITE_HOST_HOME only ever adds a directory to the refused set), so
    a tree without the guard mutates that temp dir and never the real one.
    """

    def test_every_write_under_the_hosts_unit_dir_is_refused(self):
        home = tempfile.mkdtemp(prefix="helm-test-host-home-")
        self.addCleanup(shutil.rmtree, home, True)
        unit_dir = os.path.join(home, ".config", "systemd", "user")
        wants = os.path.join(unit_dir, "timers.target.wants")
        os.makedirs(wants)
        for name in ("planted.service", "planted.timer"):
            with open(os.path.join(unit_dir, name), "w",
                      encoding="utf-8") as fh:
                fh.write("[Unit]\n")
        before = _tree(home)
        bindir, _path, ran = _host_stand_in(self)
        code = r'''
import json, os, shutil, sys
import tests
from helm import gc, pk
home = os.environ["HOME"]
unit_dir = os.path.join(home, ".config", "systemd", "user")
wants = os.path.join(unit_dir, "timers.target.wants")
def attempt(fn):
    try:
        fn()
    except BaseException as exc:
        return "%s: %s" % (type(exc).__name__, exc)
    return "done"
out = {
    "control": attempt(lambda: pk.atomic_write(
        os.path.join(home, "control.txt"), "x")),
    "installer": attempt(gc.ensure_timer),
    "atomic_write": attempt(lambda: pk.atomic_write(
        os.path.join(unit_dir, "helm-probe.timer"), "[Timer]\n")),
    "open": attempt(lambda: open(
        os.path.join(unit_dir, "helm-probe.service"), "w").close()),
    "symlink": attempt(lambda: os.symlink(
        os.path.join(unit_dir, "planted.timer"),
        os.path.join(wants, "planted.timer"))),
    "rename out": attempt(lambda: os.rename(
        os.path.join(unit_dir, "planted.timer"),
        os.path.join(home, "moved.timer"))),
    "remove": attempt(lambda: os.remove(
        os.path.join(unit_dir, "planted.service"))),
    "mkdir": attempt(lambda: os.mkdir(
        os.path.join(unit_dir, "planted.timer.d"))),
    "rmtree": attempt(lambda: shutil.rmtree(wants)),
}
print(json.dumps(out))
'''
        env = dict(os.environ, HOME=home, HELM_SUITE_HOST_HOME=home,
                   PATH=bindir + os.pathsep + os.environ.get("PATH", ""))
        out = _child(self, code, env)
        self.assertEqual(out.pop("control"), "done")
        self.assertTrue(os.path.isfile(os.path.join(home, "control.txt")))
        for op, outcome in sorted(out.items()):
            with self.subTest(op):
                self.assertTrue(outcome.startswith("SystemdRefused: "), outcome)
                self.assertIn("fake_user_systemd", outcome)
        self.assertIn(os.path.join(home, ".config", "systemd"),
                      out["atomic_write"])
        self.assertEqual(_tree(home), before + [("control.txt", False, False)])
        self.assertFalse(os.path.exists(ran), "the host's systemctl ran")


class InheritedUnitDirTest(unittest.TestCase):
    """HELM_USER_UNIT_DIR moves every installer's units (task/3307), so an
    inherited value would outrank every temp HOME an arm moves to, in the
    test process and in each child it runs. tests/__init__.py removes it; an
    arm names its unit directory through fake_user_systemd."""

    def test_an_inherited_unit_dir_reaches_neither_an_arm_nor_its_child(self):
        home = tempfile.mkdtemp(prefix="helm-test-home-")
        self.addCleanup(shutil.rmtree, home, True)
        inherited = os.path.join(home, "inherited-units")
        env = dict(os.environ, HOME=home, HELM_USER_UNIT_DIR=inherited)
        grandchild = ("from helm import gc; print(gc.timer_units()[2])")
        code = ("import json, subprocess, sys\n"
                "import tests\n"
                "from helm import gc\n"
                "child = subprocess.run([sys.executable, '-c', %r],\n"
                "                       capture_output=True, text=True,\n"
                "                       timeout=120)\n"
                "print(json.dumps([gc.timer_units()[2],\n"
                "                  child.stdout.strip(), child.stderr]))\n"
                % grandchild)
        here, below, err = _child(self, code, env)
        want = os.path.join(home, ".config", "systemd", "user",
                            "helm-gc.timer")
        self.assertIn("/.config/systemd/user/helm-gc.timer", here)
        self.assertEqual(here, want)
        self.assertIn("/.config/systemd/user/helm-gc.timer", below, err[-2000:])
        self.assertEqual(below, want)
        # POSITIVE CONTROL, the same environment in a process that never
        # imported the tests package: the variable does move the units, so
        # the answers above are the removal and not a variable nobody reads.
        outside = _child(self, "import json\nfrom helm import gc\n"
                         "print(json.dumps(gc.timer_units()[2]))\n", env)
        self.assertIn("/inherited-units/helm-gc.timer", outside)
        self.assertEqual(outside, os.path.join(inherited, "helm-gc.timer"))


class ChildBusTest(unittest.TestCase):
    """A child carries no audit hook; the environment it inherits is what
    keeps the host's systemctl away from the host's user manager."""

    def test_a_child_running_the_real_systemctl_cannot_reach_the_manager(self):
        systemctl = shutil.which("systemctl")
        private = "/run/user/%d/systemd/private" % os.getuid()
        if not systemctl or not os.path.exists(private):
            self.skipTest("this host runs no systemd user manager to reach")
        code = ("import json, shutil, subprocess, sys\n"
                "p = subprocess.run([shutil.which('systemctl'), '--user', "
                "'show', '-p', 'Version'], capture_output=True, text=True, "
                "timeout=30)\n"
                "print(json.dumps([p.returncode, p.stdout, p.stderr, "
                "'tests' in sys.modules]))\n")
        rc, out, err, imported = _child(self, code, dict(os.environ))
        self.assertFalse(imported, "the child loaded the tests package")
        self.assertNotIn("Version=", out, "the child reached the host's "
                         "user manager")
        self.assertNotEqual(rc, 0, out)
        self.assertRegex(err, "(?i)connect|bus")


class HomeFreezerTest(unittest.TestCase):
    """The fake moves HOME only after every helm module that captures HOME at
    import has captured this process's own home. Otherwise whichever case first
    imports helm.homes under the fake freezes the fake home for the rest of the
    worker, and a later lite mint names that home's CLAUDE.md in whatever slice
    order puts the two cases together."""

    def test_a_freezer_first_imported_under_the_fake_keeps_the_real_home(self):
        import helm
        import helm.homes
        real = os.path.expanduser("~")
        saved = sys.modules.pop("helm.homes")
        self.addCleanup(setattr, helm, "homes", saved)
        self.addCleanup(sys.modules.__setitem__, "helm.homes", saved)
        fake_user_systemd(self)
        self.assertNotEqual(os.path.expanduser("~"), real, "the fake is off")
        import helm.homes as fresh
        self.assertIsNot(fresh, saved, "helm.homes was not re-imported")
        self.assertEqual(fresh.DEFAULTS["claude"], os.path.join(real, ".claude"))

    def test_the_scan_names_the_known_freezers(self):
        from tests._tmphome import home_freezers
        found = home_freezers()
        self.assertGreaterEqual(len(found), 3, found)
        for name in ("helm.homes", "helm.configs._common", "helm.seat_catalog"):
            self.assertIn(name, found)


if __name__ == "__main__":
    unittest.main()
