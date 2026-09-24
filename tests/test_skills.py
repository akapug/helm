import os
import shutil
import tempfile
import unittest

import os as _os, sys as _sys  # noqa: E402
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
from tests._tmphome import home as _tmp_home  # noqa: E402
_tmp_home(prefix="helm-test-home-", var="HELM_HOME")

from helm import skills  # noqa: E402


def _mk_skill(home_dir, name, content):
    d = os.path.join(home_dir, name)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "SKILL.md"), "w") as f:
        f.write(content)


class SkillsTest(unittest.TestCase):
    def setUp(self):
        # addCleanup, not tearDown: it runs even when setUp fails partway, and
        # the un-cleaned version leaked TWO dirs PER TEST — thousands of them,
        # part of what exhausted /tmp's inodes fleet-wide (2026-07-24)
        self.h1 = tempfile.mkdtemp(prefix="helm-skills1-")
        self.addCleanup(shutil.rmtree, self.h1, ignore_errors=True)
        self.h2 = tempfile.mkdtemp(prefix="helm-skills2-")
        self.addCleanup(shutil.rmtree, self.h2, ignore_errors=True)
        _mk_skill(self.h1, "alpha", "same content")
        _mk_skill(self.h2, "alpha", "same content")          # identical shadow
        _mk_skill(self.h1, "beta", "one version")
        _mk_skill(self.h2, "beta", "another version")        # diverged shadow
        _mk_skill(self.h1, "gamma", "unique")
        _mk_skill(self.h2, "gamma-renamed", "unique")        # same content, two names
        self._orig = skills._skill_homes
        skills._skill_homes = lambda: ([], [])

    def tearDown(self):
        skills._skill_homes = self._orig

    def test_census_and_dupes(self):
        found, bad = skills.census(extra_homes=(self.h1, self.h2))
        self.assertEqual(len(found), 6)
        self.assertEqual(bad, [])
        name_dupes, content_dupes = skills.dupes(found)
        self.assertEqual(set(name_dupes), {"alpha", "beta"})
        hashes = {frozenset(s["name"] for s in v) for v in content_dupes.values()}
        self.assertIn(frozenset({"gamma", "gamma-renamed"}), hashes)
        alpha = name_dupes["alpha"]
        self.assertEqual(len({s["hash"] for s in alpha}), 1)   # identical
        beta = name_dupes["beta"]
        self.assertEqual(len({s["hash"] for s in beta}), 2)    # diverged


if __name__ == "__main__":
    unittest.main()
