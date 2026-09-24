#!/usr/bin/env python3
"""helm gate audits: the tree-wide audit list, printed instead of typed."""
import contextlib
import io
import json
import os
import re
import shutil
import sys
import tempfile
import unittest

import os as _os, sys as _sys  # noqa: E402
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
from tests._tmphome import home as _tmp_home  # noqa: E402
_tmp_home(prefix="helm-test-gateaudits-", var="HELM_HOME")

from helm import cli, gateaudits  # noqa: E402

_TREE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _ask(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = cli.main(["gate", "audits"] + argv)
    return rc, out.getvalue(), err.getvalue()


class TheListIsTheTreesOwn(unittest.TestCase):

    def test_every_listed_audit_is_a_file_in_this_tree(self):
        self.assertGreater(len(gateaudits.AUDITS), 20)
        self.assertEqual(gateaudits.missing(_TREE), [])

    def test_the_list_and_the_registries_page_name_the_same_enumerators(self):
        """Two lists of one fact drift unless something holds them. The page
        is read from its own heading to the paragraph after the list, so a
        name added anywhere else on the page does not satisfy this arm."""
        with open(os.path.join(_TREE, "docs", "MODULE_REGISTRIES.md"),
                  encoding="utf-8") as fh:
            page = fh.read()
        # THE ANCHOR MUST NOT CARRY THE COUNT. The heading spells the number
        # in English and was also the `page.index` argument, so the count
        # lived in FOUR places: this literal, the heading prose, the page's
        # own list, and ENUMERATORS. Curating correctly -- adding an audit
        # and updating the heading to match -- then made this arm raise
        # ValueError on a missing anchor instead of failing with a readable
        # diff, which is the drift this lane exists to end. Measured by
        # renaming the heading and watching the index throw.
        #
        # Anchored on the SENTENCE under the heading instead: it states the
        # section's purpose and carries no number, so the page and the list
        # grow together and this arm keeps reporting a real comparison.
        start = page.index("The test modules that enumerate the package")
        stop = page.index("`tests/test_wiring.py`", start)
        named = re.findall(r"`(test_[a-z_0-9]+)`", page[start:stop])
        # UNCONDITIONAL FLOOR FIRST, and it is not decoration. Deriving the
        # count from ENUMERATORS removes the old literal's second job: a
        # hardcoded 22 also asserted the set was NON-EMPTY, so an anchor that
        # matched nothing reddened. Two derived comparisons alone both pass
        # when both sides are empty, which is the tautology this file exists
        # to prevent elsewhere. The floor restores that guarantee without
        # putting the count back.
        self.assertTrue(named, "the page slice named no modules")
        self.assertTrue(gateaudits.ENUMERATORS, "the list is empty")
        self.assertEqual(sorted(named), sorted(gateaudits.ENUMERATORS))
        # The count is DERIVED, never retyped: one place for the number.
        self.assertEqual(len(named), len(gateaudits.ENUMERATORS), named)

    def test_no_audit_is_listed_twice(self):
        self.assertEqual(len(set(gateaudits.AUDITS)), len(gateaudits.AUDITS))


class TheCommandIsPasteable(unittest.TestCase):

    def test_one_line_naming_the_repo_and_every_audit(self):
        rc, out, _err = _ask(["--repo", _TREE])
        self.assertEqual(rc, 0)
        self.assertEqual(len(out.strip().splitlines()), 1, out)
        self.assertTrue(out.startswith(
            "fab test --repo %s -- python3 -m unittest " % _TREE), out[:120])
        for name in gateaudits.AUDITS:
            self.assertIn(" tests.%s" % name, out)

    def test_the_lanes_own_modules_ride_once_each_in_any_spelling(self):
        rc, out, _err = _ask(["--repo", _TREE, "--", "tests.test_gateaudits",
                              "test_gateaudits", "tests/test_gateaudits.py",
                              "tests.test_wiring"])
        self.assertEqual(rc, 0)
        self.assertEqual(out.count("tests.test_gateaudits"), 1, out)
        self.assertEqual(out.count("tests.test_wiring"), 1, out)
        self.assertTrue(out.strip().endswith("tests.test_gateaudits"), out[-80:])

    def test_json_carries_the_same_command(self):
        rc, out, _err = _ask(["--repo", _TREE, "--json"])
        self.assertEqual(rc, 0)
        doc = json.loads(out)
        self.assertEqual(doc["command"], gateaudits.command(_TREE))
        self.assertEqual(doc["modules"], gateaudits.modules())

    def test_a_tree_missing_a_listed_audit_prints_NOTHING_and_fails(self):
        """CONTROL: the real tree, through the same door, prints the line."""
        self.assertEqual(_ask(["--repo", _TREE])[0], 0)
        bare = tempfile.mkdtemp(prefix="helm-test-gateaudits-bare-")
        self.addCleanup(shutil.rmtree, bare, ignore_errors=True)
        os.makedirs(os.path.join(bare, "tests"))
        rc, out, err = _ask(["--repo", bare])
        self.assertEqual((rc, out), (1, ""))
        self.assertIn("names an audit the tree does not have", err)

    def test_a_stray_flag_is_refused_before_anything_prints(self):
        rc, out, err = _ask(["--bogus"])
        self.assertEqual((rc, out), (2, ""))
        self.assertIn("unknown arg '--bogus'", err)


if __name__ == "__main__":
    unittest.main()
