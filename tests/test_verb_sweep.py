#!/usr/bin/env python3
"""EVERY verb, swept — the per-verb smoke that 0.2 was missing.

`tests/test_cli_help.py` spot-checks a handful of verbs (unknown-verb honesty,
one known verb, the global forms, four dispatcher cases). Nothing walked the
WHOLE table, so a verb could join `cli.VERBS`, dispatch correctly, pass 3200
tests — and be invisible to every human and agent who ever typed `helm --help`.

FOUND BY THIS SWEEP ON ITS FIRST RUN (2026-07-24), which is the argument for it:
`watchdog` was a real, working, tested verb (test_cli_help even had a case for
it) that rendered as a BLANK LINE in `helm --help` and as the bare string
"helm watchdog" under `helm watchdog --help`. Mechanism, and it is a CLASS not an
instance: a verb registered through `_lazy()` has no `__doc__` on the wrapper, so
both render paths (cli.py root-help loop and the help-first short-circuit) fall
back through `_VERB_HELP.get(name) or fn.__doc__ or name` to nothing at all. ANY
future `_lazy` verb without a `_VERB_HELP` entry inherits exactly this, silently.

WHY A TEST AND NOT A SHELL SMOKE: `helm <verb> --help` is intercepted at the ROOT
before dispatch (cli.py), so it is read-only for every verb by construction — no
allowlist, no skip-list, and therefore no silent gap in coverage to maintain.
Being in the suite means it gates every land, not just a release.

The exit-code half is deliberately NOT the whole assertion. A verb that exits 0
and prints nothing is the SILENT-BREAK class — worse than an honest failure,
because nothing surfaces — so every check below also demands non-empty output
that NAMES the verb it was asked about.
"""
import os
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from helm import cli                                            # noqa: E402


def _run(args):
    """Drive the real CLI in a child so the assertions cover what a user gets,
    not what an in-process call returns."""
    p = subprocess.run(
        [sys.executable, "-c",
         "import sys; sys.path.insert(0, %r); from helm import cli; "
         "sys.exit(cli.main(sys.argv[1:]))" % ROOT] + list(args),
        capture_output=True, text=True, cwd=ROOT, timeout=60)
    return p.returncode, p.stdout, p.stderr


class EveryVerbHasDiscoverableHelpTest(unittest.TestCase):
    """Source-driven off `cli.VERBS`: a new verb is covered with NO edit here.
    Reading the authoritative table rather than parsing `helm --help` is the
    point — a sweep that trusted the help output could not see a verb the help
    output omits, which is the exact defect being pinned."""

    def test_every_verb_help_exits_0_and_says_something(self):
        silent, failed = [], []
        for verb in cli.VERBS:
            rc, out, err = _run([verb, "--help"])
            text = (out + err).strip()
            if rc != 0:
                failed.append((verb, rc, text[:120]))
            elif len(text) <= len("helm " + verb):
                # a bare echo of the name carries no more information than the
                # word the caller already typed
                silent.append((verb, text))
        self.assertEqual(failed, [], "verbs whose --help did not exit 0: %r"
                         % (failed,))
        self.assertEqual(
            silent, [],
            "verbs whose --help printed nothing beyond their own name — the "
            "SILENT class, and for a _lazy() verb the cause is a missing "
            "_VERB_HELP entry: %r" % (silent,))

    def test_every_verb_help_names_the_verb_it_was_asked_about(self):
        """Guards against a verb rendering ANOTHER verb's usage — a
        misrouting that a non-empty check alone would pass."""
        wrong = []
        for verb in cli.VERBS:
            _rc, out, err = _run([verb, "--help"])
            if verb not in (out + err):
                wrong.append((verb, (out + err).strip()[:120]))
        self.assertEqual(wrong, [], "help text that does not name its own "
                                    "verb: %r" % (wrong,))

    def test_every_verb_appears_in_the_root_help_listing(self):
        """DISCOVERABILITY: dispatching correctly is not enough — a verb no one
        can find is a verb no one uses. This is the assertion `watchdog` failed,
        and it generalises the same finding codex-3 made against council's
        umbrella help to the entire CLI surface."""
        rc, out, err = _run(["--help"])
        self.assertEqual(rc, 0, err)
        listing = out + err
        # match the entry-WORD at the start of a listing line, so a verb merely
        # mentioned inside some other verb's prose does not count as listed
        listed = set()
        for line in listing.splitlines():
            head = line.strip().split(" ")[0].strip()
            for sep in ("|", "["):
                head = head.split(sep)[0]
            if head:
                listed.add(head)
        missing = sorted(v for v in cli.VERBS if v not in listed)
        self.assertEqual(missing, [], "verbs in cli.VERBS that `helm --help` "
                                      "never lists: %r" % (missing,))

    def test_the_root_help_listing_has_no_blank_entries(self):
        """The exact SHAPE of the watchdog defect: the root loop prints one line
        per verb, so a verb with no resolvable help printed an empty line rather
        than failing. An empty line is a verb that exists and says nothing."""
        rc, out, _err = _run(["--help"])
        self.assertEqual(rc, 0)
        body = out.split("usage: helm <verb> [args]", 1)[-1]
        blanks = [i for i, line in enumerate(body.splitlines())
                  if line.startswith("  ") and not line.strip()]
        self.assertEqual(blanks, [], "blank verb entries in `helm --help` at "
                                     "body lines %r — a registered verb with "
                                     "no help resolves to an empty line" % blanks)

    def test_an_unknown_verb_is_still_refused_under_help(self):
        """The sweep must not have loosened the existence-probe contract that
        test_cli_help pins: `helm <bogus> --help` exits 2, never 0."""
        rc, _out, err = _run(["definitely-not-a-verb", "--help"])
        self.assertEqual(rc, 2)
        self.assertIn("unknown verb", err)


if __name__ == "__main__":
    unittest.main()
