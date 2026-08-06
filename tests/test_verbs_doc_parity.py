#!/usr/bin/env python3
"""docs/VERBS.md is load-bearing, so it gets a test.

README.md calls it "the authoritative verb surface". Nothing enforced that.
At one point four shipped verbs — `capabilities`, `fleet`, `lr`,
`watchdog` — were absent from it, two of them (`lr`, `fleet`) in daily fleet
use. Nobody wrote a bad doc; verbs simply landed and the doc did not move,
which is what always happens to a promise no test holds.

The next gate on this repo is a HUMAN READING IT END TO END, and the README
points that reader at VERBS.md as complete. A reader who finds a verb we ship
but do not document learns something worse than the gap itself: that our
documentation is not a place to look things up. That is why this test exists
at all — the cost is not four missing paragraphs, it is the reader's trust in
every OTHER page.

The rule is one-directional on purpose. Every real verb MUST appear; the doc
may additionally describe subcommands, aliases and flows that are not
top-level verbs, because a reference is allowed to be richer than a dispatch
table. So this catches the failure that actually happens (ship, forget to
document) without forbidding prose.
"""
import re
import os
import unittest

import os as _os, sys as _sys  # noqa: E402
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
from tests._tmphome import home as _tmp_home  # noqa: E402
_tmp_home(prefix="helm-test-verbsdoc-", var="HELM_HOME")

from helm import cli  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOC = os.path.join(REPO, "docs", "VERBS.md")


def documented_verbs(text):
    """Every verb the doc names — as a `helm <verb>` mention or a heading."""
    return (set(re.findall(r"`helm ([a-z][a-z0-9-]*)", text))
            | set(re.findall(r"^#+\s+`?([a-z][a-z0-9-]*)", text, re.M)))


class VerbsDocParityTest(unittest.TestCase):
    def setUp(self):
        with open(DOC, encoding="utf-8") as f:
            self.text = f.read()

    def test_every_shipped_verb_appears_in_the_reference(self):
        missing = sorted(set(cli.VERBS) - documented_verbs(self.text))
        self.assertEqual(missing, [], (
            "docs/VERBS.md is missing %d shipped verb(s): %s.\n"
            "README.md calls that file the authoritative verb surface, so a "
            "verb absent from it is a promise the repo breaks in front of "
            "whoever reads it. Add an entry (see the file for the house "
            "style) rather than relaxing this test."
            % (len(missing), ", ".join(missing))))

    def test_the_dispatch_table_is_the_source_and_it_is_not_empty(self):
        """Guards the vacuous pass: if cli.VERBS were renamed or emptied, the
        parity check above would trivially succeed while documenting nothing.
        The first version of this test read the table with a regex over
        cli.py's source and silently missed a third of the verbs."""
        self.assertGreater(len(cli.VERBS), 50)
        for v in ("sync", "projects", "store", "chat", "web"):
            self.assertIn(v, cli.VERBS, "%s is a real verb" % v)

    def test_the_four_that_were_missing_stay_documented(self):
        """A named regression, so the specific gap cannot silently reopen."""
        doc = documented_verbs(self.text)
        for v in ("capabilities", "fleet", "lr", "watchdog"):
            self.assertIn(v, doc, "%s regressed out of docs/VERBS.md" % v)

    def test_land_request_terminal_subverbs_stay_documented(self):  # noqa: VACUOUS_ASSERTION — paired positive control binds a real row/event
        for verb in ("discharge", "withdraw", "abandon", "close-landed"):
            self.assertIn(verb, self.text,
                          "docs/VERBS.md lost the lr %s contract" % verb)

    def test_the_README_still_points_at_this_file_as_authoritative(self):
        """The test's premise. If the README stops making the claim, this
        parity rule is arbitrary and should be re-argued, not silently kept."""
        with open(os.path.join(REPO, "README.md"), encoding="utf-8") as f:
            readme = f.read()
        self.assertIn("docs/VERBS.md", readme)
        # whitespace-normalised: the claim is line-wrapped in the README, and
        # the first cut of this assertion failed on its own literal quote
        flat = " ".join(readme.split())
        self.assertIn("authoritative verb surface", flat)


if __name__ == "__main__":
    unittest.main()
