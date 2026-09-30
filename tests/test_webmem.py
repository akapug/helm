#!/usr/bin/env python3
"""helm web's allocator bound (task/3715), hermetic.

The console grew to about 2 GB of RAM plus 1.3-1.9 GB of swap every hour, most
of it in glibc's per-thread arena heaps. Two knobs bound that, and glibc reads
them when a process STARTS, so they have to be where every start of a
`helm web` finds them:
  * in this process's environment, which `os.execv` hands the next image, so a
    re-exec onto a changed tree starts with them;
  * in the life already running, through mallopt(3), for a `helm web` started
    without them;
  * in the helm-web unit, through a drop-in `helm web unit --install` writes
    and never restarts the unit for.
An operator's own value in the process environment wins over helm's in the
first two. In the unit it does not: systemd reads the drop-in after the unit
file, so the drop-in replaces a value set in the unit file itself.
"""
import json
import os
import shlex
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from helm import web_server  # noqa: E402
from tests._tmphome import fake_user_systemd  # noqa: E402

KNOBS = {"MALLOC_ARENA_MAX": "2", "MALLOC_MMAP_THRESHOLD_": "131072"}
M_ARENA_MAX, M_MMAP_THRESHOLD = -8, -3
#: The one question --install asks the user manager about an unchanged drop-in.
SHOW = ["--user", "show", "helm-web.service", "--property=NeedDaemonReload",
        "--value"]


def _webmem():
    from helm import webmem
    return webmem


class ApplyTest(unittest.TestCase):

    def test_absent_knobs_are_set_and_applied_to_the_running_life(self):  # noqa: VACUOUS_ASSERTION — both observables are compared by exact equality to non-empty literals
        env, calls = {}, []
        _webmem().apply(environ=env,
                        mallopt=lambda p, v: calls.append((p, v)) or True)
        self.assertEqual(env, KNOBS)
        self.assertEqual(calls, [(M_ARENA_MAX, 2), (M_MMAP_THRESHOLD, 131072)])

    def test_an_operator_value_wins(self):
        env, calls = {"MALLOC_ARENA_MAX": "4"}, []
        _webmem().apply(environ=env,
                        mallopt=lambda p, v: calls.append((p, v)) or True)
        self.assertEqual(env, {"MALLOC_ARENA_MAX": "4",
                               "MALLOC_MMAP_THRESHOLD_": "131072"})
        self.assertEqual(calls, [(M_ARENA_MAX, 4), (M_MMAP_THRESHOLD, 131072)])

    def test_a_libc_without_mallopt_still_carries_the_env_forward(self):  # noqa: VACUOUS_ASSERTION — both observables are compared by exact equality to non-empty literals
        env = {}
        got = _webmem().apply(environ=env, mallopt=lambda p, v: None)
        self.assertEqual(env, KNOBS)
        self.assertEqual(got, {name: (value, None)
                               for name, value in KNOBS.items()})

    def test_cmd_web_bounds_the_allocator_before_it_binds(self):
        webmem, order = _webmem(), []

        def bind(port):
            order.append("bind")
            raise OSError(98, "Address already in use")
        with mock.patch.object(webmem, "apply",
                               lambda *a, **k: order.append("apply") or {}), \
                mock.patch.object(web_server, "make_server", bind), \
                mock.patch("sys.stderr"):
            self.assertEqual(web_server.cmd_web(["--port", "7690"]), 1)
        self.assertEqual(order, ["apply", "bind"])


class ReexecKeepsTheKnobsTest(unittest.TestCase):
    """The REAL re-exec path (`Follower.reexec`, the one every `helm web`
    follows a land with), in a child interpreter whose command line is swapped
    for one that prints its environment. Only the import check is stubbed."""

    def child(self, bounded):
        code = textwrap.dedent("""
            import os, sys
            sys.path.insert(0, %r)
            from helm import stopfacts_resident as r
            if %r:
                from helm import webmem
                webmem.apply()
            r._preflight = lambda: None
            r.ARGV = [sys.executable, "-c",
                      "import json, os; print(json.dumps("
                      "{k: os.environ.get(k) for k in %r}))"]
            print(r.Follower().reexec("digest"), file=sys.stderr)
            sys.exit(3)
        """) % (ROOT, bounded, sorted(KNOBS))
        tmp = tempfile.TemporaryDirectory(prefix="helm-test-webmem-")
        self.addCleanup(tmp.cleanup)
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("MALLOC_")}
        env["HELM_HOME"] = tmp.name
        p = subprocess.run([sys.executable, "-c", code], env=env, cwd=tmp.name,
                           capture_output=True, text=True, timeout=120)
        self.assertEqual(p.returncode, 0, "the child did not exec: " + p.stderr)
        return json.loads(p.stdout.strip().splitlines()[-1])

    def test_the_env_is_present_after_a_reexec(self):  # noqa: VACUOUS_ASSERTION — the None map is the control arm, and the same exec then returns the non-empty knob values
        # CONTROL FIRST, through the same exec: a process that never bounded
        # itself hands on neither knob, so the instrument can see an absence
        # and the values below are the bound's, not the parent's.
        self.assertEqual(self.child(False), {k: None for k in KNOBS})
        self.assertEqual(self.child(True), KNOBS)


class UnitDropinTest(unittest.TestCase):

    def test_the_dropin_carries_every_knob_for_the_web_unit(self):
        fake = fake_user_systemd(self)
        path, text = _webmem().unit_dropin()
        self.assertEqual(path, os.path.join(fake.unit_dir, "helm-web.service.d",
                                            "allocator.conf"))
        self.assertIn("[Service]\n", text)
        for name, value in KNOBS.items():
            self.assertIn("Environment=%s=%s\n" % (name, value), text)

    def test_install_writes_it_reloads_and_never_restarts(self):  # noqa: VACUOUS_ASSERTION — the recorded systemctl calls are compared by exact equality to a non-empty list, twice
        fake = fake_user_systemd(self)
        ok, detail = _webmem().install()
        self.assertTrue(ok, detail)
        path, text = _webmem().unit_dropin()
        with open(path, encoding="utf-8") as f:
            self.assertEqual(f.read(), text)
        self.assertEqual(fake.calls(), [["--user", "daemon-reload"]])
        # IDEMPOTENT: an unchanged drop-in is not rewritten, and the manager
        # is only asked whether it loaded it (the fake answers nothing, which
        # is not "yes"), never reloaded again
        ok, detail = _webmem().install()
        self.assertTrue(ok, detail)
        self.assertIn("unchanged", detail)
        self.assertEqual(fake.calls(), [["--user", "daemon-reload"], SHOW])

    def test_an_unchanged_dropin_the_manager_has_not_loaded_is_reloaded(self):  # noqa: VACUOUS_ASSERTION — the recorded systemctl calls are compared by exact equality to a non-empty list
        """A reload that failed (no user bus, say) leaves the drop-in written
        and the manager on the old unit. The next --install finds the text
        unchanged; that is not proof the manager loaded it, so it asks, and
        reloads when the manager says it needs one."""
        fake = fake_user_systemd(self)
        path, text = _webmem().unit_dropin()
        os.makedirs(os.path.dirname(path))
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        # the recording fake, answering "yes" to the one question asked
        with open(fake.systemctl, "w", encoding="utf-8") as f:
            f.write("#!/bin/sh\n"
                    "{ printf '%%s\\0' \"$@\"; printf '\\n'; } >> %s\n"
                    "case \"$*\" in *NeedDaemonReload*) echo yes;; esac\n"
                    "exit 0\n" % shlex.quote(fake.log))
        ok, detail = _webmem().install()
        self.assertTrue(ok, detail)
        self.assertEqual(fake.calls(), [SHOW, ["--user", "daemon-reload"]])
        self.assertIn("not loaded", detail)
        with open(path, encoding="utf-8") as f:
            self.assertEqual(f.read(), text)

    def test_the_verb_prints_without_install_and_refuses_an_unknown_flag(self):  # noqa: VACUOUS_ASSERTION — the printed drop-in is asserted present first, and the same path is asserted to exist once --install ran
        fake = fake_user_systemd(self)
        with mock.patch("sys.stdout") as out:
            self.assertEqual(web_server.cmd_web(["unit"]), 0)
        printed = "".join(c.args[0] for c in out.write.call_args_list)
        self.assertIn("Environment=MALLOC_MMAP_THRESHOLD_=131072", printed)
        self.assertEqual(fake.calls(), [], "printing the drop-in ran systemctl")
        self.assertFalse(os.path.exists(_webmem().unit_dropin()[0]))
        with mock.patch("sys.stderr"):
            self.assertEqual(web_server.cmd_web(["unit", "--bogus"]), 2)
        with mock.patch("sys.stdout"):
            self.assertEqual(web_server.cmd_web(["unit", "--install"]), 0)
        self.assertTrue(os.path.exists(_webmem().unit_dropin()[0]))


if __name__ == "__main__":
    unittest.main()
