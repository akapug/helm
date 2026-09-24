"""--help honesty — an unknown verb NEVER exits 0.

Pins the fix that retired the fleet workaround (store prior
help-exit0-masks-unknown-verb): `helm <verb> --help` is a trustworthy
existence probe again — unknown verbs refuse with exit 2 on stderr, known
verbs print their own usage line, and only the bare/global forms dump the
verb table. Plus one honesty test per swept sub-dispatcher that used to
swallow unknown subverbs and run its default action with exit 0.
"""
import contextlib
import io
import os
import tempfile
import unittest

import os as _os, sys as _sys  # noqa: E402
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
from tests._tmphome import home as _tmp_home  # noqa: E402
_tmp_home(prefix="helm-test-home-", var="HELM_HOME")

from helm import cli  # noqa: E402


def _run(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = cli.main(argv)
    return rc, out.getvalue(), err.getvalue()


class CliHelpHonestyTest(unittest.TestCase):
    def test_unknown_verb_with_help_exits_2(self):
        # THE regression: --help after an unknown verb must NOT fall through
        # to the global usage with exit 0 (the false existence probe).
        rc, out, err = _run(["frobnicate", "--help"])
        self.assertEqual(rc, 2)
        self.assertIn("unknown verb 'frobnicate'", err)
        self.assertNotIn("usage: helm <verb>", out)

    def test_unknown_verb_bare_exits_2_with_suggestion(self):
        rc, _, err = _run(["projcts"])
        self.assertEqual(rc, 2)
        self.assertIn("unknown verb 'projcts'", err)
        self.assertIn("did you mean 'projects'?", err)

    def test_known_verb_help_prints_verb_usage_not_global(self):
        rc, out, _ = _run(["store", "--help"])
        self.assertEqual(rc, 0)
        self.assertIn("store list|get", out)
        self.assertNotIn("steering station", out)

    def test_dispatch_help_renders_force_on_the_send_synopsis(self):
        rc, out, err = _run(["dispatch", "--help"])
        self.assertEqual(rc, 0, err)
        rendered = out + err
        send_help = rendered.split("|add", 1)[0]
        self.assertIn("dispatch send", send_help)
        self.assertIn("[--force]", send_help)

    def test_global_help_forms_exit_0(self):
        for argv in ([], ["--help"], ["-h"], ["help"]):
            rc, out, _ = _run(argv)
            self.assertEqual(rc, 0, argv)
            self.assertIn("usage: helm <verb>", out)


class DispatcherHonestyTest(unittest.TestCase):
    """Each swept dispatcher refuses an unknown subverb/arg with exit 2
    instead of silently running its default action."""

    def _refuses(self, argv, needle):
        rc, _, err = _run(argv)
        self.assertEqual(rc, 2, argv)
        self.assertIn(needle, err)

    def test_skills_unknown_subverb(self):
        self._refuses(["skills", "frobnicate"], "unknown verb 'frobnicate'")

    def test_creds_unknown_subverb(self):
        self._refuses(["creds", "frobnicate"], "unknown verb 'frobnicate'")

    def test_tidy_unknown_arg(self):
        self._refuses(["tidy", "frobnicate"], "unknown arg 'frobnicate'")

    def test_watchdog_unknown_arg(self):
        self._refuses(["watchdog", "frobnicate"], "unknown arg 'frobnicate'")

    def test_lr_help_names_every_terminal_annotation(self):  # noqa: VACUOUS_ASSERTION — paired positive control binds a real row/event
        rc, out, err = _run(["lr", "--help"])
        self.assertEqual(rc, 0, err)
        for verb in ("discharge", "withdraw", "abandon", "close-landed"):
            self.assertIn(verb, out)


if __name__ == "__main__":
    unittest.main()
